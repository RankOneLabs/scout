"""Tests for EvaluationStore: relevance evaluations, phase runs,
experiments, drafts, surfaced events, critiques, gate blocks, and
evaluation-feedback snapshots."""

from __future__ import annotations

import json
import pathlib
import sqlite3
from datetime import UTC, datetime

import pytest

from scout.config import GradeRecord, Message, RelevanceResult
from scout.storage.evaluations import ExperimentCASError, PhaseRunLinkageError
from scout.storage.schema import LATEST_SCHEMA_VERSION
from scout.storage.state import StateManager
from scout.verifier import GateViolation
from tests.conftest import seed_phase_run_contributors
from tests.legacy_schema_fixtures import build_legacy_conn_at_version, schema_snapshot


def _make_discord_msg(platform_id: str = "m1") -> Message:
    return Message(
        platform="discord",
        platform_id=platform_id,
        channel_name="general",
        channel_id="c1",
        author_name="bob",
        author_id="u1",
        content="hello",
        created_at=datetime.now(UTC),
    )


def _make_relevance(msg: Message, relevant: bool = True) -> RelevanceResult:
    return RelevanceResult(
        message=msg,
        relevant=relevant,
        score=0.85,
        reason="fits topic",
        relevant_to=("gateway",),
    )



class TestCritiqueFeedback:
    def test_returns_lessons(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()

        msg = Message(
            platform="test",
            platform_id="m1",
            channel_name="ch",
            channel_id="c1",
            author_name="a",
            author_id="u1",
            content="content",
            created_at=datetime.now(UTC),
        )
        post_id = in_memory_state.save_post(msg, scan_id)

        result = RelevanceResult(
            message=msg,
            relevant=True,
            score=0.9,
            reason="test",
            relevant_to=("gateway",),
        )
        eval_id = in_memory_state.save_evaluation(result, post_id, scan_id)
        draft_id = in_memory_state.save_draft(
            post_id,
            eval_id,
            "gateway",
            "Check out our tool!",
            scan_id,
        )
        in_memory_state.save_critique(draft_id, "revise", "Too promotional", scan_id)

        lessons = in_memory_state.get_recent_critique_feedback(limit=5)
        assert len(lessons) == 1
        assert lessons[0].verdict == "revise"
        assert lessons[0].feedback == "Too promotional"
        assert lessons[0].comment_text == "Check out our tool!"

    def test_empty_when_no_critiques(self, in_memory_state: StateManager) -> None:
        lessons = in_memory_state.get_recent_critique_feedback()
        assert lessons == []

class TestEvaluationPersistence:
    def test_save_evaluation_persists_keyword_route_id(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()
        now = datetime.now(UTC).isoformat()
        in_memory_state.conn.execute(
            "INSERT INTO projects (key, name, description, link, active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?)",
            ("gw", "Gateway", "Desc", "https://example.com", now, now),
        )
        in_memory_state.conn.execute(
            "INSERT INTO project_keywords "
            "(id, project_key, keyword, active, priority, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, 100, ?, ?)",
            (7, "gw", "gateway", now, now),
        )
        msg = Message(
            platform="test",
            platform_id="route-1",
            channel_name="ch",
            channel_id="c1",
            author_name="a",
            author_id="u1",
            content="content",
            created_at=datetime.now(UTC),
        )
        post_id = in_memory_state.save_post(msg, scan_id)
        result = RelevanceResult(
            message=msg,
            relevant=True,
            score=0.9,
            reason="test",
            relevant_to=("gateway",),
        )

        eval_id = in_memory_state.save_evaluation(
            result,
            post_id,
            scan_id,
            keyword_route_id=7,
        )

        row = in_memory_state.conn.execute(
            "SELECT keyword_route_id FROM evaluations WHERE id = ?",
            (eval_id,),
        ).fetchone()
        assert row is not None
        assert row["keyword_route_id"] == 7

class TestGetLatestEvaluationId:
    def test_returns_none_when_no_evaluation(
        self,
        in_memory_state: StateManager,
        sample_message: Message,
    ) -> None:
        scan_id = in_memory_state.start_scan()
        post_id = in_memory_state.save_post(sample_message, scan_id)

        assert in_memory_state.get_latest_evaluation_id(post_id, scan_id) is None

    def test_returns_most_recent_evaluation_id_for_post_in_scan(
        self,
        in_memory_state: StateManager,
        sample_message: Message,
        sample_relevance_result: RelevanceResult,
    ) -> None:
        scan_id = in_memory_state.start_scan()
        post_id = in_memory_state.save_post(sample_message, scan_id)
        in_memory_state.save_evaluation(sample_relevance_result, post_id, scan_id)
        second_eval_id = in_memory_state.save_evaluation(sample_relevance_result, post_id, scan_id)

        assert in_memory_state.get_latest_evaluation_id(post_id, scan_id) == second_eval_id

class TestGateBlocksWriter:
    """StateManager._save_gate_violations is the sole gate_blocks writer.

    See S-011: one writer eliminates column drift and guarantees every live
    block is linked to its full project, dossier, scan, post, and evaluation
    context.
    """

    def test_persist_terminal_outcome_writes_full_context_gate_block(
        self, in_memory_state: StateManager
    ) -> None:
        """A content-blocked terminal outcome's gate_blocks row carries every
        column: reason, offending text, segment index, project/dossier
        identity, scan/post/evaluation linkage, platform context, timestamp.
        """
        scan_id = in_memory_state.start_scan()
        msg = _make_discord_msg("gate-writer-1")
        post_id = in_memory_state.save_post(msg, scan_id)
        in_memory_state.commit()
        result = _make_relevance(msg)

        violation = GateViolation(
            reason_code="prohibitions",
            offending_text="buy our product now",
            segment_index=2,
        )
        contributor_ids = seed_phase_run_contributors(in_memory_state, scan_id, post_id)
        eval_id = in_memory_state.persist_terminal_outcome(
            result,
            post_id,
            scan_id,
            surface_status="gate_blocked",
            contributor_phase_run_ids=contributor_ids,
            project_key="gateway",
            posture="answer",
            dossier_revision="rev-9",
            dossier_summary_id="gw-dossier",
            gate_violations=[violation],
        )

        rows = in_memory_state.conn.execute(
            "SELECT * FROM gate_blocks WHERE evaluation_id = ?", (eval_id,)
        ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["reason_code"] == "prohibitions"
        assert row["offending_text"] == "buy our product now"
        assert row["segment_index"] == 2
        assert row["project_key"] == "gateway"
        assert row["dossier_summary_id"] == "gw-dossier"
        assert row["dossier_revision"] == "rev-9"
        assert row["scan_id"] == scan_id
        assert row["post_id"] == post_id
        assert row["evaluation_id"] == eval_id
        assert row["context"] == f"{msg.platform}:{msg.platform_id}"
        assert row["created_at"] is not None

    def test_persist_terminal_outcome_writes_one_row_per_violation(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id = in_memory_state.start_scan()
        msg = _make_discord_msg("gate-writer-2")
        post_id = in_memory_state.save_post(msg, scan_id)
        in_memory_state.commit()
        result = _make_relevance(msg)

        violations = [
            GateViolation(reason_code="fact_ids", offending_text="bad-fact", segment_index=0),
            GateViolation(
                reason_code="url_allowlist",
                offending_text="https://evil.example",
                segment_index=None,
            ),
        ]
        contributor_ids = seed_phase_run_contributors(in_memory_state, scan_id, post_id)
        eval_id = in_memory_state.persist_terminal_outcome(
            result,
            post_id,
            scan_id,
            surface_status="gate_blocked",
            contributor_phase_run_ids=contributor_ids,
            project_key="gateway",
            gate_violations=violations,
        )

        rows = in_memory_state.conn.execute(
            "SELECT reason_code FROM gate_blocks WHERE evaluation_id = ? ORDER BY id", (eval_id,)
        ).fetchall()
        assert [r["reason_code"] for r in rows] == ["fact_ids", "url_allowlist"]

    def test_insert_into_gate_blocks_has_a_single_production_writer(self) -> None:
        """A source-search regression check: verifier.py's own gate_blocks
        writer was removed, so EvaluationStore._save_gate_violations must be
        the only place production code issues INSERT INTO gate_blocks.

        Scans recursively (not just the repo root) so a writer added in any
        subdirectory — scripts/, prompts/, etc. — would also be caught.
        """
        repo_root = pathlib.Path(__file__).resolve().parent.parent
        excluded_dirs = {"tests", ".venv", "web", ".git"}

        def _is_production_file(path: pathlib.Path) -> bool:
            rel_parts = path.relative_to(repo_root).parts[:-1]
            return not any(part in excluded_dirs for part in rel_parts)

        production_files = sorted(p for p in repo_root.rglob("*.py") if _is_production_file(p))
        assert production_files, "expected to find production .py files in the repo"

        hits = [
            str(p.relative_to(repo_root))
            for p in production_files
            if "INSERT INTO gate_blocks" in p.read_text()
        ]
        assert hits == ["src/scout/storage/evaluations.py"]

class TestDossierGroundedFields:
    def test_save_evaluation_with_surface_status_and_project_key(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id = in_memory_state.start_scan()
        msg = _make_discord_msg()
        post_id = in_memory_state.save_post(msg, scan_id)
        result = _make_relevance(msg)

        eval_id = in_memory_state.save_evaluation(
            result,
            post_id,
            scan_id,
            project_key="gateway",
            posture="answer",
            surface_status="surfaced",
            dossier_revision="abc123",
            dossier_summary_id="summary-1",
        )

        row = in_memory_state.conn.execute(
            "SELECT project_key, posture, surface_status, dossier_revision, "
            "dossier_summary_id FROM evaluations WHERE id = ?",
            (eval_id,),
        ).fetchone()
        assert row is not None
        assert row["project_key"] == "gateway"
        assert row["posture"] == "answer"
        assert row["surface_status"] == "surfaced"
        assert row["dossier_revision"] == "abc123"
        assert row["dossier_summary_id"] == "summary-1"

    def test_save_draft_with_structured_output(self, in_memory_state: StateManager) -> None:
        import json

        scan_id = in_memory_state.start_scan()
        msg = _make_discord_msg()
        post_id = in_memory_state.save_post(msg, scan_id)
        result = _make_relevance(msg)
        eval_id = in_memory_state.save_evaluation(result, post_id, scan_id)

        structured = json.dumps(
            {"posture": "answer", "segments": [], "claims": [], "resources_used": []}
        )
        draft_id = in_memory_state.save_draft(
            post_id,
            eval_id,
            "gateway",
            "Hello world",
            scan_id,
            posture="answer",
            structured_output=structured,
            dossier_revision="abc123",
            dossier_summary_id="summary-1",
        )

        row = in_memory_state.conn.execute(
            "SELECT posture, structured_output, dossier_revision, dossier_summary_id "
            "FROM draft_comments WHERE id = ?",
            (draft_id,),
        ).fetchone()
        assert row is not None
        assert row["posture"] == "answer"
        assert json.loads(row["structured_output"])["posture"] == "answer"
        assert row["dossier_revision"] == "abc123"
        assert row["dossier_summary_id"] == "summary-1"

    def test_save_surfaced_event(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()
        msg = _make_discord_msg()
        post_id = in_memory_state.save_post(msg, scan_id)
        result = _make_relevance(msg)
        eval_id = in_memory_state.save_evaluation(result, post_id, scan_id)
        draft_id = in_memory_state.save_draft(post_id, eval_id, "gateway", "Draft text", scan_id)

        event_id = in_memory_state.save_surfaced_event(
            post_id=post_id,
            evaluation_id=eval_id,
            draft_id=draft_id,
            scan_id=scan_id,
            project_key="gateway",
            author_id="u1",
            platform="discord",
            surfaced_at="2025-01-01T12:00:00+00:00",
        )
        assert event_id >= 1

        row = in_memory_state.conn.execute(
            "SELECT platform, author_id, project_key, post_id, evaluation_id, draft_id "
            "FROM surfaced_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        assert row is not None
        assert row["platform"] == "discord"
        assert row["author_id"] == "u1"
        assert row["project_key"] == "gateway"
        assert row["post_id"] == post_id
        assert row["evaluation_id"] == eval_id
        assert row["draft_id"] == draft_id

class TestMigration27FeedbackSnapshots:
    """Migration 27 adds the immutable evaluation-feedback/v1 shadow
    snapshot tables: feedback_snapshots, feedback_snapshot_phases, and
    feedback_snapshot_items."""

    def test_fresh_db_has_tables_and_no_rows(self, in_memory_state: StateManager) -> None:
        assert (
            in_memory_state.conn.execute("PRAGMA user_version").fetchone()[0]
            == LATEST_SCHEMA_VERSION
        )
        for table in (
            "feedback_snapshots",
            "feedback_snapshot_phases",
            "feedback_snapshot_items",
        ):
            count = in_memory_state.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0

    def _seed_one_grade(self, state: StateManager) -> tuple[int, int]:
        scan_id = state.start_scan()
        msg = Message(
            platform="discord",
            platform_id="feedback-snap-1",
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="post",
            created_at=datetime.now(UTC),
        )
        post_id = state.save_post(msg, scan_id)
        result = RelevanceResult(
            message=msg,
            relevant=True,
            score=0.9,
            reason="relevant",
            relevant_to=("gateway",),
        )
        eval_id = state.save_evaluation(result, post_id, scan_id)
        state.save_grade(
            GradeRecord(
                post_id=post_id,
                evaluation_id=eval_id,
                scan_id=scan_id,
                source="cli",
                graded_at=datetime.now(UTC),
                relevance_judgment="correct",
                action_judgment="accept",
                schema_version=3,
            )
        )
        return scan_id, post_id

    def test_feedback_snapshots_rejects_update_and_delete(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, _post_id = self._seed_one_grade(in_memory_state)
        in_memory_state.record_feedback_snapshot(scan_id, mode="shadow")

        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "UPDATE feedback_snapshots SET mode = 'active' WHERE scan_id = ?",
                (scan_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "DELETE FROM feedback_snapshots WHERE scan_id = ?", (scan_id,)
            )

    def test_feedback_snapshot_phases_rejects_update_and_delete(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, _post_id = self._seed_one_grade(in_memory_state)
        in_memory_state.record_feedback_snapshot(scan_id, mode="shadow")
        phase_id = in_memory_state.conn.execute(
            "SELECT id FROM feedback_snapshot_phases LIMIT 1"
        ).fetchone()["id"]

        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "UPDATE feedback_snapshot_phases SET truncated = 1 WHERE id = ?",
                (phase_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "DELETE FROM feedback_snapshot_phases WHERE id = ?", (phase_id,)
            )

    def test_feedback_snapshot_items_rejects_update_and_delete(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, _post_id = self._seed_one_grade(in_memory_state)
        in_memory_state.record_feedback_snapshot(scan_id, mode="shadow")
        item_id = in_memory_state.conn.execute(
            "SELECT id FROM feedback_snapshot_items LIMIT 1"
        ).fetchone()["id"]

        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "UPDATE feedback_snapshot_items SET role = 'excluded' WHERE id = ?",
                (item_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "DELETE FROM feedback_snapshot_items WHERE id = ?", (item_id,)
            )

    def test_second_snapshot_for_same_scan_rejected_by_unique_constraint(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, _post_id = self._seed_one_grade(in_memory_state)
        in_memory_state.record_feedback_snapshot(scan_id, mode="shadow")

        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.record_feedback_snapshot(scan_id, mode="shadow")

        count = in_memory_state.conn.execute(
            "SELECT COUNT(*) FROM feedback_snapshots WHERE scan_id = ?", (scan_id,)
        ).fetchone()[0]
        assert count == 1

    def test_failure_after_partial_insert_rolls_back_whole_snapshot(
        self, in_memory_state: StateManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """persist_feedback_snapshot runs inside record_feedback_snapshot's
        single begin_immediate() transaction. If persistence fails after
        already inserting some rows, none of it must survive — a snapshot
        is only ever all-or-nothing."""
        import scout.storage.evaluations as evaluation_store_module

        scan_id, _post_id = self._seed_one_grade(in_memory_state)

        real_persist = evaluation_store_module.persist_feedback_snapshot

        def _fail_after_insert(*args: object, **kwargs: object) -> object:
            real_persist(*args, **kwargs)  # rows genuinely inserted this far
            raise RuntimeError("simulated insert failure")

        monkeypatch.setattr(
            evaluation_store_module, "persist_feedback_snapshot", _fail_after_insert
        )

        with pytest.raises(RuntimeError, match="simulated insert failure"):
            in_memory_state.record_feedback_snapshot(scan_id, mode="shadow")

        for table in (
            "feedback_snapshots",
            "feedback_snapshot_phases",
            "feedback_snapshot_items",
        ):
            count = in_memory_state.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0, f"{table} should be empty after rollback"

class TestMigration28FeedbackAuditFidelity:
    """Migration 28 makes revision schema identity and snapshot-selection
    reasons/ranks first-class immutable data instead of UI inference."""

    def _build_v27_database(self, db_path: str) -> None:
        conn = build_legacy_conn_at_version(db_path, 27)
        now = "2026-07-19T12:00:00.000Z"
        conn.execute("INSERT INTO scans (id, started_at) VALUES (1, ?)", (now,))
        conn.execute(
            "INSERT INTO posts (id, platform, platform_msg_id, content, scan_id) "
            "VALUES (1, 'discord', 'migration-28', 'post', 1)"
        )
        conn.execute(
            "INSERT INTO evaluations "
            "(id, post_id, relevant, score, scan_id, posture, surface_status) "
            "VALUES (1, 1, 1, 0.9, 1, 'answer', 'surfaced')"
        )
        conn.execute(
            "INSERT INTO grades "
            "(id, evaluation_id, post_id, scan_id, source, graded_at, "
            " relevance_judgment, action_judgment, schema_version, needs_regrade) "
            "VALUES (1, 1, 1, 1, 'web', ?, 'correct', 'accept', 2, 0)",
            (now,),
        )
        payload = json.dumps(
            {
                "id": 1,
                "evaluation_id": 1,
                "post_id": 1,
                "scan_id": 1,
                "graded_at": now,
                "schema_version": 2,
                "needs_regrade": 0,
                "relevance_judgment": "correct",
                "action_judgment": "accept",
            }
        )
        conn.execute(
            "INSERT INTO grade_revisions "
            "(id, grade_id, evaluation_id, revision, source, payload, recorded_at) "
            "VALUES (1, 1, 1, 1, 'web', ?, ?)",
            (payload, now),
        )
        conn.execute(
            "INSERT INTO feedback_snapshots "
            "(id, scan_id, policy_version, mode, as_of, lookback_days, max_grades, "
            " segment_min_grades, note_max_chars, relevance_token_budget, "
            " reply_draft_token_budget, critic_token_budget, population_count, "
            " eligible_count, excluded_count, created_at) "
            "VALUES (1, 1, 'evaluation-feedback/v1', 'shadow', ?, 90, 200, 5, "
            "240, 800, 800, 1000, 1, 1, 0, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO feedback_snapshot_phases "
            "(id, snapshot_id, phase, token_budget, token_estimate, truncated, "
            " structured_summary, rendered_text, rendered_sha256, created_at) "
            "VALUES (1, 1, 'relevance', 800, 1, 0, '{}', 'text', 'sha', ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO feedback_snapshot_items "
            "(id, snapshot_phase_id, grade_id, grade_revision_id, role, reason, created_at) "
            "VALUES (1, 1, 1, 1, 'aggregate', NULL, ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO feedback_snapshot_items "
            "(id, snapshot_phase_id, grade_id, grade_revision_id, role, reason, created_at) "
            "VALUES (2, 1, 1, 1, 'example', NULL, ?)",
            (now,),
        )
        conn.execute("PRAGMA user_version = 27")
        conn.commit()
        conn.close()

    def test_upgrades_v27_rows_with_explicit_selection_metadata(
        self, tmp_path: pathlib.Path
    ) -> None:
        db_path = str(tmp_path / "pre28.db")
        self._build_v27_database(db_path)

        with StateManager(db_path=db_path) as state:
            from scout.storage.migrations import _migrate_to_28

            _migrate_to_28(state.conn)
            _migrate_to_28(state.conn)
            revision = state.conn.execute(
                "SELECT schema_version FROM grade_revisions WHERE id = 1"
            ).fetchone()
            assert revision["schema_version"] == 2
            rows = state.conn.execute(
                "SELECT role, selection_reason, rank FROM feedback_snapshot_items ORDER BY id"
            ).fetchall()
            assert [tuple(row) for row in rows] == [
                ("aggregate", "phase_population", None),
                ("example", "selected_recent_note", 1),
            ]

    def test_database_rejects_invalid_json_and_selection_metadata(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id = in_memory_state.start_scan()
        msg = Message(
            platform="discord",
            platform_id="m28-invalid",
            channel_name="general",
            channel_id="ch",
            author_name="alice",
            author_id="u",
            content="post",
            created_at=datetime.now(UTC),
        )
        post_id = in_memory_state.save_post(msg, scan_id)
        evaluation_id = in_memory_state.save_evaluation(
            RelevanceResult(
                message=msg,
                relevant=True,
                score=0.9,
                reason="relevant",
                relevant_to=("gateway",),
            ),
            post_id,
            scan_id,
        )
        grade_id = in_memory_state.save_grade(
            GradeRecord(
                post_id=post_id,
                evaluation_id=evaluation_id,
                scan_id=scan_id,
                source="web",
                graded_at=datetime.now(UTC),
                relevance_judgment="correct",
                action_judgment="accept",
                schema_version=3,
            )
        )
        snapshot = in_memory_state.record_feedback_snapshot(scan_id, mode="shadow")
        phase_id = snapshot.phases[0].snapshot_phase_id

        with pytest.raises(sqlite3.IntegrityError, match="valid JSON"):
            in_memory_state.conn.execute(
                "INSERT INTO grade_revisions "
                "(grade_id, evaluation_id, revision, schema_version, source, payload, recorded_at) "
                "VALUES (?, ?, 99, 2, 'web', 'not-json', 'now')",
                (grade_id, evaluation_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="selection metadata"):
            in_memory_state.conn.execute(
                "INSERT INTO feedback_snapshot_items "
                "(snapshot_phase_id, grade_id, grade_revision_id, role, reason, "
                " selection_reason, rank, created_at) "
                "VALUES (?, ?, ?, 'excluded', 'test', '', NULL, 'now')",
                (phase_id, grade_id, in_memory_state.get_grade_revisions(grade_id)[0]["id"]),
            )

class TestMigration29EvaluationPhaseRuns:
    """Migration 29 adds evaluation_phase_runs: one row per relevance,
    reply_draft, or critic phase attempt, durably linking its AGENT_RUN
    Jig trace and (once an evaluation exists) the evaluation it
    contributed to."""

    def test_fresh_db_has_table_and_no_rows(self, in_memory_state: StateManager) -> None:
        assert (
            in_memory_state.conn.execute("PRAGMA user_version").fetchone()[0]
            == LATEST_SCHEMA_VERSION
        )
        count = in_memory_state.conn.execute(
            "SELECT COUNT(*) FROM evaluation_phase_runs"
        ).fetchone()[0]
        assert count == 0

    def _seed_scan_and_post(
        self, state: StateManager, platform_id: str = "phase-run-1"
    ) -> tuple[int, int]:
        scan_id = state.start_scan()
        msg = Message(
            platform="discord",
            platform_id=platform_id,
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="post",
            created_at=datetime.now(UTC),
        )
        post_id = state.save_post(msg, scan_id)
        return scan_id, post_id

    def _seed_scan_post_snapshot(
        self, state: StateManager, platform_id: str = "phase-run-1"
    ) -> tuple[int, int, dict[str, int]]:
        scan_id, post_id = self._seed_scan_and_post(state, platform_id)
        snapshot = state.record_feedback_snapshot(scan_id, mode="shadow")
        phase_by_name = {p.phase: p.snapshot_phase_id for p in snapshot.phases}
        return scan_id, post_id, phase_by_name

    def test_insert_and_get_phase_run_round_trip(self, in_memory_state: StateManager) -> None:
        scan_id, post_id, phase_by_name = self._seed_scan_post_snapshot(in_memory_state)

        phase_run_id = in_memory_state.insert_phase_run(
            scan_id=scan_id,
            post_id=post_id,
            snapshot_phase_id=phase_by_name["relevance"],
            phase="relevance",
            trace_id="trace-abc",
            model="claude-haiku-4-5-20251001",
            status="complete",
        )

        row = in_memory_state.get_phase_run(phase_run_id)
        assert row is not None
        assert row["scan_id"] == scan_id
        assert row["post_id"] == post_id
        assert row["evaluation_id"] is None
        assert row["snapshot_phase_id"] == phase_by_name["relevance"]
        assert row["phase"] == "relevance"
        assert row["trace_id"] == "trace-abc"
        assert row["model"] == "claude-haiku-4-5-20251001"
        assert row["status"] == "complete"
        assert row["created_at"] is not None

    def test_get_phase_run_missing_id_returns_none(self, in_memory_state: StateManager) -> None:
        assert in_memory_state.get_phase_run(999_999) is None

    def test_unknown_phase_or_status_rejected(self, in_memory_state: StateManager) -> None:
        scan_id, post_id, phase_by_name = self._seed_scan_post_snapshot(in_memory_state)
        with pytest.raises(ValueError, match="phase"):
            in_memory_state.insert_phase_run(
                scan_id=scan_id,
                post_id=post_id,
                snapshot_phase_id=phase_by_name["relevance"],
                phase="bogus",
                trace_id="t1",
                model="m",
                status="complete",
            )
        with pytest.raises(ValueError, match="status"):
            in_memory_state.insert_phase_run(
                scan_id=scan_id,
                post_id=post_id,
                snapshot_phase_id=phase_by_name["relevance"],
                phase="relevance",
                trace_id="t2",
                model="m",
                status="bogus",
            )

    def test_duplicate_trace_id_rejected(self, in_memory_state: StateManager) -> None:
        scan_id, post_id, phase_by_name = self._seed_scan_post_snapshot(in_memory_state)
        in_memory_state.insert_phase_run(
            scan_id=scan_id,
            post_id=post_id,
            snapshot_phase_id=phase_by_name["relevance"],
            phase="relevance",
            trace_id="dup-trace",
            model="m",
            status="complete",
        )
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.insert_phase_run(
                scan_id=scan_id,
                post_id=post_id,
                snapshot_phase_id=phase_by_name["reply_draft"],
                phase="reply_draft",
                trace_id="dup-trace",
                model="m",
                status="complete",
            )

    def test_rejects_delete(self, in_memory_state: StateManager) -> None:
        scan_id, post_id, phase_by_name = self._seed_scan_post_snapshot(in_memory_state)
        phase_run_id = in_memory_state.insert_phase_run(
            scan_id=scan_id,
            post_id=post_id,
            snapshot_phase_id=phase_by_name["relevance"],
            phase="relevance",
            trace_id="t-del",
            model="m",
            status="complete",
        )
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "DELETE FROM evaluation_phase_runs WHERE id = ?", (phase_run_id,)
            )

    def test_rejects_mutating_non_evaluation_columns(self, in_memory_state: StateManager) -> None:
        scan_id, post_id, phase_by_name = self._seed_scan_post_snapshot(in_memory_state)
        phase_run_id = in_memory_state.insert_phase_run(
            scan_id=scan_id,
            post_id=post_id,
            snapshot_phase_id=phase_by_name["relevance"],
            phase="relevance",
            trace_id="t-immutable",
            model="m",
            status="complete",
        )
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "UPDATE evaluation_phase_runs SET model = 'other-model' WHERE id = ?",
                (phase_run_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "UPDATE evaluation_phase_runs SET status = 'error' WHERE id = ?",
                (phase_run_id,),
            )

    def test_rejects_relinking_an_already_linked_row(self, in_memory_state: StateManager) -> None:
        scan_id, post_id, phase_by_name = self._seed_scan_post_snapshot(in_memory_state)
        phase_run_id = in_memory_state.insert_phase_run(
            scan_id=scan_id,
            post_id=post_id,
            snapshot_phase_id=phase_by_name["relevance"],
            phase="relevance",
            trace_id="t-relink",
            model="m",
            status="complete",
        )
        msg = Message(
            platform="discord",
            platform_id="phase-run-1",
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="post",
            created_at=datetime.now(UTC),
        )
        result = RelevanceResult(
            message=msg,
            relevant=False,
            score=0.1,
            reason="off-topic",
            relevant_to=(),
        )
        first_eval_id = in_memory_state.save_evaluation(
            result,
            post_id,
            scan_id,
            surface_status="not_relevant",
        )
        second_eval_id = in_memory_state.save_evaluation(
            result,
            post_id,
            scan_id,
            surface_status="not_relevant",
        )
        with in_memory_state.db.begin_immediate():
            in_memory_state.conn.execute(
                "UPDATE evaluation_phase_runs SET evaluation_id = ? WHERE id = ?",
                (first_eval_id, phase_run_id),
            )
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "UPDATE evaluation_phase_runs SET evaluation_id = ? WHERE id = ?",
                (second_eval_id, phase_run_id),
            )

    def test_persist_terminal_outcome_links_contributors(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, post_id = self._seed_scan_and_post(in_memory_state)
        contributor_ids = seed_phase_run_contributors(in_memory_state, scan_id, post_id, count=1)
        msg = Message(
            platform="discord",
            platform_id="phase-run-1",
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="post",
            created_at=datetime.now(UTC),
        )
        result = RelevanceResult(
            message=msg,
            relevant=False,
            score=0.1,
            reason="off-topic",
            relevant_to=(),
        )
        eval_id = in_memory_state.persist_terminal_outcome(
            result,
            post_id,
            scan_id,
            surface_status="not_relevant",
            contributor_phase_run_ids=contributor_ids,
        )
        row = in_memory_state.get_phase_run(contributor_ids[0])
        assert row is not None
        assert row["evaluation_id"] == eval_id

    def test_persist_terminal_outcome_rejects_empty_contributors(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, post_id, _ = self._seed_scan_post_snapshot(in_memory_state)
        msg = Message(
            platform="discord",
            platform_id="phase-run-1",
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="post",
            created_at=datetime.now(UTC),
        )
        result = RelevanceResult(
            message=msg,
            relevant=False,
            score=0.1,
            reason="off-topic",
            relevant_to=(),
        )
        with pytest.raises(PhaseRunLinkageError):
            in_memory_state.persist_terminal_outcome(
                result,
                post_id,
                scan_id,
                surface_status="not_relevant",
                contributor_phase_run_ids=(),
            )
        assert (
            in_memory_state.conn.execute(
                "SELECT COUNT(*) FROM evaluations WHERE post_id = ?", (post_id,)
            ).fetchone()[0]
            == 0
        )

    def test_persist_terminal_outcome_rejects_duplicate_contributors(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, post_id = self._seed_scan_and_post(in_memory_state)
        contributor_ids = seed_phase_run_contributors(in_memory_state, scan_id, post_id, count=1)
        msg = Message(
            platform="discord",
            platform_id="phase-run-1",
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="post",
            created_at=datetime.now(UTC),
        )
        result = RelevanceResult(
            message=msg,
            relevant=False,
            score=0.1,
            reason="off-topic",
            relevant_to=(),
        )
        with pytest.raises(PhaseRunLinkageError):
            in_memory_state.persist_terminal_outcome(
                result,
                post_id,
                scan_id,
                surface_status="not_relevant",
                contributor_phase_run_ids=(contributor_ids[0], contributor_ids[0]),
            )
        assert (
            in_memory_state.conn.execute(
                "SELECT COUNT(*) FROM evaluations WHERE post_id = ?", (post_id,)
            ).fetchone()[0]
            == 0
        )

    def test_persist_terminal_outcome_rejects_cross_post_contributor(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, post_id = self._seed_scan_and_post(in_memory_state)
        other_msg = Message(
            platform="discord",
            platform_id="phase-run-other",
            channel_name="general",
            channel_id="ch-1",
            author_name="bob",
            author_id="u2",
            content="other post",
            created_at=datetime.now(UTC),
        )
        other_post_id = in_memory_state.save_post(other_msg, scan_id)
        other_contributor_ids = seed_phase_run_contributors(
            in_memory_state, scan_id, other_post_id, count=1
        )

        msg = Message(
            platform="discord",
            platform_id="phase-run-1",
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="post",
            created_at=datetime.now(UTC),
        )
        result = RelevanceResult(
            message=msg,
            relevant=False,
            score=0.1,
            reason="off-topic",
            relevant_to=(),
        )
        with pytest.raises(PhaseRunLinkageError):
            in_memory_state.persist_terminal_outcome(
                result,
                post_id,
                scan_id,
                surface_status="not_relevant",
                contributor_phase_run_ids=other_contributor_ids,
            )
        assert (
            in_memory_state.conn.execute(
                "SELECT COUNT(*) FROM evaluations WHERE post_id = ?", (post_id,)
            ).fetchone()[0]
            == 0
        )
        # The other post's phase run remains unlinked — no partial link.
        assert in_memory_state.get_phase_run(other_contributor_ids[0])["evaluation_id"] is None

    def test_persist_terminal_outcome_rejects_wrong_phase_sequence(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, post_id, phase_by_name = self._seed_scan_post_snapshot(in_memory_state)
        # A lone reply_draft phase run supplied where position 0 must be
        # 'relevance' per PHASE_RUN_PHASE_ORDER.
        reply_draft_id = in_memory_state.insert_phase_run(
            scan_id=scan_id,
            post_id=post_id,
            snapshot_phase_id=phase_by_name["reply_draft"],
            phase="reply_draft",
            trace_id="t-seq",
            model="m",
            status="complete",
        )
        msg = Message(
            platform="discord",
            platform_id="phase-run-1",
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="post",
            created_at=datetime.now(UTC),
        )
        result = RelevanceResult(
            message=msg,
            relevant=False,
            score=0.1,
            reason="off-topic",
            relevant_to=(),
        )
        with pytest.raises(PhaseRunLinkageError):
            in_memory_state.persist_terminal_outcome(
                result,
                post_id,
                scan_id,
                surface_status="not_relevant",
                contributor_phase_run_ids=(reply_draft_id,),
            )
        assert (
            in_memory_state.conn.execute(
                "SELECT COUNT(*) FROM evaluations WHERE post_id = ?", (post_id,)
            ).fetchone()[0]
            == 0
        )

    def test_persist_terminal_outcome_rejects_already_linked_contributor(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, post_id = self._seed_scan_and_post(in_memory_state)
        contributor_ids = seed_phase_run_contributors(in_memory_state, scan_id, post_id, count=1)
        msg = Message(
            platform="discord",
            platform_id="phase-run-1",
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="post",
            created_at=datetime.now(UTC),
        )
        result = RelevanceResult(
            message=msg,
            relevant=False,
            score=0.1,
            reason="off-topic",
            relevant_to=(),
        )
        in_memory_state.persist_terminal_outcome(
            result,
            post_id,
            scan_id,
            surface_status="not_relevant",
            contributor_phase_run_ids=contributor_ids,
        )
        # A retry with the same (now-linked) contributor must not attach a
        # second evaluation to it.
        with pytest.raises(PhaseRunLinkageError):
            in_memory_state.persist_terminal_outcome(
                result,
                post_id,
                scan_id,
                surface_status="not_relevant",
                contributor_phase_run_ids=contributor_ids,
            )
        assert (
            in_memory_state.conn.execute(
                "SELECT COUNT(*) FROM evaluations WHERE post_id = ?", (post_id,)
            ).fetchone()[0]
            == 1
        )

    def test_persist_surfaced_outcome_links_contributors(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, post_id = self._seed_scan_and_post(in_memory_state)
        contributor_ids = seed_phase_run_contributors(in_memory_state, scan_id, post_id, count=3)
        msg = Message(
            platform="discord",
            platform_id="phase-run-1",
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="post",
            created_at=datetime.now(UTC),
        )
        result = RelevanceResult(
            message=msg,
            relevant=True,
            score=0.9,
            reason="fit",
            relevant_to=("gateway",),
        )
        evaluation_id, _draft_id, _event_id = in_memory_state.persist_surfaced_outcome(
            result,
            post_id,
            scan_id,
            project_key="gateway",
            author_id="u1",
            platform="discord",
            comment_text="hello",
            structured_output="{}",
            contributor_phase_run_ids=contributor_ids,
        )
        for phase_run_id in contributor_ids:
            row = in_memory_state.get_phase_run(phase_run_id)
            assert row["evaluation_id"] == evaluation_id

    def test_historical_phase_run_lookup_is_none_when_unavailable(
        self, in_memory_state: StateManager
    ) -> None:
        """An evaluation created before phase-run linkage existed (or by any
        path that predates this table) has no evaluation_phase_runs rows —
        an explicit absent state, never a fuzzy or inferred trace link."""
        scan_id, post_id, _ = self._seed_scan_post_snapshot(in_memory_state)
        msg = Message(
            platform="discord",
            platform_id="phase-run-1",
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="post",
            created_at=datetime.now(UTC),
        )
        result = RelevanceResult(
            message=msg,
            relevant=True,
            score=0.9,
            reason="fit",
            relevant_to=("gateway",),
        )
        eval_id = in_memory_state.save_evaluation(
            result,
            post_id,
            scan_id,
            surface_status="surfaced",
        )
        rows = in_memory_state.conn.execute(
            "SELECT id FROM evaluation_phase_runs WHERE evaluation_id = ?", (eval_id,)
        ).fetchall()
        assert rows == []

class TestExperimentRunsAndAttempts:
    """CAS lifecycle, retry/supersession, parent-status projection, and
    immutability contract for experiment_runs and its evaluation_experiments
    attempt children (v36, the versioned replay-evidence domain)."""

    def _seed_phase_run(
        self,
        state: StateManager,
        *,
        trace_id: str = "baseline-trace-1",
        platform_id: str = "exp-baseline",
    ) -> int:
        scan_id = state.start_scan()
        msg = Message(
            platform="discord",
            platform_id=platform_id,
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="post",
            created_at=datetime.now(UTC),
        )
        post_id = state.save_post(msg, scan_id)
        snapshot = state.record_feedback_snapshot(scan_id, mode="shadow")
        phase_by_name = {p.phase: p.snapshot_phase_id for p in snapshot.phases}
        return state.insert_phase_run(
            scan_id=scan_id,
            post_id=post_id,
            snapshot_phase_id=phase_by_name["relevance"],
            phase="relevance",
            trace_id=trace_id,
            model="claude-haiku-4-5-20251001",
            status="complete",
        )

    def test_create_experiment_run_starts_queued(self, in_memory_state: StateManager) -> None:
        run_id = in_memory_state.create_experiment_run(
            name="try-new-model",
            candidate_config="{}",
        )
        row = in_memory_state.get_experiment_run(run_id)
        assert row is not None
        assert row["name"] == "try-new-model"
        assert row["status"] == "queued"
        assert row["candidate_config"] == "{}"
        assert row["completed_at"] is None

    def test_create_experiment_run_rejects_blank_name(self, in_memory_state: StateManager) -> None:
        with pytest.raises(ValueError, match="blank"):
            in_memory_state.create_experiment_run(name="   ", candidate_config="{}")

    def test_create_experiment_run_rejects_invalid_json_config(
        self, in_memory_state: StateManager
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.create_experiment_run(name="bad-json", candidate_config="not json")

    def test_get_experiment_run_missing_id_returns_none(
        self, in_memory_state: StateManager
    ) -> None:
        assert in_memory_state.get_experiment_run(999_999) is None

    def test_insert_experiment_attempt_starts_queued_attempt_one(
        self, in_memory_state: StateManager
    ) -> None:
        phase_run_id = self._seed_phase_run(in_memory_state)
        run_id = in_memory_state.create_experiment_run(name="r", candidate_config="{}")
        experiment_id = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_run_id,
            baseline_evidence="{}",
        )
        row = in_memory_state.get_experiment(experiment_id)
        assert row is not None
        assert row["experiment_run_id"] == run_id
        assert row["phase_run_id"] == phase_run_id
        assert row["attempt_number"] == 1
        assert row["supersedes_experiment_id"] is None
        assert row["status"] == "queued"
        assert row["baseline_evidence"] == "{}"
        assert row["candidate_trace_id"] is None
        assert row["candidate_llm_call_count"] is None
        assert row["candidate_cost"] is None
        assert row["error_detail"] is None
        assert row["completed_at"] is None
        # The single attempt is 'queued' and so is the run's projection —
        # see TestExperimentRunStatusProjection for the full matrix.
        assert in_memory_state.get_experiment_run(run_id)["status"] == "queued"

    def test_insert_experiment_attempt_rejects_missing_run(
        self, in_memory_state: StateManager
    ) -> None:
        phase_run_id = self._seed_phase_run(in_memory_state)
        with pytest.raises(ExperimentCASError, match="no experiment_runs"):
            in_memory_state.insert_experiment_attempt(
                experiment_run_id=999_999,
                phase_run_id=phase_run_id,
                baseline_evidence="{}",
            )

    def test_insert_experiment_attempt_rejects_invalid_json_evidence(
        self, in_memory_state: StateManager
    ) -> None:
        phase_run_id = self._seed_phase_run(in_memory_state)
        run_id = in_memory_state.create_experiment_run(name="r", candidate_config="{}")
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.insert_experiment_attempt(
                experiment_run_id=run_id,
                phase_run_id=phase_run_id,
                baseline_evidence="not json",
            )

    def test_second_first_attempt_for_same_baseline_rejected(
        self, in_memory_state: StateManager
    ) -> None:
        """A baseline case may only ever get one supersedes_experiment_id=None
        attempt — a second one (without naming the case's current latest
        attempt to retry) is always a caller bug, not a valid retry."""
        phase_run_id = self._seed_phase_run(in_memory_state)
        run_id = in_memory_state.create_experiment_run(name="r", candidate_config="{}")
        in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_run_id,
            baseline_evidence="{}",
        )
        with pytest.raises(ExperimentCASError, match="already has an attempt"):
            in_memory_state.insert_experiment_attempt(
                experiment_run_id=run_id,
                phase_run_id=phase_run_id,
                baseline_evidence="{}",
            )

    def test_retry_requires_supersedes_to_match_actual_latest_attempt(
        self, in_memory_state: StateManager
    ) -> None:
        phase_run_id = self._seed_phase_run(in_memory_state)
        run_id = in_memory_state.create_experiment_run(name="r", candidate_config="{}")
        first_id = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_run_id,
            baseline_evidence="{}",
        )
        in_memory_state.cas_experiment_to_running(first_id)
        in_memory_state.fail_experiment(first_id, error_detail="boom")

        with pytest.raises(ExperimentCASError, match="not the latest attempt"):
            in_memory_state.insert_experiment_attempt(
                experiment_run_id=run_id,
                phase_run_id=phase_run_id,
                baseline_evidence="{}",
                supersedes_experiment_id=999_999,
            )

    def test_retry_chains_attempt_number_and_supersedes(
        self, in_memory_state: StateManager
    ) -> None:
        phase_run_id = self._seed_phase_run(in_memory_state)
        run_id = in_memory_state.create_experiment_run(name="r", candidate_config="{}")
        first_id = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_run_id,
            baseline_evidence="{}",
        )
        in_memory_state.cas_experiment_to_running(first_id)
        in_memory_state.fail_experiment(first_id, error_detail="transient")

        retry_id = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_run_id,
            baseline_evidence="{}",
            supersedes_experiment_id=first_id,
        )
        retry_row = in_memory_state.get_experiment(retry_id)
        assert retry_row["attempt_number"] == 2
        assert retry_row["supersedes_experiment_id"] == first_id
        assert retry_row["status"] == "queued"

        # The superseded attempt itself is untouched — old evidence stays put.
        first_row = in_memory_state.get_experiment(first_id)
        assert first_row["status"] == "failed"
        assert first_row["error_detail"] == "transient"

    def test_retry_rejected_while_prior_attempt_still_non_terminal(
        self, in_memory_state: StateManager
    ) -> None:
        phase_run_id = self._seed_phase_run(in_memory_state)
        run_id = in_memory_state.create_experiment_run(name="r", candidate_config="{}")
        first_id = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_run_id,
            baseline_evidence="{}",
        )
        in_memory_state.cas_experiment_to_running(first_id)
        with pytest.raises(ExperimentCASError, match="still 'running'"):
            in_memory_state.insert_experiment_attempt(
                experiment_run_id=run_id,
                phase_run_id=phase_run_id,
                baseline_evidence="{}",
                supersedes_experiment_id=first_id,
            )

    def test_list_experiment_attempts_orders_by_baseline_then_attempt_number(
        self, in_memory_state: StateManager
    ) -> None:
        phase_run_a = self._seed_phase_run(
            in_memory_state, trace_id="trace-a", platform_id="post-a"
        )
        phase_run_b = self._seed_phase_run(
            in_memory_state, trace_id="trace-b", platform_id="post-b"
        )
        run_id = in_memory_state.create_experiment_run(name="batch", candidate_config="{}")

        b1 = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_run_b,
            baseline_evidence="{}",
        )
        a1 = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_run_a,
            baseline_evidence="{}",
        )
        in_memory_state.cas_experiment_to_running(a1)
        in_memory_state.fail_experiment(a1, error_detail="x")
        a2 = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_run_a,
            baseline_evidence="{}",
            supersedes_experiment_id=a1,
        )

        attempts = in_memory_state.list_experiment_attempts(run_id)
        assert [row["id"] for row in attempts] == [a1, a2, b1]

    def test_full_lifecycle_queued_running_complete(self, in_memory_state: StateManager) -> None:
        phase_run_id = self._seed_phase_run(in_memory_state)
        run_id = in_memory_state.create_experiment_run(name="full-run", candidate_config="{}")
        experiment_id = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_run_id,
            baseline_evidence="{}",
        )
        in_memory_state.cas_experiment_to_running(experiment_id)
        assert in_memory_state.get_experiment(experiment_id)["status"] == "running"

        in_memory_state.record_candidate_trace(
            experiment_id,
            candidate_trace_id="candidate-trace-1",
            candidate_llm_call_count=1,
            candidate_cost=0.002,
        )
        mid = in_memory_state.get_experiment(experiment_id)
        assert mid["status"] == "running"
        assert mid["candidate_trace_id"] == "candidate-trace-1"
        assert mid["candidate_llm_call_count"] == 1
        assert mid["candidate_cost"] == 0.002

        in_memory_state.complete_experiment_with_comparison(
            experiment_id,
            jig_revision="4fae89bb04768d57be6db4cd2bdef859d1e17322",
            trace_diff=json.dumps(
                {"trace_a_id": "baseline-trace-1", "trace_b_id": "candidate-trace-1"}
            ),
            domain_diff=json.dumps({"additions": [], "removals": [], "changes": []}),
            score_evidence=json.dumps({"grader_attached": True, "delta": -0.1}),
        )
        final = in_memory_state.get_experiment(experiment_id)
        assert final["status"] == "complete"
        assert final["completed_at"] is not None

        comparison = in_memory_state.get_trace_comparison(experiment_id)
        assert comparison is not None
        assert comparison["trace_a_id"] == "baseline-trace-1"
        assert comparison["trace_b_id"] == "candidate-trace-1"
        assert comparison["jig_revision"] == "4fae89bb04768d57be6db4cd2bdef859d1e17322"
        assert json.loads(comparison["trace_diff"])["trace_b_id"] == "candidate-trace-1"
        assert json.loads(comparison["score_evidence"])["delta"] == -0.1

        assert in_memory_state.get_experiment_run(run_id)["status"] == "complete"

    def test_complete_without_score_evidence_leaves_it_null(
        self, in_memory_state: StateManager
    ) -> None:
        phase_run_id = self._seed_phase_run(in_memory_state)
        run_id = in_memory_state.create_experiment_run(name="ungraded", candidate_config="{}")
        experiment_id = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_run_id,
            baseline_evidence="{}",
        )
        in_memory_state.cas_experiment_to_running(experiment_id)
        in_memory_state.record_candidate_trace(
            experiment_id,
            candidate_trace_id="c1",
            candidate_llm_call_count=1,
            candidate_cost=0.0,
        )
        in_memory_state.complete_experiment_with_comparison(
            experiment_id,
            jig_revision="abc",
            trace_diff=json.dumps({"trace_a_id": "baseline-trace-1", "trace_b_id": "c1"}),
            domain_diff="{}",
        )
        assert in_memory_state.get_trace_comparison(experiment_id)["score_evidence"] is None

    def test_full_lifecycle_queued_running_failed(self, in_memory_state: StateManager) -> None:
        phase_run_id = self._seed_phase_run(in_memory_state)
        run_id = in_memory_state.create_experiment_run(name="will-fail", candidate_config="{}")
        experiment_id = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_run_id,
            baseline_evidence="{}",
        )
        in_memory_state.cas_experiment_to_running(experiment_id)
        in_memory_state.fail_experiment(experiment_id, error_detail="candidate execution failed")

        row = in_memory_state.get_experiment(experiment_id)
        assert row["status"] == "failed"
        assert row["error_detail"] == "candidate execution failed"
        assert row["completed_at"] is not None
        assert row["candidate_trace_id"] is None
        assert in_memory_state.get_trace_comparison(experiment_id) is None
        assert in_memory_state.get_experiment_run(run_id)["status"] == "failed"

    def test_failure_after_candidate_trace_retains_candidate_fields(
        self, in_memory_state: StateManager
    ) -> None:
        """A diff/serialization failure after the candidate trace was
        already persisted must retain that evidence — never invent a
        fresh candidate identity, and never insert a partial comparison."""
        phase_run_id = self._seed_phase_run(in_memory_state)
        run_id = in_memory_state.create_experiment_run(
            name="partial-failure", candidate_config="{}"
        )
        experiment_id = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_run_id,
            baseline_evidence="{}",
        )
        in_memory_state.cas_experiment_to_running(experiment_id)
        in_memory_state.record_candidate_trace(
            experiment_id,
            candidate_trace_id="candidate-trace-2",
            candidate_llm_call_count=2,
            candidate_cost=None,
        )
        in_memory_state.fail_experiment(experiment_id, error_detail="diff serialization failed")

        row = in_memory_state.get_experiment(experiment_id)
        assert row["status"] == "failed"
        assert row["candidate_trace_id"] == "candidate-trace-2"
        assert row["candidate_llm_call_count"] == 2
        assert row["candidate_cost"] is None
        assert in_memory_state.get_trace_comparison(experiment_id) is None

    def test_cas_to_running_rejects_non_queued(self, in_memory_state: StateManager) -> None:
        phase_run_id = self._seed_phase_run(in_memory_state)
        run_id = in_memory_state.create_experiment_run(name="double-start", candidate_config="{}")
        experiment_id = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_run_id,
            baseline_evidence="{}",
        )
        in_memory_state.cas_experiment_to_running(experiment_id)
        with pytest.raises(ExperimentCASError):
            in_memory_state.cas_experiment_to_running(experiment_id)

    def test_record_candidate_trace_rejects_when_not_running(
        self, in_memory_state: StateManager
    ) -> None:
        phase_run_id = self._seed_phase_run(in_memory_state)
        run_id = in_memory_state.create_experiment_run(name="not-running", candidate_config="{}")
        experiment_id = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_run_id,
            baseline_evidence="{}",
        )
        with pytest.raises(ExperimentCASError):
            in_memory_state.record_candidate_trace(
                experiment_id,
                candidate_trace_id="c1",
                candidate_llm_call_count=1,
                candidate_cost=0.0,
            )

    def test_record_candidate_trace_rejects_second_write(
        self, in_memory_state: StateManager
    ) -> None:
        phase_run_id = self._seed_phase_run(in_memory_state)
        run_id = in_memory_state.create_experiment_run(name="one-shot", candidate_config="{}")
        experiment_id = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_run_id,
            baseline_evidence="{}",
        )
        in_memory_state.cas_experiment_to_running(experiment_id)
        in_memory_state.record_candidate_trace(
            experiment_id,
            candidate_trace_id="c1",
            candidate_llm_call_count=1,
            candidate_cost=0.0,
        )
        with pytest.raises(ExperimentCASError):
            in_memory_state.record_candidate_trace(
                experiment_id,
                candidate_trace_id="c2",
                candidate_llm_call_count=1,
                candidate_cost=0.0,
            )
        assert in_memory_state.get_experiment(experiment_id)["candidate_trace_id"] == "c1"

    def test_complete_rejects_without_candidate_trace(self, in_memory_state: StateManager) -> None:
        phase_run_id = self._seed_phase_run(in_memory_state)
        run_id = in_memory_state.create_experiment_run(
            name="premature-complete", candidate_config="{}"
        )
        experiment_id = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_run_id,
            baseline_evidence="{}",
        )
        in_memory_state.cas_experiment_to_running(experiment_id)
        with pytest.raises(ExperimentCASError):
            in_memory_state.complete_experiment_with_comparison(
                experiment_id,
                jig_revision="abc",
                trace_diff=json.dumps(
                    {"trace_a_id": "baseline-trace-1", "trace_b_id": "candidate-trace-1"}
                ),
                domain_diff="{}",
            )
        assert in_memory_state.get_trace_comparison(experiment_id) is None

    def test_fail_rejects_when_not_running(self, in_memory_state: StateManager) -> None:
        phase_run_id = self._seed_phase_run(in_memory_state)
        run_id = in_memory_state.create_experiment_run(name="never-started", candidate_config="{}")
        experiment_id = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_run_id,
            baseline_evidence="{}",
        )
        with pytest.raises(ExperimentCASError):
            in_memory_state.fail_experiment(experiment_id, error_detail="oops")

    def test_terminal_experiment_rejects_further_updates(
        self, in_memory_state: StateManager
    ) -> None:
        phase_run_id = self._seed_phase_run(in_memory_state)
        run_id = in_memory_state.create_experiment_run(name="terminal", candidate_config="{}")
        experiment_id = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_run_id,
            baseline_evidence="{}",
        )
        in_memory_state.cas_experiment_to_running(experiment_id)
        in_memory_state.fail_experiment(experiment_id, error_detail="first failure")
        with pytest.raises(ExperimentCASError):
            in_memory_state.fail_experiment(experiment_id, error_detail="second failure")
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "UPDATE evaluation_experiments SET baseline_evidence = '{\"x\":1}' WHERE id = ?",
                (experiment_id,),
            )

    def test_evaluation_experiments_rejects_delete(self, in_memory_state: StateManager) -> None:
        phase_run_id = self._seed_phase_run(in_memory_state)
        run_id = in_memory_state.create_experiment_run(name="undeletable", candidate_config="{}")
        experiment_id = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_run_id,
            baseline_evidence="{}",
        )
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "DELETE FROM evaluation_experiments WHERE id = ?", (experiment_id,)
            )

    def test_experiment_runs_rejects_delete(self, in_memory_state: StateManager) -> None:
        run_id = in_memory_state.create_experiment_run(
            name="undeletable-run", candidate_config="{}"
        )
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute("DELETE FROM experiment_runs WHERE id = ?", (run_id,))

    def test_experiment_runs_rejects_identity_change(self, in_memory_state: StateManager) -> None:
        run_id = in_memory_state.create_experiment_run(name="fixed", candidate_config="{}")
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "UPDATE experiment_runs SET name = 'renamed' WHERE id = ?", (run_id,)
            )
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "UPDATE experiment_runs SET candidate_config = '{\"changed\":1}' WHERE id = ?",
                (run_id,),
            )

    def test_trace_comparisons_rejects_update_and_delete(
        self, in_memory_state: StateManager
    ) -> None:
        phase_run_id = self._seed_phase_run(in_memory_state)
        run_id = in_memory_state.create_experiment_run(
            name="immutable-comparison", candidate_config="{}"
        )
        experiment_id = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_run_id,
            baseline_evidence="{}",
        )
        in_memory_state.cas_experiment_to_running(experiment_id)
        in_memory_state.record_candidate_trace(
            experiment_id,
            candidate_trace_id="c1",
            candidate_llm_call_count=1,
            candidate_cost=0.0,
        )
        in_memory_state.complete_experiment_with_comparison(
            experiment_id,
            jig_revision="abc",
            trace_diff=json.dumps({"trace_a_id": "baseline-trace-1", "trace_b_id": "c1"}),
            domain_diff="{}",
        )
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "UPDATE trace_comparisons SET jig_revision = 'xyz' WHERE experiment_id = ?",
                (experiment_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "DELETE FROM trace_comparisons WHERE experiment_id = ?", (experiment_id,)
            )

    def test_trace_comparisons_rejects_second_comparison_for_same_experiment(
        self, in_memory_state: StateManager
    ) -> None:
        phase_run_id = self._seed_phase_run(in_memory_state)
        run_id = in_memory_state.create_experiment_run(name="one-comparison", candidate_config="{}")
        experiment_id = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_run_id,
            baseline_evidence="{}",
        )
        in_memory_state.cas_experiment_to_running(experiment_id)
        in_memory_state.record_candidate_trace(
            experiment_id,
            candidate_trace_id="c1",
            candidate_llm_call_count=1,
            candidate_cost=0.0,
        )
        in_memory_state.complete_experiment_with_comparison(
            experiment_id,
            jig_revision="abc",
            trace_diff=json.dumps({"trace_a_id": "baseline-trace-1", "trace_b_id": "c1"}),
            domain_diff="{}",
        )
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "INSERT INTO trace_comparisons "
                "(experiment_id, trace_a_id, trace_b_id, jig_revision, "
                "trace_diff, domain_diff, created_at) "
                "VALUES (?, 'baseline-trace-1', 'c1', 'abc', "
                '\'{"trace_a_id":"baseline-trace-1","trace_b_id":"c1"}\', '
                "'{}', datetime('now'))",
                (experiment_id,),
            )

    def test_trace_comparison_rejects_mismatched_trace_identity(
        self, in_memory_state: StateManager
    ) -> None:
        phase_run_id = self._seed_phase_run(in_memory_state)
        run_id = in_memory_state.create_experiment_run(
            name="mismatched-comparison",
            candidate_config="{}",
        )
        experiment_id = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_run_id,
            baseline_evidence="{}",
        )
        in_memory_state.cas_experiment_to_running(experiment_id)
        in_memory_state.record_candidate_trace(
            experiment_id,
            candidate_trace_id="candidate-trace",
            candidate_llm_call_count=1,
            candidate_cost=0.0,
        )

        with pytest.raises(sqlite3.IntegrityError, match="trace identities"):
            in_memory_state.complete_experiment_with_comparison(
                experiment_id,
                jig_revision="abc",
                trace_diff=json.dumps(
                    {"trace_a_id": "wrong-baseline", "trace_b_id": "candidate-trace"}
                ),
                domain_diff="{}",
            )
        assert in_memory_state.get_experiment(experiment_id)["status"] == "running"
        assert in_memory_state.get_trace_comparison(experiment_id) is None

    def test_error_detail_over_2000_chars_rejected(self, in_memory_state: StateManager) -> None:
        phase_run_id = self._seed_phase_run(in_memory_state)
        run_id = in_memory_state.create_experiment_run(name="too-long-error", candidate_config="{}")
        experiment_id = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_run_id,
            baseline_evidence="{}",
        )
        in_memory_state.cas_experiment_to_running(experiment_id)
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.fail_experiment(experiment_id, error_detail="x" * 2001)

