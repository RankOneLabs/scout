"""Tests for GradeStore: grades, grade revisions, usage overrides,
human-positive promotions, and reply-draft revisions."""

from __future__ import annotations

import json
import pathlib
import sqlite3
import threading
from datetime import UTC, datetime, timedelta, timezone

import pytest

from scout.config import GradeRecord, Message, RelevanceResult
from scout.storage.grades import (
    GradeStore,
    GradeValidationError,
    _parse_stored_graded_at,
    format_graded_at,
    parse_graded_at,
)
from scout.storage.schema import LATEST_SCHEMA_VERSION
from scout.storage.state import StateManager
from tests.legacy_schema_fixtures import build_legacy_conn_at_version


def _build_pre19_grades_db(
    db_path: str, grade_rows: list[tuple[int, str, str, str, int, int, str | None]]
) -> None:
    """Build a DB with the pre-v19 grades shape (no CHECK constraints) plus
    the rest of the latest schema, stamped at user_version=18.

    grade_rows: (post_id, source, graded_at, relevance_judgment,
                 schema_version, needs_regrade, action_judgment)
    """
    from scout.storage.state import SCHEMA

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE grades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evaluation_id INTEGER,
            post_id INTEGER NOT NULL,
            scan_id INTEGER,
            source TEXT NOT NULL,
            graded_at TEXT NOT NULL,
            relevance_judgment TEXT NOT NULL,
            rejection_reason TEXT,
            relevance_note TEXT,
            comment_quality INTEGER,
            comment_issue TEXT,
            comment_note TEXT,
            schema_version INTEGER NOT NULL DEFAULT 1,
            needs_regrade INTEGER NOT NULL DEFAULT 0,
            action_judgment TEXT,
            dimensions TEXT,
            failure_note TEXT,
            factual_offending_claim TEXT,
            factual_disposition TEXT,
            factual_contradicting_evidence TEXT,
            context_missing_input TEXT,
            posture_should_have_been TEXT,
            implication_implied_claim TEXT,
            implication_missing_support TEXT
        );
        """
    )
    for row in grade_rows:
        conn.execute(
            "INSERT INTO grades "
            "(post_id, source, graded_at, relevance_judgment, schema_version, "
            "needs_regrade, action_judgment) VALUES (?, ?, ?, ?, ?, ?, ?)",
            row,
        )
    # CREATE TABLE IF NOT EXISTS grades is a no-op — the hand-built shape above stays.
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA user_version = 18")
    conn.commit()
    conn.close()


def _build_pre20_grades_db(
    db_path: str, grade_rows: list[tuple[int, str, str, str, int, int, str | None]]
) -> None:
    """Build a DB with the post-v19/pre-v20 grades shape (graded_at CHECK is
    non-empty only, no canonical-form CHECK) plus the rest of the latest
    schema, stamped at user_version=19.

    grade_rows: (post_id, source, graded_at, relevance_judgment,
                 schema_version, needs_regrade, action_judgment)
    """
    from scout.storage.state import SCHEMA

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE grades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evaluation_id INTEGER,
            post_id INTEGER NOT NULL,
            scan_id INTEGER,
            source TEXT NOT NULL CHECK(
                source IN ('cli', 'web', 'migration') AND length(trim(source)) > 0
            ),
            graded_at TEXT NOT NULL CHECK(length(trim(graded_at)) > 0),
            relevance_judgment TEXT NOT NULL CHECK(
                relevance_judgment IN ('correct', 'false_positive', 'false_negative')
            ),
            rejection_reason TEXT,
            relevance_note TEXT,
            comment_quality INTEGER,
            comment_issue TEXT,
            comment_note TEXT,
            schema_version INTEGER NOT NULL DEFAULT 1 CHECK(schema_version IN (1, 2)),
            needs_regrade INTEGER NOT NULL DEFAULT 0 CHECK(needs_regrade IN (0, 1)),
            action_judgment TEXT CHECK(
                action_judgment IS NULL OR action_judgment IN ('accept', 'fail')
            ),
            dimensions TEXT,
            failure_note TEXT,
            factual_offending_claim TEXT,
            factual_disposition TEXT,
            factual_contradicting_evidence TEXT,
            context_missing_input TEXT,
            posture_should_have_been TEXT,
            implication_implied_claim TEXT,
            implication_missing_support TEXT
        );
        """
    )
    for row in grade_rows:
        conn.execute(
            "INSERT INTO grades "
            "(post_id, source, graded_at, relevance_judgment, schema_version, "
            "needs_regrade, action_judgment) VALUES (?, ?, ?, ?, ?, ?, ?)",
            row,
        )
    # CREATE TABLE IF NOT EXISTS grades is a no-op — the hand-built shape above stays.
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA user_version = 19")
    conn.commit()
    conn.close()


