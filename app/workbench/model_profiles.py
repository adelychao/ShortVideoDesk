"""Persistence and validation for named cloud-model profiles."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from .config import AppConfig, _validate_model_base_url
from .credentials import (
    CredentialStoreUnavailable,
    delete_model_profile_api_key,
    get_legacy_model_api_key,
    get_legacy_model_profile_api_key,
    get_model_api_key,
    get_model_profile_api_key,
    set_model_profile_api_key,
)
from .database import Database
from .providers.registry import get_provider_preset


_MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_PROFILE_NAME_MAX = 64
_PROFILE_INITIALIZED_KEY = "model_profiles_initialized"
_PROFILE_CAPABILITIES = {"text_generation", "image_generation"}


def ensure_profiles_initialized(config: AppConfig) -> None:
    """Create one compatibility profile from legacy settings exactly once."""
    database = config.database
    settings = config.get_settings()
    with database.transaction(write=True) as connection:
        marker = connection.execute(
            "SELECT value_json FROM settings WHERE key = ?", (_PROFILE_INITIALIZED_KEY,)
        ).fetchone()
        if marker is not None:
            try:
                if json.loads(marker["value_json"]) is True:
                    return
            except (TypeError, json.JSONDecodeError):
                pass

        existing = connection.execute(
            "SELECT id FROM model_profiles WHERE archived = 0 LIMIT 1"
        ).fetchone()
        if existing is None:
            provider_id = settings.get("model_provider")
            model_id = settings.get("model_id")
            preset = get_provider_preset(provider_id)
            if preset is not None and isinstance(model_id, str) and _MODEL_ID_PATTERN.fullmatch(model_id):
                now = _utc_now()
                profile_id = uuid.uuid4().hex
                profile = {
                    "id": profile_id,
                    "name": "默认配置",
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "asr_model_id": settings.get("asr_model_id") if isinstance(settings.get("asr_model_id"), str) else None,
                    "model_base_url": _validate_model_base_url(settings.get("model_base_url")),
                    "credential_id": profile_id,
                    "legacy_provider_id": None,
                    "is_default": True,
                    "created_at": now,
                    "updated_at": now,
                }
                _insert_profile(connection, profile)
        connection.execute(
            "INSERT INTO settings(key, value_json, updated_at) VALUES (?, 'true', ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = 'true', updated_at = excluded.updated_at",
            (_PROFILE_INITIALIZED_KEY, _utc_now()),
        )


def list_model_profiles(config: AppConfig) -> list[dict[str, Any]]:
    ensure_profiles_initialized(config)
    with config.database.transaction() as connection:
        rows = connection.execute(
            "SELECT * FROM model_profiles WHERE archived = 0 ORDER BY is_default DESC, updated_at DESC, name COLLATE NOCASE"
        ).fetchall()
    return [_public_profile(dict(row)) for row in rows]


def get_model_profile(config: AppConfig, profile_id: str, *, include_archived: bool = False) -> dict[str, Any] | None:
    ensure_profiles_initialized(config)
    sql = "SELECT * FROM model_profiles WHERE id = ?"
    if not include_archived:
        sql += " AND archived = 0"
    with config.database.transaction() as connection:
        row = connection.execute(sql, (profile_id,)).fetchone()
    return dict(row) if row is not None else None


def get_default_model_profile(config: AppConfig) -> dict[str, Any] | None:
    ensure_profiles_initialized(config)
    with config.database.transaction() as connection:
        row = connection.execute(
            "SELECT * FROM model_profiles WHERE archived = 0 AND is_default = 1 LIMIT 1"
        ).fetchone()
    return dict(row) if row is not None else None


def get_default_image_model_profile(config: AppConfig) -> dict[str, Any] | None:
    ensure_profiles_initialized(config)
    with config.database.transaction() as connection:
        row = connection.execute(
            "SELECT * FROM model_profiles WHERE archived = 0 AND is_image_default = 1 LIMIT 1"
        ).fetchone()
    return dict(row) if row is not None else None


def profile_api_key(profile: dict[str, Any]) -> str | None:
    credential_id = profile.get("credential_id")
    if isinstance(credential_id, str):
        return get_model_profile_api_key(credential_id)
    return None


def migrate_legacy_model_profile_key(config: AppConfig, profile_id: str) -> dict[str, Any]:
    """Copy a legacy shared API key only after an explicit user request."""
    profile = get_model_profile(config, profile_id)
    if profile is None:
        raise KeyError("模型配置不存在或已归档。")

    credential_id = profile.get("credential_id")
    provider_id = profile.get("provider_id")
    if not isinstance(credential_id, str) or not isinstance(provider_id, str):
        raise ValueError("模型配置凭据标识无效，无法迁移。")
    if get_model_profile_api_key(credential_id):
        raise ValueError("当前配置已经有 API Key，无需迁移。")

    legacy_key = get_legacy_model_profile_api_key(credential_id)
    if not legacy_key:
        legacy_key = get_legacy_model_api_key(provider_id)
    if not legacy_key:
        raise ValueError("旧凭据空间中没有找到该服务商的 API Key。")

    set_model_profile_api_key(legacy_key, credential_id)
    with config.database.transaction(write=True) as connection:
        connection.execute(
            "UPDATE model_profiles SET legacy_provider_id = NULL WHERE id = ? AND archived = 0",
            (profile_id,),
        )
    profile["legacy_provider_id"] = None
    return _public_profile(profile)


def profile_credential_status(profile: dict[str, Any]) -> dict[str, Any]:
    try:
        api_key = profile_api_key(profile)
        configured = bool(api_key)
        return {
            "configured": configured,
            "available": True,
            "masked_key": _mask_api_key(api_key) if configured and api_key is not None else None,
        }
    except CredentialStoreUnavailable:
        return {"configured": False, "available": False, "masked_key": None}


def save_model_profile(
    config: AppConfig,
    *,
    profile_id: str | None,
    name: str,
    provider_id: str,
    model_id: str,
    asr_model_id: str | None,
    model_base_url: str | None,
    api_key: str | None,
    make_default: bool | None = None,
    capabilities: list[str] | None = None,
    image_model_id: str | None = None,
    image_base_url: str | None = None,
    make_image_default: bool | None = None,
) -> dict[str, Any]:
    ensure_profiles_initialized(config)
    database = config.database
    existing: dict[str, Any] | None = None
    if profile_id:
        existing = get_model_profile(config, profile_id)
        if existing is None:
            raise KeyError("模型配置不存在。")
    normalized = _normalize_profile(
        name=name,
        provider_id=provider_id,
        model_id=model_id,
        asr_model_id=asr_model_id,
        model_base_url=model_base_url,
        capabilities=capabilities if capabilities is not None else _profile_capabilities(existing or {}),
        image_model_id=image_model_id,
        image_base_url=image_base_url,
    )

    with database.transaction() as connection:
        duplicate = connection.execute(
            "SELECT id FROM model_profiles WHERE archived = 0 AND name = ? COLLATE NOCASE",
            (normalized["name"],),
        ).fetchone()
    if duplicate is not None and str(duplicate["id"]) != profile_id:
        raise ValueError("配置名称已存在，请使用其他名称。")

    credential_id = str(existing["credential_id"]) if existing else uuid.uuid4().hex
    legacy_provider_id = existing.get("legacy_provider_id") if existing else None
    now = _utc_now()
    supports_text = "text_generation" in normalized["capabilities"]
    supports_image = "image_generation" in normalized["capabilities"]
    is_default = bool(existing and existing.get("is_default")) if make_default is None else bool(make_default)
    is_image_default = bool(existing and existing.get("is_image_default")) if make_image_default is None else bool(make_image_default)
    if is_default and not supports_text:
        raise ValueError("只有支持文本生成的配置才能设为默认文本模型。")
    if is_image_default and not supports_image:
        raise ValueError("只有支持图片生成的配置才能设为默认配图模型。")
    normalized_key = api_key.strip() if isinstance(api_key, str) and api_key.strip() else None
    if normalized_key:
        set_model_profile_api_key(normalized_key, credential_id)
        legacy_provider_id = None
    with database.transaction(write=True) as connection:
        has_text_default = connection.execute(
            "SELECT 1 FROM model_profiles WHERE archived = 0 AND is_default = 1 AND id != ? LIMIT 1",
            (profile_id or "",),
        ).fetchone() is not None
        has_image_default = connection.execute(
            "SELECT 1 FROM model_profiles WHERE archived = 0 AND is_image_default = 1 AND id != ? LIMIT 1",
            (profile_id or "",),
        ).fetchone() is not None
        if supports_text and not has_text_default and (not existing or not is_default):
            is_default = True
        if supports_image and not has_image_default and (not existing or not is_image_default):
            is_image_default = True
        if is_default:
            connection.execute("UPDATE model_profiles SET is_default = 0 WHERE archived = 0")
        elif existing and bool(existing.get("is_default")):
            connection.execute("UPDATE model_profiles SET is_default = 0 WHERE id = ?", (profile_id,))
        if is_image_default:
            connection.execute("UPDATE model_profiles SET is_image_default = 0 WHERE archived = 0")
        elif existing and bool(existing.get("is_image_default")):
            connection.execute("UPDATE model_profiles SET is_image_default = 0 WHERE id = ?", (profile_id,))
        profile = {
            "id": profile_id or uuid.uuid4().hex,
            "name": normalized["name"],
            "provider_id": normalized["provider_id"],
            "model_id": normalized["model_id"],
            "asr_model_id": normalized["asr_model_id"],
            "model_base_url": normalized["model_base_url"],
            "capabilities": normalized["capabilities"],
            "image_model_id": normalized["image_model_id"],
            "image_base_url": normalized["image_base_url"],
            "credential_id": credential_id,
            "legacy_provider_id": legacy_provider_id,
            "is_default": is_default,
            "is_image_default": is_image_default,
            "created_at": str(existing["created_at"]) if existing else now,
            "updated_at": now,
        }
        if existing:
            connection.execute(
                """UPDATE model_profiles SET name = ?, provider_id = ?, model_id = ?,
                       asr_model_id = ?, model_base_url = ?, capabilities_json = ?,
                       image_model_id = ?, image_base_url = ?, credential_id = ?,
                       legacy_provider_id = ?, is_default = ?, is_image_default = ?, updated_at = ?
                   WHERE id = ? AND archived = 0""",
                (
                    profile["name"], profile["provider_id"], profile["model_id"],
                    profile["asr_model_id"], profile["model_base_url"],
                    json.dumps(profile["capabilities"], separators=(",", ":")),
                    profile["image_model_id"], profile["image_base_url"], profile["credential_id"],
                    profile["legacy_provider_id"], int(profile["is_default"]),
                    int(profile["is_image_default"]), now, profile["id"],
                ),
            )
        else:
            _insert_profile(connection, profile)
        if profile["is_default"]:
            _write_default_settings(connection, profile)
        elif existing and bool(existing.get("is_default")):
            next_text = _next_capable_profile(connection, "text_generation", exclude_id=profile["id"])
            if next_text is not None:
                connection.execute("UPDATE model_profiles SET is_default = 1 WHERE id = ?", (next_text["id"],))
                _write_default_settings(connection, dict(next_text))
            else:
                _write_default_settings(connection, None)
        if not profile["is_image_default"] and existing and bool(existing.get("is_image_default")):
            next_image = _next_capable_profile(connection, "image_generation", exclude_id=profile["id"])
            if next_image is not None:
                connection.execute("UPDATE model_profiles SET is_image_default = 1 WHERE id = ?", (next_image["id"],))
    return _public_profile(profile)


def set_default_model_profile(config: AppConfig, profile_id: str) -> dict[str, Any]:
    profile = get_model_profile(config, profile_id)
    if profile is None:
        raise KeyError("模型配置不存在。")
    if "text_generation" not in _profile_capabilities(profile):
        raise ValueError("该配置未启用文本生成能力。")
    with config.database.transaction(write=True) as connection:
        connection.execute("UPDATE model_profiles SET is_default = 0 WHERE archived = 0")
        connection.execute(
            "UPDATE model_profiles SET is_default = 1, updated_at = ? WHERE id = ? AND archived = 0",
            (_utc_now(), profile_id),
        )
        profile["is_default"] = 1
        _write_default_settings(connection, profile)
    return _public_profile(profile)


def set_default_image_model_profile(config: AppConfig, profile_id: str) -> dict[str, Any]:
    profile = get_model_profile(config, profile_id)
    if profile is None:
        raise KeyError("模型配置不存在。")
    if "image_generation" not in _profile_capabilities(profile):
        raise ValueError("该配置未启用图片生成能力。")
    with config.database.transaction(write=True) as connection:
        connection.execute("UPDATE model_profiles SET is_image_default = 0 WHERE archived = 0")
        connection.execute(
            "UPDATE model_profiles SET is_image_default = 1, updated_at = ? WHERE id = ? AND archived = 0",
            (_utc_now(), profile_id),
        )
        profile["is_image_default"] = 1
    return _public_profile(profile)


def archive_model_profile(config: AppConfig, profile_id: str) -> dict[str, Any]:
    profile = get_model_profile(config, profile_id)
    if profile is None:
        raise KeyError("模型配置不存在。")
    now = _utc_now()
    with config.database.transaction(write=True) as connection:
        connection.execute(
            "UPDATE model_profiles SET archived = 1, is_default = 0, is_image_default = 0, updated_at = ? WHERE id = ?",
            (now, profile_id),
        )
        if bool(profile.get("is_default")):
            next_row = _next_capable_profile(connection, "text_generation")
            if next_row is not None:
                connection.execute("UPDATE model_profiles SET is_default = 1 WHERE id = ?", (next_row["id"],))
                _write_default_settings(connection, dict(next_row))
                next_default = dict(next_row)
                next_default["is_default"] = 1
            else:
                _write_default_settings(connection, None)
                next_default = None
        else:
            next_row = connection.execute(
                "SELECT * FROM model_profiles WHERE archived = 0 AND is_default = 1 LIMIT 1"
            ).fetchone()
            next_default = dict(next_row) if next_row is not None else None
        next_image = None
        if bool(profile.get("is_image_default")):
            next_image = _next_capable_profile(connection, "image_generation")
            if next_image is not None:
                connection.execute("UPDATE model_profiles SET is_image_default = 1 WHERE id = ?", (next_image["id"],))
        referenced = connection.execute(
            "SELECT 1 FROM task_model_configs WHERE profile_id = ? "
            "UNION ALL SELECT 1 FROM transcription_step_configs WHERE profile_id = ? LIMIT 1",
            (profile_id, profile_id),
        ).fetchone() is not None
    credential_retained = referenced
    if not referenced and not profile.get("legacy_provider_id"):
        try:
            delete_model_profile_api_key(str(profile["credential_id"]))
        except CredentialStoreUnavailable:
            credential_retained = True
    return {
        "archived": True,
        "credential_retained_for_tasks": credential_retained,
        "default_profile": _public_profile(next_default) if next_default else None,
        "image_default_profile": _public_profile(dict(next_image)) if next_image is not None else None,
    }


def save_task_model_config(database: Database, task_id: str, profile: dict[str, Any]) -> None:
    with database.transaction(write=True) as connection:
        connection.execute(
            """INSERT OR REPLACE INTO task_model_configs (
                   task_id, profile_id, profile_name, credential_id, legacy_provider_id,
                   provider_id, model_id, asr_model_id, model_base_url, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id, profile["id"], profile["name"], profile["credential_id"],
                profile.get("legacy_provider_id"), profile["provider_id"], profile["model_id"],
                profile.get("asr_model_id"), profile.get("model_base_url"), _utc_now(),
            ),
        )


