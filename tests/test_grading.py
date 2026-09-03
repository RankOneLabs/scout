"""Tests for v3 causal grading validation and signal helpers."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

import scout.grading.service as grading
from scout.config import (
    GradeRecord,
    GradingSignal,
    Message,
    RelevanceResult,
)
from scout.grading.service import (
    format_grading_signals,
    format_review_item,
    grade_envelope_payload,
    review_scan,
    validate_grade_envelope,
)
from scout.storage.state import GradeValidationError, StateManager


def _fake_editor(*, replacement: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace grading.subprocess.run so the "editor" overwrites the tmpfile
    with `replacement` — or, when None, leaves it exactly as seeded (an
    unedited save)."""

    def fake_run(argv: list[str], check: bool = False) -> None:
        if replacement is not None:
            Path(argv[-1]).write_text(replacement)

    monkeypatch.setattr("scout.grading.service.subprocess.run", fake_run)


def _capture_saved_grades(
    state: StateManager, monkeypatch: pytest.MonkeyPatch
) -> list[GradeRecord]:
    """Wrap state.save_grade to record every GradeRecord it's called with,
    while still delegating to the real implementation so callers can also
    assert on persisted state."""
    captured: list[GradeRecord] = []
    original = state.save_grade

    def capture(grade: GradeRecord) -> int:
        captured.append(grade)
        return original(grade)

    monkeypatch.setattr(state, "save_grade", capture)
    return captured


class TestGradeEnvelopePayload:
    """grade_envelope_payload is the sole GradeRecord -> validation-payload
    adapter (CLI and StateManager both route through it). The exhaustive
    accept/fail/causal-detail/posture verdict matrix lives in
    tests/test_grading_contract.py's cross-runtime fixture matrix — these
    tests only prove the adapter itself carries every field through
    (including edited_text) without dropping any before validation."""

    def _grade(self, **overrides: object) -> GradeRecord:
        defaults: dict[str, object] = dict(
            post_id=1,
            source="cli",
            graded_at=datetime.now(UTC),
            relevance_judgment="correct",
            action_judgment="accept",
        )
        defaults.update(overrides)
        return GradeRecord(**defaults)  # type: ignore[arg-type]

    def test_payload_carries_every_field(self) -> None:
        grade = self._grade(
            action_judgment="fail",
            dimensions=["tone"],
            failure_note="overly formal",
            edited_text="a corrected reply",
        )
        payload = grade_envelope_payload(grade)
        assert payload == {
            "relevance_judgment": "correct",
            "action_judgment": "fail",
            "dimensions": ["tone"],
            "failure_note": "overly formal",
            "factual_offending_claim": None,
            "factual_disposition": None,
            "factual_contradicting_evidence": None,
            "context_missing_input": None,
            "posture_should_have_been": None,
            "implication_implied_claim": None,
            "implication_missing_support": None,
            "edited_text": "a corrected reply",
        }

    def test_accept_payload_validates(self) -> None:
        payload = grade_envelope_payload(self._grade())
        assert validate_grade_envelope(payload, None) == []

    def test_accept_with_edited_text_is_rejected(self) -> None:
        payload = grade_envelope_payload(self._grade(edited_text="a rewrite"))
        assert validate_grade_envelope(payload, None) != []

    def test_fail_with_edited_text_only_validates(self) -> None:
        payload = grade_envelope_payload(
            self._grade(
                action_judgment="fail",
                dimensions=["tone"],
                edited_text="a less formal rewrite",
            )
        )
        assert validate_grade_envelope(payload, None) == []

    def test_fail_without_failure_note_or_edited_text_or_detail_is_rejected(self) -> None:
        payload = grade_envelope_payload(
            self._grade(action_judgment="fail", dimensions=["tone"])
        )
        assert validate_grade_envelope(payload, None) != []


