"""Local non-secret settings and path validation for the workbench."""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

from .database import Database, PROJECT_ROOT
from .providers.registry import get_provider_preset
from .prompts import DEFAULT_KNOWLEDGE_CARD_PROMPT
from .script_styles import default_script_style_templates, normalize_script_style_templates


IMAGE_DIRECTORY_DEFAULTS = {
    "video_publish_cover_root": PROJECT_ROOT / "data" / "workbench" / "video-publish-covers",
    "article_image_root": PROJECT_ROOT / "data" / "article-assets",
}
SETTING_KEYS = (
    "archive_root",
    "obsidian_reference_root",
    "obsidian_output_root",
    "model_provider",
    "model_id",
    "asr_model_id",
    "model_base_url",
    "vector_search_enabled",
    "local_asr_device",
    "knowledge_card_prompt",
    "script_style_templates",
    "video_publish_auto_cleanup_enabled",
    "video_publish_retention_days",
    *IMAGE_DIRECTORY_DEFAULTS,
)
DEFAULT_SETTINGS: dict[str, object] = {
    "archive_root": None,
    "obsidian_reference_root": None,
    "obsidian_output_root": None,
    "model_provider": "zhipu",
    "model_id": "glm-5.3-flash",
    "asr_model_id": "glm-asr-2512",
    "model_base_url": None,
    "vector_search_enabled": False,
    "local_asr_device": "auto",
    "knowledge_card_prompt": DEFAULT_KNOWLEDGE_CARD_PROMPT,
    "script_style_templates": default_script_style_templates(),
    "video_publish_auto_cleanup_enabled": True,
    "video_publish_retention_days": 15,
    **{key: str(path) for key, path in IMAGE_DIRECTORY_DEFAULTS.items()},
}
_MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_PROVIDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class AppConfig:
    """Read and write user-selected paths in the local SQLite settings table."""

    def __init__(self, database: Database | None = None) -> None:
        self.database = database or Database()

    @property
    def archive_root(self) -> Path | None:
        value = self.get_settings()["archive_root"]
        return Path(value) if isinstance(value, str) and value else None

    def get_settings(self) -> dict[str, object]:
        self.database.migrate()
        values = deepcopy(DEFAULT_SETTINGS)
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT key, value_json FROM settings WHERE key IN ("
                + ",".join("?" for _ in SETTING_KEYS)
                + ")",
                SETTING_KEYS,
            ).fetchall()
        for row in rows:
            key = str(row["key"])
            try:
                decoded = json.loads(row["value_json"])
            except (TypeError, json.JSONDecodeError, RecursionError):
                continue
            if key in values:
                values[key] = decoded
        # Never surface malformed historical settings as arbitrary objects.
        for key in ("archive_root", "obsidian_reference_root", "obsidian_output_root"):
            raw = values[key]
            if not isinstance(raw, str) or not raw:
                values[key] = None
                continue
            try:
                values[key] = str(_validated_local_path(raw))
            except ValueError:
                values[key] = None
        for key, default in IMAGE_DIRECTORY_DEFAULTS.items():
            raw = values[key]
            if isinstance(raw, str) and raw.strip():
                try:
                    values[key] = str(_validated_local_path(raw))
                    continue
                except ValueError:
                    # 保留不可用的已配置路径，写入时给出错误，避免静默改写到其他盘。
                    continue
            values[key] = str(default)
        raw_provider = values["model_provider"]
        values["model_provider"] = (
            raw_provider.strip()
            if isinstance(raw_provider, str)
            and _PROVIDER_ID_PATTERN.fullmatch(raw_provider.strip())
            else None
        )
        raw_model_id = values["model_id"]
        values["model_id"] = (
            raw_model_id.strip()
            if isinstance(raw_model_id, str)
            and _MODEL_ID_PATTERN.fullmatch(raw_model_id.strip())
            else None
        )
        raw_asr_model_id = values["asr_model_id"]
        values["asr_model_id"] = (
            raw_asr_model_id.strip()
            if isinstance(raw_asr_model_id, str)
            and _MODEL_ID_PATTERN.fullmatch(raw_asr_model_id.strip())
            else None
        )
        try:
            values["model_base_url"] = _validate_model_base_url(values["model_base_url"])
        except ValueError:
            # Historical or manually edited SQLite values must receive the
            # same treatment as new settings and must never be echoed to UI.
            values["model_base_url"] = None
        values["vector_search_enabled"] = values["vector_search_enabled"] is True
        if values['local_asr_device'] not in ('auto', 'cpu', 'cuda'):
            values['local_asr_device'] = 'auto'
        prompt = values["knowledge_card_prompt"]
        values["knowledge_card_prompt"] = (
            prompt.strip()
            if isinstance(prompt, str) and prompt.strip() and len(prompt) <= 20_000
            else DEFAULT_KNOWLEDGE_CARD_PROMPT
        )
        try:
            values["script_style_templates"] = normalize_script_style_templates(values["script_style_templates"])
        except ValueError:
            # Malformed historical values should fall back to editable defaults.
            values["script_style_templates"] = default_script_style_templates()
        return values

    def get_directory_conflicts(self) -> list[dict[str, str]]:
        return _directory_conflicts(self.get_settings())

    def set_archive_root(self, path: str | Path | None) -> Path | None:
        current = self.get_settings()
        current["archive_root"] = str(path) if path is not None else None
        saved = self.update_settings(current)
        value = saved["archive_root"]
        return Path(value) if isinstance(value, str) else None

    def update_settings(self, values: dict[str, object]) -> dict[str, object]:
        unknown = set(values) - set(SETTING_KEYS)
        if unknown:
            raise ValueError("settings contain unsupported fields")
        previous = self.get_settings()
        current = {**previous, **values}
        normalized: dict[str, object] = {}

        path_modes = {
            "archive_root": (True, True),
            "obsidian_reference_root": (False, False),
            "obsidian_output_root": (True, True),
        }
        for key, (create_missing, writable) in path_modes.items():
            raw = current[key]
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                normalized[key] = None
                continue
            if not isinstance(raw, str):
                raise ValueError(f"{key} must be a local directory path")
            normalized[key] = str(
                validate_directory(raw, create_missing=create_missing, writable=writable)
            )

        for key, default in IMAGE_DIRECTORY_DEFAULTS.items():
            raw = current[key]
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                raw = str(default)
            if not isinstance(raw, str):
                raise ValueError("图片保存目录必须是本机文件夹。")
            normalized[key] = previous[key] if raw == previous[key] else str(
                _validate_image_directory(key, raw)
            )

        provider = current["model_provider"]
        if provider is not None:
            if not isinstance(provider, str) or not _PROVIDER_ID_PATTERN.fullmatch(provider.strip()):
                raise ValueError("模型服务商标识格式无效。")
            provider = provider.strip()
            if get_provider_preset(provider) is None:
                raise ValueError("所选模型服务商不在支持的预设列表中。")
        normalized["model_provider"] = provider

        model_id = current["model_id"]
        if model_id is not None:
            if not isinstance(model_id, str) or not _MODEL_ID_PATTERN.fullmatch(model_id.strip()):
                raise ValueError("模型 ID 格式无效。")
            model_id = model_id.strip()
        normalized["model_id"] = model_id

        asr_model_id = current["asr_model_id"]
        if asr_model_id is not None:
            if not isinstance(asr_model_id, str) or not _MODEL_ID_PATTERN.fullmatch(asr_model_id.strip()):
                raise ValueError("ASR 模型 ID 格式无效。")
            asr_model_id = asr_model_id.strip()
        normalized["asr_model_id"] = asr_model_id

        base_url = current["model_base_url"]
        normalized["model_base_url"] = _validate_model_base_url(base_url)
        vector_enabled = current["vector_search_enabled"]
        if not isinstance(vector_enabled, bool):
            raise ValueError("向量检索开关值无效。")
        normalized["vector_search_enabled"] = vector_enabled
        device = current['local_asr_device']
        if device not in ('auto', 'cpu', 'cuda'):
            raise ValueError('本地转写设备无效。')
        normalized['local_asr_device'] = device

        auto_cleanup = current["video_publish_auto_cleanup_enabled"]
        if not isinstance(auto_cleanup, bool):
            raise ValueError("视频发布自动清理开关值无效。")
        normalized["video_publish_auto_cleanup_enabled"] = auto_cleanup

        retention_days = current["video_publish_retention_days"]
        if isinstance(retention_days, bool) or not isinstance(retention_days, int) or not 1 <= retention_days <= 3650:
            raise ValueError("视频发布自动清理期限请输入 1 到 3650 之间的整数天。")
        normalized["video_publish_retention_days"] = retention_days

        prompt = current["knowledge_card_prompt"]
        if not isinstance(prompt, str) or len(prompt) > 20_000:
            raise ValueError("知识卡提示词无效或超过 20,000 个字符。")
        normalized["knowledge_card_prompt"] = prompt.strip() or DEFAULT_KNOWLEDGE_CARD_PROMPT

        normalized["script_style_templates"] = normalize_script_style_templates(current["script_style_templates"])

        conflicts = _directory_conflicts(normalized)
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction(write=True) as connection:
            for key, value in normalized.items():
                connection.execute(
                    """INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET
                           value_json = excluded.value_json,
                           updated_at = excluded.updated_at""",
                    (key, json.dumps(value, ensure_ascii=False), now),
                )
        normalized["directory_conflicts"] = conflicts
        return normalized


