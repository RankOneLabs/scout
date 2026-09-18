"""Pure decision composition functions."""

from __future__ import annotations

from collections.abc import Callable

from scout.typesafe.models import Answers, ChoiceAnswer, DecisionRecord, ProbabilityAnswer

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
