"""OpenAI Images compatible article image generation and local configuration."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .config import AppConfig, _validate_model_base_url
from .credentials import (
    CredentialStoreUnavailable,
    delete_model_profile_api_key,
    delete_article_image_api_key,
    get_article_image_api_key,
    set_article_image_api_key,
)
from .model_profiles import (
    get_default_image_model_profile,
    get_model_profile,
    list_model_profiles,
    profile_api_key,
    profile_credential_status,
    save_model_profile,
)
from .providers.registry import PROVIDER_PRESETS, get_provider_preset


_SETTINGS_KEY = "article_image_generation"
_MIGRATED_KEY = "article_image_profile_migrated"
_MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_MAX_IMAGE_BYTES = 10_000_000
_MAX_RESPONSE_BYTES = 32_000_000


class ArticleImageError(RuntimeError):
    """A safe, user-facing image generation failure."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


def get_image_config(config: AppConfig) -> dict[str, Any]:
    ensure_legacy_image_profile(config)
    profile = get_default_image_model_profile(config)
    if profile is not None:
        provider = get_provider_preset(profile.get("provider_id"))
        model_id = _safe_model_id(profile.get("image_model_id")) or _safe_model_id(profile.get("model_id"))
        base_url = (
            _safe_base_url(profile.get("image_base_url"))
            or _safe_base_url(profile.get("model_base_url"))
            or _safe_base_url(provider.default_base_url if provider else None)
        )
        credential = profile_credential_status(profile)
        return {
            "base_url": base_url,
            "model_id": model_id or "",
            "provider_id": profile.get("provider_id"),
            "api_key_configured": credential["configured"],
            "credential_available": credential["available"],
            "configured": bool(credential["configured"] and base_url and model_id),
            "profile_id": profile.get("id"),
            "profile_name": profile.get("name"),
        }

    with config.database.transaction() as connection:
        row = connection.execute("SELECT value_json FROM settings WHERE key = ?", (_SETTINGS_KEY,)).fetchone()
    try:
        raw = json.loads(str(row["value_json"])) if row is not None else {}
    except (TypeError, json.JSONDecodeError, RecursionError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    try:
        api_key = get_article_image_api_key()
        credential_available = True
    except CredentialStoreUnavailable:
        api_key = None
        credential_available = False
    return {
        "base_url": _safe_base_url(raw.get("base_url")),
        "model_id": _safe_model_id(raw.get("model_id")),
        "provider_id": _provider_for_image_url(_safe_base_url(raw.get("base_url")) or ""),
        "api_key_configured": bool(api_key),
        "credential_available": credential_available,
        "configured": bool(api_key and _safe_base_url(raw.get("base_url")) and _safe_model_id(raw.get("model_id"))),
        "profile_id": None,
        "profile_name": None,
    }


def ensure_legacy_image_profile(config: AppConfig) -> None:
    """Import the former image settings once and retire their key only after verification."""
    with config.database.transaction() as connection:
        marker = connection.execute("SELECT value_json FROM settings WHERE key = ?", (_MIGRATED_KEY,)).fetchone()
        row = connection.execute("SELECT value_json FROM settings WHERE key = ?", (_SETTINGS_KEY,)).fetchone()
    if marker is not None:
        try:
            if json.loads(marker["value_json"]) is True:
                return
        except (TypeError, json.JSONDecodeError):
            pass
    try:
        raw = json.loads(str(row["value_json"])) if row is not None else {}
    except (TypeError, json.JSONDecodeError, RecursionError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    base_url = _safe_base_url(raw.get("base_url"))
    model_id = _safe_model_id(raw.get("model_id"))
    if not base_url or not model_id:
        _mark_legacy_image_profile_migrated(config)
        return
    try:
        legacy_key = get_article_image_api_key()
    except CredentialStoreUnavailable:
        # Leave the marker unset so a later settings visit can retry after the
        # Windows credential store becomes available.
        return

    provider_id = _provider_for_image_url(base_url)
    for public_profile in list_model_profiles(config):
        profile = get_model_profile(config, str(public_profile["id"]))
        if profile is None or _safe_model_id(profile.get("image_model_id") or profile.get("model_id")) != model_id:
            continue
        provider = get_provider_preset(profile.get("provider_id"))
        candidate_url = (
            _safe_base_url(profile.get("image_base_url"))
            or _safe_base_url(profile.get("model_base_url"))
            or _safe_base_url(provider.default_base_url if provider else None)
        )
        if candidate_url != base_url:
            continue
        try:
            candidate_key = profile_api_key(profile)
        except CredentialStoreUnavailable:
            return
        if legacy_key and candidate_key != legacy_key:
            continue
        capabilities = sorted(set(public_profile.get("capabilities", [])) | {"image_generation"})
        save_model_profile(
            config,
            profile_id=str(profile["id"]),
            name=str(profile["name"]),
            provider_id=str(profile["provider_id"]),
            model_id=str(profile["model_id"]),
            asr_model_id=profile.get("asr_model_id"),
            model_base_url=profile.get("model_base_url"),
            api_key=None,
            make_default=bool(profile.get("is_default")),
            capabilities=capabilities,
            image_model_id=profile.get("image_model_id"),
            image_base_url=profile.get("image_base_url"),
            make_image_default=True,
        )
        _complete_legacy_image_profile_migration(config, str(profile["id"]), legacy_key)
        return

    profile_names = {str(item.get("name", "")).casefold() for item in list_model_profiles(config)}
    name = "文章配图模型"
    suffix = 2
    while name.casefold() in profile_names:
        name = f"文章配图模型 {suffix}"
        suffix += 1
    try:
        saved = save_model_profile(
            config,
            profile_id=None,
            name=name,
            provider_id=provider_id,
            model_id=model_id,
            asr_model_id=None,
            model_base_url=base_url,
            api_key=legacy_key,
            make_default=False,
            capabilities=["image_generation"],
            image_model_id=None,
            image_base_url=None,
            make_image_default=True,
        )
    except CredentialStoreUnavailable:
        # The legacy credential stays available and will be retried on the next read.
        return
    _complete_legacy_image_profile_migration(config, str(saved["id"]), legacy_key)


def _mark_legacy_image_profile_migrated(config: AppConfig) -> None:
    with config.database.transaction(write=True) as connection:
        connection.execute(
            "INSERT INTO settings(key, value_json, updated_at) VALUES (?, 'true', ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = 'true', updated_at = excluded.updated_at",
            (_MIGRATED_KEY, _utc_now()),
        )


def _complete_legacy_image_profile_migration(config: AppConfig, profile_id: str, legacy_key: str | None) -> None:
    profile = get_model_profile(config, profile_id)
    if profile is None:
        return
    try:
        copied_key = profile_api_key(profile)
        if legacy_key and copied_key != legacy_key:
            return
        if legacy_key:
            delete_article_image_api_key()
    except CredentialStoreUnavailable:
        return
    with config.database.transaction(write=True) as connection:
        connection.execute(
            "INSERT INTO settings(key, value_json, updated_at) VALUES (?, 'true', ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = 'true', updated_at = excluded.updated_at",
            (_MIGRATED_KEY, _utc_now()),
        )


def _provider_for_image_url(base_url: str) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    host = (parsed.hostname or "").casefold()
    for provider_id, provider in PROVIDER_PRESETS.items():
        if provider.default_base_url:
            default_host = (urllib.parse.urlsplit(provider.default_base_url).hostname or "").casefold()
            if host and host == default_host:
                return provider_id
    return "custom_openai"


def save_image_config(
    config: AppConfig,
    *,
    base_url: str | None,
    model_id: str,
    api_key: str | None,
    clear_api_key: bool,
) -> dict[str, Any]:
    normalized_base_url = _validate_model_base_url(base_url)
    normalized_model_id = _safe_model_id(model_id)
    if not normalized_base_url or not normalized_model_id:
        raise ValueError("请填写图片模型的 API 根地址和模型 ID。")
    image_profile = get_default_image_model_profile(config)
    if image_profile is not None:
        provider = get_provider_preset(image_profile.get("provider_id"))
        shared_base_url = _safe_base_url(image_profile.get("model_base_url")) or _safe_base_url(provider.default_base_url if provider else None)
        try:
            capabilities = json.loads(str(image_profile.get("capabilities_json") or "[]"))
        except (TypeError, json.JSONDecodeError):
            capabilities = ["image_generation"]
        if not isinstance(capabilities, list):
            capabilities = ["image_generation"]
        if clear_api_key:
            delete_model_profile_api_key(str(image_profile["credential_id"]))
            with config.database.transaction(write=True) as connection:
                connection.execute("UPDATE model_profiles SET legacy_provider_id = NULL WHERE id = ?", (image_profile["id"],))
        save_model_profile(
            config,
            profile_id=str(image_profile["id"]),
            name=str(image_profile["name"]),
            provider_id=str(image_profile["provider_id"]),
            model_id=str(image_profile["model_id"]),
            asr_model_id=image_profile.get("asr_model_id"),
            model_base_url=image_profile.get("model_base_url"),
            api_key=api_key,
            make_default=bool(image_profile.get("is_default")),
            capabilities=capabilities,
            image_model_id=normalized_model_id if normalized_model_id != image_profile.get("model_id") else None,
            image_base_url=normalized_base_url if normalized_base_url != shared_base_url else None,
            make_image_default=True,
        )
        return get_image_config(config)
    if clear_api_key:
        delete_article_image_api_key()
    elif isinstance(api_key, str) and api_key.strip():
        set_article_image_api_key(api_key)
    with config.database.transaction(write=True) as connection:
        connection.execute(
            """INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at""",
            (_SETTINGS_KEY, json.dumps({"base_url": normalized_base_url, "model_id": normalized_model_id}, ensure_ascii=False, separators=(",", ":")), _utc_now()),
        )
        connection.execute(
            "INSERT INTO settings(key, value_json, updated_at) VALUES (?, 'false', ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = 'false', updated_at = excluded.updated_at",
            (_MIGRATED_KEY, _utc_now()),
        )
    return get_image_config(config)


def generate_image(config: AppConfig, prompt: str) -> tuple[bytes, str, dict[str, Any]]:
    settings = get_image_config(config)
    if not settings["configured"]:
        raise ArticleImageError("请先在设置 → 模型与转写中配置配图默认模型和 API Key。")
    clean_prompt = prompt.strip()
    if not clean_prompt or len(clean_prompt) > 4000:
        raise ArticleImageError("配图提示词不能为空，且不能超过 4000 个字符。")
    try:
        api_key = _get_effective_image_api_key(config)
    except CredentialStoreUnavailable as exc:
        raise ArticleImageError(str(exc)) from None
    if not api_key:
        raise ArticleImageError("图片模型 API Key 尚未配置。")
    provider_id = settings.get("provider_id")
    model_id = str(settings["model_id"])
    if provider_id == "zhipu" and model_id.casefold() in {"glm-5.3-flash", "glm-5.3-flashx", "glm-5.3-flashx-legal"}:
        raise ArticleImageError(
            f"当前图片模型 ID 为 {model_id}。它支持理解图片，但只输出文本。"
            "智谱图片生成请在设置 → 模型与转写 → 编辑该配置，将“图片模型 ID”填写为 glm-image；"
            "文本模型 ID、服务地址和 API Key 可以继续共用。"
        )
    endpoint = str(settings["base_url"]).rstrip("/") + "/images/generations"
    request_body: dict[str, Any] = {"model": model_id, "prompt": clean_prompt}
    if provider_id == "zhipu":
        request_body["size"] = "1728x736"
    else:
        request_body["n"] = 1
    body = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.build_opener(_NoRedirectHandler()).open(request, timeout=180) as response:
            payload_bytes = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(payload_bytes) > _MAX_RESPONSE_BYTES:
            raise ArticleImageError("图片模型响应超过 32 MB，已停止读取。")
        payload = json.loads(payload_bytes.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = _provider_error_message(exc)
        suffix = f"：{detail}" if detail else "。请检查服务地址、模型 ID、额度和账号权限"
        raise ArticleImageError(f"图片模型接口请求失败（HTTP {exc.code}）{suffix}。") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ArticleImageError("无法连接图片模型接口；请检查服务地址与网络后重试。") from None
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ArticleImageError("图片模型返回了无法识别的响应。") from None
    if not isinstance(payload, dict):
        raise ArticleImageError("图片模型返回了无法识别的响应。")
    data = payload.get("data")
    first = data[0] if isinstance(data, list) and data else None
    if not isinstance(first, dict):
        raise ArticleImageError("图片模型没有返回图片。请检查模型权限和服务商响应格式。")
    if isinstance(first.get("b64_json"), str):
        try:
            image = base64.b64decode(first["b64_json"], validate=True)
        except (binascii.Error, ValueError):
            raise ArticleImageError("图片模型返回的 Base64 图片无效。") from None
    elif isinstance(first.get("url"), str):
        image = _download_public_image(first["url"])
    else:
        raise ArticleImageError("图片模型响应缺少图片数据或下载地址。")
    if not image or len(image) > _MAX_IMAGE_BYTES:
        raise ArticleImageError("生成图片必须小于 10 MB。")
    suffix = _image_suffix(image)
    return image, suffix, {
        "model_id": str(settings["model_id"]),
        "generated_at": _utc_now(),
        "request_id": str(payload.get("id") or payload.get("request_id") or "")[:128],
    }


def _get_effective_image_api_key(config: AppConfig) -> str | None:
    profile = get_default_image_model_profile(config)
    if profile is not None:
        return profile_api_key(profile)
    return get_article_image_api_key()


def _provider_error_message(error: urllib.error.HTTPError) -> str | None:
    try:
        payload = json.loads(error.read(8192).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    detail = payload.get("error")
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("msg") or detail.get("code")
    elif isinstance(detail, str):
        message = detail
    else:
        message = payload.get("message") or payload.get("msg")
    if not isinstance(message, str) or not message.strip():
        return None
    clean = " ".join(message.split())
    clean = re.sub(r"https?://[^\s\"'<>]+", "<链接已隐藏>", clean, flags=re.IGNORECASE)
    clean = re.sub(
        r"(?i)(authorization\s*[:=]\s*bearer\s+|(?:api[_-]?key|access[_-]?token|secret|signature|cookie)\s*[:=]\s*)[^\s,;\"']+",
        r"\1<已隐藏>",
        clean,
    )
    return clean[:300]


def _download_public_image(url: str) -> bytes:
    opener = urllib.request.build_opener(_NoRedirectHandler())
    current_url = url
    for _ in range(4):
        _validate_public_download_url(current_url)
        request = urllib.request.Request(current_url, headers={"Accept": "image/png,image/jpeg,image/webp"}, method="GET")
        try:
            with opener.open(request, timeout=60) as response:
                image = response.read(_MAX_IMAGE_BYTES + 1)
            break
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location") if exc.code in {301, 302, 303, 307, 308} else None
            if not location:
                raise ArticleImageError("无法下载图片模型生成的图片；请重试。") from None
            current_url = urllib.parse.urljoin(current_url, location)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ArticleImageError("无法下载图片模型生成的图片；请重试。") from None
    else:
        raise ArticleImageError("图片模型返回的下载地址重定向次数过多。")
    if len(image) > _MAX_IMAGE_BYTES:
        raise ArticleImageError("生成图片必须小于 10 MB。")
    _image_suffix(image)
    return image


def _validate_public_download_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    try:
        port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
    except ValueError:
        raise ArticleImageError("图片模型返回的下载地址无效。") from None
    host = (parsed.hostname or "").strip("[]").casefold()
    if parsed.scheme.casefold() not in {"https", "http"} or not host or parsed.username or parsed.password or parsed.fragment:
        raise ArticleImageError("图片模型返回的下载地址无效。")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    except (OSError, UnicodeError):
        raise ArticleImageError("无法解析图片模型返回的下载地址。") from None
    if not addresses or any(not _is_public_address(address) for address in addresses):
        raise ArticleImageError("图片模型返回了不安全的下载地址。")


def _is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast or address.is_unspecified)


def _image_suffix(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    raise ArticleImageError("当前只接受 JPEG、PNG 或 WebP 图片响应。")


def _safe_base_url(value: object) -> str | None:
    try:
        return _validate_model_base_url(value)
    except ValueError:
        return None


def _safe_model_id(value: object) -> str | None:
    if not isinstance(value, str) or not _MODEL_ID_PATTERN.fullmatch(value.strip()):
        return None
    return value.strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
