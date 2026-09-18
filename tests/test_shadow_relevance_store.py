import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from scout.config import Account, GradeRecord, Message, RelevanceResult
from scout.storage.shadow_relevance import ShadowRelevanceStore, ShadowRunWrite
from scout.storage.state import StateManager


def _parents(state: StateManager) -> tuple[int, int, Message]:
    scan_id = state.start_scan()
    msg = Message(
        "bluesky",
        "shadow-post",
        "feed",
        "feed",
        Account("bluesky", "author", "Author", "author.test"),
        "text",
        datetime.now(UTC),
    )
    return scan_id, state.save_post(msg, scan_id), msg


def test_record_is_idempotent_and_backfills_evaluation() -> None:
    with StateManager(":memory:") as state:
        scan_id, post_id, msg = _parents(state)
        write = ShadowRunWrite(
            scan_id=scan_id,
            post_id=post_id,
            backend="placeholder",
            model="fixture",
            catalogue_id="catalogue",
            catalogue_version="a" * 64,
            request_id="request-1",
            state={"post": {"id": "shadow-post"}},
            status="ok",
            answers={"answers": {}},
            decision={"eligible": True},
            eligible=True,
            p_eligible=0.9,
            uncertain=False,
        )
        first = state.shadow_relevance.record_shadow_run(write)
        second = state.shadow_relevance.record_shadow_run(write)
        assert first.id == second.id
        assert len(state.shadow_relevance.list_runs_for_scan(scan_id)) == 1

        evaluation_id = state.save_evaluation(
            RelevanceResult(msg, True, 0.9, "fixture"), post_id, scan_id
        )
        assert state.shadow_relevance.backfill_evaluation_id(first.id, evaluation_id)
        assert not state.shadow_relevance.backfill_evaluation_id(first.id, evaluation_id)
        since = state.shadow_relevance.list_runs_since(first.created_at)
        assert since[0].evaluation_id == evaluation_id


def test_record_rejects_conflicting_idempotency_key() -> None:
    with StateManager(":memory:") as state:
        scan_id, post_id, _ = _parents(state)
        write = ShadowRunWrite(
            scan_id=scan_id,
            post_id=post_id,
            backend="placeholder",
            model="fixture",
            catalogue_id="catalogue",
            catalogue_version="a" * 64,
            request_id="request-1",
            state={"post": {"id": "shadow-post"}},
            status="ok",
        )
        state.shadow_relevance.record_shadow_run(write)

        with pytest.raises(sqlite3.IntegrityError, match="conflicting shadow relevance"):
            state.shadow_relevance.record_shadow_run(
                replace(write, state={"post": {"id": "different-post"}})
            )


def test_backfill_requires_matching_evaluation_identity() -> None:
    with StateManager(":memory:") as state:
        scan_id, post_id, message = _parents(state)
        run = state.shadow_relevance.record_shadow_run(
            ShadowRunWrite(
                scan_id=scan_id,
                post_id=post_id,
                backend="placeholder",
                model="fixture",
                catalogue_id="catalogue",
                catalogue_version="a" * 64,
                request_id="request-1",
                state={"post": {"id": "shadow-post"}},
                status="ok",
            )
        )
        other = Message(
            "bluesky",
            "other-post",
            "feed",
            "feed",
            message.author,
            "text",
            datetime.now(UTC),
        )
        other_post_id = state.save_post(other, scan_id)
        wrong_evaluation_id = state.save_evaluation(
            RelevanceResult(other, True, 0.9, "fixture"), other_post_id, scan_id
        )
        assert not state.shadow_relevance.backfill_evaluation_id(run.id, wrong_evaluation_id)

        evaluation_id = state.save_evaluation(
            RelevanceResult(message, True, 0.9, "fixture"), post_id, scan_id
        )
        assert state.shadow_relevance.backfill_evaluation_id(run.id, evaluation_id)


def test_report_hides_grade_after_latest_revision_invalidates_it() -> None:
    with StateManager(":memory:") as state:
        scan_id, post_id, message = _parents(state)
        evaluation_id = state.save_evaluation(
            RelevanceResult(message, True, 0.9, "fixture"), post_id, scan_id
        )
        state.shadow_relevance.record_shadow_run(
            ShadowRunWrite(
                scan_id=scan_id,
                post_id=post_id,
                evaluation_id=evaluation_id,
                backend="placeholder",
                model="fixture",
                catalogue_id="catalogue",
                catalogue_version="a" * 64,
                request_id="report-grade",
                state={"post": {"id": "shadow-post"}},
                status="ok",
                answers={"answers": {}},
                decision={"reason": "fixture"},
                eligible=True,
            )
        )
        grade_id = state.save_grade(
            GradeRecord(
                post_id=post_id,
                evaluation_id=evaluation_id,
                source="web",
                graded_at=datetime.now(UTC),
                relevance_judgment="correct",
                action_judgment="accept",
                schema_version=3,
            )
        )

        assert ShadowRelevanceStore.report_rows(state.conn, scan_id=scan_id)[0].human_grade == (
            "correct"
        )
        with state.db.begin_immediate():
            state.mark_grade_needs_regrade_for_remediation(
                grade_id, remediation_reason="test invalidation"
            )

        rows = ShadowRelevanceStore.report_rows(state.conn, scan_id=scan_id)
        assert len(rows) == 1
        assert rows[0].human_grade is None
