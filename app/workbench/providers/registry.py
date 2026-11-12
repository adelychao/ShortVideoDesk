"""Provider presets and on-demand model metadata from Models.dev."""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .base import ModelCapability


@dataclass(frozen=True)
class ProviderPreset:
    id: str
    label: str
    litellm_route: str
    models_dev_id: str | None
    default_base_url: str | None
    transcription_mode: str
    description: str


PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "zhipu": ProviderPreset(
        id="zhipu",
        label="智谱 AI（BigModel）",
        litellm_route="custom_openai",
        models_dev_id="zai",
        default_base_url="https://open.bigmodel.cn/api/paas/v4",
        transcription_mode="openai_compatible",
        description="适用于 GLM 文本/图像模型；ASR 使用兼容转写接口。",
    ),
    "qwen": ProviderPreset(
        id="qwen",
        label="阿里云百炼（通义千问）",
        litellm_route="dashscope",
        models_dev_id="alibaba",
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        transcription_mode="unsupported",
        description="适用于通义文本/图像模型；当前不接入其专有语音转写接口。",
    ),
    "deepseek": ProviderPreset(
        id="deepseek",
        label="DeepSeek",
        litellm_route="deepseek",
        models_dev_id="deepseek",
        default_base_url="https://api.deepseek.com",
        transcription_mode="unsupported",
        description="适用于 DeepSeek 文本模型；当前不提供语音转写接口。",
    ),
    "openai": ProviderPreset(
        id="openai",
        label="OpenAI",
        litellm_route="openai",
        models_dev_id="openai",
        default_base_url="https://api.openai.com/v1",
        transcription_mode="litellm",
        description="适用于 OpenAI 文本/图像模型及 LiteLLM 支持的语音转写模型。",
    ),
    "siliconflow": ProviderPreset(
        id="siliconflow",
        label="SiliconFlow",
        litellm_route="custom_openai",
        models_dev_id="siliconflow",
        default_base_url="https://api.siliconflow.cn/v1",
        transcription_mode="openai_compatible",
        description="适用于 OpenAI 兼容模型；语音转写取决于服务端是否提供兼容接口。",
    ),
    "custom_openai": ProviderPreset(
        id="custom_openai",
        label="自定义 OpenAI 兼容服务",
        litellm_route="custom_openai",
        models_dev_id=None,
        default_base_url=None,
        transcription_mode="openai_compatible",
        description="填写服务商提供的 OpenAI 兼容 API 根地址；仅当端点实现 audio/transcriptions 时可用于 ASR。",
    ),
}

_MODELS_DEV_URL = "https://models.dev/api.json"
_MODELS_DEV_MAX_BYTES = 24 * 1024 * 1024
_MODELS_DEV_TIMEOUT_SECONDS = 8
_MODELS_DEV_TTL_SECONDS = 6 * 60 * 60
_MODELS_DEV_RETRY_SECONDS = 60
_catalog_lock = threading.Lock()
_catalog_data: dict[str, Any] | None = None
_catalog_loaded_at = 0.0
_catalog_retry_after = 0.0

# Models.dev currently focuses on decision/chat models; preserve the two
# already-established Zhipu IDs when that online catalog is unavailable.
_FALLBACK_CAPABILITIES: dict[tuple[str, str], frozenset[str]] = {
    ("zhipu", "glm-5.3-flash"): frozenset({"text", "image"}),
    ("zhipu", "glm-asr-2512"): frozenset({"audio"}),
}


def list_provider_presets() -> list[dict[str, object]]:
    return [
        {
            "id": item.id,
            "label": item.label,
            "default_base_url": item.default_base_url,
            "supports_audio_transcription": item.transcription_mode != "unsupported",
            "description": item.description,
        }
        for item in PROVIDER_PRESETS.values()
    ]


def get_provider_preset(provider_id: object) -> ProviderPreset | None:
    if not isinstance(provider_id, str):
        return None
    return PROVIDER_PRESETS.get(provider_id.strip())


def get_default_base_url(provider_id: object) -> str | None:
    preset = get_provider_preset(provider_id)
    return preset.default_base_url if preset else None