class TestExperimentRunStatusProjection:
    """The parent experiment_runs.status projection: recomputed inside the
    same transaction as every child CAS write from each baseline case's
    latest attempt, including the partial-mix case and retry reopening."""

    def _two_baseline_run(self, state: StateManager) -> tuple[int, int, int]:
        scan_id = state.start_scan()
        snapshot = state.record_feedback_snapshot(scan_id, mode="shadow")
        phase_by_name = {p.phase: p.snapshot_phase_id for p in snapshot.phases}
        phase_run_ids = []
        for trace_id in ("trace-x", "trace-y"):
            msg = Message(
                platform="discord",
                platform_id=f"post-{trace_id}",
                channel_name="general",
                channel_id="ch-1",
                author_name="alice",
                author_id="u1",
                content="post",
                created_at=datetime.now(UTC),
            )
            post_id = state.save_post(msg, scan_id)
            phase_run_ids.append(
                state.insert_phase_run(
                    scan_id=scan_id,
                    post_id=post_id,
                    snapshot_phase_id=phase_by_name["relevance"],
                    phase="relevance",
                    trace_id=trace_id,
                    model="claude-haiku-4-5-20251001",
                    status="complete",
                )
            )
        run_id = state.create_experiment_run(name="batch", candidate_config="{}")
        return run_id, phase_run_ids[0], phase_run_ids[1]

    def test_all_queued_projects_queued_before_any_cas(self, in_memory_state: StateManager) -> None:
        run_id, phase_a, phase_b = self._two_baseline_run(in_memory_state)
        in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_a,
            baseline_evidence="{}",
        )
        in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_b,
            baseline_evidence="{}",
        )
        # Both baseline cases' latest (and only) attempt is still 'queued' —
        # the projection reads 'queued', not 'running', until something CASes.
        assert in_memory_state.get_experiment_run(run_id)["status"] == "queued"

    def test_one_running_one_queued_projects_running(self, in_memory_state: StateManager) -> None:
        run_id, phase_a, phase_b = self._two_baseline_run(in_memory_state)
        a1 = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_a,
            baseline_evidence="{}",
        )
        in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_b,
            baseline_evidence="{}",
        )
        in_memory_state.cas_experiment_to_running(a1)
        assert in_memory_state.get_experiment_run(run_id)["status"] == "running"

    def test_mixed_terminal_projects_partial(self, in_memory_state: StateManager) -> None:
        run_id, phase_a, phase_b = self._two_baseline_run(in_memory_state)
        a1 = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_a,
            baseline_evidence="{}",
        )
        b1 = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_b,
            baseline_evidence="{}",
        )
        in_memory_state.cas_experiment_to_running(a1)
        in_memory_state.record_candidate_trace(
            a1,
            candidate_trace_id="cand-a",
            candidate_llm_call_count=1,
            candidate_cost=0.0,
        )
        in_memory_state.complete_experiment_with_comparison(
            a1,
            jig_revision="rev",
            trace_diff=json.dumps({"trace_a_id": "trace-x", "trace_b_id": "cand-a"}),
            domain_diff="{}",
        )
        in_memory_state.cas_experiment_to_running(b1)
        in_memory_state.fail_experiment(b1, error_detail="boom")

        run_row = in_memory_state.get_experiment_run(run_id)
        assert run_row["status"] == "partial"
        assert run_row["completed_at"] is not None

    def test_all_complete_projects_complete(self, in_memory_state: StateManager) -> None:
        run_id, phase_a, phase_b = self._two_baseline_run(in_memory_state)
        for phase_run_id, trace_id, cand in (
            (phase_a, "trace-x", "cand-a"),
            (phase_b, "trace-y", "cand-b"),
        ):
            attempt_id = in_memory_state.insert_experiment_attempt(
                experiment_run_id=run_id,
                phase_run_id=phase_run_id,
                baseline_evidence="{}",
            )
            in_memory_state.cas_experiment_to_running(attempt_id)
            in_memory_state.record_candidate_trace(
                attempt_id,
                candidate_trace_id=cand,
                candidate_llm_call_count=1,
                candidate_cost=0.0,
            )
            in_memory_state.complete_experiment_with_comparison(
                attempt_id,
                jig_revision="rev",
                trace_diff=json.dumps({"trace_a_id": trace_id, "trace_b_id": cand}),
                domain_diff="{}",
            )
        assert in_memory_state.get_experiment_run(run_id)["status"] == "complete"

    def test_all_failed_projects_failed(self, in_memory_state: StateManager) -> None:
        run_id, phase_a, phase_b = self._two_baseline_run(in_memory_state)
        for phase_run_id in (phase_a, phase_b):
            attempt_id = in_memory_state.insert_experiment_attempt(
                experiment_run_id=run_id,
                phase_run_id=phase_run_id,
                baseline_evidence="{}",
            )
            in_memory_state.cas_experiment_to_running(attempt_id)
            in_memory_state.fail_experiment(attempt_id, error_detail="boom")
        assert in_memory_state.get_experiment_run(run_id)["status"] == "failed"

    def test_retry_reopens_partial_run_to_running_and_clears_completed_at(
        self, in_memory_state: StateManager
    ) -> None:
        run_id, phase_a, phase_b = self._two_baseline_run(in_memory_state)
        a1 = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_a,
            baseline_evidence="{}",
        )
        b1 = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_b,
            baseline_evidence="{}",
        )
        in_memory_state.cas_experiment_to_running(a1)
        in_memory_state.record_candidate_trace(
            a1,
            candidate_trace_id="cand-a",
            candidate_llm_call_count=1,
            candidate_cost=0.0,
        )
        in_memory_state.complete_experiment_with_comparison(
            a1,
            jig_revision="rev",
            trace_diff=json.dumps({"trace_a_id": "trace-x", "trace_b_id": "cand-a"}),
            domain_diff="{}",
        )
        in_memory_state.cas_experiment_to_running(b1)
        in_memory_state.fail_experiment(b1, error_detail="boom")
        assert in_memory_state.get_experiment_run(run_id)["status"] == "partial"

        # Retry just the failed baseline case (b) — the run reopens.
        b2 = in_memory_state.insert_experiment_attempt(
            experiment_run_id=run_id,
            phase_run_id=phase_b,
            baseline_evidence="{}",
            supersedes_experiment_id=b1,
        )
        reopened = in_memory_state.get_experiment_run(run_id)
        assert reopened["status"] == "running"
        assert reopened["completed_at"] is None

        in_memory_state.cas_experiment_to_running(b2)
        in_memory_state.record_candidate_trace(
            b2,
            candidate_trace_id="cand-b2",
            candidate_llm_call_count=1,
            candidate_cost=0.0,
        )
        in_memory_state.complete_experiment_with_comparison(
            b2,
            jig_revision="rev",
            trace_diff=json.dumps({"trace_a_id": "trace-y", "trace_b_id": "cand-b2"}),
            domain_diff="{}",
        )
        final = in_memory_state.get_experiment_run(run_id)
        assert final["status"] == "complete"
        assert final["completed_at"] is not None
        # The originally-failed attempt b1 is untouched.
        assert in_memory_state.get_experiment(b1)["status"] == "failed"

