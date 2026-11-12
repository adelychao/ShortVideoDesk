"""LiteLLM adapter preserving the workbench's provider and audit contracts."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Sequence

from .base import (
    ModelCapability,
    ProviderAuthenticationError,
    ProviderCapabilityError,
    ProviderConfigurationError,
    ProviderInputError,
    ProviderNetworkError,
    ProviderRateLimitError,
    ProviderRequestRejectedError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    OcrFrame,
    OcrResponse,
    TextResponse,
    TranscriptResponse,
    UsageUnits,
)
from .openai_compatible import ZhipuOpenAICompatibleProvider, _read_image
from .registry import get_model_capability, get_provider_preset


_CHAT_TIMEOUT_SECONDS = 600
_TRANSCRIPTION_TIMEOUT_SECONDS = 90


class _AnyOpenAICompatibleAudioCatalog:
    """The user-entered ASR field selects the audio endpoint and model ID."""

    def get_model(self, model_id: str) -> ModelCapability:
        return ModelCapability("openai_compatible", model_id, frozenset({"audio"}))


class LiteLLMModelProvider:
    """Use LiteLLM for chat/vision and compatible ASR routes where available."""

    def __init__(
        self,
        api_key: str,
        *,
        provider_id: str,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        preset = get_provider_preset(provider_id)
        if preset is None:
            raise ProviderConfigurationError()
        if not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 4096:
            raise ProviderConfigurationError()
        resolved_base_url = (base_url or preset.default_base_url or "").strip().rstrip("/")
        if preset.id == "custom_openai" and not resolved_base_url:
            raise ProviderConfigurationError()
        self.provider_id = preset.id
        self._preset = preset
        self._api_key = api_key.strip()
        self._base_url = resolved_base_url or None
        self._timeout_seconds = float(timeout_seconds or _CHAT_TIMEOUT_SECONDS)
        self._request_id: str | None = None

    def set_client_request_id(self, request_id: str) -> None:
        if not isinstance(request_id, str) or not 6 <= len(request_id) <= 64 or not all(
            ch.isascii() and (ch.isalnum() or ch in "-_") for ch in request_id
        ):
            raise ProviderInputError()
        self._request_id = request_id

    def get_model_capability(self, model_id: str) -> ModelCapability | None:
        return get_model_capability(self.provider_id, model_id)

    def transcribe_audio(self, path: Path, model_id: str) -> TranscriptResponse:
        if self._preset.transcription_mode == "unsupported":
            raise ProviderCapabilityError()
        if self._preset.transcription_mode == "openai_compatible":
            audio_provider = ZhipuOpenAICompatibleProvider(
                self._api_key,
                base_url=self._base_url,
                catalog=_AnyOpenAICompatibleAudioCatalog(),  # type: ignore[arg-type]
                timeout_seconds=_TRANSCRIPTION_TIMEOUT_SECONDS,
            )
            return audio_provider.transcribe_audio(path, model_id)

        try:
            from litellm import transcription
        except ImportError:
            raise ProviderConfigurationError() from None
        try:
            with Path(path).open("rb") as audio_file:
                response = transcription(
                    model=self._model_name(model_id),
                    file=audio_file,
                    api_key=self._api_key,
                    api_base=self._base_url,
                    timeout=_TRANSCRIPTION_TIMEOUT_SECONDS,
                    num_retries=0,
                    response_format="json",
                )
        except Exception as exc:
            _raise_provider_error(exc)
        text = _field(response, "text")
        if not isinstance(text, str):
            raise ProviderResponseError()
        return TranscriptResponse(
            text=text,
            request_id=_string_field(response, "id") or _string_field(response, "request_id"),
            usage=UsageUnits(audio_seconds=None),
        )

    def analyze_text(self, prompt: str, text: str, model_id: str) -> TextResponse:
        response = self._completion(
            model_id,
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": text},
            ],
        )
        return TextResponse(
            text=_response_text(response),
            request_id=_string_field(response, "id"),
            usage=_response_usage(response),
        )

    def analyze_images(
        self,
        prompt: str,
        images: Sequence[Path],
        frame_ids: Sequence[str],
        model_id: str,
    ) -> OcrResponse:
        if not images or len(images) != len(frame_ids) or any(
            not isinstance(frame_id, str) or not frame_id.strip() for frame_id in frame_ids
        ):
            raise ProviderInputError()
        if len(set(frame_ids)) != len(frame_ids):
            raise ProviderInputError()
        capability = self.get_model_capability(model_id)
        if capability is not None and not capability.supports("image"):
            raise ProviderCapabilityError()
        content: list[dict[str, object]] = [
            {
                "type": "text",
                "text": (
                    f"{prompt}\n\n"
                    "仅转录图像中实际可见的文字，不要推断或补写。"
                    "请严格返回 JSON：{\"frames\":[{\"frame_id\":\"输入帧 ID\",\"text\":\"该帧可见文字；无文字时为空字符串\"}]}。"
                    "每个输入帧 ID 必须恰好出现一次，并保持输入顺序。\n"
                    "输入帧 ID 顺序：" + json.dumps(list(frame_ids), ensure_ascii=False)
                ),
            }
        ]
        for path in images:
            image_bytes, mime_type = _read_image(path)
            data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
            content.append({"type": "image_url", "image_url": {"url": data_url}})
        response = self._completion(
            model_id,
            [
                {
                    "role": "system",
                    "content": "你是 OCR 助手，只提取可见文字，并遵循用户指定的 JSON 格式。",
                },
                {"role": "user", "content": content},
            ],
        )
        raw_text = _response_text(response)
        frames = _parse_ocr_frames(raw_text, frame_ids)
        usage = _response_usage(response)
        return OcrResponse(
            frames=frames,
            request_id=_string_field(response, "id"),
            usage=UsageUnits(
                input_tokens=usage.input_tokens,
                audio_seconds=usage.audio_seconds,
                image_count=len(images),
                output_tokens=usage.output_tokens,
            ),
            raw_text=raw_text,
        )

    def embed_text(self, texts: Sequence[str], model_id: str) -> list[list[float]]:
        del texts, model_id
        raise ProviderCapabilityError()

    def count_input_tokens(self, text: str, model_id: str) -> int | None:
        if not text.strip():
            raise ProviderInputError()
        del model_id
        return None

    def _completion(self, model_id: str, messages: list[dict[str, object]]):
        capability = self.get_model_capability(model_id)
        if capability is not None and not capability.supports("text"):
            raise ProviderCapabilityError()
        if not model_id.strip():
            raise ProviderInputError()
        try:
            from litellm import completion
        except ImportError:
            raise ProviderConfigurationError() from None
        kwargs: dict[str, object] = {
            "model": self._model_name(model_id),
            "messages": messages,
            "api_key": self._api_key,
            "timeout": self._timeout_seconds,
            "num_retries": 0,
        }
        if self._base_url:
            kwargs["api_base"] = self._base_url
        if self._request_id:
            kwargs["extra_headers"] = {"X-Client-Request-Id": self._request_id}
        try:
            return completion(**kwargs)
        except Exception as exc:
            _raise_provider_error(exc)

    def _model_name(self, model_id: str) -> str:
        if not isinstance(model_id, str) or not model_id.strip():
            raise ProviderInputError()
        return f"{self._preset.litellm_route}/{model_id.strip()}"


def create_litellm_provider(
    api_key: str,
    *,
    provider_id: str,
    base_url: str | None = None,
) -> LiteLLMModelProvider:
    return LiteLLMModelProvider(api_key, provider_id=provider_id, base_url=base_url)


def _raise_provider_error(exc: Exception) -> None:
    name = type(exc).__name__.casefold()
    if "authentication" in name or "permissiondenied" in name:
        raise ProviderAuthenticationError() from None
    if "ratelimit" in name:
        raise ProviderRateLimitError() from None
    if "timeout" in name:
        raise ProviderTimeoutError() from None
    if "apiconnection" in name or "connection" in name or "network" in name:
        raise ProviderNetworkError() from None
    if "serviceunavailable" in name or "internalserver" in name:
        raise ProviderUnavailableError() from None
    if any(token in name for token in ("badrequest", "notfound", "unsupported", "contextwindow")):
        raise ProviderRequestRejectedError() from None
    if "apierror" in name:
        raise ProviderUnavailableError() from None
    raise ProviderResponseError() from None


def _field(value: object, name: str) -> object:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _string_field(value: object, name: str) -> str | None:
    item = _field(value, name)
    return item.strip()[:256] if isinstance(item, str) and item.strip() else None


def _response_text(response: object) -> str:
    choices = _field(response, "choices")
    if not isinstance(choices, (list, tuple)) or not choices:
        raise ProviderResponseError()
    message = _field(choices[0], "message")
    content = _field(message, "content")
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts = [
            item_text
            for item in content
            if isinstance((item_text := _field(item, "text")), str)
        ]
        if parts:
            return "\n".join(parts)
    raise ProviderResponseError()


def _response_usage(response: object) -> UsageUnits:
    usage = _field(response, "usage")
    prompt_tokens = _field(usage, "prompt_tokens")
    completion_tokens = _field(usage, "completion_tokens")
    return UsageUnits(
        input_tokens=prompt_tokens if isinstance(prompt_tokens, int) and prompt_tokens >= 0 else None,
        output_tokens=completion_tokens if isinstance(completion_tokens, int) and completion_tokens >= 0 else None,
    )


def _parse_ocr_frames(content: str, frame_ids: Sequence[str]) -> list[OcrFrame]:
    candidate = content.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            candidate = "\n".join(lines[1:-1]).strip()
    try:
        decoded = json.loads(candidate)
    except (json.JSONDecodeError, RecursionError):
        raise ProviderResponseError() from None
    if not isinstance(decoded, dict) or not isinstance(decoded.get("frames"), list):
        raise ProviderResponseError()
    frames_by_id: dict[str, str] = {}
    for item in decoded["frames"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("frame_id"), str)
            or not isinstance(item.get("text"), str)
        ):
            raise ProviderResponseError()
        frame_id = item["frame_id"]
        if frame_id not in frame_ids or frame_id in frames_by_id:
            raise ProviderResponseError()
        frames_by_id[frame_id] = item["text"]
    if set(frames_by_id) != set(frame_ids):
        raise ProviderResponseError()
    return [OcrFrame(frame_id=frame_id, text=frames_by_id[frame_id]) for frame_id in frame_ids]


__all__ = ["LiteLLMModelProvider", "create_litellm_provider"]
