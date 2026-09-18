from datetime import UTC, datetime

import pytest

from scout.typesafe.fitting import cost_policy, extract_features, fit_weight_set
from scout.typesafe.models import Answers


def _answers(document: dict[str, object]) -> Answers:
    return Answers.model_validate(
        {"answers": document, "request_id": "request", "model": "fixture"}
    )


def test_extract_features_covers_probability_score_and_choice_with_none() -> None:
    features = extract_features(
        _answers(
            {
                "relevance": {"kind": "probability", "probability": 0.8},
                "quality": {
                    "kind": "score",
                    "levels": [
                        {"level": "low", "probability": 0.1},
                        {"level": "high", "probability": 0.9},
                    ],
                    "confidence": 0.7,
                },
                "has_signal": {
                    "kind": "choice",
                    "probabilities": {"none": 0.25, "some": 0.75},
                    "confidence": 0.8,
                },
                "annotation": {
                    "kind": "choice",
                    "probabilities": {"person": 0.6, "company": 0.4},
                    "confidence": 0.9,
                },
            }
        )
    )
    assert features == {
        "relevance": 0.8,
        "quality/low": 0.1,
        "quality/high": 0.9,
        "has_signal": 0.75,
    }


def test_cost_policy_and_fit_are_reproducible() -> None:
    threshold, band = cost_policy(2.0, 1.0)
    assert threshold == pytest.approx(2 / 3)
    assert band.lower == pytest.approx(1 / 3)
    assert band.upper == pytest.approx(2 / 3)
    rows = [(1, {"q": 0.1}, False), (2, {"q": 0.9}, True)]
    fitted = fit_weight_set(
        rows,
        catalogue_version="a" * 64,
        c_fp=2.0,
        c_fn=1.0,
        fitted_at=datetime(2026, 9, 17, tzinfo=UTC),
    )
    repeated = fit_weight_set(
        rows,
        catalogue_version="a" * 64,
        c_fp=2.0,
        c_fn=1.0,
        fitted_at=datetime(2026, 9, 18, tzinfo=UTC),
    )
    assert fitted.weight_set_version == repeated.weight_set_version
    assert fitted.row_digest == repeated.row_digest