def get_task_model_config(database: Database, task_id: str) -> dict[str, Any] | None:
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT * FROM task_model_configs WHERE task_id = ?", (task_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def task_model_settings(config: AppConfig, task_id: str) -> dict[str, Any]:
    snapshot = get_task_model_config(config.database, task_id)
    if snapshot is not None:
        return {
            "model_profile_id": snapshot["profile_id"],
            "model_profile_name": snapshot["profile_name"],
            "credential_id": snapshot["credential_id"],
            "legacy_provider_id": snapshot.get("legacy_provider_id"),
            "model_provider": snapshot["provider_id"],
            "model_id": snapshot["model_id"],
            "asr_model_id": snapshot.get("asr_model_id"),
            "model_base_url": snapshot.get("model_base_url"),
        }
    return config.get_settings()


def get_transcription_step_config(database: Database, task_id: str) -> dict[str, Any] | None:
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT * FROM transcription_step_configs WHERE task_id = ?", (task_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def save_transcription_step_config(
    database: Database,
    task_id: str,
    *,
    mode: str,
    profile: dict[str, Any] | None = None,
    asr_model_id: str | None = None,
    local_model: str | None = None,
    local_language: str | None = None,
    local_device: str | None = None,
) -> None:
    if mode not in {"cloud", "local"}:
        raise ValueError("转写方式无效。")
    profile = profile or {}
    values = (
        task_id,
        mode,
        profile.get("id") if mode == "cloud" else None,
        profile.get("name") if mode == "cloud" else None,
        profile.get("credential_id") if mode == "cloud" else None,
        profile.get("legacy_provider_id") if mode == "cloud" else None,
        profile.get("provider_id") if mode == "cloud" else None,
        profile.get("model_base_url") if mode == "cloud" else None,
        asr_model_id if mode == "cloud" else None,
        local_model if mode == "local" else None,
        local_language if mode == "local" else None,
        local_device if mode == "local" else None,
        _utc_now(),
    )
    with database.transaction(write=True) as connection:
        connection.execute(
            """INSERT INTO transcription_step_configs (
                   task_id, mode, profile_id, profile_name, credential_id,
                   legacy_provider_id, provider_id, model_base_url, asr_model_id,
                   local_model, local_language, local_device, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(task_id) DO UPDATE SET
                   mode = excluded.mode, profile_id = excluded.profile_id,
                   profile_name = excluded.profile_name, credential_id = excluded.credential_id,
                   legacy_provider_id = excluded.legacy_provider_id, provider_id = excluded.provider_id,
                   model_base_url = excluded.model_base_url, asr_model_id = excluded.asr_model_id,
                   local_model = excluded.local_model, local_language = excluded.local_language,
                   local_device = excluded.local_device, updated_at = excluded.updated_at""",
            values,
        )


