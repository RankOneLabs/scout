"""Versioned fitted weights for the offline typesafe relevance gate."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class UncertainBand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lower: float = Field(ge=0.0, le=1.0)
    upper: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def ordered(self) -> UncertainBand:
        if self.lower > self.upper:
            raise ValueError("uncertain band lower bound exceeds upper bound")
        return self


class WeightSet(BaseModel):
    """Portable data used only by an explicitly registered fitted gate."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    format: Literal["scout.typesafe-weight-set/v1"] = "scout.typesafe-weight-set/v1"
    weights: dict[str, float]
    bias: float
    threshold: float = Field(ge=0.0, le=1.0)
    uncertain_band: UncertainBand
    catalogue_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    weight_set_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    fitted_at: str
    row_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    c_fp: float = Field(gt=0.0)
    c_fn: float = Field(gt=0.0)
