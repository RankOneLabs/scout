"""Pure decision composition functions."""

from __future__ import annotations

from collections.abc import Callable

from scout.typesafe.models import Answers, ChoiceAnswer, DecisionRecord, ProbabilityAnswer


def derive_band(confidence: float) -> str:
    if confidence >= 0.8:
        return "high"
    if confidence >= 0.5:
        return "medium"
    return "low"


def _probability(answers: Answers) -> tuple[str, ProbabilityAnswer]:
    for question_id, answer in answers.answers.items():
        if isinstance(answer, ProbabilityAnswer):
            return question_id, answer
    raise ValueError("gate_v1 requires a probability answer")


def account_annotation(answers: Answers) -> DecisionRecord:
    for question_id, answer in answers.answers.items():
        if isinstance(answer, ChoiceAnswer):
            label, probability = max(answer.probabilities.items(), key=lambda item: item[1])
            return DecisionRecord(
                eligible=True,
                p_eligible=1.0,
                uncertain=answer.confidence < 0.5,
                reason="account annotation pass-through",
                account_label=label,
                account_confidence=answer.confidence,
                details={"question_id": question_id, "label_probability": probability},
            )
    raise ValueError("account_annotation requires a choice answer")


def gate_v1(answers: Answers) -> DecisionRecord:
    question_id, answer = _probability(answers)
    annotation: ChoiceAnswer | None = next(
        (value for value in answers.answers.values() if isinstance(value, ChoiceAnswer)), None
    )
    label: str | None = None
    account_confidence: float | None = None
    if annotation is not None and annotation.probabilities:
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
        details={"question_id": question_id, "confidence_band": derive_band(answer.confidence)},
    )


Decide = Callable[[Answers], DecisionRecord]
DECIDE_REGISTRY: dict[str, Decide] = {
    "gate_v1": gate_v1,
    "account_annotation": account_annotation,
}
