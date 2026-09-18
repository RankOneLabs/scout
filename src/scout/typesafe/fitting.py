"""Pure feature extraction and the isolated scikit-learn fitting boundary."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime

from scout.typesafe.models import Answers, ChoiceAnswer, ProbabilityAnswer, ScoreAnswer
from scout.typesafe.weights import UncertainBand, WeightSet


def extract_features(answers: Answers) -> dict[str, float]:
    """Project tagged answers to stable, question-keyed numeric features.

    Probability and choice-with-none answers use the question id. Score levels
    use ``<question id>/<level>`` so each declared level has its own coefficient.
    Other choice answers are annotations rather than classifier inputs.
    """
    features: dict[str, float] = {}
    for question_id, answer in answers.answers.items():
        if isinstance(answer, ProbabilityAnswer):
            features[question_id] = float(answer.probability)
        elif isinstance(answer, ScoreAnswer):
            for level in answer.levels:
                key = f"{question_id}/{level.level}"
                if key in features:
                    raise ValueError(f"duplicate score level feature: {key}")
                features[key] = float(level.probability)
        elif isinstance(answer, ChoiceAnswer):
            none_keys = [key for key in answer.probabilities if key.casefold() == "none"]
            if none_keys:
                features[question_id] = 1.0 - float(answer.probabilities[none_keys[0]])
    return features


def cost_policy(c_fp: float, c_fn: float) -> tuple[float, UncertainBand]:
    """Return Bayes cost threshold and the symmetric-cost ambiguity interval."""
    if not math.isfinite(c_fp) or not math.isfinite(c_fn) or c_fp <= 0 or c_fn <= 0:
        raise ValueError("c_fp and c_fn must be finite and greater than zero")
    threshold = c_fp / (c_fp + c_fn)
    complement = 1.0 - threshold
    return threshold, UncertainBand(
        lower=min(threshold, complement), upper=max(threshold, complement)
    )


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def digest_rows(rows: Sequence[tuple[int, Mapping[str, float], bool]]) -> str:
    document = [
        {"evaluation_id": evaluation_id, "features": dict(sorted(features.items())), "label": label}
        for evaluation_id, features, label in sorted(rows, key=lambda row: row[0])
    ]
    return hashlib.sha256(_canonical(document)).hexdigest()


def fit_weight_set(
    rows: Sequence[tuple[int, Mapping[str, float], bool]],
    *,
    catalogue_version: str,
    c_fp: float,
    c_fn: float,
    fitted_at: datetime,
) -> WeightSet:
    """Fit deterministic L2 logistic weights without leaking sklearn into scan imports."""
    from sklearn.linear_model import LogisticRegression

    if not rows:
        raise ValueError("no finalized training rows are available")
    labels = [int(label) for _, _, label in rows]
    if len(set(labels)) != 2:
        raise ValueError("training rows must contain both relevance classes")
    feature_keys = sorted({key for _, features, _ in rows for key in features})
    if not feature_keys:
        raise ValueError("training rows contain no supported answer features")
    matrix = [[float(features.get(key, 0.0)) for key in feature_keys] for _, features, _ in rows]
    classifier = LogisticRegression(
        l1_ratio=0.0, solver="liblinear", C=1.0, max_iter=1000, random_state=0
    )
    classifier.fit(matrix, labels)
    weights = dict(zip(feature_keys, (float(value) for value in classifier.coef_[0]), strict=True))
    bias = float(classifier.intercept_[0])
    row_digest = digest_rows(rows)
    threshold, uncertain_band = cost_policy(c_fp, c_fn)
    version_document = {
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
    version = hashlib.sha256(_canonical(version_document)).hexdigest()
    return WeightSet(
        weights=weights,
        bias=bias,
        threshold=threshold,
        uncertain_band=uncertain_band,
        catalogue_version=catalogue_version,
        weight_set_version=version,
        fitted_at=fitted_at.isoformat(),
        row_digest=row_digest,
        c_fp=c_fp,
        c_fn=c_fn,
    )
