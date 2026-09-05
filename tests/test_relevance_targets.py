"""Behavioral tests for human-label derivation, not model-label training."""

from __future__ import annotations

import pytest

from scout.grading.relevance_targets import (
    HumanRelevanceTarget,
    RelevanceTargetSource,
    TargetExclusionReason,
    derive_relevance_target,
)
from scout.result import Err, Ok


@pytest.mark.parametrize(
    ("judgment", "decision", "expected"),
    [
        ("correct", 0, False),
        ("correct", 1, True),
        ("false_positive", 1, False),
        ("false_negative", 0, True),
    ],
)
def test_human_judgment_determines_target(
    judgment: str,
    decision: int,
    expected: bool,
) -> None:
    source = RelevanceTargetSource(
        grade_id=7, evaluation_id=42, original_relevant=decision, relevance_judgment=judgment
    )
    assert derive_relevance_target(source) == Ok(HumanRelevanceTarget(7, 42, expected))


@pytest.mark.parametrize(
    ("judgment", "decision", "reason"),
    [
        ("false_positive", 0, "inconsistent_relevance_judgment"),
        ("false_negative", 1, "inconsistent_relevance_judgment"),
        ("correct", None, "invalid_original_decision"),
        ("correct", 2, "invalid_original_decision"),
        ("", 0, "unsupported_relevance_judgment"),
        ("ungraded", 0, "unsupported_relevance_judgment"),
        ("legacy", 1, "unsupported_relevance_judgment"),
    ],
)
def test_invalid_or_ungraded_example_has_explicit_exclusion(
    judgment: str,
    decision: int | None,
    reason: TargetExclusionReason,
) -> None:
    result = derive_relevance_target(
        RelevanceTargetSource(
            grade_id=7,
            evaluation_id=42,
            original_relevant=decision,
            relevance_judgment=judgment,
        )
    )
    assert isinstance(result, Err)
    assert result.error.reason == reason


def test_missing_evaluation_is_not_given_an_inferred_identity() -> None:
    result = derive_relevance_target(
        RelevanceTargetSource(
            grade_id=7,
            evaluation_id=None,
            original_relevant=1,
            relevance_judgment="correct",
        )
    )
    assert isinstance(result, Err)
    assert result.error.reason == "missing_evaluation_linkage"


def test_exclusion_preserves_source_ids_for_audit() -> None:
    result = derive_relevance_target(
        RelevanceTargetSource(
            grade_id=7,
            evaluation_id=42,
            original_relevant=1,
            relevance_judgment="false_negative",
        )
    )
    assert isinstance(result, Err)
    assert (result.error.grade_id, result.error.evaluation_id) == (7, 42)
