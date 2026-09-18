"""Versioned fitted weights for the offline typesafe relevance gate."""

from __future__ import annotations

import hashlib
import json
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


def weight_set_version(
    *,
    weights: dict[str, float],
    bias: float,
    threshold: float,
    uncertain_band: UncertainBand,
    catalogue_version: str,
    row_digest: str,
    c_fp: float,
    c_fn: float,
) -> str:
    """Derive the portable model identity from every behavior-defining field."""
    document = {
        "format": "scout.typesafe-weight-set/v1",
        "weights": weights,
        "bias": bias,
        "threshold": threshold,
        "uncertain_band": uncertain_band.model_dump(mode="json"),
        "catalogue_version": catalogue_version,
        "row_digest": row_digest,
        "c_fp": c_fp,
        "c_fn": c_fn,
    }
    canonical = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


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

    @model_validator(mode="after")
    def version_matches_contents(self) -> WeightSet:
        expected = weight_set_version(
            weights=self.weights,
            bias=self.bias,
            threshold=self.threshold,
            uncertain_band=self.uncertain_band,
            catalogue_version=self.catalogue_version,
            row_digest=self.row_digest,
            c_fp=self.c_fp,
            c_fn=self.c_fn,
        )
        if self.weight_set_version != expected:
            raise ValueError("weight_set_version does not match model contents")
        return self