class TestGradeStorage:
    """Tests for StateManager v3 grade operations."""

    def _setup_eval(self, state: StateManager) -> tuple[int, int, int]:
        """Create a scan, post, and evaluation. Returns (scan_id, post_id, eval_id)."""
        scan_id = state.start_scan()
        msg = Message(
            platform="discord",
            platform_id="grade-test-1",
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="Looking for agent tools",
            created_at=datetime.now(UTC),
        )
        post_id = state.save_post(msg, scan_id)
        result = RelevanceResult(
            message=msg, relevant=True, score=0.9,
            reason="relevant", relevant_to=("gateway",),
        )
        eval_id = state.save_evaluation(result, post_id, scan_id)
        return scan_id, post_id, eval_id

    def _make_pass_grade(
        self, post_id: int, scan_id: int, eval_id: int | None = None
    ) -> GradeRecord:
        return GradeRecord(
            post_id=post_id,
            evaluation_id=eval_id,
            source="cli",
            graded_at=datetime.now(UTC),
            relevance_judgment="correct",
            action_judgment="accept",
            scan_id=scan_id,
            schema_version=3,
            needs_regrade=False,
        )

    def _make_fail_grade(
        self, post_id: int, scan_id: int, eval_id: int | None = None
    ) -> GradeRecord:
        return GradeRecord(
            post_id=post_id,
            evaluation_id=eval_id,
            source="cli",
            graded_at=datetime.now(UTC),
            relevance_judgment="correct",
            action_judgment="fail",
            scan_id=scan_id,
            schema_version=3,
            needs_regrade=False,
            dimensions=["tone"],
            failure_note="overly formal",
        )

    def test_save_pass_grade_returns_id(self, in_memory_state: StateManager) -> None:
        scan_id, post_id, eval_id = self._setup_eval(in_memory_state)
        grade = self._make_pass_grade(post_id, scan_id, eval_id)
        row_id = in_memory_state.save_grade(grade)
        assert row_id >= 1

    def test_get_grade_round_trip_pass(self, in_memory_state: StateManager) -> None:
        scan_id, post_id, eval_id = self._setup_eval(in_memory_state)
        grade = self._make_pass_grade(post_id, scan_id, eval_id)
        in_memory_state.save_grade(grade)
        loaded = in_memory_state.get_grade(post_id)
        assert loaded is not None
        assert loaded.relevance_judgment == "correct"
        assert loaded.action_judgment == "accept"
        assert loaded.dimensions is None
        assert loaded.failure_note is None

    def test_get_grade_round_trip_fail(self, in_memory_state: StateManager) -> None:
        scan_id, post_id, eval_id = self._setup_eval(in_memory_state)
        grade = GradeRecord(
            post_id=post_id,
            evaluation_id=eval_id,
            source="web",
            graded_at=datetime.now(UTC),
            relevance_judgment="false_positive",
            action_judgment="fail",
            scan_id=scan_id,
            schema_version=3,
            needs_regrade=False,
            dimensions=["contextual_understanding", "posture"],
            failure_note="misread context; should have asked",
            context_missing_input="parent post intent",
            posture_should_have_been="ask",
        )
        in_memory_state.save_grade(grade)
        loaded = in_memory_state.get_grade(post_id)
        assert loaded is not None
        assert loaded.action_judgment == "fail"
        assert loaded.dimensions == ["contextual_understanding", "posture"]
        assert loaded.failure_note == "misread context; should have asked"
        assert loaded.context_missing_input == "parent post intent"
        assert loaded.posture_should_have_been == "ask"

    def test_get_grade_factual_detail_round_trip(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, post_id, eval_id = self._setup_eval(in_memory_state)
        grade = GradeRecord(
            post_id=post_id,
            evaluation_id=eval_id,
            source="web",
            graded_at=datetime.now(UTC),
            relevance_judgment="correct",
            action_judgment="fail",
            scan_id=scan_id,
            schema_version=3,
            needs_regrade=False,
            dimensions=["factual_support"],
            failure_note="contradicted by docs",
            factual_offending_claim="50k users",
            factual_disposition="contradicted",
            factual_contradicting_evidence="docs say 10k",
        )
        in_memory_state.save_grade(grade)
        loaded = in_memory_state.get_grade(post_id)
        assert loaded is not None
        assert loaded.factual_offending_claim == "50k users"
        assert loaded.factual_disposition == "contradicted"
        assert loaded.factual_contradicting_evidence == "docs say 10k"

    def test_get_grade_implication_detail_round_trip(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, post_id, eval_id = self._setup_eval(in_memory_state)
        grade = GradeRecord(
            post_id=post_id,
            evaluation_id=eval_id,
            source="web",
            graded_at=datetime.now(UTC),
            relevance_judgment="correct",
            action_judgment="fail",
            scan_id=scan_id,
            schema_version=3,
            needs_regrade=False,
            dimensions=["unsupported_implication"],
            failure_note="implied community without basis",
            implication_implied_claim="active community",
            implication_missing_support="no community fact in dossier",
        )
        in_memory_state.save_grade(grade)
        loaded = in_memory_state.get_grade(post_id)
        assert loaded is not None
        assert loaded.implication_implied_claim == "active community"
        assert loaded.implication_missing_support == "no community fact in dossier"

    def test_get_grade_edited_text_round_trip(self, in_memory_state: StateManager) -> None:
        """edited_text has no column of its own on grades — it's durably
        stored via a reply_draft_revisions row, pointed to by
        grades.reply_revision_id. This proves the correction survives a
        real save_grade/get_grade round trip, not just in-memory."""
        scan_id, post_id, eval_id = self._setup_eval(in_memory_state)
        in_memory_state.save_draft(post_id, eval_id, "gateway", "Draft comment", scan_id)
        grade = GradeRecord(
            post_id=post_id,
            evaluation_id=eval_id,
            source="cli",
            graded_at=datetime.now(UTC),
            relevance_judgment="correct",
            action_judgment="fail",
            scan_id=scan_id,
            dimensions=["tone"],
            edited_text="a less formal rewrite of the draft reply",
        )
        in_memory_state.save_grade(grade)

        loaded = in_memory_state.get_grade(post_id)
        assert loaded is not None
        assert loaded.edited_text == "a less formal rewrite of the draft reply"

        by_eval = in_memory_state.get_grade_for_evaluation(eval_id)
        assert by_eval is not None
        assert by_eval.edited_text == "a less formal rewrite of the draft reply"

    def test_edited_text_updates_on_regrade_append_a_new_reply_revision(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, post_id, eval_id = self._setup_eval(in_memory_state)
        in_memory_state.save_draft(post_id, eval_id, "gateway", "Draft comment", scan_id)
        in_memory_state.save_grade(GradeRecord(
            post_id=post_id, evaluation_id=eval_id, source="cli",
            graded_at=datetime.now(UTC), relevance_judgment="correct",
            action_judgment="fail", scan_id=scan_id,
            dimensions=["tone"], edited_text="first correction",
        ))
        in_memory_state.save_grade(GradeRecord(
            post_id=post_id, evaluation_id=eval_id, source="cli",
            graded_at=datetime.now(UTC), relevance_judgment="correct",
            action_judgment="fail", scan_id=scan_id,
            dimensions=["tone"], edited_text="second, better correction",
        ))

        loaded = in_memory_state.get_grade(post_id)
        assert loaded is not None
        assert loaded.edited_text == "second, better correction"

        revision_count = in_memory_state.conn.execute(
            "SELECT COUNT(*) FROM reply_draft_revisions"
        ).fetchone()[0]
        assert revision_count == 2

    def test_save_grade_upserts_on_same_evaluation(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, post_id, eval_id = self._setup_eval(in_memory_state)
        grade1 = self._make_pass_grade(post_id, scan_id, eval_id)
        in_memory_state.save_grade(grade1)

        grade2 = self._make_fail_grade(post_id, scan_id, eval_id)
        in_memory_state.save_grade(grade2)

        loaded = in_memory_state.get_grade(post_id)
        assert loaded is not None
        assert loaded.action_judgment == "fail"

        count = in_memory_state.conn.execute(
            "SELECT COUNT(*) FROM grades WHERE post_id = ?", (post_id,)
        ).fetchone()[0]
        assert count == 1

    def test_get_grade_returns_none_for_ungraded(
        self, in_memory_state: StateManager
    ) -> None:
        assert in_memory_state.get_grade(9999) is None

    def test_get_grading_progress_counts_v2_only(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id = in_memory_state.start_scan()

        msg1 = Message(
            platform="discord", platform_id="gp-1", channel_name="ch",
            channel_id="c1", author_name="a", author_id="u1",
            content="post 1", created_at=datetime.now(UTC),
        )
        msg2 = Message(
            platform="discord", platform_id="gp-2", channel_name="ch",
            channel_id="c1", author_name="b", author_id="u2",
            content="post 2", created_at=datetime.now(UTC),
        )
        p1 = in_memory_state.save_post(msg1, scan_id)
        p2 = in_memory_state.save_post(msg2, scan_id)

        r1 = RelevanceResult(message=msg1, relevant=True, score=0.8,
                             reason="r1", relevant_to=("a",))
        r2 = RelevanceResult(message=msg2, relevant=True, score=0.7,
                             reason="r2", relevant_to=("a",))
        e1 = in_memory_state.save_evaluation(r1, p1, scan_id)
        e2 = in_memory_state.save_evaluation(r2, p2, scan_id)

        # Save v2 grade for p1 only
        in_memory_state.save_grade(self._make_pass_grade(p1, scan_id, e1))

        graded, total = in_memory_state.get_grading_progress(scan_id)
        assert graded == 1
        assert total == 2

        # Also grade p2
        in_memory_state.save_grade(self._make_pass_grade(p2, scan_id, e2))
        graded, total = in_memory_state.get_grading_progress(scan_id)
        assert graded == 2
        assert total == 2

    def test_get_recent_grading_signals_empty(
        self, in_memory_state: StateManager
    ) -> None:
        sig = in_memory_state.get_recent_grading_signals()
        assert sig.total_graded == 0
        assert sig.pass_count == 0
        assert sig.fail_count == 0

    def test_get_recent_grading_signals_aggregates(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id = in_memory_state.start_scan()

        for i, (rel, action, dims, note) in enumerate([
            ("correct", "accept", None, None),
            ("correct", "accept", None, None),
            ("correct", "fail", ["tone"], "bad tone"),
            ("false_positive", "fail", ["contextual_understanding"], "missed ctx"),
        ]):
            msg = Message(
                platform="discord", platform_id=f"sig-{i}", channel_name="ch",
                channel_id="c1", author_name="a", author_id="u1",
                content=f"post {i}", created_at=datetime.now(UTC),
            )
            pid = in_memory_state.save_post(msg, scan_id)
            result = RelevanceResult(
                message=msg, relevant=True, score=0.8,
                reason="r", relevant_to=("a",),
            )
            eid = in_memory_state.save_evaluation(result, pid, scan_id)
            in_memory_state.save_grade(GradeRecord(
                post_id=pid, evaluation_id=eid, source="cli",
                graded_at=datetime.now(UTC), scan_id=scan_id,
                relevance_judgment=rel, action_judgment=action,
                schema_version=3, needs_regrade=False,
                dimensions=dims, failure_note=note,
                context_missing_input="ctx" if "contextual_understanding" in (dims or []) else None,
            ))

        in_memory_state.complete_scan(scan_id, 4, 3)
        sig = in_memory_state.get_recent_grading_signals(limit_scans=1)

        assert sig.total_graded == 4
        assert sig.pass_count == 2
        assert sig.fail_count == 2
        assert sig.false_positive_count == 1

    def test_export_eval_cases_v2(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()
        msg = Message(
            platform="discord", platform_id="ec-1", channel_name="general",
            channel_id="c1", author_name="alice", author_id="u1",
            content="need agent tools", created_at=datetime.now(UTC),
        )
        post_id = in_memory_state.save_post(msg, scan_id)
        result = RelevanceResult(
            message=msg, relevant=True, score=0.9,
            reason="relevant", relevant_to=("gateway",),
        )
        eval_id = in_memory_state.save_evaluation(result, post_id, scan_id)
        in_memory_state.save_draft(post_id, eval_id, "gateway", "Great draft", scan_id)
        in_memory_state.save_grade(self._make_pass_grade(post_id, scan_id, eval_id))

        cases = in_memory_state.export_eval_cases()
        assert len(cases) == 1
        assert cases[0]["source"]["content"] == "need agent tools"
        assert cases[0]["grade"]["relevance_judgment"] == "correct"
        assert cases[0]["grade"]["action_judgment"] == "accept"
        assert cases[0]["evaluation"]["draft"] == "Great draft"

    def test_export_eval_cases_since_uses_canonical_utc_timestamp(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id = in_memory_state.start_scan()
        msg = Message(
            platform="discord", platform_id="ec-since", channel_name="general",
            channel_id="c1", author_name="alice", author_id="u1",
            content="timestamp boundary", created_at=datetime.now(UTC),
        )
        post_id = in_memory_state.save_post(msg, scan_id)
        result = RelevanceResult(
            message=msg, relevant=True, score=0.9,
            reason="relevant", relevant_to=("gateway",),
        )
        eval_id = in_memory_state.save_evaluation(result, post_id, scan_id)
        in_memory_state.save_draft(post_id, eval_id, "gateway", "Draft", scan_id)
        grade = replace(
            self._make_pass_grade(post_id, scan_id, eval_id),
            graded_at=datetime(2026, 1, 1, 0, 30, tzinfo=UTC),
        )
        in_memory_state.save_grade(grade)

        since = datetime(
            2026, 1, 1, 1, 0, tzinfo=timezone(timedelta(hours=1))
        )

        assert len(in_memory_state.export_eval_cases(since=since)) == 1

    def test_export_eval_cases_excludes_needs_regrade(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id = in_memory_state.start_scan()
        msg = Message(
            platform="discord", platform_id="ec-2", channel_name="ch",
            channel_id="c1", author_name="a", author_id="u1",
            content="post", created_at=datetime.now(UTC),
        )
        post_id = in_memory_state.save_post(msg, scan_id)
        result = RelevanceResult(
            message=msg, relevant=True, score=0.8,
            reason="r", relevant_to=("gateway",),
        )
        eval_id = in_memory_state.save_evaluation(result, post_id, scan_id)

        # Save a v1 needs_regrade grade via the migration lane — a legacy
        # v1 row (schema_version=1, action_judgment=None) is exactly what
        # save_grade's schema-v2-only enforcement now rejects.
        grade = GradeRecord(
            post_id=post_id, evaluation_id=eval_id, source="cli",
            graded_at=datetime.now(UTC), scan_id=scan_id,
            relevance_judgment="correct", action_judgment=None,
            schema_version=1, needs_regrade=True,
        )
        in_memory_state.save_grade_for_migration(
            grade, migration_reason="test: legacy v1 row fixture"
        )

        cases = in_memory_state.export_eval_cases()
        assert len(cases) == 0

    def test_get_gradeable_items_returns_all_evaluations(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id = in_memory_state.start_scan()
        msg = Message(
            platform="discord", platform_id="gi-1", channel_name="general",
            channel_id="c1", author_name="alice", author_id="u1",
            content="agent question", created_at=datetime.now(UTC),
        )
        post_id = in_memory_state.save_post(msg, scan_id)
        result = RelevanceResult(
            message=msg, relevant=True, score=0.85,
            reason="relevant", relevant_to=("gateway",),
        )
        eval_id = in_memory_state.save_evaluation(result, post_id, scan_id)
        in_memory_state.save_draft(post_id, eval_id, "gateway", "Draft text", scan_id)

        items = in_memory_state.get_gradeable_items(scan_id)
        assert len(items) == 1
        assert items[0]["post_id"] == post_id
        assert items[0]["content"] == "agent question"
        assert items[0]["score"] == 0.85
        assert items[0]["comment_text"] == "Draft text"
        assert items[0]["relevance_judgment"] is None  # not yet graded
        assert items[0]["action_judgment"] is None

    def test_signals_empty_when_no_grades(self, in_memory_state: StateManager) -> None:
        grading_signal = in_memory_state.get_recent_grading_signals()
        assert grading_signal.total_graded == 0


class TestSaveGradeValidationBoundary:
    """save_grade is the mandatory validated entry point for schema-v3
    writes: it must reject invalid grades before any INSERT/UPDATE reaches
    SQLite, using the evaluation's stored posture rather than a
    caller-supplied one."""

    def _setup_eval(
        self, state: StateManager, posture: str | None = None
    ) -> tuple[int, int, int]:
        scan_id = state.start_scan()
        msg = Message(
            platform="discord",
            platform_id="boundary-test-1",
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="Looking for agent tools",
            created_at=datetime.now(UTC),
        )
        post_id = state.save_post(msg, scan_id)
        result = RelevanceResult(
            message=msg, relevant=True, score=0.9,
            reason="relevant", relevant_to=("gateway",),
        )
        eval_id = state.save_evaluation(result, post_id, scan_id, posture=posture)
        return scan_id, post_id, eval_id

    def _grades_row_count(self, state: StateManager, post_id: int) -> int:
        return state.conn.execute(
            "SELECT COUNT(*) FROM grades WHERE post_id = ?", (post_id,)
        ).fetchone()[0]

    def test_edited_text_without_a_draft_raises_and_writes_nothing(
        self, in_memory_state: StateManager
    ) -> None:
        """The shared JSON Schema has no DB access, so it can't tell
        whether an evaluation has a draft to correct — that's
        StateManager's job at the persistence boundary."""
        scan_id, post_id, eval_id = self._setup_eval(in_memory_state)
        grade = GradeRecord(
            post_id=post_id,
            evaluation_id=eval_id,
            source="cli",
            graded_at=datetime.now(UTC),
            relevance_judgment="correct",
            action_judgment="fail",
            scan_id=scan_id,
            dimensions=["tone"],
            edited_text="a forged correction with nothing to correct",
        )
        with pytest.raises(GradeValidationError):
            in_memory_state.save_grade(grade)
        assert self._grades_row_count(in_memory_state, post_id) == 0
        reply_revision_count = in_memory_state.conn.execute(
            "SELECT COUNT(*) FROM reply_draft_revisions"
        ).fetchone()[0]
        assert reply_revision_count == 0

    def test_contradicted_without_evidence_raises_and_writes_nothing(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, post_id, eval_id = self._setup_eval(in_memory_state)
        grade = GradeRecord(
            post_id=post_id,
            evaluation_id=eval_id,
            source="cli",
            graded_at=datetime.now(UTC),
            relevance_judgment="correct",
            action_judgment="fail",
            scan_id=scan_id,
            dimensions=["factual_support"],
            failure_note="wrong fact",
            factual_offending_claim="50k users",
            factual_disposition="contradicted",
        )
        with pytest.raises(GradeValidationError):
            in_memory_state.save_grade(grade)
        assert self._grades_row_count(in_memory_state, post_id) == 0

    def test_missing_fail_dimensions_raises_and_writes_nothing(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, post_id, eval_id = self._setup_eval(in_memory_state)
        grade = GradeRecord(
            post_id=post_id,
            evaluation_id=eval_id,
            source="cli",
            graded_at=datetime.now(UTC),
            relevance_judgment="correct",
            action_judgment="fail",
            scan_id=scan_id,
            failure_note="something was wrong",
        )
        with pytest.raises(GradeValidationError):
            in_memory_state.save_grade(grade)
        assert self._grades_row_count(in_memory_state, post_id) == 0

    def test_missing_failure_note_raises_and_writes_nothing(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, post_id, eval_id = self._setup_eval(in_memory_state)
        grade = GradeRecord(
            post_id=post_id,
            evaluation_id=eval_id,
            source="cli",
            graded_at=datetime.now(UTC),
            relevance_judgment="correct",
            action_judgment="fail",
            scan_id=scan_id,
            dimensions=["tone"],
        )
        with pytest.raises(GradeValidationError):
            in_memory_state.save_grade(grade)
        assert self._grades_row_count(in_memory_state, post_id) == 0

    def test_mismatched_post_id_raises_and_writes_nothing(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, post_id, eval_id = self._setup_eval(in_memory_state)
        grade = GradeRecord(
            post_id=post_id + 999,
            evaluation_id=eval_id,
            source="cli",
            graded_at=datetime.now(UTC),
            relevance_judgment="correct",
            action_judgment="accept",
            scan_id=scan_id,
        )
        with pytest.raises(GradeValidationError):
            in_memory_state.save_grade(grade)
        assert self._grades_row_count(in_memory_state, post_id + 999) == 0

    def test_mismatched_scan_id_raises_and_writes_nothing(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, post_id, eval_id = self._setup_eval(in_memory_state)
        other_scan_id = in_memory_state.start_scan()
        grade = GradeRecord(
            post_id=post_id,
            evaluation_id=eval_id,
            source="cli",
            graded_at=datetime.now(UTC),
            relevance_judgment="correct",
            action_judgment="accept",
            scan_id=other_scan_id,
        )
        with pytest.raises(GradeValidationError):
            in_memory_state.save_grade(grade)
        assert self._grades_row_count(in_memory_state, post_id) == 0

    def test_missing_evaluation_raises_and_writes_nothing(
        self, in_memory_state: StateManager
    ) -> None:
        grade = GradeRecord(
            post_id=1,
            evaluation_id=999999,
            source="cli",
            graded_at=datetime.now(UTC),
            relevance_judgment="correct",
            action_judgment="accept",
        )
        with pytest.raises(GradeValidationError):
            in_memory_state.save_grade(grade)
        assert self._grades_row_count(in_memory_state, 1) == 0

    def test_no_evaluation_resolvable_raises_and_writes_nothing(
        self, in_memory_state: StateManager
    ) -> None:
        grade = GradeRecord(
            post_id=1,
            source="cli",
            graded_at=datetime.now(UTC),
            relevance_judgment="correct",
            action_judgment="accept",
        )
        with pytest.raises(GradeValidationError):
            in_memory_state.save_grade(grade)
        assert self._grades_row_count(in_memory_state, 1) == 0

    def test_equal_posture_correction_raises_and_writes_nothing(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, post_id, eval_id = self._setup_eval(in_memory_state, posture="ask")
        grade = GradeRecord(
            post_id=post_id,
            evaluation_id=eval_id,
            source="cli",
            graded_at=datetime.now(UTC),
            relevance_judgment="correct",
            action_judgment="fail",
            scan_id=scan_id,
            dimensions=["posture"],
            failure_note="posture was wrong",
            posture_should_have_been="ask",
        )
        with pytest.raises(GradeValidationError):
            in_memory_state.save_grade(grade)
        assert self._grades_row_count(in_memory_state, post_id) == 0

    def test_valid_posture_correction_differing_from_actual_succeeds(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, post_id, eval_id = self._setup_eval(in_memory_state, posture="ask")
        grade = GradeRecord(
            post_id=post_id,
            evaluation_id=eval_id,
            source="cli",
            graded_at=datetime.now(UTC),
            relevance_judgment="correct",
            action_judgment="fail",
            scan_id=scan_id,
            dimensions=["posture"],
            failure_note="posture was wrong",
            posture_should_have_been="answer",
        )
        in_memory_state.save_grade(grade)
        assert self._grades_row_count(in_memory_state, post_id) == 1

    def test_schema_version_1_raises_and_writes_nothing(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, post_id, eval_id = self._setup_eval(in_memory_state)
        grade = GradeRecord(
            post_id=post_id,
            evaluation_id=eval_id,
            source="cli",
            graded_at=datetime.now(UTC),
            relevance_judgment="correct",
            action_judgment="accept",
            scan_id=scan_id,
            schema_version=1,
        )
        with pytest.raises(GradeValidationError):
            in_memory_state.save_grade(grade)
        assert self._grades_row_count(in_memory_state, post_id) == 0

    def test_null_action_judgment_raises_and_writes_nothing(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, post_id, eval_id = self._setup_eval(in_memory_state)
        grade = GradeRecord(
            post_id=post_id,
            evaluation_id=eval_id,
            source="cli",
            graded_at=datetime.now(UTC),
            relevance_judgment="correct",
            action_judgment=None,
            scan_id=scan_id,
        )
        with pytest.raises(GradeValidationError):
            in_memory_state.save_grade(grade)
        assert self._grades_row_count(in_memory_state, post_id) == 0

    def test_save_grade_for_migration_requires_reason(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, post_id, eval_id = self._setup_eval(in_memory_state)
        grade = GradeRecord(
            post_id=post_id,
            evaluation_id=eval_id,
            source="migration",
            graded_at=datetime.now(UTC),
            relevance_judgment="correct",
            action_judgment=None,
            scan_id=scan_id,
            schema_version=1,
            needs_regrade=True,
        )
        with pytest.raises(ValueError):
            in_memory_state.save_grade_for_migration(grade, migration_reason="   ")
        assert self._grades_row_count(in_memory_state, post_id) == 0

        in_memory_state.save_grade_for_migration(
            grade, migration_reason="backfill legacy v1 corpus"
        )
        assert self._grades_row_count(in_memory_state, post_id) == 1

    def test_invalid_source_raises_and_writes_nothing(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, post_id, eval_id = self._setup_eval(in_memory_state)
        grade = GradeRecord(
            post_id=post_id,
            evaluation_id=eval_id,
            source="migration",
            graded_at=datetime.now(UTC),
            relevance_judgment="correct",
            action_judgment="accept",
            scan_id=scan_id,
        )
        with pytest.raises(GradeValidationError):
            in_memory_state.save_grade(grade)
        assert self._grades_row_count(in_memory_state, post_id) == 0

    def test_save_grade_for_migration_rejects_mismatched_post_id(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, post_id, eval_id = self._setup_eval(in_memory_state)
        grade = GradeRecord(
            post_id=post_id + 999,
            evaluation_id=eval_id,
            source="migration",
            graded_at=datetime.now(UTC),
            relevance_judgment="correct",
            action_judgment=None,
            scan_id=scan_id,
            schema_version=1,
            needs_regrade=True,
        )
        with pytest.raises(ValueError):
            in_memory_state.save_grade_for_migration(
                grade, migration_reason="backfill legacy v1 corpus"
            )
        assert self._grades_row_count(in_memory_state, post_id + 999) == 0

    def test_save_grade_for_migration_rejects_mismatched_scan_id(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, post_id, eval_id = self._setup_eval(in_memory_state)
        other_scan_id = in_memory_state.start_scan()
        grade = GradeRecord(
            post_id=post_id,
            evaluation_id=eval_id,
            source="migration",
            graded_at=datetime.now(UTC),
            relevance_judgment="correct",
            action_judgment=None,
            scan_id=other_scan_id,
            schema_version=1,
            needs_regrade=True,
        )
        with pytest.raises(ValueError):
            in_memory_state.save_grade_for_migration(
                grade, migration_reason="backfill legacy v1 corpus"
            )
        assert self._grades_row_count(in_memory_state, post_id) == 0


class TestFormatReviewItem:
    def test_includes_all_fields(self) -> None:
        item = {
            "channel_name": "general",
            "author_name": "alice",
            "created_at": "2026-03-27T08:00:00",
            "content": "looking for agent tools",
            "score": 0.85,
            "relevant": True,
            "surface_status": "surfaced",
            "posture": "answer",
            "comment_text": "Great question about agents",
            "evaluation_id": 1,
            "post_id": 1,
        }
        output = format_review_item(item, 0, 5)
        assert "Evaluation 1/5" in output
        assert "#general" in output
        assert "alice" in output
        assert "looking for agent tools" in output
        assert "0.85" in output
        assert "Great question about agents" in output

    def test_truncates_long_content(self) -> None:
        item = {
            "content": "x" * 600,
            "score": 0.5,
            "relevant": False,
        }
        output = format_review_item(item, 0, 1)
        assert "..." in output

    def test_handles_missing_draft(self) -> None:
        item = {"content": "test", "score": 0.5, "comment_text": None, "relevant": False}
        output = format_review_item(item, 0, 1)
        # No draft should mean no "Draft:" line
        assert "Draft:" not in output

    def test_shows_existing_grade(self) -> None:
        item = {
            "content": "test",
            "score": 0.5,
            "relevance_judgment": "correct",
            "action_judgment": "accept",
            "relevant": True,
        }
        output = format_review_item(item, 0, 1)
        assert "Already graded: correct" in output
        assert "accept" in output


class TestReviewScan:
    def _setup_scan_with_eval(
        self, state: StateManager
    ) -> tuple[int, int, int]:
        scan_id = state.start_scan()
        msg = Message(
            platform="discord", platform_id="review-1",
            channel_name="general", channel_id="c1",
            author_name="alice", author_id="u1",
            content="agent question", created_at=datetime.now(UTC),
        )
        post_id = state.save_post(msg, scan_id)
        result = RelevanceResult(
            message=msg, relevant=True, score=0.85,
            reason="relevant", relevant_to=("gateway",),
        )
        eval_id = state.save_evaluation(result, post_id, scan_id)
        state.save_draft(post_id, eval_id, "gateway", "Draft comment", scan_id)
        return scan_id, post_id, eval_id

    def test_review_saves_pass_grade(
        self, in_memory_state: StateManager, monkeypatch: object
    ) -> None:
        scan_id, post_id, _ = self._setup_scan_with_eval(in_memory_state)
        # c=correct, a=accept (explicit — the CLI no longer defaults on blank input)
        inputs = iter(["c", "a"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))  # type: ignore[attr-defined]

        review_scan(in_memory_state, scan_id)

        grade = in_memory_state.get_grade(post_id)
        assert grade is not None
        assert grade.relevance_judgment == "correct"
        assert grade.action_judgment == "accept"
        assert grade.dimensions is None

    def test_review_saves_fail_grade(
        self, in_memory_state: StateManager, monkeypatch: object
    ) -> None:
        scan_id, post_id, _ = self._setup_scan_with_eval(in_memory_state)
        _fake_editor(replacement=None, monkeypatch=monkeypatch)
        # c=correct, f=fail, "5" (tone dim), note
        inputs = iter(["c", "f", "5", "too formal"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))  # type: ignore[attr-defined]

        review_scan(in_memory_state, scan_id)

        grade = in_memory_state.get_grade(post_id)
        assert grade is not None
        assert grade.action_judgment == "fail"
        assert grade.dimensions is not None
        assert "tone" in grade.dimensions
        assert grade.failure_note == "too formal"

    def test_skip_does_not_save(
        self, in_memory_state: StateManager, monkeypatch: object
    ) -> None:
        scan_id, post_id, _ = self._setup_scan_with_eval(in_memory_state)
        inputs = iter(["s"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))  # type: ignore[attr-defined]

        review_scan(in_memory_state, scan_id)

        assert in_memory_state.get_grade(post_id) is None

    def test_quit_stops_early(
        self, in_memory_state: StateManager, monkeypatch: object
    ) -> None:
        scan_id, _, _ = self._setup_scan_with_eval(in_memory_state)

        msg2 = Message(
            platform="discord", platform_id="review-2",
            channel_name="general", channel_id="c1",
            author_name="bob", author_id="u2",
            content="another question", created_at=datetime.now(UTC),
        )
        post2_id = in_memory_state.save_post(msg2, scan_id)
        r2 = RelevanceResult(message=msg2, relevant=True, score=0.6,
                             reason="r2", relevant_to=("a",))
        in_memory_state.save_evaluation(r2, post2_id, scan_id)

        inputs = iter(["q"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))  # type: ignore[attr-defined]

        review_scan(in_memory_state, scan_id)

        graded, total = in_memory_state.get_grading_progress(scan_id)
        assert graded == 0
        assert total == 2

    def test_relevance_prompt_retries_on_invalid_choice(
        self, in_memory_state: StateManager, monkeypatch: object
    ) -> None:
        scan_id, post_id, _ = self._setup_scan_with_eval(in_memory_state)
        # invalid relevance choice, then valid; invalid action choice, then valid
        inputs = iter(["x", "c", "z", "a"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))  # type: ignore[attr-defined]

        review_scan(in_memory_state, scan_id)

        grade = in_memory_state.get_grade(post_id)
        assert grade is not None
        assert grade.relevance_judgment == "correct"
        assert grade.action_judgment == "accept"

    def test_dimensions_prompt_retries_until_valid_selection(
        self, in_memory_state: StateManager, monkeypatch: object
    ) -> None:
        scan_id, post_id, _ = self._setup_scan_with_eval(in_memory_state)
        _fake_editor(replacement=None, monkeypatch=monkeypatch)
        # c=correct, f=fail, "" (no valid dimension) then "5" (tone), then note
        inputs = iter(["c", "f", "", "5", "too formal"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))  # type: ignore[attr-defined]

        review_scan(in_memory_state, scan_id)

        grade = in_memory_state.get_grade(post_id)
        assert grade is not None
        assert grade.dimensions == ["tone"]

    def test_failure_note_prompt_retries_until_non_whitespace(
        self, in_memory_state: StateManager, monkeypatch: object
    ) -> None:
        scan_id, post_id, _ = self._setup_scan_with_eval(in_memory_state)
        _fake_editor(replacement=None, monkeypatch=monkeypatch)
        # c=correct, f=fail, "5" (tone), blank note then whitespace-only then real note
        inputs = iter(["c", "f", "5", "", "   ", "too formal"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))  # type: ignore[attr-defined]

        review_scan(in_memory_state, scan_id)

        grade = in_memory_state.get_grade(post_id)
        assert grade is not None
        assert grade.failure_note == "too formal"

    def test_posture_prompt_rejects_no_op_correction(
        self, in_memory_state: StateManager, monkeypatch: object
    ) -> None:
        scan_id, post_id, eval_id = self._setup_scan_with_eval(in_memory_state)
        in_memory_state.conn.execute(
            "UPDATE evaluations SET posture = 'ask' WHERE id = ?", (eval_id,)
        )
        in_memory_state.conn.commit()
        _fake_editor(replacement=None, monkeypatch=monkeypatch)

        # c=correct, f=fail, "4" (posture dim), note,
        # posture choice "3" (ask — same as actual, rejected), then "1" (answer)
        inputs = iter(["c", "f", "4", "wrong posture", "3", "1"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))  # type: ignore[attr-defined]

        review_scan(in_memory_state, scan_id)

        grade = in_memory_state.get_grade(post_id)
        assert grade is not None
        assert grade.posture_should_have_been == "answer"


class TestEditedTextCLI:
    """The grading-specific single-buffer CLI editor: seeded from the
    machine draft, opened at most once per fail. These tests assert on
    the GradeRecord captured at the save_grade call site rather than a
    DB round trip — see TestGradeStorage.test_get_grade_edited_text_round_trip
    for proof that a saved edited_text does survive a real
    save_grade/get_grade round trip (via reply_draft_revisions)."""

    def _setup_scan_with_eval(
        self, state: StateManager, *, with_draft: bool = True
    ) -> tuple[int, int, int]:
        scan_id = state.start_scan()
        msg = Message(
            platform="discord", platform_id="edit-1",
            channel_name="general", channel_id="c1",
            author_name="alice", author_id="u1",
            content="agent question", created_at=datetime.now(UTC),
        )
        post_id = state.save_post(msg, scan_id)
        result = RelevanceResult(
            message=msg, relevant=True, score=0.85,
            reason="relevant", relevant_to=("gateway",),
        )
        eval_id = state.save_evaluation(result, post_id, scan_id)
        if with_draft:
            state.save_draft(post_id, eval_id, "gateway", "Draft comment", scan_id)
        return scan_id, post_id, eval_id

    def test_editor_seeded_with_machine_draft(
        self, in_memory_state: StateManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scan_id, _post_id, _eval_id = self._setup_scan_with_eval(in_memory_state)
        seen_initial: dict[str, str] = {}

        def fake_run(argv: list[str], check: bool = False) -> None:
            seen_initial["text"] = Path(argv[-1]).read_text()

        monkeypatch.setattr("scout.grading.service.subprocess.run", fake_run)
        inputs = iter(["c", "f", "5", "too formal"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))  # type: ignore[attr-defined]

        review_scan(in_memory_state, scan_id)

        assert seen_initial["text"] == "Draft comment"

    def test_editor_never_opens_when_no_draft_exists(
        self, in_memory_state: StateManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without a draft there is nothing to correct, and
        StateManager rejects edited_text when the evaluation has no
        draft_comments row — so the editor must never even be offered
        here (a grader typing into an empty buffer would otherwise
        crash review_scan with an uncaught GradeValidationError)."""
        scan_id, _post_id, _eval_id = self._setup_scan_with_eval(
            in_memory_state, with_draft=False
        )
        captured = _capture_saved_grades(in_memory_state, monkeypatch)

        def fail_if_called(argv: list[str], check: bool = False) -> None:
            raise AssertionError("editor must not be invoked without a draft")

        monkeypatch.setattr("scout.grading.service.subprocess.run", fail_if_called)
        inputs = iter(["c", "f", "5", "too formal"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))  # type: ignore[attr-defined]

        review_scan(in_memory_state, scan_id)

        assert captured[0].edited_text is None

    @pytest.mark.parametrize("draft_text", ["", None])
    def test_editor_opens_when_existing_draft_text_is_empty(
        self,
        in_memory_state: StateManager,
        monkeypatch: pytest.MonkeyPatch,
        draft_text: str | None,
    ) -> None:
        scan_id, _post_id, eval_id = self._setup_scan_with_eval(in_memory_state)
        in_memory_state.conn.execute(
            "UPDATE draft_comments SET comment_text = ? WHERE evaluation_id = ?",
            (draft_text, eval_id),
        )
        in_memory_state.conn.commit()
        captured = _capture_saved_grades(in_memory_state, monkeypatch)
        _fake_editor(replacement="a corrected reply", monkeypatch=monkeypatch)
        inputs = iter(["c", "f", "5", "too formal"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))  # type: ignore[attr-defined]

        review_scan(in_memory_state, scan_id)

        assert captured[0].edited_text == "a corrected reply"

    def test_unchanged_content_skips_edit(
        self, in_memory_state: StateManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scan_id, _post_id, _eval_id = self._setup_scan_with_eval(in_memory_state)
        captured = _capture_saved_grades(in_memory_state, monkeypatch)
        _fake_editor(replacement="Draft comment", monkeypatch=monkeypatch)  # no-op save
        inputs = iter(["c", "f", "5", "too formal"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))  # type: ignore[attr-defined]

        review_scan(in_memory_state, scan_id)

        assert captured[0].edited_text is None

    def test_empty_content_skips_edit(
        self, in_memory_state: StateManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scan_id, _post_id, _eval_id = self._setup_scan_with_eval(in_memory_state)
        captured = _capture_saved_grades(in_memory_state, monkeypatch)
        _fake_editor(replacement="   ", monkeypatch=monkeypatch)
        inputs = iter(["c", "f", "5", "too formal"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))  # type: ignore[attr-defined]

        review_scan(in_memory_state, scan_id)

        assert captured[0].edited_text is None

    def test_changed_content_captured_as_edited_text(
        self, in_memory_state: StateManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scan_id, _post_id, _eval_id = self._setup_scan_with_eval(in_memory_state)
        captured = _capture_saved_grades(in_memory_state, monkeypatch)
        _fake_editor(replacement="  a less formal rewrite  ", monkeypatch=monkeypatch)
        inputs = iter(["c", "f", "5", "too formal"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))  # type: ignore[attr-defined]

        review_scan(in_memory_state, scan_id)

        assert captured[0].edited_text == "a less formal rewrite"

    def test_edited_text_persists_across_a_validation_retry(
        self, in_memory_state: StateManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scan_id, _post_id, _eval_id = self._setup_scan_with_eval(in_memory_state)
        captured = _capture_saved_grades(in_memory_state, monkeypatch)

        editor_calls = 0

        def fake_run(argv: list[str], check: bool = False) -> None:
            nonlocal editor_calls
            editor_calls += 1
            Path(argv[-1]).write_text("a corrected reply")

        monkeypatch.setattr("scout.grading.service.subprocess.run", fake_run)

        validate_calls = 0
        real_validate = grading.validate_grade_envelope

        def flaky_validate(payload: object, posture: object) -> list[str]:
            nonlocal validate_calls
            validate_calls += 1
            if validate_calls == 1:
                return ["synthetic failure forcing a retry"]
            return real_validate(payload, posture)  # type: ignore[arg-type]

        monkeypatch.setattr("scout.grading.service.validate_grade_envelope", flaky_validate)

        # Dimensions + failure note are re-prompted on every retry of the
        # outer loop, so the input script repeats them; the editor and its
        # captured text are not, since they're gathered once beforehand.
        inputs = iter(["c", "f", "5", "too formal", "5", "too formal"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))  # type: ignore[attr-defined]

        review_scan(in_memory_state, scan_id)

        assert editor_calls == 1
        # 1 synthetic failure + 1 real pass inside review_scan's own retry
        # loop, plus 1 more from StateManager.save_grade's independent
        # persistence-time revalidation.
        assert validate_calls == 3
        assert captured[0].edited_text == "a corrected reply"

    def test_false_positive_fail_also_captures_edited_text(
        self, in_memory_state: StateManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scan_id, _post_id, _eval_id = self._setup_scan_with_eval(in_memory_state)
        captured = _capture_saved_grades(in_memory_state, monkeypatch)
        _fake_editor(replacement="a corrected reply", monkeypatch=monkeypatch)
        # p=false_positive (action is forced to fail, no accept/fail prompt)
        inputs = iter(["p", "5", "too formal"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))  # type: ignore[attr-defined]

        review_scan(in_memory_state, scan_id)

        assert captured[0].relevance_judgment == "false_positive"
        assert captured[0].action_judgment == "fail"
        assert captured[0].edited_text == "a corrected reply"

    def test_false_negative_fail_also_captures_edited_text(
        self, in_memory_state: StateManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scan_id, _post_id, _eval_id = self._setup_scan_with_eval(in_memory_state)
        captured = _capture_saved_grades(in_memory_state, monkeypatch)
        _fake_editor(replacement="a corrected reply", monkeypatch=monkeypatch)
        # n=false_negative (action is forced to fail, no accept/fail prompt)
        inputs = iter(["n", "5", "too formal"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))  # type: ignore[attr-defined]

        review_scan(in_memory_state, scan_id)

        assert captured[0].relevance_judgment == "false_negative"
        assert captured[0].action_judgment == "fail"
        assert captured[0].edited_text == "a corrected reply"

    def test_accept_never_prompts_the_editor(
        self, in_memory_state: StateManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scan_id, _post_id, _eval_id = self._setup_scan_with_eval(in_memory_state)
        captured = _capture_saved_grades(in_memory_state, monkeypatch)

        def fail_if_called(argv: list[str], check: bool = False) -> None:
            raise AssertionError("editor must not be invoked for an accept")

        monkeypatch.setattr("scout.grading.service.subprocess.run", fail_if_called)
        inputs = iter(["c", "a"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))  # type: ignore[attr-defined]

        review_scan(in_memory_state, scan_id)

        assert captured[0].edited_text is None


class TestSignalFormatting:
    def test_grading_signals_empty(self) -> None:
        sig = GradingSignal(
            total_graded=0,
            pass_count=0,
            false_positive_count=0,
            false_negative_count=0,
            fail_count=0,
            dimension_counts=(),
            posture_correction_count=0,
            factual_unsupported_count=0,
            factual_contradicted_count=0,
            recent_causal_examples=(),
        )
        assert format_grading_signals(sig) == ""

    def test_grading_signals_with_failures(self) -> None:
        sig = GradingSignal(
            total_graded=10,
            pass_count=7,
            false_positive_count=1,
            false_negative_count=1,
            fail_count=3,
            dimension_counts=(("tone", 2), ("posture", 1)),
            posture_correction_count=1,
            factual_unsupported_count=0,
            factual_contradicted_count=1,
            recent_causal_examples=("overly formal", "wrong posture"),
        )
        text = format_grading_signals(sig)
        assert "7 of 10 passed" in text
        assert "tone" in text
        assert "posture correction" in text
        assert "contradicted" in text
        assert "overly formal" in text