class TestMigration31ComparisonIdentity:
    @staticmethod
    def _v30_connection(*, embedded_baseline: str = "baseline-trace") -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE evaluation_phase_runs (
                id INTEGER PRIMARY KEY,
                trace_id TEXT NOT NULL
            );
            CREATE TABLE evaluation_experiments (
                id INTEGER PRIMARY KEY,
                phase_run_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                candidate_trace_id TEXT
            );
            CREATE TABLE trace_comparisons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id INTEGER NOT NULL UNIQUE,
                jig_revision TEXT NOT NULL,
                trace_diff TEXT NOT NULL,
                domain_diff TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO evaluation_phase_runs VALUES (1, 'baseline-trace');
            INSERT INTO evaluation_experiments VALUES (1, 1, 'complete', 'candidate-trace');
            """
        )
        conn.execute(
            "INSERT INTO trace_comparisons "
            "(experiment_id, jig_revision, trace_diff, domain_diff, created_at) "
            "VALUES (1, 'rev', ?, '{}', '2026-01-01T00:00:00Z')",
            (
                json.dumps(
                    {
                        "trace_a_id": embedded_baseline,
                        "trace_b_id": "candidate-trace",
                    }
                ),
            ),
        )
        return conn

    def test_backfills_verified_v30_identities_into_not_null_columns(self) -> None:
        from scout.storage.migrations import _migrate_to_31

        conn = self._v30_connection()
        try:
            _migrate_to_31(conn)
            columns = {
                row["name"]: row for row in conn.execute("PRAGMA table_info(trace_comparisons)")
            }
            assert columns["trace_a_id"]["notnull"] == 1
            assert columns["trace_b_id"]["notnull"] == 1
            row = conn.execute(
                "SELECT trace_a_id, trace_b_id FROM trace_comparisons WHERE id = 1"
            ).fetchone()
            assert tuple(row) == ("baseline-trace", "candidate-trace")
        finally:
            conn.close()

    def test_aborts_instead_of_backfilling_untrustworthy_v30_identity(self) -> None:
        from scout.storage.migrations import _migrate_to_31

        conn = self._v30_connection(embedded_baseline="wrong-baseline")
        try:
            with pytest.raises(sqlite3.IntegrityError, match="untrustworthy trace identities"):
                _migrate_to_31(conn)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(trace_comparisons)")}
            assert "trace_a_id" not in columns
        finally:
            conn.close()

class TestMigration36ExperimentEvidence:
    """v35->v36: every existing one-off evaluation_experiments row (across
    all four terminal/non-terminal statuses) becomes exactly one
    experiment_runs parent with exactly one linked child attempt, with all
    historical evidence preserved exactly — never fabricated."""

    _V1_CONFIG_TEMPLATE = {
        "version": 1,
        "phase": "relevance",
        "model": "claude-sonnet-4-20250514",
        "system_prompt": "You are Scout's relevance evaluator.",
        "system_prompt_sha256": "deadbeef",
        "recorded_input_reused": True,
        "recorded_input_sha256": "inputhash",
        "baseline_prompt_reused": True,
        "grader_attached": False,
    }

    @classmethod
    def _v35_connection(cls) -> sqlite3.Connection:
        """A v35-shaped database with one evaluation_experiments row for
        each of the four statuses a real pre-migration one-off could be
        in: complete (with its trace_comparisons row), failed, queued, and
        running (both without a comparison yet)."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE evaluation_phase_runs (
                id INTEGER PRIMARY KEY,
                trace_id TEXT NOT NULL,
                model TEXT NOT NULL
            );
            CREATE TABLE evaluation_experiments (
                id                       INTEGER PRIMARY KEY,
                phase_run_id             INTEGER NOT NULL,
                name                     TEXT NOT NULL,
                status                   TEXT NOT NULL,
                candidate_config         TEXT NOT NULL,
                candidate_trace_id       TEXT UNIQUE,
                candidate_llm_call_count INTEGER,
                candidate_cost           REAL,
                error_detail             TEXT,
                created_at               TEXT NOT NULL,
                completed_at             TEXT
            );
            CREATE TABLE trace_comparisons (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id INTEGER NOT NULL UNIQUE,
                trace_a_id    TEXT NOT NULL,
                trace_b_id    TEXT NOT NULL,
                jig_revision  TEXT NOT NULL,
                trace_diff    TEXT NOT NULL,
                domain_diff   TEXT NOT NULL,
                created_at    TEXT NOT NULL
            );
            INSERT INTO evaluation_phase_runs (id, trace_id, model)
                VALUES (1, 'baseline-trace-1', 'claude-haiku-4-5-20251001');
            """
        )

        v1_reused = json.dumps(cls._V1_CONFIG_TEMPLATE, sort_keys=True, separators=(",", ":"))
        v1_unreused = json.dumps(
            {**cls._V1_CONFIG_TEMPLATE, "baseline_prompt_reused": False},
            sort_keys=True,
            separators=(",", ":"),
        )

        rows = [
            # id, status, candidate_config, candidate_trace_id, llm_calls, cost, error, completed_at
            (
                1,
                "complete",
                v1_reused,
                "candidate-trace-1",
                1,
                0.0025,
                None,
                "2026-01-01T00:05:00.000Z",
            ),
            (
                2,
                "failed",
                v1_unreused,
                None,
                None,
                None,
                "boom: candidate execution failed",
                "2026-01-01T00:06:00.000Z",
            ),
            (3, "queued", v1_reused, None, None, None, None, None),
            (4, "running", v1_reused, None, None, None, None, None),
        ]
        for exp_id, status, config, trace_id, calls, cost, error, completed_at in rows:
            conn.execute(
                "INSERT INTO evaluation_experiments "
                "(id, phase_run_id, name, status, candidate_config, candidate_trace_id, "
                "candidate_llm_call_count, candidate_cost, error_detail, created_at, completed_at) "
                "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    exp_id,
                    f"experiment-{exp_id}",
                    status,
                    config,
                    trace_id,
                    calls,
                    cost,
                    error,
                    "2026-01-01T00:00:00.000Z",
                    completed_at,
                ),
            )
        conn.execute(
            "INSERT INTO trace_comparisons "
            "(experiment_id, trace_a_id, trace_b_id, jig_revision, "
            "trace_diff, domain_diff, created_at) "
            "VALUES (1, 'baseline-trace-1', 'candidate-trace-1', "
            "'4fae89bb04768d57be6db4cd2bdef859d1e17322', ?, ?, '2026-01-01T00:05:00.000Z')",
            (
                json.dumps(
                    {
                        "trace_a_id": "baseline-trace-1",
                        "trace_b_id": "candidate-trace-1",
                        "comparison_complete": True,
                    }
                ),
                json.dumps(
                    {
                        "baseline": {"complete": True},
                        "candidate": {"complete": True},
                        "grader_not_attached": True,
                    }
                ),
            ),
        )
        conn.commit()
        return conn

    def test_creates_exactly_one_parent_per_old_row(self) -> None:
        from scout.storage.migrations import _migrate_to_36

        conn = self._v35_connection()
        try:
            _migrate_to_36(conn)
            run_count = conn.execute("SELECT COUNT(*) FROM experiment_runs").fetchone()[0]
            assert run_count == 4
            children = conn.execute(
                "SELECT id, experiment_run_id FROM evaluation_experiments ORDER BY id"
            ).fetchall()
            assert [row["id"] for row in children] == [1, 2, 3, 4]
            # Every child's parent id is distinct — a genuine one-to-one split.
            assert len({row["experiment_run_id"] for row in children}) == 4
        finally:
            conn.close()

    def test_preserves_ids_status_traces_counts_costs_errors_timestamps(self) -> None:
        from scout.storage.migrations import _migrate_to_36

        conn = self._v35_connection()
        try:
            _migrate_to_36(conn)

            complete_row = conn.execute(
                "SELECT * FROM evaluation_experiments WHERE id = 1"
            ).fetchone()
            assert complete_row["status"] == "complete"
            assert complete_row["candidate_trace_id"] == "candidate-trace-1"
            assert complete_row["candidate_llm_call_count"] == 1
            assert complete_row["candidate_cost"] == pytest.approx(0.0025)
            assert complete_row["error_detail"] is None
            assert complete_row["created_at"] == "2026-01-01T00:00:00.000Z"
            assert complete_row["completed_at"] == "2026-01-01T00:05:00.000Z"
            assert complete_row["attempt_number"] == 1
            assert complete_row["supersedes_experiment_id"] is None

            failed_row = conn.execute(
                "SELECT * FROM evaluation_experiments WHERE id = 2"
            ).fetchone()
            assert failed_row["status"] == "failed"
            assert failed_row["error_detail"] == "boom: candidate execution failed"
            assert failed_row["candidate_trace_id"] is None

            queued_row = conn.execute(
                "SELECT * FROM evaluation_experiments WHERE id = 3"
            ).fetchone()
            assert queued_row["status"] == "queued"
            assert queued_row["completed_at"] is None

            running_row = conn.execute(
                "SELECT * FROM evaluation_experiments WHERE id = 4"
            ).fetchone()
            assert running_row["status"] == "running"
            assert running_row["completed_at"] is None

            parent_statuses = {
                row["id"]: row["status"]
                for row in conn.execute("SELECT er.id, er.status FROM experiment_runs er")
            }
            # Each single-child run's status mirrors its only child exactly
            # (no mixed-terminal case is possible with exactly one baseline).
            for child_id, expected_status in (
                (1, "complete"),
                (2, "failed"),
                (3, "queued"),
                (4, "running"),
            ):
                run_id = conn.execute(
                    "SELECT experiment_run_id FROM evaluation_experiments WHERE id = ?", (child_id,)
                ).fetchone()["experiment_run_id"]
                assert parent_statuses[run_id] == expected_status

            names = {
                row["id"]: row["name"]
                for row in conn.execute("SELECT id, name FROM experiment_runs")
            }
            run_id_for_1 = conn.execute(
                "SELECT experiment_run_id FROM evaluation_experiments WHERE id = 1"
            ).fetchone()["experiment_run_id"]
            assert names[run_id_for_1] == "experiment-1"
        finally:
            conn.close()

    def test_trace_diff_and_domain_diff_bytes_preserved_exactly(self) -> None:
        from scout.storage.migrations import _migrate_to_36

        conn = self._v35_connection()
        pre_trace_diff = conn.execute(
            "SELECT trace_diff, domain_diff FROM trace_comparisons WHERE experiment_id = 1"
        ).fetchone()
        try:
            _migrate_to_36(conn)
            post = conn.execute(
                "SELECT trace_diff, domain_diff, score_evidence FROM trace_comparisons "
                "WHERE experiment_id = 1"
            ).fetchone()
            assert post["trace_diff"] == pre_trace_diff["trace_diff"]
            assert post["domain_diff"] == pre_trace_diff["domain_diff"]
            assert post["score_evidence"] is None
        finally:
            conn.close()

    def test_candidate_config_split_into_parent_v2_and_child_baseline_evidence(self) -> None:
        from scout.storage.migrations import _migrate_to_36

        conn = self._v35_connection()
        try:
            _migrate_to_36(conn)

            run_id = conn.execute(
                "SELECT experiment_run_id FROM evaluation_experiments WHERE id = 1"
            ).fetchone()["experiment_run_id"]
            parent_config = json.loads(
                conn.execute(
                    "SELECT candidate_config FROM experiment_runs WHERE id = ?", (run_id,)
                ).fetchone()["candidate_config"]
            )
            assert parent_config == {
                "version": 2,
                "phase": "relevance",
                "model": "claude-sonnet-4-20250514",
                "system_prompt": "You are Scout's relevance evaluator.",
                "system_prompt_sha256": "deadbeef",
                "grader_attached": False,
            }
            assert "recorded_input_sha256" not in parent_config
            assert "baseline_prompt_reused" not in parent_config

            evidence = json.loads(
                conn.execute(
                    "SELECT baseline_evidence FROM evaluation_experiments WHERE id = 1"
                ).fetchone()["baseline_evidence"]
            )
            assert evidence["version"] == 2
            assert evidence["recorded_input_sha256"] == "inputhash"
            assert evidence["baseline_prompt_reused"] is True
            # No migrated row was ever graded — the correction-oracle pin
            # fields never appear on backfilled evidence.
            assert "reply_revision_id" not in evidence
            assert "correction_sha256" not in evidence
        finally:
            conn.close()

    def test_unreused_baseline_prompt_hash_left_null_not_fabricated(self) -> None:
        """v1 never separately recorded the baseline's own prompt hash
        when it differed from the candidate's — migration must not guess
        it, even though it does know the candidate's own hash."""
        from scout.storage.migrations import _migrate_to_36

        conn = self._v35_connection()
        try:
            _migrate_to_36(conn)
            evidence = json.loads(
                conn.execute(
                    "SELECT baseline_evidence FROM evaluation_experiments WHERE id = 2"
                ).fetchone()["baseline_evidence"]
            )
            assert evidence["baseline_prompt_reused"] is False
            assert "baseline_prompt_sha256" not in evidence
        finally:
            conn.close()

    def test_migrated_schema_converges_with_fresh_bootstrap(self) -> None:
        """The migrated tables' structural shape (columns/fks/indexes/
        checks) matches a fresh v36 bootstrap — see TestSchemaConvergence
        for the full-chain (v1->v36) equivalent of this check."""
        from scout.storage.migrations import _migrate_to_36
        from scout.storage.state import SCHEMA

        conn = self._v35_connection()
        try:
            _migrate_to_36(conn)
            migrated_snapshot, _ = schema_snapshot(conn)
        finally:
            conn.close()

        fresh_conn = sqlite3.connect(":memory:")
        fresh_conn.row_factory = sqlite3.Row
        fresh_conn.executescript(SCHEMA)
        fresh_snapshot, _ = schema_snapshot(fresh_conn)
        fresh_conn.close()

        for table in ("experiment_runs", "evaluation_experiments", "trace_comparisons"):
            assert migrated_snapshot[table] == fresh_snapshot[table], table
