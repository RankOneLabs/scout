"""Derive human relevance targets from Scout's existing grade/evaluation join.

This transform maps judgments, not population eligibility. Callers must first
validate the grade with Scout's shared grading contract and preserve exclusions,
revision identity, and recorded context when assembling a corpus.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from scout.grading.feedback import GradePopulationRow
from scout.result import Err, Ok, Result


@dataclass(frozen=True, slots=True)
class RelevanceTargetSource:
    """Projection of GradePopulationRow's authoritative grade/evaluation fields.

    Source IDs retain their existing integer representation. A rejected model
    decision alone is deliberately insufficient: a human judgment is required.
    """

    grade_id: int
    evaluation_id: int | None
    original_relevant: int | None
    relevance_judgment: str


TargetExclusionReason = Literal[
    "missing_evaluation_linkage",
    "invalid_original_decision",
    "unsupported_relevance_judgment",
    "inconsistent_relevance_judgment",
]


@dataclass(frozen=True, slots=True)
class TargetExclusion:
    operation: Literal["derive_relevance_target"]
    grade_id: int
    evaluation_id: int | None
    reason: TargetExclusionReason


@dataclass(frozen=True, slots=True)
class HumanRelevanceTarget:
    grade_id: int
    evaluation_id: int
    is_relevant: bool


def project_relevance_target_source(row: GradePopulationRow) -> RelevanceTargetSource:
    return RelevanceTargetSource(
        grade_id=row.grade_id,
        evaluation_id=row.evaluation_id,
        original_relevant=row.evaluation_relevant,
        relevance_judgment=row.relevance_judgment,
    )


def derive_relevance_target(
    source: RelevanceTargetSource,
) -> Result[HumanRelevanceTarget, TargetExclusion]:
    """Correct retains the model decision; FP/FN invert only consistent decisions."""
    if source.evaluation_id is None:
        return _exclude(source, "missing_evaluation_linkage")
    if source.original_relevant not in (0, 1):
        return _exclude(source, "invalid_original_decision")
    if source.relevance_judgment == "correct":
        is_relevant = bool(source.original_relevant)
    elif source.relevance_judgment == "false_positive":
        if source.original_relevant != 1:
            return _exclude(source, "inconsistent_relevance_judgment")
        is_relevant = False
    elif source.relevance_judgment == "false_negative":
        if source.original_relevant != 0:
            return _exclude(source, "inconsistent_relevance_judgment")
        is_relevant = True
    else:
        return _exclude(source, "unsupported_relevance_judgment")
    return Ok(
        HumanRelevanceTarget(
            grade_id=source.grade_id,
            evaluation_id=source.evaluation_id,
            is_relevant=is_relevant,
        )
    )


def _exclude(
    source: RelevanceTargetSource,
    reason: TargetExclusionReason,
) -> Err[TargetExclusion]:
    return Err(
        TargetExclusion(
            operation="derive_relevance_target",
            grade_id=source.grade_id,
            evaluation_id=source.evaluation_id,
            reason=reason,
        )
    )