def get_model_capability(provider_id: str, model_id: str) -> ModelCapability | None:
    capability, _, _ = lookup_model_capability(provider_id, model_id)
    return capability


def lookup_model_capability(
    provider_id: str,
    model_id: str,
) -> tuple[ModelCapability | None, bool, str | None]:
    """Return (capability, catalog_available, display_name) without guessing."""
    preset = get_provider_preset(provider_id)
    if preset is None or not model_id.strip() or preset.models_dev_id is None:
        return None, False, None

    fallback_modalities = _FALLBACK_CAPABILITIES.get((provider_id, model_id.casefold()))
    if fallback_modalities is not None:
        return ModelCapability(provider_id, model_id, fallback_modalities), False, None

    catalog = _get_models_dev_catalog()
    if catalog is None:
        return None, False, None
    provider_data = catalog.get(preset.models_dev_id) if preset.models_dev_id else None
    models = provider_data.get("models") if isinstance(provider_data, dict) else None
    if not isinstance(models, dict):
        return None, True, None

    wanted = model_id.strip().casefold()
    for catalog_id, raw_model in models.items():
        if not isinstance(raw_model, dict):
            continue
        aliases = {
            str(catalog_id).casefold(),
            str(raw_model.get("id", "")).casefold(),
            str(raw_model.get("canonical_model_id", "")).casefold(),
        }
        if wanted not in aliases:
            continue
        modalities_record = raw_model.get("modalities")
        input_modalities = modalities_record.get("input") if isinstance(modalities_record, dict) else None
        if not isinstance(input_modalities, list):
            return None, True, _optional_text(raw_model.get("name"))
        modalities = frozenset(
            item.strip().casefold()
            for item in input_modalities
            if isinstance(item, str) and item.strip()
        )
        limit_record = raw_model.get("limit")
        context_limit = limit_record.get("context") if isinstance(limit_record, dict) else None
        max_input_tokens = context_limit if isinstance(context_limit, int) and context_limit > 0 else None
        return (
            ModelCapability(
                provider_id=provider_id,
                model_id=model_id,
                modalities=modalities,
                max_input_tokens=max_input_tokens,
            ),
            True,
            _optional_text(raw_model.get("name")),
        )
    return None, True, None


def _get_models_dev_catalog() -> dict[str, Any] | None:
    global _catalog_data, _catalog_loaded_at, _catalog_retry_after
    now = time.monotonic()
    with _catalog_lock:
        if _catalog_data is not None and now - _catalog_loaded_at < _MODELS_DEV_TTL_SECONDS:
            return _catalog_data
        if now < _catalog_retry_after:
            return _catalog_data

        request = urllib.request.Request(
            _MODELS_DEV_URL,
            headers={"Accept": "application/json", "User-Agent": "LocalVideoWorkbench/1.0"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=_MODELS_DEV_TIMEOUT_SECONDS) as response:
                payload = response.read(_MODELS_DEV_MAX_BYTES + 1)
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
            _catalog_retry_after = now + _MODELS_DEV_RETRY_SECONDS
            return _catalog_data if _catalog_data is not None else None
        if len(payload) > _MODELS_DEV_MAX_BYTES:
            _catalog_retry_after = now + _MODELS_DEV_RETRY_SECONDS
            return _catalog_data if _catalog_data is not None else None
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            _catalog_retry_after = now + _MODELS_DEV_RETRY_SECONDS
            return _catalog_data if _catalog_data is not None else None
        if not isinstance(decoded, dict):
            _catalog_retry_after = now + _MODELS_DEV_RETRY_SECONDS
            return _catalog_data if _catalog_data is not None else None
        _catalog_data = decoded
        _catalog_loaded_at = now
        _catalog_retry_after = 0.0
        return _catalog_data


def _optional_text(value: object) -> str | None:
    return value.strip()[:200] if isinstance(value, str) and value.strip() else None


__all__ = [
    "PROVIDER_PRESETS",
    "ProviderPreset",
    "get_default_base_url",
    "get_model_capability",
    "get_provider_preset",
    "list_provider_presets",
    "lookup_model_capability",
]