def _validated_local_path(value: str | Path) -> Path:
    raw = str(value).strip()
    if not raw.strip() or "\x00" in raw:
        raise ValueError("目录必须是非空的绝对本机路径。")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", raw) or raw.casefold().startswith("file:"):
        raise ValueError("目录必须是文件系统路径。")
    normalized = raw.replace("/", "\\").casefold()
    if normalized.startswith(("\\\\?\\", "\\.\\", "\\??\\", "\\device\\")):
        raise ValueError("不接受 Windows 设备路径。")
    if raw.startswith(("\\\\", "//")):
        raise ValueError("不接受网络共享路径。")
    path = Path(raw).expanduser()
    if not path.is_absolute() or ".." in path.parts or ".." in PureWindowsPath(raw).parts:
        raise ValueError("目录必须是绝对路径，且不能包含路径穿越。")
    try:
        if path.exists() and path.is_symlink():
            raise ValueError("目录本身不能是符号链接。")
    except OSError as exc:
        raise ValueError("目录不可访问。") from exc
    return path


def _validated_archive_root(value: str | Path) -> Path:
    """Keep the original archive-root helper as a compatibility entry point."""
    return _validated_local_path(value)


def validate_directory(
    value: str | Path,
    *,
    create_missing: bool = False,
    writable: bool = False,
) -> Path:
    path = _validated_local_path(value)
    try:
        if not path.exists() and create_missing:
            path.mkdir(parents=True, exist_ok=True)
        if not path.exists() or not path.is_dir():
            raise ValueError("所选目录不存在或不是文件夹。")
        if not os.access(path, os.R_OK):
            raise ValueError("所选目录不可读取。")
        if writable:
            probe = path / f".workbench-write-check-{secrets.token_hex(12)}.tmp"
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            descriptor = os.open(probe, flags, 0o600)
            try:
                os.write(descriptor, b"workbench")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            probe.unlink()
        return path.resolve(strict=True)
    except ValueError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise ValueError("所选目录读写检查失败。") from exc


