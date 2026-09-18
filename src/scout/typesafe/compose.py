"""Pure decision composition functions."""

from __future__ import annotations

from collections.abc import Callable

from scout.typesafe.models import (
    Answers,
    ChoiceAnswer,
    DecisionRecord,
    ProbabilityAnswer,
    ScoreAnswer,
)

RELEVANCE_QUESTION_ID = "relevance"
ACCOUNT_TYPE_QUESTION_ID = "account_type"
SUBSTANCE_QUESTION_IDS = (
    "operational_claim",
    "reasoned_practice",
    "operational_question",
)


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


def _required_probability(answers: Answers, question_id: str) -> float:
    answer = answers.answers.get(question_id)
    if not isinstance(answer, ProbabilityAnswer):
        raise ValueError(f"missing probability answer for {question_id}")
    return answer.probability


def _required_choice(answers: Answers, question_id: str) -> ChoiceAnswer:
    answer = answers.answers.get(question_id)
    if not isinstance(answer, ChoiceAnswer):
        raise ValueError(f"missing choice answer for {question_id}")
    return answer


def _required_score(answers: Answers, question_id: str) -> ScoreAnswer:
    answer = answers.answers.get(question_id)
    if not isinstance(answer, ScoreAnswer):
        raise ValueError(f"missing score answer for {question_id}")
    return answer


def derive_agent_ops_band(answers: Answers, threshold: float = 0.5) -> str:
    exclusion = _required_choice(answers, "exclusion")
    if 1.0 - exclusion.probabilities.get("none", 0.0) >= threshold:
        return "out_of_scope"
    if (
        max(_required_probability(answers, name) for name in SUBSTANCE_QUESTION_IDS) >= (threshold)
        and _required_probability(answers, "point_in_own_text") >= threshold
    ):
        return "substantive"
    if _required_probability(answers, "on_topic_pointer") >= threshold:
        return "pointer"
    if _required_probability(answers, "general_building") >= threshold:
        return "building"
    return "out_of_scope"


def agent_ops_relevance_v1(answers: Answers) -> DecisionRecord:
    band = _required_score(answers, "band")
    band_probabilities = {level.level: level.probability for level in band.levels}
    p_substantive = band_probabilities.get("3", band_probabilities.get("substantive", 0.0))
    exclusion = _required_choice(answers, "exclusion")
    p_exclusion = 1.0 - exclusion.probabilities.get("none", 0.0)
    exclusion_label = max(exclusion.probabilities, key=exclusion.probabilities.__getitem__)
    account = _required_choice(answers, ACCOUNT_TYPE_QUESTION_ID)
    account_label = max(account.probabilities, key=account.probabilities.__getitem__)
    return DecisionRecord(
        eligible=p_substantive >= 0.5 and p_exclusion < 0.5,
        p_eligible=p_substantive,
        uncertain=False,
        reason=(
            f"hard exclusion: {exclusion_label} ({p_exclusion:.3f})"
            if p_exclusion >= 0.5
            else f"band substantive probability {p_substantive:.3f}"
        ),
        account_label=account_label,
        account_confidence=account.confidence,
        details={
            "band_probabilities": band_probabilities,
            "exclusion": exclusion_label,
            "exclusion_probability": p_exclusion,
            "derived_band": derive_agent_ops_band(answers),
        },
    )


Decide = Callable[[Answers], DecisionRecord]
DECIDE_REGISTRY: dict[str, Decide] = {
    "gate_v1": gate_v1,
    "account_annotation": account_annotation,
    "agent_ops_relevance/v1": agent_ops_relevance_v1,
}