def transcription_step_settings(config: AppConfig, task_id: str) -> dict[str, Any]:
    settings = task_model_settings(config, task_id)
    step_config = get_transcription_step_config(config.database, task_id)
    if step_config is None:
        return settings
    if step_config["mode"] == "local":
        return {
            **settings,
            "transcription_mode": "local",
            "local_model": step_config.get("local_model"),
            "local_language": step_config.get("local_language"),
            "local_device": step_config.get("local_device"),
        }
    return {
        **settings,
        "transcription_mode": "cloud",
        "model_profile_id": step_config.get("profile_id"),
        "model_profile_name": step_config.get("profile_name"),
        "credential_id": step_config.get("credential_id"),
        "legacy_provider_id": step_config.get("legacy_provider_id"),
        "model_provider": step_config.get("provider_id"),
        "asr_model_id": step_config.get("asr_model_id"),
        "model_base_url": step_config.get("model_base_url"),
    }


def task_profile_api_key(settings: dict[str, Any]) -> str | None:
    credential_id = settings.get("credential_id")
    if isinstance(credential_id, str):
        api_key = get_model_profile_api_key(credential_id)
        if api_key:
            return api_key
        legacy_provider_id = settings.get("legacy_provider_id")
        if isinstance(legacy_provider_id, str):
            return get_model_api_key(legacy_provider_id)
        return None
    provider_id = settings.get("model_provider")
    return get_model_api_key(str(provider_id)) if isinstance(provider_id, str) else None


