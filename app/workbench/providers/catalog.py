"""Declared model capabilities for the selected Zhipu models."""

from __future__ import annotations

from decimal import Decimal

from .base import CostEstimate, ModelCapability, UsageUnits


PROVIDER_ID = "zhipu"
ASR_MODEL_ID = "glm-asr-2512"
TEXT_VISION_MODEL_ID = "glm-5.3-flash"


class ZhipuModelCatalog:
    """The two explicitly selected models; no embedding model is declared."""

    _models = (
        ModelCapability(
            provider_id=PROVIDER_ID,
            model_id=ASR_MODEL_ID,
            modalities=frozenset({"audio"}),
            max_input_tokens=None,
            pricing=None,
        ),
        ModelCapability(
            provider_id=PROVIDER_ID,
            model_id=TEXT_VISION_MODEL_ID,
            modalities=frozenset({"text", "image"}),
            max_input_tokens=1_000_000,
            pricing=None,
        ),
    )

    def list_models(self) -> list[ModelCapability]:
        return list(self._models)

    def get_model(self, model_id: str) -> ModelCapability | None:
        return next((model for model in self._models if model.model_id == model_id), None)

    def estimate_cost(self, model_id: str, units: UsageUnits) -> CostEstimate | None:
        del units  # No prices were provided or independently established for this catalog.
        if self.get_model(model_id) is None:
            return None
        return CostEstimate(currency="CNY", amount=None, unit_breakdown={}, known=False)


MODEL_CATALOG = ZhipuModelCatalog()


__all__ = [
    "ASR_MODEL_ID",
    "MODEL_CATALOG",
    "PROVIDER_ID",
    "TEXT_VISION_MODEL_ID",
    "ZhipuModelCatalog",
]
