"""Shared model provider contracts and result records."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Protocol, Sequence


@dataclass(frozen=True)
class ModelCapability:
    """Capabilities the adapter can use for one specific model."""

    provider_id: str
    model_id: str
    modalities: frozenset[str]
    max_input_tokens: int | None = None
    pricing: dict[str, Decimal] | None = None

    def supports(self, modality: str) -> bool:
        return modality in self.modalities


@dataclass(frozen=True)
class UsageUnits:
    """Provider-reported usage, with unavailable units represented as None."""

    input_tokens: int | None = None
    audio_seconds: Decimal | None = None
    image_count: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class CostEstimate:
    """Estimated cost; an unknown amount must never be presented as a quote."""

    currency: str
    amount: Decimal | None
    unit_breakdown: dict[str, Decimal] = field(default_factory=dict)
    known: bool = False


@dataclass(frozen=True)
class TranscriptSegment:
    start_seconds: float
    end_seconds: float
    text: str


@dataclass(frozen=True)
class TranscriptResponse:
    text: str
    segments: list[TranscriptSegment] = field(default_factory=list)
    request_id: str | None = None
    usage: UsageUnits = field(default_factory=UsageUnits)


@dataclass(frozen=True)
class TextResponse:
    text: str
    request_id: str | None = None
    usage: UsageUnits = field(default_factory=UsageUnits)


@dataclass(frozen=True)
class OcrFrame:
    frame_id: str
    text: str


@dataclass(frozen=True)
class OcrResponse:
    frames: list[OcrFrame]
    request_id: str | None = None
    usage: UsageUnits = field(default_factory=UsageUnits)
    raw_text: str | None = None


class ProviderError(RuntimeError):
    """A safe, stable provider failure with no remote body or request details."""

    code = "provider_error"

    def __init__(self) -> None:
        super().__init__(self.code)


class ProviderConfigurationError(ProviderError):
    code = "provider_not_configured"


class ProviderCapabilityError(ProviderError):
    code = "unsupported_capability"


class ProviderInputError(ProviderError):
    code = "invalid_input"


class ProviderAuthenticationError(ProviderError):
    code = "authentication_failed"


class ProviderRateLimitError(ProviderError):
    code = "rate_limited"


class ProviderRequestRejectedError(ProviderError):
    code = "request_rejected"


class ProviderPayloadTooLargeError(ProviderError):
    code = "payload_too_large"


class ProviderUnavailableError(ProviderError):
    code = "provider_unavailable"


class ProviderTimeoutError(ProviderError):
    code = "provider_timeout"


class ProviderNetworkError(ProviderError):
    code = "provider_network_error"


class ProviderResponseError(ProviderError):
    code = "invalid_provider_response"


class ModelCatalog(Protocol):
    def list_models(self) -> list[ModelCapability]: ...

    def get_model(self, model_id: str) -> ModelCapability | None: ...

    def estimate_cost(self, model_id: str, units: UsageUnits) -> CostEstimate | None: ...


class ModelProvider(Protocol):
    provider_id: str

    def get_model_capability(self, model_id: str) -> ModelCapability | None: ...

    def transcribe_audio(self, path: Path, model_id: str) -> TranscriptResponse: ...

    def analyze_text(self, prompt: str, text: str, model_id: str) -> TextResponse: ...

    def analyze_images(
        self,
        prompt: str,
        images: Sequence[Path],
        frame_ids: Sequence[str],
        model_id: str,
    ) -> OcrResponse: ...

    def embed_text(self, texts: Sequence[str], model_id: str) -> list[list[float]]: ...

    def count_input_tokens(self, text: str, model_id: str) -> int | None: ...