def _normalize_profile(
    *, name: str, provider_id: str, model_id: str,
    asr_model_id: str | None, model_base_url: str | None,
    capabilities: list[str], image_model_id: str | None, image_base_url: str | None,
) -> dict[str, Any]:
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > _PROFILE_NAME_MAX:
        raise ValueError("配置名称必填，且不能超过 64 个字符。")
    preset = get_provider_preset(provider_id)
    if preset is None:
        raise ValueError("请选择有效的模型服务商。")
    if not isinstance(model_id, str) or not _MODEL_ID_PATTERN.fullmatch(model_id.strip()):
        raise ValueError("请填写有效的模型 ID。")
    if not isinstance(capabilities, list) or not capabilities or any(item not in _PROFILE_CAPABILITIES for item in capabilities):
        raise ValueError("请选择文本生成或图片生成能力。")
    normalized_asr = asr_model_id.strip() if isinstance(asr_model_id, str) else ""
    if normalized_asr and not _MODEL_ID_PATTERN.fullmatch(normalized_asr):
        raise ValueError("音频转写模型 ID 格式无效。")
    normalized_url = _validate_model_base_url(model_base_url)
    if preset.default_base_url is None and normalized_url is None:
        raise ValueError("该服务商需要填写服务地址。")
    normalized_image_model = image_model_id.strip() if isinstance(image_model_id, str) else ""
    if normalized_image_model and not _MODEL_ID_PATTERN.fullmatch(normalized_image_model):
        raise ValueError("图片生成模型 ID 格式无效。")
    normalized_image_url = _validate_model_base_url(image_base_url)
    if "image_generation" not in capabilities:
        normalized_image_model = ""
        normalized_image_url = None
    return {
        "name": name.strip(),
        "provider_id": provider_id,
        "model_id": model_id.strip(),
        "asr_model_id": normalized_asr or None,
        "model_base_url": normalized_url,
        "capabilities": sorted(set(capabilities)),
        "image_model_id": normalized_image_model or None,
        "image_base_url": normalized_image_url,
    }