def _validate_image_directory(key: str, value: str) -> Path:
    path = _validated_local_path(value).resolve(strict=False)
    workbench = PROJECT_ROOT / "data" / "workbench"
    article_legacy = IMAGE_DIRECTORY_DEFAULTS["article_image_root"]
    protected = (workbench / "video-publish", workbench / "video-publish-drafts",
                 workbench / "video-publish-cover-uploads", workbench / "uploads", article_legacy)
    if any(path.is_relative_to(root) for root in protected) and not (
        key == "article_image_root" and path == article_legacy
    ):
        raise ValueError("请选择独立的图片保存目录，避开旧任务、草稿和文章文件夹。")
    return validate_directory(value, create_missing=True, writable=True)


def _validate_model_base_url(value: object) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("模型服务地址格式无效。")
    parsed = urlsplit(value.strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("模型服务地址格式无效。") from exc
    host = (parsed.hostname or "").casefold()
    if (
        parsed.scheme.casefold() not in {"https", "http"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (parsed.scheme.casefold() == "http" and host not in {"localhost", "127.0.0.1", "::1"})
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("模型服务地址必须使用 HTTPS；仅允许本机 HTTP，并且不能包含凭据或查询参数。")
    scheme = parsed.scheme.casefold()
    if (
        scheme not in {"https", "http"}
        or (scheme == "http" and host not in {"localhost", "127.0.0.1", "::1"})
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("模型服务地址需使用 HTTPS；仅允许本机 HTTP。")
    return value.strip().rstrip("/")


def _directory_conflicts(values: dict[str, object]) -> list[dict[str, str]]:
    paths: dict[str, Path] = {}
    for key, value in values.items():
        if key not in {"archive_root", "obsidian_reference_root", "obsidian_output_root"} or not isinstance(value, str):
            continue
        try:
            paths[key] = Path(value).resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
    conflicts: list[dict[str, str]] = []
    reference = paths.get("obsidian_reference_root")
    output = paths.get("obsidian_output_root")
    archive = paths.get("archive_root")
    if reference is not None and output is not None and reference == output:
        conflicts.append({"code": "reference_equals_output", "message": "知识库参考目录与输出目录相同。"})
    if archive is not None and output is not None:
        try:
            output.relative_to(archive)
            conflicts.append({"code": "output_inside_archive", "message": "知识库输出目录位于媒体归档目录内。"})
        except ValueError:
            try:
                archive.relative_to(output)
                conflicts.append({"code": "archive_inside_output", "message": "媒体归档目录位于知识库输出目录内。"})
            except ValueError:
                pass
    return conflicts
