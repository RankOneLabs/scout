"""Regression coverage for UnitOfWork: the shared-connection transaction
boundary that keeps a write spanning more than one aggregate store atomic.

ScanStore, PostStore, EvaluationStore, GradeStore, and RegistryStore are
all constructed with the same UnitOfWork wrapping the one Db connection
StateManager owns (unit_of_work.py) — no store opens an independent
connection. The sole production case that composes writes across two of
these stores in a single transaction is
negative_case_promotion.py::promote_negative_case, which opens
``state.db.begin_immediate()`` once and, inside it, calls
``persist_terminal_outcome``/``persist_surfaced_outcome`` (an
EvaluationStore write, via the StateManager facade) followed by
``state.complete_human_positive_promotion`` (a GradeStore write). These
tests reproduce that exact shape directly against StateManager's public
surface — the same one external callers use — to prove the composed unit
really is atomic on the one shared connection: an injected failure after
both stores have written rolls every row back together, and a clean exit
commits every row together. See
docs/transactions-and-scan-durability.md for the full contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from scout.config import GradeRecord, Message, RelevanceResult
from scout.storage.state import StateManager


def _seed_source_evaluation(state: StateManager) -> tuple[int, int, int]:
    scan_id = state.start_scan(run_kind="live")
    message = Message(
        platform="discord",
        platform_id="uow-source-1",
        channel_name="general",
        channel_id="channel-1",
        author_name="alice",
        author_id="alice-1",
        content="This should receive a response",
        created_at=datetime.now(UTC),
    )
    post_id = state.save_post(message, scan_id)
    source_evaluation_id = state.save_evaluation(
        RelevanceResult(
            message=message, relevant=False, score=0.1,
            reason="model skipped it", relevant_to=(),
        ),
        post_id,
        scan_id,
    )
    return scan_id, post_id, source_evaluation_id


def _claim_promotion(
    state: StateManager, scan_id: int, post_id: int, source_evaluation_id: int
) -> dict[str, Any]:
    grade = GradeRecord(
        post_id=post_id,
        evaluation_id=source_evaluation_id,
        scan_id=scan_id,
        source="web",
        graded_at=datetime.now(UTC),
        relevance_judgment="false_negative",
        action_judgment="fail",
        dimensions=["usefulness"],
        failure_note="Scout should have surfaced this",
    )
    return state.begin_human_positive_promotion(grade)


def test_composed_evaluation_and_grade_write_commits_together(tmp_path) -> None:
    """The negative_case_promotion.py shape: an EvaluationStore write
    (save_evaluation for the target outcome) composed with a GradeStore
    write (complete_human_positive_promotion) inside one
    state.db.begin_immediate() block. Both must land durably together."""
    with StateManager(db_path=str(tmp_path / "scout.db")) as state:
        scan_id, post_id, source_evaluation_id = _seed_source_evaluation(state)
        _claim_promotion(state, scan_id, post_id, source_evaluation_id)

        target_scan_id = state.start_scan(run_kind="human_positive")
        state.attach_human_positive_promotion_scan(source_evaluation_id, target_scan_id)
        message = Message(
            platform="discord", platform_id="uow-source-1", channel_name="general",
            channel_id="channel-1", author_name="alice", author_id="alice-1",
            content="This should receive a response", created_at=datetime.now(UTC),
        )

        with state.db.begin_immediate():
            target_evaluation_id = state.save_evaluation(
                RelevanceResult(
                    message=message, relevant=True, score=1.0,
                    reason="human override", relevant_to=("gateway",),
                ),
                post_id,
                target_scan_id,
                project_key="gateway",
                surface_status="surfaced",
            )
            state.complete_human_positive_promotion(
                source_evaluation_id,
                scan_id=target_scan_id,
                target_evaluation_id=target_evaluation_id,
            )

        assert state.get_evaluation(target_evaluation_id) is not None
        promotion = state.get_human_positive_promotion(source_evaluation_id)
        assert promotion is not None
        assert promotion["status"] == "completed"
        assert promotion["target_evaluation_id"] == target_evaluation_id


def test_composed_evaluation_and_grade_write_rolls_back_together_on_failure(tmp_path) -> None:
    """Same composed unit as above, but the caller's transaction fails
    after both stores have written. Since EvaluationStore and GradeStore
    share one UnitOfWork/Db connection, the EvaluationStore's evaluation
    insert and the GradeStore's promotion-completion update must both roll
    back — neither aggregate's write may survive on its own."""
    with StateManager(db_path=str(tmp_path / "scout.db")) as state:
        scan_id, post_id, source_evaluation_id = _seed_source_evaluation(state)
        _claim_promotion(state, scan_id, post_id, source_evaluation_id)

        target_scan_id = state.start_scan(run_kind="human_positive")
        state.attach_human_positive_promotion_scan(source_evaluation_id, target_scan_id)
        message = Message(
            platform="discord", platform_id="uow-source-1", channel_name="general",
            channel_id="channel-1", author_name="alice", author_id="alice-1",
            content="This should receive a response", created_at=datetime.now(UTC),
        )

        evaluations_before = state.conn.execute(
            "SELECT COUNT(*) FROM evaluations"
        ).fetchone()[0]

        with (
            pytest.raises(RuntimeError, match="injected cross-aggregate failure"),
            state.db.begin_immediate(),
        ):
            target_evaluation_id = state.save_evaluation(
                RelevanceResult(
                    message=message, relevant=True, score=1.0,
                    reason="human override", relevant_to=("gateway",),
                ),
                post_id,
                target_scan_id,
                project_key="gateway",
                surface_status="surfaced",
            )
            state.complete_human_positive_promotion(
                source_evaluation_id,
                scan_id=target_scan_id,
                target_evaluation_id=target_evaluation_id,
            )
            raise RuntimeError("injected cross-aggregate failure")

        # EvaluationStore's write rolled back: no new evaluation row.
        evaluations_after = state.conn.execute(
            "SELECT COUNT(*) FROM evaluations"
        ).fetchone()[0]
        assert evaluations_after == evaluations_before

        # GradeStore's write rolled back too: the promotion is still
        # "running", not "completed" — proof the two stores' writes
        # shared one atomic unit rather than each committing on its own.
        promotion = state.get_human_positive_promotion(source_evaluation_id)
        assert promotion is not None
        assert promotion["status"] == "running"
        assert promotion["target_evaluation_id"] is None
