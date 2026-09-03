"""spend_estimate/v1: reproducible offline-replay cost estimation.

Repricing is the only estimation strategy this module implements: a
baseline trace's own recorded, priced token usage (summed across every
LLM_CALL span on its verified AGENT_RUN root) is repriced at a candidate
model's catalog rate. The catalog (``contracts/replay-pricing.v1.json``) is
plain data — a versioned, dated, sourced table of USD-per-million-token
rates keyed by exact model identity — never fetched live and never a JSON
Schema. Loading is read-only and issues no network or database calls.

A pair is unpriceable, never a hard error, whenever either side of the
repricing is unavailable: the baseline trace has no complete per-call usage
evidence (a missing count, not a legitimate zero), or the candidate model
has no catalog entry. Callers classify unpriceable pairs and exclude them
from spend before any paid execution — this module only ever answers "what
would this pair cost if both sides are known," never invents a number for
the unknown side.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jig import Span, SpanKind

from scout.resources import runtime_resource

PRICING_CATALOG_VERSION = 1

DEFAULT_PRICING_CATALOG_PATH = runtime_resource("contracts", "replay-pricing.v1.json")


class PricingCatalogError(Exception):
    """The pricing catalog file is missing, malformed, or fails validation."""


@dataclass(frozen=True, slots=True)
class ModelRate:
    """One model's catalog rate, in USD per one million tokens."""

    input_usd_per_million: float
    output_usd_per_million: float


@dataclass(frozen=True, slots=True)
class PricingCatalog:
    """A loaded, validated replay-pricing v1 catalog and its own content
    identity (`catalog_hash`) — the exact price evidence a canonical replay
    plan pins."""

    version: int
    as_of: str
    source_url: str
    catalog_hash: str
    models: dict[str, ModelRate]

    def rate_for(self, model: str) -> ModelRate | None:
        return self.models.get(model)


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Total input/output tokens recorded across every LLM_CALL span on one
    verified AGENT_RUN trace."""

    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class PriceEstimate:
    """One priced baseline/candidate pair: the candidate model's rate
    applied to the baseline's own recorded token usage."""

    candidate_model: str
    input_tokens: int
    output_tokens: int
    estimated_usd: float


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    )


def _positive_finite_rate(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        rate = float(value)
    except (OverflowError, ValueError):
        return None
    return rate if math.isfinite(rate) and rate > 0 else None


def load_pricing_catalog(path: Path | str | None = None) -> PricingCatalog:
    """Load and validate one replay-pricing v1 catalog file.

    Raises PricingCatalogError for a missing file, invalid JSON, an
    unsupported version, or any model entry with a missing/non-positive
    rate. `catalog_hash` is the SHA-256 of the catalog's own canonical JSON
    serialization (sorted keys, compact separators) — reproducible from the
    file's content alone, independent of formatting or key order on disk.
    """
    catalog_path = Path(path) if path is not None else DEFAULT_PRICING_CATALOG_PATH
    try:
        raw = catalog_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PricingCatalogError(
            f"could not read pricing catalog {catalog_path!r}: {exc}"
        ) from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PricingCatalogError(
            f"pricing catalog {catalog_path!r} is not valid JSON: {exc}"
        ) from exc

    if not isinstance(document, dict):
        raise PricingCatalogError("pricing catalog must be a JSON object")
    version = document.get("version")
    if version != PRICING_CATALOG_VERSION:
        raise PricingCatalogError(
            f"pricing catalog version {version!r} is not the supported "
            f"{PRICING_CATALOG_VERSION!r}"
        )
    as_of = document.get("as_of")
    if not isinstance(as_of, str) or not as_of:
        raise PricingCatalogError("pricing catalog as_of must be a non-empty string")
    source_url = document.get("source_url")
    if not isinstance(source_url, str) or not source_url:
        raise PricingCatalogError("pricing catalog source_url must be a non-empty string")
    raw_models = document.get("models")
    if not isinstance(raw_models, dict) or not raw_models:
        raise PricingCatalogError("pricing catalog models must be a non-empty object")

    models: dict[str, ModelRate] = {}
    for model_id, entry in raw_models.items():
        if not isinstance(entry, dict):
            raise PricingCatalogError(f"pricing catalog entry for {model_id!r} must be an object")
        input_rate = entry.get("input_usd_per_million")
        output_rate = entry.get("output_usd_per_million")
        validated_input_rate = _positive_finite_rate(input_rate)
        if validated_input_rate is None:
            raise PricingCatalogError(
                f"pricing catalog entry for {model_id!r} has an invalid input_usd_per_million"
            )
        validated_output_rate = _positive_finite_rate(output_rate)
        if validated_output_rate is None:
            raise PricingCatalogError(
                f"pricing catalog entry for {model_id!r} has an invalid output_usd_per_million"
            )
        models[model_id] = ModelRate(
            input_usd_per_million=validated_input_rate,
            output_usd_per_million=validated_output_rate,
        )

    catalog_hash = hashlib.sha256(_canonical_json(document).encode("utf-8")).hexdigest()
    return PricingCatalog(
        version=version, as_of=as_of, source_url=source_url,
        catalog_hash=catalog_hash, models=models,
    )


def aggregate_baseline_usage(spans: list[Span]) -> TokenUsage | None:
    """Sum input/output tokens across every LLM_CALL span on a trace.

    Returns None — never a zero — whenever there is no LLM_CALL span, or
    any LLM_CALL span lacks recorded usage: a missing count must never be
    silently treated as a free call.
    """
    llm_calls = [span for span in spans if span.kind == SpanKind.LLM_CALL]
    if not llm_calls or any(span.usage is None for span in llm_calls):
        return None
    input_tokens = sum(span.usage.input_tokens for span in llm_calls if span.usage is not None)
    output_tokens = sum(span.usage.output_tokens for span in llm_calls if span.usage is not None)
    return TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens)


def price_pair(
    usage: TokenUsage | None, candidate_model: str, catalog: PricingCatalog,
) -> PriceEstimate | None:
    """Reprice one baseline's recorded usage at `candidate_model`'s catalog
    rate. Returns None (unpriceable) when usage is unavailable or the
    model has no catalog entry — never a fabricated estimate."""
    if usage is None:
        return None
    rate = catalog.rate_for(candidate_model)
    if rate is None:
        return None
    estimated_usd = (
        usage.input_tokens / 1_000_000 * rate.input_usd_per_million
        + usage.output_tokens / 1_000_000 * rate.output_usd_per_million
    )
    return PriceEstimate(
        candidate_model=candidate_model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        estimated_usd=estimated_usd,
    )


__all__ = [
    "DEFAULT_PRICING_CATALOG_PATH",
    "PRICING_CATALOG_VERSION",
    "ModelRate",
    "PriceEstimate",
    "PricingCatalog",
    "PricingCatalogError",
    "TokenUsage",
    "aggregate_baseline_usage",
    "load_pricing_catalog",
    "price_pair",
]
