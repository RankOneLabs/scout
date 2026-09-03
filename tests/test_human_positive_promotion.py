from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scout.config import GradeRecord, Message, RelevanceResult
from scout.storage.state import HumanPositivePromotionInProgressError, StateManager


def _source(state: StateManager) -> tuple[int, int, int, Message]:
    scan_id = state.start_scan(run_kind="live")
    message = Message(
        platform="discord",
        platform_id="human-positive-1",
        channel_name="general",
        channel_id="channel-1",
        author_name="alice",
        author_id="alice-1",
        content="This should receive a response",
        created_at=datetime.now(UTC),
    )
    post_id = state.save_post(message, scan_id)
    source_id = state.save_evaluation(
        RelevanceResult(
            message=message,
            relevant=False,
            score=0.1,
            reason="model skipped it",
            relevant_to=(),
        ),
        post_id,
        scan_id,
    )
    return scan_id, post_id, source_id, message


def _grade(scan_id: int, post_id: int, source_id: int) -> GradeRecord:
    return GradeRecord(
        post_id=post_id,
        evaluation_id=source_id,
        scan_id=scan_id,
        source="web",
        graded_at=datetime.now(UTC),
        relevance_judgment="false_negative",
        action_judgment="fail",
        dimensions=["usefulness"],
        failure_note="Scout should have surfaced this",
    )


def test_promotion_claim_is_durable_retryable_and_idempotent(tmp_path) -> None:
    with StateManager(db_path=str(tmp_path / "scout.db")) as state:
        source_scan_id, post_id, source_id, message = _source(state)
        grade = _grade(source_scan_id, post_id, source_id)

        claim = state.begin_human_positive_promotion(grade)
        assert claim["status"] == "running"
        assert state.get_grade_for_evaluation(source_id) is not None
        with pytest.raises(HumanPositivePromotionInProgressError):
            state.begin_human_positive_promotion(grade)

        state.fail_human_positive_promotion(source_id, error_detail="model unavailable")
        assert state.get_human_positive_promotion(source_id)["status"] == "failed"

        retry = state.begin_human_positive_promotion(grade)
        assert retry["status"] == "running"
        target_scan_id = state.start_scan(run_kind="human_positive")
        state.attach_human_positive_promotion_scan(source_id, target_scan_id)
        target_id = state.save_evaluation(
            RelevanceResult(
                message=message,
                relevant=True,
                score=1.0,
                reason="human override",
                relevant_to=("gateway",),
            ),
            post_id,
            target_scan_id,
            project_key="gateway",
            surface_status="surfaced",
        )
        with state.db.begin_immediate():
            state.complete_human_positive_promotion(
                source_id,
                scan_id=target_scan_id,
                target_evaluation_id=target_id,
            )

        completed = state.begin_human_positive_promotion(grade)
        assert completed["status"] == "completed"
        assert completed["target_evaluation_id"] == target_id
        assert completed["source_evaluation_id"] != completed["target_evaluation_id"]


def test_response_only_phase_sequence_can_own_promoted_draft(tmp_path) -> None:
    with StateManager(db_path=str(tmp_path / "scout.db")) as state:
        _source_scan_id, post_id, _source_id, message = _source(state)
        target_scan_id = state.start_scan(run_kind="human_positive")
        snapshot = state.record_feedback_snapshot(target_scan_id, mode="shadow")
        phase_ids = {phase.phase: phase.snapshot_phase_id for phase in snapshot.phases}
        reply_run_id = state.insert_phase_run(
            scan_id=target_scan_id,
            post_id=post_id,
            snapshot_phase_id=phase_ids["reply_draft"],
            phase="reply_draft",
            trace_id="trace-human-reply",
            model="test-model",
            status="complete",
        )
        critic_run_id = state.insert_phase_run(
            scan_id=target_scan_id,
            post_id=post_id,
            snapshot_phase_id=phase_ids["critic"],
            phase="critic",
            trace_id="trace-human-critic",
            model="test-model",
            status="complete",
        )

        evaluation_id, draft_id, _event_id = state.persist_surfaced_outcome(
            RelevanceResult(
                message=message,
                relevant=True,
                score=1.0,
                reason="human override",
                relevant_to=("gateway",),
            ),
            post_id,
            target_scan_id,
            project_key="gateway",
            author_id=message.author_id,
            platform=message.platform,
            comment_text="Generated response",
            structured_output="{}",
            contributor_phase_run_ids=(reply_run_id, critic_run_id),
            allow_response_only_phase_runs=True,
        )

        assert state.get_phase_run(reply_run_id)["evaluation_id"] == evaluation_id
        assert state.get_phase_run(critic_run_id)["evaluation_id"] == evaluation_id
        assert state.conn.execute(
            "SELECT comment_text FROM draft_comments WHERE id = ?", (draft_id,)
        ).fetchone()["comment_text"] == "Generated response"
