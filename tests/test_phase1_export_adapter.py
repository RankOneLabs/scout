"""Tests for the storage-neutral grade export -> Jig adapter boundary."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from jig import NullFeedbackLoop, ScoreSource, SQLiteFeedbackLoop

from scout.config import GradeRecord, Message, RelevanceResult
from scout.evals.phase1.export_adapter import (
    CANONICAL_FAILURE_DIMENSIONS,
    ExportedGradeRejected,
    ScoutJigProjection,
    exported_grade_to_eval_case,
    project_exported_grade,
    project_exported_grades,
)
from scout.storage.state import StateManager


def _record() -> dict[str, Any]:
    return {
        "source": {
            "platform": "discord",
            "channel": "support",
            "content": "How do I configure the gateway?",
            "parent": {"author_name": "Sam", "text": "Earlier context"},
        },
        "evaluation": {
            "evaluation_id": 42,
            "source_relevant": True,
            "posture": "answer",
            "surface_status": "surfaced",
            "project_key": "gateway",
            "dossier_revision": "a" * 40,
            "dossier_summary_id": "gateway-dossier",
        },
        "grade": {
            "relevance_judgment": "correct",
            "action_judgment": "accept",
            "dimensions": None,
            "failure_note": None,
            "factual_offending_claim": None,
            "factual_disposition": None,
            "factual_contradicting_evidence": None,
            "context_missing_input": None,
            "posture_should_have_been": None,
            "implication_implied_claim": None,
            "implication_missing_support": None,
            "graded_at": "2026-07-14T05:00:00.000Z",
            "grade_source": "cli",
        },
    }


def test_importing_state_manager_does_not_load_jig_or_eval_harness() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import scout.storage.state; "
            "assert 'jig' not in sys.modules; "
            "assert 'scout.evals.phase1.models' not in sys.modules",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_exported_grade_converts_to_phase1_eval_case() -> None:
    case = exported_grade_to_eval_case(_record())

    assert case.input.startswith("[SCOUT_EVAL_CASE_ID:export-evaluation-42]\n")
    assert "parent_author: Sam" in case.input
    assert json.loads(case.expected) == {
        "expected_posture": "answer",
        "expected_source_relevant": True,
        "expected_terminal_status": "surfaced",
        "replay_ready": True,
        "replay_unready_reason": None,
    }
    assert case.context["evaluation_id"] == 42
    assert case.metadata["grade"]["action_judgment"] == "accept"
    assert "graded_at" not in case.metadata["grade"]
    assert "grade_source" not in case.metadata["grade"]


def test_false_positive_export_needs_no_dossier_identity() -> None:
    record = _record()
    record["evaluation"].update(
        {"project_key": None, "dossier_revision": None, "dossier_summary_id": None}
    )
    record["grade"].update(
        {
            "relevance_judgment": "false_positive",
            "action_judgment": "fail",
            "dimensions": ["tone"],
            "failure_note": "This message was not relevant.",
        }
    )

    case = exported_grade_to_eval_case(record)

    assert json.loads(case.expected)["expected_terminal_status"] == "not_relevant"


def test_export_rejects_invalid_grade_contract() -> None:
    record = _record()
    record["grade"]["relevance_judgment"] = "false_positive"

    with pytest.raises(ExportedGradeRejected, match="invalid grade"):
        exported_grade_to_eval_case(record)


def test_false_negative_missing_dossier_identity_is_replay_unready_not_rejected() -> None:
    """Missing counterfactual identity has a materialization rule, not a
    rejection — only an invalid grade contract or a JSON-serialization
    failure rejects a record now."""
    record = _record()
    record["grade"].update(
        {
            "relevance_judgment": "false_negative",
            "action_judgment": "fail",
            "dimensions": ["contextual_understanding"],
            "failure_note": "should have been surfaced",
            "context_missing_input": "the original post text",
        }
    )
    record["evaluation"].update(
        {
            "project_key": None,
            "dossier_revision": None,
            "dossier_summary_id": None,
            "surface_status": "not_relevant",
        }
    )

    projection = project_exported_grade(record)

    expected = json.loads(projection.eval_case.expected)
    assert expected == {
        "expected_source_relevant": True,
        "expected_posture": None,
        "expected_terminal_status": None,
        "replay_ready": False,
        "replay_unready_reason": "missing_counterfactual_identity",
    }
    assert projection.eval_case.context["project_key"] is None
    assert projection.eval_case.context["dossier_revision"] is None
    assert projection.eval_case.context["dossier_summary_id"] is None

    meta = projection.result_metadata
    assert meta["observed_outcome"] == "not_relevant"
    assert meta["expected_outcome"] is None
    assert meta["expected_source_relevant"] is True
    assert meta["expected_posture"] is None
    assert meta["expected_terminal_status"] is None
    assert meta["project_key"] is None
    assert meta["dossier_revision"] is None
    assert meta["dossier_summary_id"] is None
    assert meta["replay_ready"] is False
    assert meta["replay_unready_reason"] == "missing_counterfactual_identity"


def test_false_negative_with_complete_identity_is_replay_ready() -> None:
    record = _record()
    record["evaluation"]["surface_status"] = "not_relevant"
    record["grade"].update(
        {
            "relevance_judgment": "false_negative",
            "action_judgment": "fail",
            "dimensions": ["contextual_understanding"],
            "failure_note": "should have been surfaced",
            "context_missing_input": "the original post text",
        }
    )
    # evaluation already carries project_key/dossier_revision/dossier_summary_id
    # and posture from _record(), so identity is complete even though the
    # original call was not_relevant.

    projection = project_exported_grade(record)

    expected = json.loads(projection.eval_case.expected)
    assert expected["replay_ready"] is True
    assert expected["replay_unready_reason"] is None
    assert expected["expected_terminal_status"] == "surfaced"
    assert projection.result_metadata["replay_ready"] is True
    assert projection.result_metadata["observed_outcome"] == "not_relevant"


# ---------------------------------------------------------------------------
# ScoutJigProjection / project_exported_grade / project_exported_grades
# ---------------------------------------------------------------------------


def test_project_exported_grade_returns_scout_jig_projection() -> None:
    assert isinstance(project_exported_grade(_record()), ScoutJigProjection)


def test_exported_grade_to_eval_case_matches_projection_eval_case() -> None:
    record = _record()
    assert exported_grade_to_eval_case(record) == project_exported_grade(record).eval_case


def test_projection_emits_exactly_seven_canonical_dimension_scores() -> None:
    projection = project_exported_grade(_record())
    assert len(projection.scores) == 7
    assert {s.dimension for s in projection.scores} == set(CANONICAL_FAILURE_DIMENSIONS)
    assert [s.dimension for s in projection.scores] == list(CANONICAL_FAILURE_DIMENSIONS)


def test_projection_scores_are_all_human_source() -> None:
    projection = project_exported_grade(_record())
    assert all(s.source == ScoreSource.HUMAN for s in projection.scores)


def test_no_failed_dimensions_scores_all_one() -> None:
    projection = project_exported_grade(_record())
    assert all(s.value == 1.0 for s in projection.scores)


def test_failed_dimensions_score_zero_others_score_one() -> None:
    record = _record()
    record["evaluation"].update(
        {"project_key": None, "dossier_revision": None, "dossier_summary_id": None}
    )
    record["grade"].update(
        {
            "relevance_judgment": "false_positive",
            "action_judgment": "fail",
            "dimensions": ["tone", "factual_support"],
            "failure_note": "not relevant",
            "factual_offending_claim": "the gateway is free",
            "factual_disposition": "unsupported",
        }
    )
    projection = project_exported_grade(record)
    by_dim = {s.dimension: s.value for s in projection.scores}
    assert by_dim["tone"] == 0.0
    assert by_dim["factual_support"] == 0.0
    assert by_dim["contextual_understanding"] == 1.0
    assert by_dim["unsupported_implication"] == 1.0
    assert by_dim["posture"] == 1.0
    assert by_dim["usefulness"] == 1.0


def test_common_causal_metadata_present_on_every_score() -> None:
    record = _record()
    record["source"]["post_id"] = "post-99"
    record["evaluation"]["scan_id"] = 7
    projection = project_exported_grade(record)

    for score in projection.scores:
        meta = score.metadata
        assert meta is not None
        assert meta["case_id"] == "export-evaluation-42"
        assert meta["evaluation_id"] == 42
        assert meta["post_id"] == "post-99"
        assert meta["scan_id"] == 7
        assert meta["graded_at"] == "2026-07-14T05:00:00.000Z"
        assert meta["grade_source"] == "cli"
        assert meta["relevance_judgment"] == "correct"
        assert meta["action_judgment"] == "accept"
        assert meta["project_key"] == "gateway"
        assert meta["dossier_revision"] == "a" * 40
        assert meta["dossier_summary_id"] == "gateway-dossier"


def test_factual_support_metadata_carries_dimension_specific_fields() -> None:
    record = _record()
    record["grade"].update(
        {
            "relevance_judgment": "false_positive",
            "action_judgment": "fail",
            "dimensions": ["factual_support"],
            "failure_note": "unsupported claim",
            "factual_offending_claim": "the gateway is free",
            "factual_disposition": "unsupported",
        }
    )
    projection = project_exported_grade(record)
    by_dim = {s.dimension: s for s in projection.scores}

    factual_meta = by_dim["factual_support"].metadata
    assert factual_meta["offending_claim"] == "the gateway is free"
    assert factual_meta["disposition"] == "unsupported"
    assert factual_meta["contradicting_evidence"] is None

    # tone/usefulness carry no dimension-specific keys beyond the common set.
    tone_meta = by_dim["tone"].metadata
    assert "offending_claim" not in tone_meta
    assert "missing_input" not in tone_meta
    assert "observed_posture" not in tone_meta
    assert "implied_claim" not in tone_meta


def test_posture_metadata_carries_observed_and_should_have_been() -> None:
    record = _record()
    record["evaluation"]["posture"] = "answer"
    record["grade"].update(
        {
            "relevance_judgment": "false_positive",
            "action_judgment": "fail",
            "dimensions": ["posture"],
            "failure_note": "wrong posture",
            "posture_should_have_been": "abstain",
        }
    )
    projection = project_exported_grade(record)
    posture_meta = {s.dimension: s.metadata for s in projection.scores}["posture"]
    assert posture_meta["observed_posture"] == "answer"
    assert posture_meta["should_have_been"] == "abstain"


def test_unsupported_implication_metadata_carries_implied_claim_and_missing_support() -> None:
    record = _record()
    record["grade"].update(
        {
            "relevance_judgment": "false_positive",
            "action_judgment": "fail",
            "dimensions": ["unsupported_implication"],
            "failure_note": "unsupported implication",
            "implication_implied_claim": "implies X",
            "implication_missing_support": "no evidence for X",
        }
    )
    projection = project_exported_grade(record)
    meta = {s.dimension: s.metadata for s in projection.scores}["unsupported_implication"]
    assert meta["implied_claim"] == "implies X"
    assert meta["missing_support"] == "no evidence for X"


def test_contextual_understanding_metadata_carries_missing_input() -> None:
    record = _record()
    record["grade"].update(
        {
            "relevance_judgment": "false_positive",
            "action_judgment": "fail",
            "dimensions": ["contextual_understanding"],
            "failure_note": "missing context",
            "context_missing_input": "the original post text",
        }
    )
    projection = project_exported_grade(record)
    meta = {s.dimension: s.metadata for s in projection.scores}["contextual_understanding"]
    assert meta["missing_input"] == "the original post text"


def test_project_exported_grades_batch_preserves_order() -> None:
    record_a = _record()
    record_b = _record()
    record_b["evaluation"]["evaluation_id"] = 43

    projections = project_exported_grades([record_a, record_b])
    assert [p.eval_case.context["evaluation_id"] for p in projections] == [42, 43]


def test_project_exported_grades_propagates_rejection() -> None:
    bad_record = _record()
    bad_record["evaluation"]["evaluation_id"] = None

    with pytest.raises(ExportedGradeRejected):
        project_exported_grades([_record(), bad_record])


def test_scout_jig_projection_is_frozen() -> None:
    projection = project_exported_grade(_record())
    with pytest.raises(FrozenInstanceError):
        projection.eval_case = None  # type: ignore[misc]


def test_scout_jig_projection_scores_is_a_tuple() -> None:
    projection = project_exported_grade(_record())
    assert isinstance(projection.scores, tuple)


# ---------------------------------------------------------------------------
# result_content / result_metadata
# ---------------------------------------------------------------------------


def test_result_content_uses_draft_when_present() -> None:
    record = _record()
    record["evaluation"]["draft"] = "Here's a suggestion for the gateway config."

    projection = project_exported_grade(record)
    assert projection.result_content == "Here's a suggestion for the gateway config."


def test_result_content_preserves_empty_string_draft_verbatim() -> None:
    """An empty-string draft is still a draft Scout actually produced — it
    must not be treated the same as no draft at all."""
    record = _record()
    record["evaluation"]["draft"] = ""

    projection = project_exported_grade(record)
    assert projection.result_content == ""


def test_result_content_is_canonical_terminal_payload_without_draft() -> None:
    record = _record()
    record["evaluation"]["draft"] = None

    projection = project_exported_grade(record)
    assert projection.result_content == json.dumps(
        {"draft": None, "outcome": "surfaced", "terminal": True},
        sort_keys=True,
        separators=(",", ":"),
    )
    # Never substitute EvalCase.expected for actual result content.
    assert projection.result_content != projection.eval_case.expected


def test_result_metadata_carries_projection_version_and_identity() -> None:
    record = _record()
    record["source"]["post_id"] = "post-99"
    record["evaluation"]["scan_id"] = 7

    projection = project_exported_grade(record)
    meta = projection.result_metadata
    assert meta["projection_version"] == "scout-finalized-grades-v1"
    assert meta["case_id"] == "export-evaluation-42"
    assert meta["evaluation_id"] == 42
    assert meta["post_id"] == "post-99"
    assert meta["scan_id"] == 7
    assert meta["project_key"] == "gateway"
    assert meta["dossier_revision"] == "a" * 40
    assert meta["dossier_summary_id"] == "gateway-dossier"
    assert meta["grade"] == projection.eval_case.metadata["grade"]


def test_result_metadata_observed_and_expected_outcome_on_complete_path() -> None:
    projection = project_exported_grade(_record())
    meta = projection.result_metadata
    assert meta["observed_outcome"] == "surfaced"
    assert meta["expected_outcome"] == "surfaced"
    assert meta["expected_outcome"] == meta["expected_terminal_status"]
    assert meta["replay_ready"] is True
    assert meta["replay_unready_reason"] is None


# ---------------------------------------------------------------------------
# Read-only guard: the projection never writes to Jig, regardless of what
# real Jig storage sits beside a populated Scout StateManager.
# ---------------------------------------------------------------------------


def _seed_graded_evaluation(state: StateManager) -> None:
    scan_id = state.start_scan()
    msg = Message(
        platform="discord",
        platform_id="projection-guard-1",
        channel_name="general",
        channel_id="ch-1",
        author_name="alice",
        author_id="u1",
        content="how do I configure the gateway?",
        created_at=datetime.now(UTC),
    )
    post_id = state.save_post(msg, scan_id)
    result = RelevanceResult(
        message=msg, relevant=True, score=0.8,
        reason="matches project", relevant_to=("gateway",),
    )
    evaluation_id = state.save_evaluation(result, post_id, scan_id)
    state.complete_scan(scan_id, 1, 1)

    # false_positive keeps the projection on the not_relevant path, which
    # needs no dossier identity — this fixture only cares about grade
    # export + projection plumbing, not dossier resolution.
    state.save_grade(GradeRecord(
        post_id=post_id,
        evaluation_id=evaluation_id,
        scan_id=scan_id,
        source="cli",
        graded_at=datetime.now(UTC),
        relevance_judgment="false_positive",
        action_judgment="fail",
        dimensions=["tone"],
        failure_note="not actually relevant",
    ))


@pytest.mark.asyncio
async def test_projection_never_writes_to_a_real_jig_feedback_loop(
    in_memory_state: StateManager, tmp_path: Path
) -> None:
    """A real Jig SQLiteFeedbackLoop sitting beside a populated Scout
    StateManager must stay completely empty after running the projection —
    project_exported_grade[s] accepts mappings and constructs jig objects
    only; it never receives a FeedbackLoop and never calls store_result/score.
    """
    _seed_graded_evaluation(in_memory_state)
    records = in_memory_state.export_eval_cases()
    assert records, "expected at least one finalized grade export record"

    feedback = SQLiteFeedbackLoop(db_path=str(tmp_path / "feedback.db"))
    try:
        projections = project_exported_grades(records)
        assert len(projections) == len(records)
        assert all(len(p.scores) == 7 for p in projections)

        db = await feedback._get_db()  # noqa: SLF001 - integration contract probe
        result_count = await (await db.execute("SELECT COUNT(*) FROM results")).fetchone()
        score_count = await (await db.execute("SELECT COUNT(*) FROM scores")).fetchone()
        assert result_count == (0,)
        assert score_count == (0,)
    finally:
        await feedback.close()


async def test_null_feedback_loop_creates_no_database_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NullFeedbackLoop must never touch disk, even when driven through the
    same store_result/score calls a real FeedbackLoop would receive."""
    monkeypatch.chdir(tmp_path)
    loop = NullFeedbackLoop()
    result_id = await loop.store_result("content", "input")
    await loop.score(result_id, [])

    assert list(tmp_path.iterdir()) == []
