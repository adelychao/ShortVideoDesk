from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CommentAnalysisOptionsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)

    mode: Literal['economy', 'full'] = 'economy'
    batch_size: int = Field(default=200, ge=25, le=300)
    candidate_limit: int = Field(default=2000, ge=100, le=50000)
    budget_cny: float = Field(default=1.0, gt=0, le=10000)
    token_budget: int = Field(default=300000, ge=10000, le=100000000)
    input_price_per_million: float = Field(default=0.8, ge=0, le=100000)
    output_price_per_million: float = Field(default=2.8, ge=0, le=100000)
    max_output_tokens: int = Field(default=32768, ge=256, le=32768)
