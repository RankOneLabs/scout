"""Pure decision composition functions."""

from __future__ import annotations

import math
from collections.abc import Callable

from scout.typesafe.fitting import extract_features
from scout.typesafe.models import Answers, ChoiceAnswer, DecisionRecord, ProbabilityAnswer
from scout.typesafe.weights import WeightSet

RELEVANCE_QUESTION_ID = "relevance"
ACCOUNT_TYPE_QUESTION_ID = "account_type"


def derive_band(confidence: float) -> str:
    if confidence >= 0.8:
        return "high"
    if confidence >= 0.5:
        return "medium"
    return "low"


def _probability(answers: Answers) -> ProbabilityAnswer:
    answer = answers.answers.get(RELEVANCE_QUESTION_ID)
    if not isinstance(answer, ProbabilityAnswer):
        raise ValueError("gate_v1 requires a 'relevance' probability answer")
    return answer


def _account_type(answers: Answers) -> ChoiceAnswer:
    answer = answers.answers.get(ACCOUNT_TYPE_QUESTION_ID)
    if not isinstance(answer, ChoiceAnswer):
        raise ValueError("account annotation requires an 'account_type' choice answer")
    return answer


def account_annotation(answers: Answers) -> DecisionRecord:
    answer = _account_type(answers)
    label, probability = max(answer.probabilities.items(), key=lambda item: item[1])
    return DecisionRecord(
        eligible=True,
        p_eligible=1.0,
        uncertain=answer.confidence < 0.5,
        reason="account annotation pass-through",
        account_label=label,
        account_confidence=answer.confidence,
        details={"question_id": ACCOUNT_TYPE_QUESTION_ID, "label_probability": probability},
    )


def gate_v1(answers: Answers) -> DecisionRecord:
    answer = _probability(answers)
    annotation = answers.answers.get(ACCOUNT_TYPE_QUESTION_ID)
    label: str | None = None
    account_confidence: float | None = None
    if isinstance(annotation, ChoiceAnswer):
        label = max(annotation.probabilities.items(), key=lambda item: item[1])[0]
        account_confidence = annotation.confidence
    uncertain = answer.confidence < 0.5 or 0.4 < answer.probability < 0.6
    return DecisionRecord(
        eligible=answer.probability >= 0.5,
        p_eligible=answer.probability,
        uncertain=uncertain,
        reason=(
            "eligible probability meets threshold"
            if answer.probability >= 0.5
            else "eligible probability below threshold"
        ),
        account_label=label,
        account_confidence=account_confidence,
        details={
            "question_id": RELEVANCE_QUESTION_ID,
            "confidence_band": derive_band(answer.confidence),
        },
    )


Decide = Callable[[Answers], DecisionRecord]
DECIDE_REGISTRY: dict[str, Decide] = {
    "gate_v1": gate_v1,
    "account_annotation": account_annotation,
}


def apply_weight_set(answers: Answers, weight_set: WeightSet) -> DecisionRecord:
    """Apply portable weights as a pure transform; fitting is never imported here."""
    features = extract_features(answers)
    logit = weight_set.bias + sum(
        coefficient * features.get(key, 0.0)
        for key, coefficient in weight_set.weights.items()
    )
    probability = 1.0 / (1.0 + math.exp(-logit))
    eligible = probability >= weight_set.threshold
    return DecisionRecord(
        eligible=eligible,
        p_eligible=probability,
        uncertain=(
            weight_set.uncertain_band.lower
            <= probability
            <= weight_set.uncertain_band.upper
        ),
        reason=(
            "fitted probability meets cost threshold"
            if eligible
            else "fitted probability below cost threshold"
        ),
        details={
            "weight_set_version": weight_set.weight_set_version,
            "threshold": weight_set.threshold,
        },
    )


def register_fitted_gate(weight_set: WeightSet) -> str:
    """Register one explicit fitted gate for tests and future promotion only."""
    name = f"fitted_gate/{weight_set.weight_set_version}"

    def decide(answers: Answers) -> DecisionRecord:
        return apply_weight_set(answers, weight_set)

    DECIDE_REGISTRY[name] = decide
    return name