def _public_profile(profile: dict[str, Any] | None) -> dict[str, Any]:
    if profile is None:
        return {}
    result = {
        "id": str(profile["id"]),
        "name": str(profile["name"]),
        "provider_id": str(profile["provider_id"]),
        "model_id": str(profile["model_id"]),
        "asr_model_id": profile.get("asr_model_id"),
        "model_base_url": profile.get("model_base_url"),
        "capabilities": _profile_capabilities(profile),
        "image_model_id": profile.get("image_model_id"),
        "image_base_url": profile.get("image_base_url"),
        "is_default": bool(profile.get("is_default")),
        "is_image_default": bool(profile.get("is_image_default")),
        "credential": profile_credential_status(profile),
    }
    preset = get_provider_preset(result["provider_id"])
    result["provider_name"] = preset.label if preset else result["provider_id"]
    result["supports_audio_transcription"] = bool(preset and preset.transcription_mode != "unsupported")
    return result


def _insert_profile(connection: sqlite3.Connection, profile: dict[str, Any]) -> None:
    connection.execute(
        """INSERT INTO model_profiles (
               id, name, provider_id, model_id, asr_model_id, model_base_url,
               capabilities_json, image_model_id, image_base_url, credential_id,
               legacy_provider_id, is_default, is_image_default, archived, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
        (
            profile["id"], profile["name"], profile["provider_id"], profile["model_id"],
            profile.get("asr_model_id"), profile.get("model_base_url"),
            json.dumps(profile.get("capabilities", ["text_generation"]), separators=(",", ":")),
            profile.get("image_model_id"), profile.get("image_base_url"), profile["credential_id"],
            profile.get("legacy_provider_id"), int(bool(profile.get("is_default"))),
            int(bool(profile.get("is_image_default"))), profile["created_at"], profile["updated_at"],
        ),
    )


def _profile_capabilities(profile: dict[str, Any]) -> list[str]:
    value = profile.get("capabilities")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item in _PROFILE_CAPABILITIES]
    raw = profile.get("capabilities_json")
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else None
    except (TypeError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, str) and item in _PROFILE_CAPABILITIES]
    return ["text_generation"]


def _next_capable_profile(
    connection: sqlite3.Connection,
    capability: str,
    *,
    exclude_id: str | None = None,
) -> sqlite3.Row | None:
    if capability not in _PROFILE_CAPABILITIES:
        return None
    return connection.execute(
        "SELECT * FROM model_profiles WHERE archived = 0 AND capabilities_json LIKE ? AND id != ? "
        "ORDER BY updated_at DESC, name COLLATE NOCASE LIMIT 1",
        (f'%"{capability}"%', exclude_id or ""),
    ).fetchone()


def _write_default_settings(connection: sqlite3.Connection, profile: dict[str, Any] | None) -> None:
    values = {
        "model_provider": profile.get("provider_id") if profile else None,
        "model_id": profile.get("model_id") if profile else None,
        "asr_model_id": profile.get("asr_model_id") if profile else None,
        "model_base_url": profile.get("model_base_url") if profile else None,
    }
    now = _utc_now()
    connection.executemany(
        """INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at""",
        [(key, json.dumps(value, ensure_ascii=False), now) for key, value in values.items()],
    )


def _mask_api_key(api_key: str) -> str:
    return "••••••••" if len(api_key) <= 8 else f"{api_key[:4]}…{api_key[-4:]}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