def _build_pre21_grades_db(
    db_path: str,
    grade_rows: list[tuple[int, str, str, str, int, int, str | None, str | None, str | None]],
) -> None:
    """Build a DB with the post-v20/pre-v21 grades shape (relevance_note and
    comment_note still present) plus the rest of the latest schema, stamped
    at user_version=20.

    grade_rows: (post_id, source, graded_at, relevance_judgment,
                 schema_version, needs_regrade, action_judgment,
                 relevance_note, comment_note)
    """
    from scout.storage.state import SCHEMA

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE grades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evaluation_id INTEGER,
            post_id INTEGER NOT NULL,
            scan_id INTEGER,
            source TEXT NOT NULL CHECK(
                source IN ('cli', 'web', 'migration') AND length(trim(source)) > 0
            ),
            graded_at TEXT NOT NULL CHECK(
                length(graded_at) = 24
                AND substr(graded_at, 5, 1) = '-'
                AND substr(graded_at, 8, 1) = '-'
                AND substr(graded_at, 11, 1) = 'T'
                AND substr(graded_at, 14, 1) = ':'
                AND substr(graded_at, 17, 1) = ':'
                AND substr(graded_at, 20, 1) = '.'
                AND substr(graded_at, 24, 1) = 'Z'
            ),
            relevance_judgment TEXT NOT NULL CHECK(
                relevance_judgment IN ('correct', 'false_positive', 'false_negative')
            ),
            rejection_reason TEXT,
            relevance_note TEXT,
            comment_quality INTEGER,
            comment_issue TEXT,
            comment_note TEXT,
            schema_version INTEGER NOT NULL DEFAULT 1 CHECK(schema_version IN (1, 2)),
            needs_regrade INTEGER NOT NULL DEFAULT 0 CHECK(needs_regrade IN (0, 1)),
            action_judgment TEXT CHECK(
                action_judgment IS NULL OR action_judgment IN ('accept', 'fail')
            ),
            dimensions TEXT,
            failure_note TEXT,
            factual_offending_claim TEXT,
            factual_disposition TEXT,
            factual_contradicting_evidence TEXT,
            context_missing_input TEXT,
            posture_should_have_been TEXT,
            implication_implied_claim TEXT,
            implication_missing_support TEXT
        );
        CREATE UNIQUE INDEX grades_evaluation_id_unique
            ON grades(evaluation_id) WHERE evaluation_id IS NOT NULL;
        CREATE INDEX grades_scan_id_idx ON grades(scan_id);
        """
    )
    # CREATE TABLE IF NOT EXISTS grades is a no-op — the hand-built shape above stays.
    # SCHEMA also creates posts (among other tables), which the grades rows
    # inserted below need to reference to satisfy the FK once it's enforced.
    conn.executescript(SCHEMA)
    # Seed minimal posts rows for every post_id the grades reference —
    # without them, migration 21's rebuilt grades.post_id FK would dangle
    # once FK enforcement is back on, masking real FK regressions.
    for post_id in {row[0] for row in grade_rows}:
        conn.execute(
            "INSERT INTO posts (id, platform, platform_msg_id) VALUES (?, ?, ?)",
            (post_id, "discord", f"pre21-post-{post_id}"),
        )
    for row in grade_rows:
        conn.execute(
            "INSERT INTO grades "
            "(post_id, source, graded_at, relevance_judgment, schema_version, "
            "needs_regrade, action_judgment, relevance_note, comment_note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
    conn.execute("PRAGMA user_version = 20")
    conn.commit()
    conn.close()


def _build_pre26_db_with_grades(db_path: str) -> tuple[int, int]:
    """Build a genuine pre-migration-26 database that already has grades
    rows — one linked to an evaluation, one legacy row with
    evaluation_id IS NULL — so the v26 backfill has real data to convert.

    Seeds through the real StateManager write path (so the grades rows
    match production shape exactly, and grade_revisions already gets
    populated by that same write path), then strips every v26 object
    including the grade_revisions rows the write path just created, and
    stamps user_version=25. Migration 26 must recreate all of it from
    scratch on the next open. Returns (linked_grade_id, unlinked_grade_id).
    """
    with StateManager(db_path=db_path) as state:
        scan_id = state.start_scan()
        msg = Message(
            platform="discord",
            platform_id="pre26-linked",
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="linked post",
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
        linked_id = state.save_grade(
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

        msg2 = Message(
            platform="discord",
            platform_id="pre26-unlinked",
            channel_name="general",
            channel_id="ch-1",
            author_name="bob",
            author_id="u2",
            content="unlinked post",
            created_at=datetime.now(UTC),
        )
        post_id2 = state.save_post(msg2, scan_id)
        unlinked_id = state.save_grade_for_migration(
            GradeRecord(
                post_id=post_id2,
                source="migration",
                graded_at=datetime(2025, 1, 1, tzinfo=UTC),
                relevance_judgment="correct",
                schema_version=1,
            ),
            migration_reason="v1 backfill",
        )
        state.commit()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("DROP TRIGGER grade_revisions_no_update")
    conn.execute("DROP TRIGGER grade_revisions_no_delete")
    conn.execute("DELETE FROM grade_revisions")
    conn.execute("DROP INDEX grade_revisions_grade_id_revision_unique")
    conn.execute("DROP TABLE grade_revisions")
    conn.execute("DROP TABLE grade_usage_overrides")
    conn.execute("PRAGMA user_version = 25")
    conn.commit()
    conn.close()
    return linked_id, unlinked_id


class TestGradedAtFormat:
    """format_graded_at/parse_graded_at are the sole write-boundary rule for
    the canonical YYYY-MM-DDTHH:MM:SS.mmmZ graded_at representation."""

    def test_format_converts_to_utc_and_truncates_to_milliseconds(self) -> None:
        when = datetime(2026, 1, 1, 5, 0, 0, 123456, tzinfo=timezone(timedelta(hours=5)))
        assert format_graded_at(when) == "2026-01-01T00:00:00.123Z"

    def test_format_rejects_naive_datetime(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            format_graded_at(datetime(2026, 1, 1, 0, 0, 0))

    def test_parse_round_trips_canonical_form(self) -> None:
        canonical = "2026-01-01T00:00:00.123Z"
        assert format_graded_at(parse_graded_at(canonical)) == canonical

    def test_parse_rejects_non_canonical_shape(self) -> None:
        with pytest.raises(ValueError, match="not in canonical form"):
            parse_graded_at("2026-01-01T00:00:00+00:00")

    def test_stored_parser_accepts_legacy_offset_timestamp(self) -> None:
        assert _parse_stored_graded_at(
            "2025-01-01T00:00:00+00:00"
        ) == datetime(2025, 1, 1, tzinfo=UTC)

class TestGradeUpsertConcurrency:
    """File-backed SQLite: the resolved-evaluation_id path in _write_grade
    runs the adoption UPDATE plus ON CONFLICT DO UPDATE inside one BEGIN
    IMMEDIATE transaction, so two independent connections grading the same
    evaluation race at the DB layer instead of in application control flow."""

    def _seed_evaluation(self, db_path: str) -> tuple[int, int, int]:
        with StateManager(db_path=db_path) as seed:
            scan_id = seed.start_scan()
            msg = Message(
                platform="discord",
                platform_id="concurrency-1",
                channel_name="general",
                channel_id="ch-1",
                author_name="alice",
                author_id="u1",
                content="test post",
                created_at=datetime.now(UTC),
            )
            post_id = seed.save_post(msg, scan_id)
            result = RelevanceResult(
                message=msg,
                relevant=True,
                score=0.9,
                reason="relevant",
                relevant_to=("gateway",),
            )
            eval_id = seed.save_evaluation(result, post_id, scan_id)
            seed.commit()
        return scan_id, post_id, eval_id

    def test_two_connections_grading_same_evaluation_both_succeed(
        self, tmp_path: pathlib.Path
    ) -> None:
        db_path = str(tmp_path / "grade-concurrency.db")
        scan_id, post_id, eval_id = self._seed_evaluation(db_path)

        errors: list[BaseException] = []
        lock = threading.Lock()

        def grade(action_judgment: str) -> None:
            try:
                with StateManager(db_path=db_path) as sm:
                    sm.save_grade(
                        GradeRecord(
                            post_id=post_id,
                            evaluation_id=eval_id,
                            scan_id=scan_id,
                            source="cli",
                            graded_at=datetime.now(UTC),
                            relevance_judgment="correct",
                            action_judgment=action_judgment,
                            schema_version=3,
                            dimensions=["tone"] if action_judgment == "fail" else None,
                            failure_note="too casual" if action_judgment == "fail" else None,
                        )
                    )
            except BaseException as exc:  # noqa: BLE001 - captured for assertion below
                with lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=grade, args=("accept",)),
            threading.Thread(target=grade, args=("fail",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []

        with StateManager(db_path=db_path) as sm:
            rows = sm.conn.execute(
                "SELECT * FROM grades WHERE evaluation_id = ?", (eval_id,)
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["action_judgment"] in ("accept", "fail")

    def test_legacy_null_row_adoption_preserves_id_and_canonicalizes_timestamp(
        self, tmp_path: pathlib.Path
    ) -> None:
        db_path = str(tmp_path / "grade-adoption.db")
        scan_id, post_id, eval_id = self._seed_evaluation(db_path)

        with StateManager(db_path=db_path) as sm:
            legacy_id = sm.save_grade_for_migration(
                GradeRecord(
                    post_id=post_id,
                    scan_id=scan_id,
                    source="migration",
                    graded_at=datetime(2025, 1, 1, tzinfo=UTC),
                    relevance_judgment="correct",
                    schema_version=1,
                ),
                migration_reason="v1 backfill",
            )
            sm.commit()

        with StateManager(db_path=db_path) as sm:
            new_id = sm.save_grade(
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
            assert new_id == legacy_id

            row = sm.conn.execute("SELECT * FROM grades WHERE id = ?", (legacy_id,)).fetchone()
            assert row["evaluation_id"] == eval_id
            assert row["action_judgment"] == "accept"
            assert format_graded_at(parse_graded_at(row["graded_at"])) == row["graded_at"]

            count = sm.conn.execute("SELECT COUNT(*) FROM grades").fetchone()[0]
            assert count == 1

class TestMigration19GradeStorageInvariants:
    """Migration 19 adds storage-level CHECK constraints to grades and
    preflights existing rows, aborting with offending IDs instead of
    coercing or deleting them."""

    def test_offending_row_aborts_migration_with_id(self, tmp_path) -> None:
        db_path = str(tmp_path / "pre19-bad.db")
        _build_pre19_grades_db(
            db_path,
            [(1, "cli", "2026-01-01T00:00:00+00:00", "correct", 2, 0, "accept")],
        )
        conn = sqlite3.connect(db_path)
        bad_id = conn.execute(
            "INSERT INTO grades "
            "(post_id, source, graded_at, relevance_judgment, schema_version, "
            "needs_regrade, action_judgment) "
            "VALUES (2, 'not_a_real_source', '2026-01-01T00:00:00+00:00', "
            "'correct', 2, 0, 'accept')"
        ).lastrowid
        conn.commit()
        conn.close()

        with pytest.raises(RuntimeError, match=str(bad_id)):
            StateManager(db_path=db_path)

    def test_clean_rows_migrate_and_new_check_constraints_apply(self, tmp_path) -> None:
        db_path = str(tmp_path / "pre19-clean.db")
        _build_pre19_grades_db(
            db_path,
            [
                (1, "cli", "2026-01-01T00:00:00+00:00", "correct", 2, 0, "accept"),
                (2, "cli", "2026-01-01T00:00:00+00:00", "correct", 1, 1, None),
            ],
        )

        with StateManager(db_path=db_path) as state:
            assert state.conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
            rows = state.conn.execute("SELECT post_id FROM grades ORDER BY post_id").fetchall()
            assert [r["post_id"] for r in rows] == [1, 2]

            with pytest.raises(sqlite3.IntegrityError):
                state.conn.execute(
                    "INSERT INTO grades (post_id, source, graded_at, relevance_judgment, "
                    "schema_version, needs_regrade, action_judgment) "
                    "VALUES (3, 'not_a_real_source', '2026-01-01T00:00:00+00:00', "
                    "'correct', 2, 0, 'accept')"
                )

class TestMigration20GradedAtCanonicalForm:
    """Migration 20 normalizes graded_at to the canonical UTC millisecond
    form and adds a storage-level CHECK for its fixed shape, preflighting
    unparseable rows instead of fabricating a chronology for them."""

    def test_z_offset_and_naive_timestamps_all_normalize(self, tmp_path) -> None:
        db_path = str(tmp_path / "pre20-mixed.db")
        _build_pre20_grades_db(
            db_path,
            [
                (1, "cli", "2026-01-01T00:00:00.5Z", "correct", 2, 0, "accept"),
                (2, "cli", "2026-01-01T05:00:00+05:00", "correct", 2, 0, "accept"),
                (3, "cli", "2026-01-01T00:00:00.123456", "correct", 2, 0, "accept"),
            ],
        )

        with StateManager(db_path=db_path) as state:
            assert state.conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
            rows = {
                row["post_id"]: row["graded_at"]
                for row in state.conn.execute("SELECT post_id, graded_at FROM grades")
            }
            assert rows[1] == "2026-01-01T00:00:00.500Z"
            # 05:00+05:00 is the same instant as 00:00 UTC.
            assert rows[2] == "2026-01-01T00:00:00.000Z"
            # Naive values are treated as already UTC.
            assert rows[3] == "2026-01-01T00:00:00.123Z"

    def test_unparseable_graded_at_aborts_migration_with_id(self, tmp_path) -> None:
        db_path = str(tmp_path / "pre20-bad.db")
        _build_pre20_grades_db(
            db_path,
            [(1, "cli", "not-a-timestamp", "correct", 2, 0, "accept")],
        )
        conn = sqlite3.connect(db_path)
        bad_id = conn.execute("SELECT id FROM grades WHERE post_id = 1").fetchone()[0]
        conn.close()

        with pytest.raises(RuntimeError, match=str(bad_id)):
            StateManager(db_path=db_path)

    def test_new_check_constraint_rejects_non_canonical_shape(self, tmp_path) -> None:
        db_path = str(tmp_path / "pre20-clean.db")
        _build_pre20_grades_db(
            db_path,
            [(1, "cli", "2026-01-01T00:00:00Z", "correct", 2, 0, "accept")],
        )

        with StateManager(db_path=db_path) as state, pytest.raises(sqlite3.IntegrityError):
            state.conn.execute(
                "INSERT INTO grades (post_id, source, graded_at, relevance_judgment, "
                "schema_version, needs_regrade, action_judgment) "
                "VALUES (2, 'cli', '2026-01-01T00:00:00+00:00', "
                "'correct', 2, 0, 'accept')"
            )

class TestMigration21DropGradeNotes:
    """Migration 21 drops the unused grades.relevance_note and
    grades.comment_note columns while preserving every other retained
    column, row content, needs_regrade flags, canonical graded_at values,
    CHECK constraints, foreign keys, and the partial unique evaluation_id
    index."""

    def test_columns_dropped_and_data_preserved(self, tmp_path) -> None:
        db_path = str(tmp_path / "pre21.db")
        _build_pre21_grades_db(
            db_path,
            [
                (
                    1,
                    "cli",
                    "2026-01-01T00:00:00.000Z",
                    "correct",
                    2,
                    0,
                    "accept",
                    "some relevance note",
                    "some comment note",
                ),
                (
                    2,
                    "cli",
                    "2026-01-02T00:00:00.000Z",
                    "false_positive",
                    2,
                    0,
                    "fail",
                    None,
                    None,
                ),
            ],
        )

        with StateManager(db_path=db_path) as state:
            assert state.conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION

            cols = {row["name"] for row in state.conn.execute("PRAGMA table_info(grades)")}
            assert "relevance_note" not in cols
            assert "comment_note" not in cols
            assert "rejection_reason" in cols
            assert "comment_quality" in cols
            assert "comment_issue" in cols

            rows = state.conn.execute(
                "SELECT id, post_id, graded_at, relevance_judgment, "
                "schema_version, needs_regrade, action_judgment "
                "FROM grades ORDER BY post_id"
            ).fetchall()
            assert [r["post_id"] for r in rows] == [1, 2]
            assert [r["id"] for r in rows] == [1, 2]
            assert rows[0]["graded_at"] == "2026-01-01T00:00:00.000Z"
            assert rows[0]["needs_regrade"] == 0
            assert rows[1]["relevance_judgment"] == "false_positive"
            assert rows[1]["action_judgment"] == "fail"

            # CHECK constraints survive: an existing post_id with an invalid
            # source violates only the source CHECK.
            with pytest.raises(sqlite3.IntegrityError):
                state.conn.execute(
                    "INSERT INTO grades (post_id, source, graded_at, "
                    "relevance_judgment, schema_version, needs_regrade, "
                    "action_judgment) VALUES (1, 'not_a_real_source', "
                    "'2026-01-01T00:00:00.000Z', 'correct', 2, 0, 'accept')"
                )

            # The FK survives: a valid source with a nonexistent post_id
            # violates only the foreign key.
            with pytest.raises(sqlite3.IntegrityError):
                state.conn.execute(
                    "INSERT INTO grades (post_id, source, graded_at, "
                    "relevance_judgment, schema_version, needs_regrade, "
                    "action_judgment) VALUES (999, 'cli', "
                    "'2026-01-01T00:00:00.000Z', 'correct', 2, 0, 'accept')"
                )
            indexes = {row["name"] for row in state.conn.execute("PRAGMA index_list(grades)")}
            assert "grades_evaluation_id_unique" in indexes
            assert "grades_scan_id_idx" in indexes

class TestMigration26GradeRevisionsAndUsageOverrides:
    """Migration 26 adds the immutable grade_revisions table, the
    current-state grade_usage_overrides table, and backfills exactly one
    migration_snapshot revision per pre-existing grade — linked and
    unlinked alike — convergently."""

    def test_migrates_v25_db_backfills_one_revision_per_grade(self, tmp_path: pathlib.Path) -> None:
        db_path = str(tmp_path / "pre26.db")
        linked_id, unlinked_id = _build_pre26_db_with_grades(db_path)

        with StateManager(db_path=db_path) as state:
            assert state.conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
            linked_row = state.conn.execute(
                "SELECT evaluation_id FROM grades WHERE id = ?", (linked_id,)
            ).fetchone()
            revisions = state.get_grade_revisions(linked_id)
            assert len(revisions) == 1
            assert revisions[0]["revision"] == 1
            assert revisions[0]["source"] == "migration_snapshot"
            assert revisions[0]["evaluation_id"] == linked_row["evaluation_id"]
            payload = json.loads(revisions[0]["payload"])
            assert payload["id"] == linked_id
            assert payload["relevance_judgment"] == "correct"
            assert payload["action_judgment"] == "accept"

            unlinked_revisions = state.get_grade_revisions(unlinked_id)
            assert len(unlinked_revisions) == 1
            assert unlinked_revisions[0]["revision"] == 1
            assert unlinked_revisions[0]["source"] == "migration_snapshot"
            assert unlinked_revisions[0]["evaluation_id"] is None
            unlinked_payload = json.loads(unlinked_revisions[0]["payload"])
            assert unlinked_payload["evaluation_id"] is None
            assert unlinked_payload["schema_version"] == 1

    def test_idempotent_on_rerun(self, tmp_path: pathlib.Path) -> None:
        db_path = str(tmp_path / "pre26-idem.db")
        linked_id, unlinked_id = _build_pre26_db_with_grades(db_path)

        with StateManager(db_path=db_path) as state:
            from scout.storage.migrations import _migrate_to_26

            _migrate_to_26(state.conn)
            _migrate_to_26(state.conn)
            state.conn.commit()

            assert state.get_grade_revision_count(linked_id) == 1
            assert state.get_grade_revision_count(unlinked_id) == 1
            total = state.conn.execute("SELECT COUNT(*) FROM grade_revisions").fetchone()[0]
            assert total == 2

    def test_fresh_db_has_no_revisions_or_overrides(self, in_memory_state: StateManager) -> None:
        assert (
            in_memory_state.conn.execute("PRAGMA user_version").fetchone()[0]
            == LATEST_SCHEMA_VERSION
        )
        assert (
            in_memory_state.conn.execute("SELECT COUNT(*) FROM grade_revisions").fetchone()[0] == 0
        )
        assert (
            in_memory_state.conn.execute("SELECT COUNT(*) FROM grade_usage_overrides").fetchone()[0]
            == 0
        )

    def test_grade_revisions_rejects_update_and_delete(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()
        msg = Message(
            platform="discord",
            platform_id="immutable-1",
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="post",
            created_at=datetime.now(UTC),
        )
        post_id = in_memory_state.save_post(msg, scan_id)
        result = RelevanceResult(
            message=msg,
            relevant=True,
            score=0.9,
            reason="relevant",
            relevant_to=("gateway",),
        )
        eval_id = in_memory_state.save_evaluation(result, post_id, scan_id)
        grade_id = in_memory_state.save_grade(
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

        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "UPDATE grade_revisions SET source = 'web' WHERE grade_id = ?",
                (grade_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "DELETE FROM grade_revisions WHERE grade_id = ?", (grade_id,)
            )

class TestGradeRevisionsWritePath:
    """The centralized write primitive (_write_grade /
    _upsert_resolved_grade_in_transaction) appends exactly one immutable
    grade_revisions row, atomically with the grades upsert, for every
    sanctioned write lane: save_grade, save_grade_for_remediation, and
    save_grade_for_migration."""

    def _seed_evaluation(
        self, state: StateManager, platform_id: str = "rev-1"
    ) -> tuple[int, int, int]:
        scan_id = state.start_scan()
        msg = Message(
            platform="discord",
            platform_id=platform_id,
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="test post",
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
        return scan_id, post_id, eval_id

    def test_save_grade_creates_revision_one(self, in_memory_state: StateManager) -> None:
        _scan_id, post_id, eval_id = self._seed_evaluation(in_memory_state)
        grade_id = in_memory_state.save_grade(
            GradeRecord(
                post_id=post_id,
                evaluation_id=eval_id,
                source="web",
                graded_at=datetime.now(UTC),
                relevance_judgment="correct",
                action_judgment="accept",
                schema_version=3,
            )
        )

        assert in_memory_state.get_grade_revision_count(grade_id) == 1
        revisions = in_memory_state.get_grade_revisions(grade_id)
        assert revisions[0]["revision"] == 1
        assert revisions[0]["source"] == "web"
        assert revisions[0]["evaluation_id"] == eval_id
        assert revisions[0]["schema_version"] == 3
        payload = json.loads(revisions[0]["payload"])
        assert payload["id"] == grade_id
        assert payload["action_judgment"] == "accept"
        assert payload["needs_regrade"] == 0

    def test_resaving_grade_appends_revision_two_oldest_to_newest(
        self, in_memory_state: StateManager
    ) -> None:
        _scan_id, post_id, eval_id = self._seed_evaluation(in_memory_state)
        grade_id = in_memory_state.save_grade(
            GradeRecord(
                post_id=post_id,
                evaluation_id=eval_id,
                source="cli",
                graded_at=datetime.now(UTC),
                relevance_judgment="correct",
                action_judgment="accept",
                schema_version=3,
            )
        )
        in_memory_state.save_grade(
            GradeRecord(
                post_id=post_id,
                evaluation_id=eval_id,
                source="web",
                graded_at=datetime.now(UTC),
                relevance_judgment="correct",
                action_judgment="fail",
                schema_version=3,
                dimensions=["tone"],
                failure_note="too casual",
            )
        )

        revisions = in_memory_state.get_grade_revisions(grade_id)
        assert [r["revision"] for r in revisions] == [1, 2]
        assert revisions[0]["source"] == "cli"
        assert revisions[1]["source"] == "web"
        payload_1 = json.loads(revisions[0]["payload"])
        payload_2 = json.loads(revisions[1]["payload"])
        assert payload_1["action_judgment"] == "accept"
        assert payload_2["action_judgment"] == "fail"
        assert payload_2["dimensions"] == ["tone"]
        assert in_memory_state.get_grade_revision_count(grade_id) == 2

    def test_validation_error_writes_no_grade_and_no_revision(
        self, in_memory_state: StateManager
    ) -> None:
        _scan_id, post_id, eval_id = self._seed_evaluation(in_memory_state)
        with pytest.raises(GradeValidationError):
            in_memory_state.save_grade(
                GradeRecord(
                    post_id=post_id,
                    evaluation_id=eval_id,
                    source="web",
                    graded_at=datetime.now(UTC),
                    relevance_judgment="correct",
                    action_judgment="fail",
                    schema_version=3,  # no dimensions/failure_note
                )
            )
        assert (
            in_memory_state.conn.execute(
                "SELECT COUNT(*) FROM grades WHERE evaluation_id = ?", (eval_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            in_memory_state.conn.execute("SELECT COUNT(*) FROM grade_revisions").fetchone()[0] == 0
        )

    def test_injected_revision_insert_failure_rolls_back_grade_upsert(
        self, in_memory_state: StateManager, monkeypatch
    ) -> None:
        _scan_id, post_id, eval_id = self._seed_evaluation(in_memory_state)

        def _boom(self, grade_id, evaluation_id, source):
            raise RuntimeError("injected revision insert failure")

        monkeypatch.setattr(GradeStore, "_insert_grade_revision", _boom)
        with pytest.raises(RuntimeError, match="injected revision insert failure"):
            in_memory_state.save_grade(
                GradeRecord(
                    post_id=post_id,
                    evaluation_id=eval_id,
                    source="web",
                    graded_at=datetime.now(UTC),
                    relevance_judgment="correct",
                    action_judgment="accept",
                    schema_version=3,
                )
            )

        assert (
            in_memory_state.conn.execute(
                "SELECT COUNT(*) FROM grades WHERE evaluation_id = ?", (eval_id,)
            ).fetchone()[0]
            == 0
        )

    def test_save_grade_for_remediation_appends_revision_in_outer_transaction(
        self, in_memory_state: StateManager
    ) -> None:
        _scan_id, post_id, eval_id = self._seed_evaluation(in_memory_state)
        with in_memory_state.db.begin_immediate():
            grade_id = in_memory_state.save_grade_for_remediation(
                GradeRecord(
                    post_id=post_id,
                    evaluation_id=eval_id,
                    source="cli",
                    graded_at=datetime.now(UTC),
                    relevance_judgment="correct",
                    action_judgment="accept",
                    schema_version=3,
                ),
                remediation_reason="corpus audit fix",
            )

        assert in_memory_state.get_grade_revision_count(grade_id) == 1
        assert in_memory_state.get_grade_revisions(grade_id)[0]["source"] == "cli"

    def test_mark_needs_regrade_for_remediation_appends_atomic_revision(
        self, in_memory_state: StateManager
    ) -> None:
        _scan_id, post_id, eval_id = self._seed_evaluation(in_memory_state)
        grade_id = in_memory_state.save_grade(
            GradeRecord(
                post_id=post_id,
                evaluation_id=eval_id,
                source="web",
                graded_at=datetime.now(UTC),
                relevance_judgment="correct",
                action_judgment="accept",
                schema_version=3,
            )
        )

        with in_memory_state.db.begin_immediate():
            changed = in_memory_state.mark_grade_needs_regrade_for_remediation(
                grade_id, remediation_reason="corpus audit"
            )

        assert changed is True
        revisions = in_memory_state.get_grade_revisions(grade_id)
        assert [row["revision"] for row in revisions] == [1, 2]
        assert revisions[1]["source"] == "migration"
        assert revisions[1]["schema_version"] == 3
        assert json.loads(revisions[1]["payload"])["needs_regrade"] == 1

        with in_memory_state.db.begin_immediate():
            changed_again = in_memory_state.mark_grade_needs_regrade_for_remediation(
                grade_id, remediation_reason="same audit"
            )
        assert changed_again is False
        assert in_memory_state.get_grade_revision_count(grade_id) == 2

    def test_save_grade_for_migration_unresolvable_evaluation_still_gets_revision(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id = in_memory_state.start_scan()
        msg = Message(
            platform="discord",
            platform_id="orphan-1",
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="orphan post",
            created_at=datetime.now(UTC),
        )
        post_id = in_memory_state.save_post(msg, scan_id)
        grade_id = in_memory_state.save_grade_for_migration(
            GradeRecord(
                post_id=post_id,
                source="migration",
                graded_at=datetime(2025, 1, 1, tzinfo=UTC),
                relevance_judgment="correct",
                schema_version=1,
            ),
            migration_reason="v1 backfill",
        )

        revisions = in_memory_state.get_grade_revisions(grade_id)
        assert len(revisions) == 1
        assert revisions[0]["revision"] == 1
        assert revisions[0]["source"] == "migration"
        assert revisions[0]["evaluation_id"] is None

    def test_unlinked_grade_adoption_preserves_grade_id_and_appends_revision(
        self, tmp_path: pathlib.Path
    ) -> None:
        db_path = str(tmp_path / "adoption.db")
        with StateManager(db_path=db_path) as seed:
            scan_id = seed.start_scan()
            msg = Message(
                platform="discord",
                platform_id="adopt-1",
                channel_name="general",
                channel_id="ch-1",
                author_name="alice",
                author_id="u1",
                content="post",
                created_at=datetime.now(UTC),
            )
            post_id = seed.save_post(msg, scan_id)
            result = RelevanceResult(
                message=msg,
                relevant=True,
                score=0.9,
                reason="relevant",
                relevant_to=("gateway",),
            )
            eval_id = seed.save_evaluation(result, post_id, scan_id)
            seed.commit()

        with StateManager(db_path=db_path) as state:
            # No scan_id: _resolve_grade_evaluation_id has nothing to look
            # up evaluation from, so this row lands genuinely unlinked
            # (evaluation_id IS NULL, scan_id IS NULL) rather than
            # resolving eval_id via the post/scan lookup.
            legacy_id = state.save_grade_for_migration(
                GradeRecord(
                    post_id=post_id,
                    source="migration",
                    graded_at=datetime(2025, 1, 1, tzinfo=UTC),
                    relevance_judgment="correct",
                    schema_version=1,
                ),
                migration_reason="v1 backfill",
            )
            state.commit()

        with StateManager(db_path=db_path) as state:
            # scan_id=None matches the legacy row's NULL scan_id, so the
            # adoption UPDATE in _upsert_resolved_grade_in_transaction
            # finds and adopts it by id instead of inserting a new row.
            adopted_id = state.save_grade(
                GradeRecord(
                    post_id=post_id,
                    evaluation_id=eval_id,
                    scan_id=None,
                    source="cli",
                    graded_at=datetime.now(UTC),
                    relevance_judgment="correct",
                    action_judgment="accept",
                    schema_version=3,
                )
            )
            assert adopted_id == legacy_id

            revisions = state.get_grade_revisions(legacy_id)
            assert [r["revision"] for r in revisions] == [1, 2]
            assert revisions[0]["source"] == "migration"
            assert revisions[0]["evaluation_id"] is None
            assert revisions[1]["source"] == "cli"
            assert revisions[1]["evaluation_id"] == eval_id

    def test_concurrent_writes_allocate_consecutive_revisions(self, tmp_path: pathlib.Path) -> None:
        db_path = str(tmp_path / "grade-revision-concurrency.db")
        with StateManager(db_path=db_path) as seed:
            scan_id = seed.start_scan()
            msg = Message(
                platform="discord",
                platform_id="concurrent-rev-1",
                channel_name="general",
                channel_id="ch-1",
                author_name="alice",
                author_id="u1",
                content="test post",
                created_at=datetime.now(UTC),
            )
            post_id = seed.save_post(msg, scan_id)
            result = RelevanceResult(
                message=msg,
                relevant=True,
                score=0.9,
                reason="relevant",
                relevant_to=("gateway",),
            )
            eval_id = seed.save_evaluation(result, post_id, scan_id)
            seed.commit()

        errors: list[BaseException] = []
        lock = threading.Lock()

        def grade(action_judgment: str) -> None:
            try:
                with StateManager(db_path=db_path) as sm:
                    sm.save_grade(
                        GradeRecord(
                            post_id=post_id,
                            evaluation_id=eval_id,
                            scan_id=scan_id,
                            source="cli",
                            graded_at=datetime.now(UTC),
                            relevance_judgment="correct",
                            action_judgment=action_judgment,
                            schema_version=3,
                            dimensions=["tone"] if action_judgment == "fail" else None,
                            failure_note="too casual" if action_judgment == "fail" else None,
                        )
                    )
            except BaseException as exc:  # noqa: BLE001 - captured for assertion below
                with lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=grade, args=("accept",)),
            threading.Thread(target=grade, args=("fail",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []

        with StateManager(db_path=db_path) as sm:
            grade_id = sm.get_grade_id_for_evaluation(eval_id)
            assert grade_id is not None
            revisions = sm.get_grade_revisions(grade_id)
            assert [r["revision"] for r in revisions] == [1, 2]

class TestGradeRevisionConvergenceRepair:
    """converge_grade_revision_for_remediation: the only remediation lane
    for restoring grade_revisions convergence — read-check-write under one
    transaction, idempotent, append-only, never touches grades, never
    fabricates evaluation_id."""

    def _seed_evaluation(
        self, state: StateManager, platform_id: str = "convergence-1"
    ) -> tuple[int, int, int]:
        scan_id = state.start_scan()
        msg = Message(
            platform="discord",
            platform_id=platform_id,
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="test post",
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
        return scan_id, post_id, eval_id

    def test_already_converged_grade_writes_nothing(self, in_memory_state: StateManager) -> None:
        _scan_id, post_id, eval_id = self._seed_evaluation(in_memory_state)
        grade_id = in_memory_state.save_grade(
            GradeRecord(
                post_id=post_id,
                evaluation_id=eval_id,
                source="web",
                graded_at=datetime.now(UTC),
                relevance_judgment="correct",
                action_judgment="accept",
                schema_version=3,
            )
        )

        with in_memory_state.db.begin_immediate():
            status = in_memory_state.converge_grade_revision_for_remediation(
                grade_id, remediation_reason="convergence audit"
            )

        assert status == "converged"
        assert in_memory_state.get_grade_revision_count(grade_id) == 1

    def test_missing_revision_grade_gets_one_migration_revision(
        self, in_memory_state: StateManager
    ) -> None:
        _scan_id, post_id, _eval_id = self._seed_evaluation(
            in_memory_state, platform_id="convergence-missing"
        )
        # A revision-less current row — e.g. a legitimately unlinked
        # historical gap — inserted directly, bypassing every sanctioned
        # write lane (all of which always append a revision).
        cursor = in_memory_state.conn.execute(
            "INSERT INTO grades (evaluation_id, post_id, source, graded_at, "
            "relevance_judgment, schema_version, needs_regrade, action_judgment) "
            "VALUES (NULL, ?, 'cli', ?, 'correct', 2, 0, 'accept')",
            (post_id, format_graded_at(datetime.now(UTC))),
        )
        grade_id = cursor.lastrowid
        assert grade_id is not None
        in_memory_state.commit()
        assert in_memory_state.get_grade_revision_count(grade_id) == 0

        with in_memory_state.db.begin_immediate():
            status = in_memory_state.converge_grade_revision_for_remediation(
                grade_id, remediation_reason="convergence audit"
            )

        assert status == "missing_revision"
        revisions = in_memory_state.get_grade_revisions(grade_id)
        assert len(revisions) == 1
        assert revisions[0]["source"] == "migration"
        # Never fabricates a linkage the current row doesn't have.
        assert revisions[0]["evaluation_id"] is None
        assert json.loads(revisions[0]["payload"])["evaluation_id"] is None

    def test_divergent_revision_grade_gets_one_migration_revision(
        self, in_memory_state: StateManager
    ) -> None:
        _scan_id, post_id, eval_id = self._seed_evaluation(
            in_memory_state, platform_id="convergence-divergent"
        )
        grade_id = in_memory_state.save_grade(
            GradeRecord(
                post_id=post_id,
                evaluation_id=eval_id,
                source="web",
                graded_at=datetime.now(UTC),
                relevance_judgment="correct",
                action_judgment="accept",
                schema_version=3,
            )
        )
        # Directly mutate the current row without appending a revision —
        # simulates history drifting out of sync with the current state.
        in_memory_state.conn.execute(
            "UPDATE grades SET relevance_judgment = 'false_positive', "
            "action_judgment = 'fail', dimensions = '[\"tone\"]', "
            "failure_note = 'drift' WHERE id = ?",
            (grade_id,),
        )
        in_memory_state.commit()

        with in_memory_state.db.begin_immediate():
            status = in_memory_state.converge_grade_revision_for_remediation(
                grade_id, remediation_reason="convergence audit"
            )

        assert status == "divergent_revision"
        revisions = in_memory_state.get_grade_revisions(grade_id)
        assert [r["revision"] for r in revisions] == [1, 2]
        assert revisions[1]["source"] == "migration"
        new_payload = json.loads(revisions[1]["payload"])
        assert new_payload["relevance_judgment"] == "false_positive"
        assert new_payload["dimensions"] == ["tone"]

    def test_repair_is_idempotent_on_rerun(self, in_memory_state: StateManager) -> None:
        _scan_id, post_id, eval_id = self._seed_evaluation(
            in_memory_state, platform_id="convergence-idempotent"
        )
        grade_id = in_memory_state.save_grade(
            GradeRecord(
                post_id=post_id,
                evaluation_id=eval_id,
                source="web",
                graded_at=datetime.now(UTC),
                relevance_judgment="correct",
                action_judgment="accept",
                schema_version=3,
            )
        )
        in_memory_state.conn.execute(
            "UPDATE grades SET relevance_judgment = 'false_positive', "
            "action_judgment = 'fail', dimensions = '[\"tone\"]', "
            "failure_note = 'drift' WHERE id = ?",
            (grade_id,),
        )
        in_memory_state.commit()

        with in_memory_state.db.begin_immediate():
            first = in_memory_state.converge_grade_revision_for_remediation(
                grade_id, remediation_reason="convergence audit"
            )
        assert first == "divergent_revision"
        assert in_memory_state.get_grade_revision_count(grade_id) == 2

        with in_memory_state.db.begin_immediate():
            second = in_memory_state.converge_grade_revision_for_remediation(
                grade_id, remediation_reason="convergence audit rerun"
            )
        assert second == "converged"
        assert in_memory_state.get_grade_revision_count(grade_id) == 2

    def test_never_writes_to_grades_table(self, in_memory_state: StateManager) -> None:
        _scan_id, post_id, _eval_id = self._seed_evaluation(
            in_memory_state, platform_id="convergence-no-grades-write"
        )
        cursor = in_memory_state.conn.execute(
            "INSERT INTO grades (evaluation_id, post_id, source, graded_at, "
            "relevance_judgment, schema_version, needs_regrade, action_judgment) "
            "VALUES (NULL, ?, 'cli', ?, 'correct', 2, 0, 'accept')",
            (post_id, format_graded_at(datetime.now(UTC))),
        )
        grade_id = cursor.lastrowid
        in_memory_state.commit()
        before = dict(in_memory_state.get_grade_row_by_id(grade_id))

        with in_memory_state.db.begin_immediate():
            in_memory_state.converge_grade_revision_for_remediation(
                grade_id, remediation_reason="convergence audit"
            )

        after = dict(in_memory_state.get_grade_row_by_id(grade_id))
        assert after == before

    def test_joins_caller_transaction_atomically(
        self, in_memory_state: StateManager, monkeypatch
    ) -> None:
        _scan_id, post_id, _eval_id = self._seed_evaluation(
            in_memory_state, platform_id="convergence-atomic"
        )
        cursor = in_memory_state.conn.execute(
            "INSERT INTO grades (evaluation_id, post_id, source, graded_at, "
            "relevance_judgment, schema_version, needs_regrade, action_judgment) "
            "VALUES (NULL, ?, 'cli', ?, 'correct', 2, 0, 'accept')",
            (post_id, format_graded_at(datetime.now(UTC))),
        )
        grade_id = cursor.lastrowid
        in_memory_state.commit()

        def _boom(self, grade_id, evaluation_id, source):
            raise RuntimeError("injected revision insert failure")

        monkeypatch.setattr(GradeStore, "_insert_grade_revision", _boom)
        with (
            pytest.raises(RuntimeError, match="injected revision insert failure"),
            in_memory_state.db.begin_immediate(),
        ):
            in_memory_state.converge_grade_revision_for_remediation(
                grade_id, remediation_reason="convergence audit"
            )

        assert in_memory_state.get_grade_revision_count(grade_id) == 0

    def test_unknown_grade_id_raises(self, in_memory_state: StateManager) -> None:
        with (
            pytest.raises(GradeValidationError),
            in_memory_state.db.begin_immediate(),
        ):
            in_memory_state.converge_grade_revision_for_remediation(
                999999, remediation_reason="convergence audit"
            )

    def test_blank_remediation_reason_raises(self, in_memory_state: StateManager) -> None:
        _scan_id, post_id, eval_id = self._seed_evaluation(
            in_memory_state, platform_id="convergence-blank-reason"
        )
        grade_id = in_memory_state.save_grade(
            GradeRecord(
                post_id=post_id,
                evaluation_id=eval_id,
                source="web",
                graded_at=datetime.now(UTC),
                relevance_judgment="correct",
                action_judgment="accept",
                schema_version=3,
            )
        )
        with pytest.raises(ValueError, match="remediation_reason is required"):
            in_memory_state.converge_grade_revision_for_remediation(
                grade_id, remediation_reason="   "
            )

class TestGradeUsageOverrides:
    def _grade_id(self, state: StateManager, platform_id: str = "override-1") -> int:
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
        result = RelevanceResult(
            message=msg,
            relevant=True,
            score=0.9,
            reason="relevant",
            relevant_to=("gateway",),
        )
        eval_id = state.save_evaluation(result, post_id, scan_id)
        return state.save_grade(
            GradeRecord(
                post_id=post_id,
                evaluation_id=eval_id,
                source="cli",
                graded_at=datetime.now(UTC),
                relevance_judgment="correct",
                action_judgment="accept",
                schema_version=3,
            )
        )

    def test_auto_mode_stores_null_reason(self, in_memory_state: StateManager) -> None:
        grade_id = self._grade_id(in_memory_state)
        row = in_memory_state.save_grade_usage_override(grade_id, mode="auto", reason=None)
        assert row["mode"] == "auto"
        assert row["reason"] is None

    def test_exclude_mode_requires_nonblank_reason(self, in_memory_state: StateManager) -> None:
        grade_id = self._grade_id(in_memory_state)
        with pytest.raises(GradeValidationError):
            in_memory_state.save_grade_usage_override(grade_id, mode="exclude", reason=None)
        with pytest.raises(GradeValidationError):
            in_memory_state.save_grade_usage_override(grade_id, mode="exclude", reason="   ")

    def test_exclude_mode_trims_reason(self, in_memory_state: StateManager) -> None:
        grade_id = self._grade_id(in_memory_state)
        row = in_memory_state.save_grade_usage_override(
            grade_id, mode="exclude", reason="  stale evidence  "
        )
        assert row["mode"] == "exclude"
        assert row["reason"] == "stale evidence"

    def test_invalid_mode_rejected(self, in_memory_state: StateManager) -> None:
        grade_id = self._grade_id(in_memory_state)
        with pytest.raises(GradeValidationError):
            in_memory_state.save_grade_usage_override(grade_id, mode="force_include", reason=None)

    def test_nonexistent_grade_rejected(self, in_memory_state: StateManager) -> None:
        with pytest.raises(GradeValidationError):
            in_memory_state.save_grade_usage_override(999999, mode="auto", reason=None)

    def test_upsert_updates_existing_row_not_duplicate(self, in_memory_state: StateManager) -> None:
        grade_id = self._grade_id(in_memory_state)
        in_memory_state.save_grade_usage_override(grade_id, mode="exclude", reason="first reason")
        in_memory_state.save_grade_usage_override(grade_id, mode="auto", reason=None)

        count = in_memory_state.conn.execute(
            "SELECT COUNT(*) FROM grade_usage_overrides WHERE grade_id = ?", (grade_id,)
        ).fetchone()[0]
        assert count == 1
        row = in_memory_state.conn.execute(
            "SELECT mode, reason FROM grade_usage_overrides WHERE grade_id = ?", (grade_id,)
        ).fetchone()
        assert row["mode"] == "auto"
        assert row["reason"] is None

    def test_get_grade_id_for_evaluation(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()
        msg = Message(
            platform="discord",
            platform_id="lookup-1",
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="post",
            created_at=datetime.now(UTC),
        )
        post_id = in_memory_state.save_post(msg, scan_id)
        result = RelevanceResult(
            message=msg,
            relevant=True,
            score=0.9,
            reason="relevant",
            relevant_to=("gateway",),
        )
        eval_id = in_memory_state.save_evaluation(result, post_id, scan_id)
        assert in_memory_state.get_grade_id_for_evaluation(eval_id) is None

        grade_id = in_memory_state.save_grade(
            GradeRecord(
                post_id=post_id,
                evaluation_id=eval_id,
                source="cli",
                graded_at=datetime.now(UTC),
                relevance_judgment="correct",
                action_judgment="accept",
                schema_version=3,
            )
        )
        assert in_memory_state.get_grade_id_for_evaluation(eval_id) == grade_id

class TestMigration34ReplyDraftRevisions:
    """Migration 34 adds the immutable reply_draft_revisions lineage table
    for draft_comments corrections, and a nullable grades.reply_revision_id
    pointer into it."""

    def _seed_draft_comment(self, state: StateManager) -> tuple[int, int, int, int]:
        scan_id = state.start_scan()
        msg = Message(
            platform="discord",
            platform_id="reply-rev-1",
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
        draft_id = state.save_draft(
            post_id=post_id,
            evaluation_id=eval_id,
            project_key="gateway",
            comment_text="original reply",
            scan_id=scan_id,
        )
        return draft_id, eval_id, post_id, scan_id

    def test_fresh_db_has_table_and_no_rows(self, in_memory_state: StateManager) -> None:
        assert (
            in_memory_state.conn.execute("PRAGMA user_version").fetchone()[0]
            == LATEST_SCHEMA_VERSION
        )
        count = in_memory_state.conn.execute(
            "SELECT COUNT(*) FROM reply_draft_revisions"
        ).fetchone()[0]
        assert count == 0

    def test_grades_reply_revision_id_column_exists_and_nullable(
        self, in_memory_state: StateManager
    ) -> None:
        cols = {
            row["name"]: row["notnull"]
            for row in in_memory_state.conn.execute("PRAGMA table_info(grades)")
        }
        assert "reply_revision_id" in cols
        assert cols["reply_revision_id"] == 0

    def test_insert_and_parent_lineage(self, in_memory_state: StateManager) -> None:
        draft_id, _eval_id, _post_id, _scan_id = self._seed_draft_comment(in_memory_state)
        now = datetime.now(UTC).isoformat()
        v1_id = in_memory_state.conn.execute(
            "INSERT INTO reply_draft_revisions "
            "(draft_comment_id, version, parent_revision_id, reply_text, source, created_at) "
            "VALUES (?, 1, NULL, ?, 'cli', ?)",
            (draft_id, "corrected v1", now),
        ).lastrowid
        v2_id = in_memory_state.conn.execute(
            "INSERT INTO reply_draft_revisions "
            "(draft_comment_id, version, parent_revision_id, reply_text, source, created_at) "
            "VALUES (?, 2, ?, ?, 'cli', ?)",
            (draft_id, v1_id, "corrected v2", now),
        ).lastrowid
        row = in_memory_state.conn.execute(
            "SELECT parent_revision_id, reply_text FROM reply_draft_revisions WHERE id = ?",
            (v2_id,),
        ).fetchone()
        assert row["parent_revision_id"] == v1_id
        assert row["reply_text"] == "corrected v2"

    def test_unique_draft_comment_id_and_version(self, in_memory_state: StateManager) -> None:
        draft_id, *_ = self._seed_draft_comment(in_memory_state)
        now = datetime.now(UTC).isoformat()
        in_memory_state.conn.execute(
            "INSERT INTO reply_draft_revisions "
            "(draft_comment_id, version, reply_text, source, created_at) "
            "VALUES (?, 1, 'v1', 'cli', ?)",
            (draft_id, now),
        )
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "INSERT INTO reply_draft_revisions "
                "(draft_comment_id, version, reply_text, source, created_at) "
                "VALUES (?, 1, 'dup', 'cli', ?)",
                (draft_id, now),
            )

    def test_foreign_key_enforced_on_invalid_draft_comment_id(
        self, in_memory_state: StateManager
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "INSERT INTO reply_draft_revisions "
                "(draft_comment_id, version, reply_text, source, created_at) "
                "VALUES (999999, 1, 'orphan', 'cli', ?)",
                (now,),
            )

    def test_rejects_update_and_delete(self, in_memory_state: StateManager) -> None:
        draft_id, *_ = self._seed_draft_comment(in_memory_state)
        now = datetime.now(UTC).isoformat()
        rev_id = in_memory_state.conn.execute(
            "INSERT INTO reply_draft_revisions "
            "(draft_comment_id, version, reply_text, source, created_at) "
            "VALUES (?, 1, 'v1', 'cli', ?)",
            (draft_id, now),
        ).lastrowid

        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "UPDATE reply_draft_revisions SET reply_text = 'edited' WHERE id = ?",
                (rev_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "DELETE FROM reply_draft_revisions WHERE id = ?", (rev_id,)
            )

    def test_grades_reply_revision_id_defaults_null_and_accepts_fk(
        self, in_memory_state: StateManager
    ) -> None:
        draft_id, eval_id, post_id, scan_id = self._seed_draft_comment(in_memory_state)
        grade_id = in_memory_state.save_grade(
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
        row = in_memory_state.conn.execute(
            "SELECT reply_revision_id FROM grades WHERE id = ?", (grade_id,)
        ).fetchone()
        assert row["reply_revision_id"] is None

        now = datetime.now(UTC).isoformat()
        rev_id = in_memory_state.conn.execute(
            "INSERT INTO reply_draft_revisions "
            "(draft_comment_id, version, reply_text, source, created_at) "
            "VALUES (?, 1, 'v1', 'cli', ?)",
            (draft_id, now),
        ).lastrowid
        in_memory_state.conn.execute(
            "UPDATE grades SET reply_revision_id = ? WHERE id = ?", (rev_id, grade_id)
        )
        row = in_memory_state.conn.execute(
            "SELECT reply_revision_id FROM grades WHERE id = ?", (grade_id,)
        ).fetchone()
        assert row["reply_revision_id"] == rev_id

    def test_grades_reply_revision_id_rejects_invalid_fk(
        self, in_memory_state: StateManager
    ) -> None:
        _draft_id, eval_id, post_id, scan_id = self._seed_draft_comment(in_memory_state)
        grade_id = in_memory_state.save_grade(
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
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "UPDATE grades SET reply_revision_id = 999999 WHERE id = ?", (grade_id,)
            )

    def test_v33_db_upgrades_without_replaying_32_or_33(self, tmp_path: pathlib.Path) -> None:
        """Migration 34 must not touch human_positive_promotions (32) or
        autonomy_events (33) — it only adds new objects."""
        db_path = str(tmp_path / "pre34.db")
        conn = build_legacy_conn_at_version(db_path, 33)
        conn.execute("PRAGMA user_version = 33")
        conn.commit()
        conn.close()

        with StateManager(db_path=db_path) as state:
            assert state.conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
            cols = {row["name"] for row in state.conn.execute("PRAGMA table_info(grades)")}
            assert "reply_revision_id" in cols

            reply_triggers = {
                row[0]
                for row in state.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                    "AND tbl_name = 'reply_draft_revisions'"
                )
            }
            assert reply_triggers == {
                "reply_draft_revisions_no_update",
                "reply_draft_revisions_no_delete",
            }

            # migrations 32/33 objects are untouched by the 33->34 step
            autonomy_triggers = {
                row[0]
                for row in state.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                    "AND tbl_name = 'autonomy_events'"
                )
            }
            assert autonomy_triggers == {
                "autonomy_events_no_update",
                "autonomy_events_no_delete",
            }
            promo_cols = {
                row["name"]
                for row in state.conn.execute("PRAGMA table_info(human_positive_promotions)")
            }
            assert "status" in promo_cols

class TestSaveGradeReplyRevisionLifecycle:
    """save_grade's resolved-evaluation write lane: an edit-bearing save
    atomically creates and links the next reply_draft_revisions row plus a
    matching grade_revisions entry, a no-edit regrade preserves an existing
    correction link instead of severing it, and any failing step inside the
    one BEGIN IMMEDIATE transaction leaves no partial trace."""

    def _seed_draft_comment(self, state: StateManager) -> tuple[int, int, int, int]:
        scan_id = state.start_scan()
        msg = Message(
            platform="discord",
            platform_id="atomic-rev-1",
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
        draft_id = state.save_draft(
            post_id=post_id,
            evaluation_id=eval_id,
            project_key="gateway",
            comment_text="original reply",
            scan_id=scan_id,
        )
        return draft_id, eval_id, post_id, scan_id

    def _fail_grade(
        self, *, post_id: int, evaluation_id: int, edited_text: str | None
    ) -> GradeRecord:
        return GradeRecord(
            post_id=post_id,
            evaluation_id=evaluation_id,
            source="cli",
            graded_at=datetime.now(UTC),
            relevance_judgment="false_positive",
            action_judgment="fail",
            schema_version=3,
            dimensions=["tone"],
            failure_note="too casual",
            edited_text=edited_text,
        )

    def _accept_grade(self, *, post_id: int, evaluation_id: int) -> GradeRecord:
        return GradeRecord(
            post_id=post_id,
            evaluation_id=evaluation_id,
            source="cli",
            graded_at=datetime.now(UTC),
            relevance_judgment="correct",
            action_judgment="accept",
            schema_version=3,
        )

    def test_initial_no_edit_grade_leaves_reply_revision_id_null(
        self, in_memory_state: StateManager
    ) -> None:
        _draft_id, eval_id, post_id, _scan_id = self._seed_draft_comment(in_memory_state)
        grade_id = in_memory_state.save_grade(
            self._accept_grade(post_id=post_id, evaluation_id=eval_id)
        )

        row = in_memory_state.get_grade_row_by_id(grade_id)
        assert row["reply_revision_id"] is None
        count = in_memory_state.conn.execute(
            "SELECT COUNT(*) FROM reply_draft_revisions"
        ).fetchone()[0]
        assert count == 0

    def test_get_grade_row_by_id_keeps_needs_regrade_as_stored_integer(
        self, in_memory_state: StateManager
    ) -> None:
        """The facade's legacy row shape: needs_regrade is the stored 0/1,
        not GradeRow's bool. grading_api_sidecar serializes this dict
        straight into its grade responses, where the web client's Grade
        type declares the field a number."""
        _draft_id, eval_id, post_id, _scan_id = self._seed_draft_comment(in_memory_state)
        grade_id = in_memory_state.save_grade(
            self._accept_grade(post_id=post_id, evaluation_id=eval_id)
        )

        row = in_memory_state.get_grade_row_by_id(grade_id)
        assert row["needs_regrade"] == 0
        assert type(row["needs_regrade"]) is int

    def test_edit_bearing_save_creates_and_links_one_reply_revision(
        self, in_memory_state: StateManager
    ) -> None:
        draft_id, eval_id, post_id, _scan_id = self._seed_draft_comment(in_memory_state)
        grade_id = in_memory_state.save_grade(
            self._fail_grade(
                post_id=post_id, evaluation_id=eval_id, edited_text="a corrected reply"
            )
        )

        row = in_memory_state.get_grade_row_by_id(grade_id)
        assert row["reply_revision_id"] is not None
        rev = in_memory_state.conn.execute(
            "SELECT * FROM reply_draft_revisions WHERE id = ?", (row["reply_revision_id"],)
        ).fetchone()
        assert rev["draft_comment_id"] == draft_id
        assert rev["version"] == 1
        assert rev["parent_revision_id"] is None
        assert rev["reply_text"] == "a corrected reply"
        assert rev["source"] == "cli"

        revisions = in_memory_state.get_grade_revisions(grade_id)
        assert len(revisions) == 1
        payload = json.loads(revisions[0]["payload"])
        assert payload["edited_text"] == "a corrected reply"
        assert payload["reply_revision_id"] == row["reply_revision_id"]

    def test_successive_edits_form_versions_with_correct_parent_links(
        self, in_memory_state: StateManager
    ) -> None:
        draft_id, eval_id, post_id, _scan_id = self._seed_draft_comment(in_memory_state)
        grade_id = in_memory_state.save_grade(
            self._fail_grade(post_id=post_id, evaluation_id=eval_id, edited_text="v1 text")
        )
        v1_id = in_memory_state.get_grade_row_by_id(grade_id)["reply_revision_id"]

        in_memory_state.save_grade(
            self._fail_grade(post_id=post_id, evaluation_id=eval_id, edited_text="v2 text")
        )
        v2_id = in_memory_state.get_grade_row_by_id(grade_id)["reply_revision_id"]
        assert v2_id != v1_id

        revisions = in_memory_state.conn.execute(
            "SELECT * FROM reply_draft_revisions WHERE draft_comment_id = ? ORDER BY version",
            (draft_id,),
        ).fetchall()
        assert [r["version"] for r in revisions] == [1, 2]
        assert revisions[0]["id"] == v1_id
        assert revisions[1]["id"] == v2_id
        assert revisions[1]["parent_revision_id"] == v1_id

        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "UPDATE reply_draft_revisions SET reply_text = 'tamper' WHERE id = ?", (v1_id,)
            )
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute("DELETE FROM reply_draft_revisions WHERE id = ?", (v2_id,))

        grade_revisions = in_memory_state.get_grade_revisions(grade_id)
        assert [r["revision"] for r in grade_revisions] == [1, 2]
        assert json.loads(grade_revisions[1]["payload"])["reply_revision_id"] == v2_id
        assert json.loads(grade_revisions[1]["payload"])["edited_text"] == "v2 text"

    def test_later_no_edit_regrade_preserves_reply_revision_id(
        self, in_memory_state: StateManager
    ) -> None:
        draft_id, eval_id, post_id, _scan_id = self._seed_draft_comment(in_memory_state)
        in_memory_state.save_grade(
            self._fail_grade(post_id=post_id, evaluation_id=eval_id, edited_text="corrected text")
        )
        grade_id = in_memory_state.get_grade_id_for_evaluation(eval_id)
        assert grade_id is not None
        linked_id = in_memory_state.get_grade_row_by_id(grade_id)["reply_revision_id"]
        assert linked_id is not None

        in_memory_state.save_grade(self._accept_grade(post_id=post_id, evaluation_id=eval_id))

        row = in_memory_state.get_grade_row_by_id(grade_id)
        assert row["reply_revision_id"] == linked_id
        count = in_memory_state.conn.execute(
            "SELECT COUNT(*) FROM reply_draft_revisions WHERE draft_comment_id = ?", (draft_id,)
        ).fetchone()[0]
        assert count == 1

        revisions = in_memory_state.get_grade_revisions(grade_id)
        assert len(revisions) == 2
        preserved_payload = json.loads(revisions[1]["payload"])
        assert preserved_payload["reply_revision_id"] == linked_id
        assert preserved_payload["edited_text"] == "corrected text"

        grade = in_memory_state.get_grade_for_evaluation(eval_id)
        assert grade is not None
        assert grade.edited_text == "corrected text"
        assert grade.relevance_judgment == "correct"

    def test_edit_bearing_save_without_draft_comments_raises_and_writes_nothing(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id = in_memory_state.start_scan()
        msg = Message(
            platform="discord",
            platform_id="draftless-1",
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="post",
            created_at=datetime.now(UTC),
        )
        post_id = in_memory_state.save_post(msg, scan_id)
        result = RelevanceResult(
            message=msg,
            relevant=True,
            score=0.9,
            reason="relevant",
            relevant_to=("gateway",),
        )
        eval_id = in_memory_state.save_evaluation(result, post_id, scan_id)

        with pytest.raises(GradeValidationError):
            in_memory_state.save_grade(
                self._fail_grade(
                    post_id=post_id,
                    evaluation_id=eval_id,
                    edited_text="nothing to attach this to",
                )
            )

        assert (
            in_memory_state.conn.execute("SELECT COUNT(*) FROM reply_draft_revisions").fetchone()[0]
            == 0
        )
        assert in_memory_state.conn.execute("SELECT COUNT(*) FROM grades").fetchone()[0] == 0
        assert (
            in_memory_state.conn.execute("SELECT COUNT(*) FROM grade_revisions").fetchone()[0] == 0
        )

    def test_failed_write_rolls_back_reply_revision_and_grade_mutation(
        self, in_memory_state: StateManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _draft_id, eval_id, post_id, _scan_id = self._seed_draft_comment(in_memory_state)
        grade_id = in_memory_state.save_grade(
            self._accept_grade(post_id=post_id, evaluation_id=eval_id)
        )

        def _boom(self: StateManager, grade_id: int, evaluation_id: int | None, source: str) -> int:
            raise RuntimeError("injected revision insert failure")

        monkeypatch.setattr(GradeStore, "_insert_grade_revision", _boom)

        with pytest.raises(RuntimeError, match="injected revision insert failure"):
            in_memory_state.save_grade(
                self._fail_grade(
                    post_id=post_id, evaluation_id=eval_id, edited_text="should never persist"
                )
            )

        assert (
            in_memory_state.conn.execute("SELECT COUNT(*) FROM reply_draft_revisions").fetchone()[0]
            == 0
        )
        row = in_memory_state.get_grade_row_by_id(grade_id)
        assert row["reply_revision_id"] is None
        assert row["relevance_judgment"] == "correct"
        assert row["action_judgment"] == "accept"
        assert in_memory_state.get_grade_revision_count(grade_id) == 1

    def test_concurrent_edit_bearing_saves_do_not_duplicate_versions_or_mismatch_parents(
        self, tmp_path: pathlib.Path
    ) -> None:
        db_path = str(tmp_path / "reply-revision-concurrency.db")
        with StateManager(db_path=db_path) as seed:
            draft_id, eval_id, post_id, scan_id = self._seed_draft_comment(seed)
            seed.commit()

        errors: list[BaseException] = []
        lock = threading.Lock()

        def grade(source: str, text: str) -> None:
            try:
                with StateManager(db_path=db_path) as sm:
                    sm.save_grade(
                        GradeRecord(
                            post_id=post_id,
                            evaluation_id=eval_id,
                            scan_id=scan_id,
                            source=source,
                            graded_at=datetime.now(UTC),
                            relevance_judgment="false_positive",
                            action_judgment="fail",
                            schema_version=3,
                            dimensions=["tone"],
                            failure_note="too casual",
                            edited_text=text,
                        )
                    )
            except BaseException as exc:  # noqa: BLE001 - captured for assertion below
                with lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=grade, args=("cli", "cli edit")),
            threading.Thread(target=grade, args=("web", "web edit")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []

        with StateManager(db_path=db_path) as sm:
            revisions = sm.conn.execute(
                "SELECT * FROM reply_draft_revisions WHERE draft_comment_id = ? ORDER BY version",
                (draft_id,),
            ).fetchall()
            assert [r["version"] for r in revisions] == [1, 2]
            assert revisions[1]["parent_revision_id"] == revisions[0]["id"]

            grade_id = sm.get_grade_id_for_evaluation(eval_id)
            assert grade_id is not None
            row = sm.get_grade_row_by_id(grade_id)
            assert row["reply_revision_id"] == revisions[1]["id"]

            grade_revisions = sm.get_grade_revisions(grade_id)
            assert [r["revision"] for r in grade_revisions] == [1, 2]
            assert (
                json.loads(grade_revisions[1]["payload"])["reply_revision_id"] == revisions[1]["id"]
            )

class TestEvaluationIdentityGating:
    """Tests verifying evaluation-identity-scoped selection for review, progress, and export.

    One post is evaluated under two scan IDs (original + rescore). Each scan has its
    own evaluation and draft. Correct behavior: review, progress, and export are
    scoped by evaluation scan_id, NOT by posts.scan_id.
    """

    def _make_msg(self, platform_id: str) -> Message:
        return Message(
            platform="discord",
            platform_id=platform_id,
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="test content for eval identity",
            created_at=datetime.now(UTC),
        )

    def _setup_rescore_scenario(self, state: StateManager) -> dict[str, int]:
        """Create a post in scan1 with eval+draft, then rescore in scan2 with new eval+draft."""
        scan1 = state.start_scan()
        msg = self._make_msg("eval-ident-1")
        post_id = state.save_post(msg, scan1)

        result1 = RelevanceResult(
            message=msg,
            relevant=True,
            score=0.7,
            reason="original scan",
            relevant_to=("gateway",),
        )
        eval1_id = state.save_evaluation(result1, post_id, scan1)
        draft1_id = state.save_draft(post_id, eval1_id, "gateway", "Draft from scan1", scan1)
        state.complete_scan(scan1, 1, 1)

        scan2 = state.start_scan()
        result2 = RelevanceResult(
            message=msg,
            relevant=True,
            score=0.9,
            reason="rescore",
            relevant_to=("gateway",),
        )
        eval2_id = state.save_evaluation(result2, post_id, scan2)
        draft2_id = state.save_draft(post_id, eval2_id, "gateway", "Draft from scan2", scan2)
        state.complete_scan(scan2, 1, 1, advance_watermark=False)

        return {
            "post_id": post_id,
            "scan1": scan1,
            "scan2": scan2,
            "eval1_id": eval1_id,
            "eval2_id": eval2_id,
            "draft1_id": draft1_id,
            "draft2_id": draft2_id,
        }

    def test_gradeable_items_scoped_to_rescore_evaluation(
        self, in_memory_state: StateManager
    ) -> None:
        """get_gradeable_items for scan2 must surface the scan2 evaluation and its draft,
        not the scan1 draft, even though the post.scan_id is scan1."""
        ids = self._setup_rescore_scenario(in_memory_state)

        items = in_memory_state.get_gradeable_items(ids["scan2"])
        assert len(items) == 1
        assert items[0]["post_id"] == ids["post_id"]
        assert items[0]["comment_text"] == "Draft from scan2"

    def test_grading_progress_matches_evaluation_scope(self, in_memory_state: StateManager) -> None:
        """get_grading_progress denominator must count evaluations in the scan,
        not posts whose scan_id matches (which would miss rescored evaluations)."""
        ids = self._setup_rescore_scenario(in_memory_state)

        graded, total = in_memory_state.get_grading_progress(ids["scan2"])
        assert total == 1
        assert graded == 0

        grade = GradeRecord(
            post_id=ids["post_id"],
            scan_id=ids["scan2"],
            source="cli",
            graded_at=datetime.now(UTC),
            relevance_judgment="correct",
            action_judgment="accept",
        )
        in_memory_state.save_grade(grade)

        graded, total = in_memory_state.get_grading_progress(ids["scan2"])
        assert total == 1
        assert graded == 1

    def test_export_eval_cases_pairs_each_draft_with_own_evaluation(
        self, in_memory_state: StateManager
    ) -> None:
        """export_eval_cases must join drafts through evaluation_id (not post_id) to
        avoid pairing scan1's draft with scan2's evaluation or vice versa.
        When scan_id is provided, only cases for that scan's evaluations are returned."""
        ids = self._setup_rescore_scenario(in_memory_state)

        grade = GradeRecord(
            post_id=ids["post_id"],
            evaluation_id=ids["eval1_id"],
            scan_id=ids["scan1"],
            source="cli",
            graded_at=datetime.now(UTC),
            relevance_judgment="correct",
            action_judgment="accept",
        )
        in_memory_state.save_grade(grade)

        # Scoped to scan1: must return exactly scan1's draft.
        cases_scan1 = in_memory_state.export_eval_cases(scan_id=ids["scan1"])
        assert len(cases_scan1) == 1
        assert cases_scan1[0]["evaluation"]["draft"] == "Draft from scan1"
        assert cases_scan1[0]["evaluation"]["evaluation_id"] == ids["eval1_id"]

        # Scoped to scan2: scan1's grade must not qualify scan2's evaluation.
        cases_scan2 = in_memory_state.export_eval_cases(scan_id=ids["scan2"])
        assert cases_scan2 == []

        in_memory_state.save_grade(
            GradeRecord(
                post_id=ids["post_id"],
                evaluation_id=ids["eval2_id"],
                scan_id=ids["scan2"],
                source="cli",
                graded_at=datetime.now(UTC),
                relevance_judgment="correct",
                action_judgment="accept",
            )
        )

        # Once scan2 has its own grade, it returns exactly scan2's draft.
        cases_scan2 = in_memory_state.export_eval_cases(scan_id=ids["scan2"])
        assert len(cases_scan2) == 1
        assert cases_scan2[0]["evaluation"]["draft"] == "Draft from scan2"
        assert cases_scan2[0]["evaluation"]["evaluation_id"] == ids["eval2_id"]

        # Global (no scan_id): both evaluations are graded and returned in
        # descending graded_at order.
        cases_all = in_memory_state.export_eval_cases()
        assert len(cases_all) == 2
