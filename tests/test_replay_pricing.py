"""Tests for spend_estimate/v1 repricing (replay_pricing.py)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from jig import Span, SpanKind, Usage

import scout.replay.pricing as rp


def _llm_span(*, input_tokens: int | None, output_tokens: int | None = 0) -> Span:
    usage = None
    if input_tokens is not None:
        usage = Usage(input_tokens=input_tokens, output_tokens=output_tokens or 0)
    return Span(
        id="s1", trace_id="t1", kind=SpanKind.LLM_CALL, name="llm_call",
        started_at=datetime.now(UTC), usage=usage,
    )


class TestLoadPricingCatalog:
    def test_loads_the_real_checked_in_catalog(self) -> None:
        catalog = rp.load_pricing_catalog()
        assert catalog.version == 1
        assert catalog.as_of
        assert catalog.source_url
        assert "claude-haiku-4-5-20251001" in catalog.models
        assert len(catalog.catalog_hash) == 64

    def test_hash_is_deterministic_for_identical_content(self, tmp_path) -> None:
        doc = {
            "version": 1, "as_of": "2026-01-01", "source_url": "https://example.com",
            "models": {"m": {"input_usd_per_million": 1.0, "output_usd_per_million": 2.0}},
        }
        path_a = tmp_path / "a.json"
        path_b = tmp_path / "b.json"
        path_a.write_text(json.dumps(doc, indent=2))
        path_b.write_text(json.dumps(doc, sort_keys=False, separators=(",", ":")))

        catalog_a = rp.load_pricing_catalog(path_a)
        catalog_b = rp.load_pricing_catalog(path_b)
        assert catalog_a.catalog_hash == catalog_b.catalog_hash

    def test_missing_file_raises(self, tmp_path) -> None:
        with pytest.raises(rp.PricingCatalogError, match="could not read"):
            rp.load_pricing_catalog(tmp_path / "nope.json")

    def test_invalid_json_raises(self, tmp_path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{not json")
        with pytest.raises(rp.PricingCatalogError, match="not valid JSON"):
            rp.load_pricing_catalog(path)

    def test_wrong_version_raises(self, tmp_path) -> None:
        path = tmp_path / "wrong.json"
        path.write_text(json.dumps({
            "version": 2, "as_of": "2026-01-01", "source_url": "u",
            "models": {"m": {"input_usd_per_million": 1.0, "output_usd_per_million": 2.0}},
        }))
        with pytest.raises(rp.PricingCatalogError, match="version"):
            rp.load_pricing_catalog(path)

    def test_negative_rate_raises(self, tmp_path) -> None:
        path = tmp_path / "neg.json"
        path.write_text(json.dumps({
            "version": 1, "as_of": "2026-01-01", "source_url": "u",
            "models": {"m": {"input_usd_per_million": -1.0, "output_usd_per_million": 2.0}},
        }))
        with pytest.raises(rp.PricingCatalogError, match="input_usd_per_million"):
            rp.load_pricing_catalog(path)

    @pytest.mark.parametrize("rate", [0, 0.0])
    def test_zero_rate_raises(self, tmp_path, rate) -> None:
        path = tmp_path / "zero.json"
        path.write_text(json.dumps({
            "version": 1, "as_of": "2026-01-01", "source_url": "u",
            "models": {"m": {
                "input_usd_per_million": 1.0, "output_usd_per_million": rate,
            }},
        }))
        with pytest.raises(rp.PricingCatalogError, match="output_usd_per_million"):
            rp.load_pricing_catalog(path)

    @pytest.mark.parametrize(
        "rate", [float("nan"), float("inf"), float("-inf"), 10 ** 1000],
    )
    def test_non_finite_rate_raises_pricing_catalog_error(self, tmp_path, rate) -> None:
        path = tmp_path / "non-finite.json"
        path.write_text(json.dumps({
            "version": 1, "as_of": "2026-01-01", "source_url": "u",
            "models": {"m": {
                "input_usd_per_million": rate, "output_usd_per_million": 2.0,
            }},
        }))
        with pytest.raises(rp.PricingCatalogError, match="input_usd_per_million"):
            rp.load_pricing_catalog(path)

    def test_empty_models_raises(self, tmp_path) -> None:
        path = tmp_path / "empty.json"
        path.write_text(json.dumps({
            "version": 1, "as_of": "2026-01-01", "source_url": "u", "models": {},
        }))
        with pytest.raises(rp.PricingCatalogError, match="models"):
            rp.load_pricing_catalog(path)


class TestAggregateBaselineUsage:
    def test_sums_across_multiple_llm_calls(self) -> None:
        spans = [
            _llm_span(input_tokens=100, output_tokens=50),
            _llm_span(input_tokens=200, output_tokens=25),
        ]
        usage = rp.aggregate_baseline_usage(spans)
        assert usage == rp.TokenUsage(input_tokens=300, output_tokens=75)

    def test_no_llm_calls_is_none(self) -> None:
        span = Span(
            id="s1", trace_id="t1", kind=SpanKind.AGENT_RUN, name="root",
            started_at=datetime.now(UTC),
        )
        assert rp.aggregate_baseline_usage([span]) is None

    def test_missing_usage_on_any_call_is_none_not_partial(self) -> None:
        spans = [_llm_span(input_tokens=100, output_tokens=50), _llm_span(input_tokens=None)]
        assert rp.aggregate_baseline_usage(spans) is None


class TestPricePair:
    @pytest.fixture
    def catalog(self) -> rp.PricingCatalog:
        return rp.PricingCatalog(
            version=1, as_of="2026-01-01", source_url="https://example.com",
            catalog_hash="deadbeef",
            models={"model-a": rp.ModelRate(input_usd_per_million=1.0, output_usd_per_million=2.0)},
        )

    def test_prices_known_model(self, catalog: rp.PricingCatalog) -> None:
        usage = rp.TokenUsage(input_tokens=1_000_000, output_tokens=500_000)
        estimate = rp.price_pair(usage, "model-a", catalog)
        assert estimate is not None
        assert estimate.estimated_usd == pytest.approx(1.0 + 1.0)
        assert estimate.candidate_model == "model-a"

    def test_unknown_model_is_unpriceable(self, catalog: rp.PricingCatalog) -> None:
        usage = rp.TokenUsage(input_tokens=1_000, output_tokens=1_000)
        assert rp.price_pair(usage, "unknown-model", catalog) is None

    def test_missing_usage_is_unpriceable(self, catalog: rp.PricingCatalog) -> None:
        assert rp.price_pair(None, "model-a", catalog) is None
