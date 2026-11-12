"""Standard-library OpenAI-compatible adapter for Zhipu's selected models."""

from __future__ import annotations

import base64
import json
import mimetypes
import socket
import struct
import urllib.error
import urllib.request
import uuid
import wave
from http.client import HTTPException
from io import BytesIO
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit

from .base import (
    ModelCapability,
    ProviderAuthenticationError,
    ProviderCapabilityError,
    ProviderConfigurationError,
    ProviderInputError,
    ProviderNetworkError,
    ProviderPayloadTooLargeError,
    ProviderRateLimitError,
    ProviderRequestRejectedError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    TextResponse,
    TranscriptResponse,
    UsageUnits,
    OcrFrame,
    OcrResponse,
)
from .catalog import (
    ASR_MODEL_ID,
    MODEL_CATALOG,
    PROVIDER_ID,
    TEXT_VISION_MODEL_ID,
    ZhipuModelCatalog,
)


DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
_MAX_AUDIO_BYTES = 25 * 1000 * 1000
_MAX_AUDIO_SECONDS = 30.0
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_REQUEST_TIMEOUT_SECONDS = 90
_CHAT_TIMEOUT_SECONDS = 600


class ZhipuOpenAICompatibleProvider:
    """Implements only the audio, text, and image operations selected in Task 10."""

    provider_id = PROVIDER_ID

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        catalog: ZhipuModelCatalog | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 4096:
            raise ProviderConfigurationError()
        chosen_base_url = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
        parsed = urlsplit(chosen_base_url)
        try:
            port = parsed.port
        except ValueError:
            raise ProviderConfigurationError() from None
        scheme = parsed.scheme.casefold()
        host = (parsed.hostname or "").casefold()
        if (
            scheme not in {"https", "http"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (scheme == "http" and host not in {"localhost", "127.0.0.1", "::1"})
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ProviderConfigurationError()
        if timeout_seconds is not None and (not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0):
            raise ProviderConfigurationError()

        # Keep credentials out of reprs and exception text; callers supply keys
        # obtained from the operating-system credential store.
        self._api_key = api_key.strip()
        self._base_url = chosen_base_url
        self._catalog = catalog or MODEL_CATALOG
        self._timeout_seconds = float(timeout_seconds if timeout_seconds is not None else _REQUEST_TIMEOUT_SECONDS)
        self._chat_timeout_seconds = float(timeout_seconds if timeout_seconds is not None else _CHAT_TIMEOUT_SECONDS)
        self._client_request_id: str | None = None

    def set_client_request_id(self, request_id: str) -> None:
        if not isinstance(request_id, str) or not 6 <= len(request_id) <= 64 or not all(ch.isascii() and (ch.isalnum() or ch in '-_') for ch in request_id):
            raise ProviderInputError()
        self._client_request_id = request_id

    def get_model_capability(self, model_id: str) -> ModelCapability | None:
        return self._catalog.get_model(model_id)

    def transcribe_audio(self, path: Path, model_id: str) -> TranscriptResponse:
        self._require_model_modality(model_id, "audio")
        audio, filename, mime_type = _read_validated_audio(path)
        fields = (("model", model_id), ("stream", "false"))
        body, content_type = _multipart_body(fields, "file", filename, mime_type, audio)
        payload = self._request("/audio/transcriptions", body, content_type)
        text = payload.get("text")
        if not isinstance(text, str):
            raise ProviderResponseError()
        request_id = _optional_string(payload.get("request_id")) or _optional_string(payload.get("id"))
        # The transcription endpoint does not return segments, timestamps, or usage.
        return TranscriptResponse(text=text, segments=[], request_id=request_id, usage=UsageUnits())

    def analyze_text(self, prompt: str, text: str, model_id: str) -> TextResponse:
        self._require_model_modality(model_id, "text")
        _require_nonempty_text(prompt)
        _require_nonempty_text(text)
        payload = self._chat_completion(
            model_id,
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": text},
            ],
        )
        result_text = _chat_text(payload)
        return TextResponse(
            text=result_text,
            request_id=_request_id(payload),
            usage=_chat_usage(payload),
        )

    def analyze_images(
        self,
        prompt: str,
        images: Sequence[Path],
        frame_ids: Sequence[str],
        model_id: str,
    ) -> OcrResponse:
        self._require_model_modality(model_id, "image")
        _require_nonempty_text(prompt)
        if not images or len(images) != len(frame_ids) or any(
            not isinstance(frame_id, str) or not frame_id.strip() for frame_id in frame_ids
        ):
            raise ProviderInputError()
        if len(set(frame_ids)) != len(frame_ids):
            raise ProviderInputError()

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
        for path, frame_id in zip(images, frame_ids):
            image_bytes, mime_type = _read_image(path)
            data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
            content.append({"type": "image_url", "image_url": {"url": data_url}})

        payload = self._chat_completion(
            model_id,
            [
                {
                    "role": "system",
                    "content": "你是 OCR 助手，只提取可见文字，并遵循用户指定的 JSON 格式。",
                },
                {"role": "user", "content": content},
            ],
        )
        result_text = _chat_text(payload)
        frames = _parse_ocr_frames(result_text, frame_ids)
        usage = _chat_usage(payload)
        usage = UsageUnits(
            input_tokens=usage.input_tokens,
            audio_seconds=usage.audio_seconds,
            image_count=len(images),
            output_tokens=usage.output_tokens,
        )
        return OcrResponse(frames=frames, request_id=_request_id(payload), usage=usage, raw_text=result_text)

    def embed_text(self, texts: Sequence[str], model_id: str) -> list[list[float]]:
        del texts
        self._require_model_modality(model_id, "embed")
        # Neither selected model advertises embedding support.
        raise ProviderCapabilityError()

    def count_input_tokens(self, text: str, model_id: str) -> int | None:
        _require_nonempty_text(text)
        self._require_model_modality(model_id, "text")
        # No tokenizer or counting endpoint is documented for these models.
        return None

    def _require_model_modality(self, model_id: str, modality: str) -> ModelCapability:
        capability = self.get_model_capability(model_id)
        if capability is None or not capability.supports(modality):
            raise ProviderCapabilityError()
        return capability

    def _chat_completion(self, model_id: str, messages: list[dict[str, object]]) -> dict[str, object]:
        request_payload: dict[str, object] = {"model": model_id, "messages": messages, "stream": False}
        if self._client_request_id is not None:
            request_payload["request_id"] = self._client_request_id
        body = json.dumps(
            request_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return self._request("/chat/completions", body, "application/json; charset=utf-8", timeout_seconds=self._chat_timeout_seconds)

    def _request(self, endpoint: str, body: bytes, content_type: str, *, timeout_seconds: float | None = None) -> dict[str, object]:
        request = urllib.request.Request(
            self._base_url + endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": content_type,
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds if timeout_seconds is None else timeout_seconds) as response:
                response_body = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(response_body) > _MAX_RESPONSE_BYTES:
                    raise ProviderResponseError()
        except urllib.error.HTTPError as exc:
            raise _http_error(exc.code) from None
        except (socket.timeout, TimeoutError):
            raise ProviderTimeoutError() from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise ProviderTimeoutError() from None
            raise ProviderNetworkError() from None
        except (HTTPException, OSError):
            raise ProviderNetworkError() from None

        try:
            decoded = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise ProviderResponseError() from None
        if not isinstance(decoded, dict):
            raise ProviderResponseError()
        return decoded


def create_provider(
    api_key: str,
    base_url: str | None = None,
    *,
    provider_id: str = PROVIDER_ID,
):
    """Create a provider adapter after the caller reads its scoped OS key."""
    from .litellm_adapter import create_litellm_provider

    return create_litellm_provider(api_key, provider_id=provider_id, base_url=base_url)


def _http_error(status_code: int):
    if status_code in {408, 504}:
        return ProviderTimeoutError()
    if status_code in {401, 403}:
        return ProviderAuthenticationError()
    if status_code == 429:
        return ProviderRateLimitError()
    if status_code == 413:
        return ProviderPayloadTooLargeError()
    if status_code in {400, 404, 422}:
        return ProviderRequestRejectedError()
    if 500 <= status_code <= 599:
        return ProviderUnavailableError()
    return ProviderNetworkError()


def _read_validated_audio(path: Path) -> tuple[bytes, str, str]:
    try:
        audio_path = Path(path)
        suffix = audio_path.suffix.casefold()
        if suffix not in {".wav", ".mp3"} or not audio_path.is_file():
            raise ProviderInputError()
        if audio_path.stat().st_size > _MAX_AUDIO_BYTES:
            raise ProviderPayloadTooLargeError()
        audio = audio_path.read_bytes()
    except ProviderInputError:
        raise
    except ProviderPayloadTooLargeError:
        raise
    except OSError:
        raise ProviderInputError() from None
    if not audio or len(audio) > _MAX_AUDIO_BYTES:
        if len(audio) > _MAX_AUDIO_BYTES:
            raise ProviderPayloadTooLargeError()
        raise ProviderInputError()

    if suffix == ".wav":
        duration = _wav_duration(audio)
        filename, mime_type = "audio.wav", "audio/wav"
    else:
        duration = _mp3_duration(audio)
        filename, mime_type = "audio.mp3", "audio/mpeg"
    if duration > _MAX_AUDIO_SECONDS:
        raise ProviderInputError()
    return audio, filename, mime_type


def _wav_duration(audio: bytes) -> float:
    try:
        with wave.open(BytesIO(audio), "rb") as wav_file:
            sample_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
    except (wave.Error, EOFError, OSError):
        raise ProviderInputError() from None
    if sample_rate <= 0:
        raise ProviderInputError()
    return frame_count / sample_rate


def _mp3_duration(audio: bytes) -> float:
    """Read MPEG audio frame headers to enforce the documented 30-second cap."""
    offset = 0
    if audio.startswith(b"ID3") and len(audio) >= 10:
        tag_size_bytes = audio[6:10]
        if any(byte & 0x80 for byte in tag_size_bytes):
            raise ProviderInputError()
        tag_size = (
            (tag_size_bytes[0] << 21)
            | (tag_size_bytes[1] << 14)
            | (tag_size_bytes[2] << 7)
            | tag_size_bytes[3]
        )
        offset = 10 + tag_size + (10 if audio[5] & 0x10 else 0)
        if offset > len(audio):
            raise ProviderInputError()

    elapsed_seconds = 0.0
    frames = 0
    while offset + 4 <= len(audio):
        header = struct.unpack_from(">I", audio, offset)[0]
        if (header >> 21) != 0x7FF:
            offset += 1
            continue
        version = (header >> 19) & 0x3
        layer = (header >> 17) & 0x3
        bitrate_index = (header >> 12) & 0xF
        sample_index = (header >> 10) & 0x3
        padding = (header >> 9) & 0x1
        if version == 1 or layer == 0 or bitrate_index in {0, 15} or sample_index == 3:
            offset += 1
            continue

        mpeg1 = version == 3
        layer_number = 4 - layer
        if mpeg1:
            bitrate_tables = {
                1: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0),
                2: (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 0),
                3: (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448, 0),
            }
        else:
            bitrate_tables = {
                1: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
                2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
                3: (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, 0),
            }
        base_rates = (44100, 48000, 32000)
        sample_rate = base_rates[sample_index] // (1 if version == 3 else 2 if version == 2 else 4)
        bitrate = bitrate_tables[layer_number][bitrate_index] * 1000
        samples_per_frame = 384 if layer_number == 1 else 1152 if layer_number == 2 or mpeg1 else 576
        if layer_number == 1:
            frame_length = (12 * bitrate // sample_rate + padding) * 4
        else:
            coefficient = 144 if mpeg1 or layer_number == 2 else 72
            frame_length = coefficient * bitrate // sample_rate + padding
        if frame_length < 4 or offset + frame_length > len(audio):
            offset += 1
            continue
        elapsed_seconds += samples_per_frame / sample_rate
        frames += 1
        offset += frame_length

    if frames == 0:
        raise ProviderInputError()
    return elapsed_seconds


def _multipart_body(
    fields: Sequence[tuple[str, str]],
    file_field: str,
    filename: str,
    mime_type: str,
    content: bytes,
) -> tuple[bytes, str]:
    boundary = "----workbench-" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.extend(
            (
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"),
                value.encode("utf-8"),
                b"\r\n",
            )
        )
    chunks.extend(
        (
            f"--{boundary}\r\n".encode("ascii"),
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode("ascii"),
            f"Content-Type: {mime_type}\r\n\r\n".encode("ascii"),
            content,
            b"\r\n",
            f"--{boundary}--\r\n".encode("ascii"),
        )
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _read_image(path: Path) -> tuple[bytes, str]:
    try:
        image_path = Path(path)
        if not image_path.is_file():
            raise ProviderInputError()
        image_bytes = image_path.read_bytes()
    except ProviderInputError:
        raise
    except OSError:
        raise ProviderInputError() from None
    if not image_bytes:
        raise ProviderInputError()
    mime_type = mimetypes.guess_type(image_path.name)[0]
    if mime_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
        raise ProviderInputError()
    return image_bytes, mime_type


def _require_nonempty_text(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ProviderInputError()


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _request_id(payload: dict[str, object]) -> str | None:
    return _optional_string(payload.get("request_id")) or _optional_string(payload.get("id"))


def _chat_text(payload: dict[str, object]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProviderResponseError()
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ProviderResponseError()
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            item["text"]
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        if parts:
            return "\n".join(parts)
    raise ProviderResponseError()


def _chat_usage(payload: dict[str, object]) -> UsageUnits:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return UsageUnits()
    return UsageUnits(
        input_tokens=_nonnegative_int(usage.get("prompt_tokens")),
        output_tokens=_nonnegative_int(usage.get("completion_tokens")),
    )


def _nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


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


__all__ = [
    "DEFAULT_BASE_URL",
    "ZhipuOpenAICompatibleProvider",
    "create_provider",
    "ASR_MODEL_ID",
    "TEXT_VISION_MODEL_ID",
]
