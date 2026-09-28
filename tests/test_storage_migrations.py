"""Focused migration tests for storage changes after the v47 baseline."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scout.storage.migrations import _migrate_to_48, _migrate_to_49
from scout.storage.schema import LATEST_SCHEMA_VERSION
from scout.storage.state import StateManager

EVALUATION_DEPENDENT_TABLES = {
    "critiques",
    "draft_comments",
    "evaluation_phase_runs",
    "gate_blocks",
    "grade_revisions",
    "grades",
    "human_positive_promotions",
    "parent_context_assessments",
    "review_dispositions",
    "shadow_relevance_runs",
    "surfaced_events",
}


def _seed_evaluation_dependents(
    conn: sqlite3.Connection, *, evaluation_id: int, post_id: int, scan_id: int
) -> None:
    """Put the evaluation behind every pre-v48 inbound foreign key."""
    timestamp = "2026-09-27T00:00:00.000Z"
    conn.execute(
        "INSERT INTO parent_context_assessments "
        "(evaluation_id, assessor, assessed_at, without_parent_relevance, "
        "without_parent_posture, explanation) VALUES (?, 'test', ?, 'no', 'ignore', 'test')",
        (evaluation_id, timestamp),
    )
    draft_id = conn.execute(
        "INSERT INTO draft_comments(post_id, evaluation_id, comment_text, created_at) "
        "VALUES (?, ?, 'test', ?) RETURNING id",
        (post_id, evaluation_id, timestamp),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO critiques(draft_id, evaluation_id, verdict, created_at) "
        "VALUES (?, ?, 'pass', ?)",
        (draft_id, evaluation_id, timestamp),
    )
    grade_id = conn.execute(
        "INSERT INTO grades(evaluation_id, post_id, source, graded_at, "
        "relevance_judgment) VALUES (?, ?, 'cli', ?, 'correct') RETURNING id",
        (evaluation_id, post_id, timestamp),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO gate_blocks(reason_code, scan_id, post_id, evaluation_id, created_at) "
        "VALUES ('test', ?, ?, ?, ?)",
        (scan_id, post_id, evaluation_id, timestamp),
    )
    conn.execute(
        "INSERT INTO surfaced_events(platform, author_id, surfaced_at, post_id, "
        "evaluation_id, draft_id, created_at) VALUES ('test', 'author', ?, ?, ?, ?, ?)",
        (timestamp, post_id, evaluation_id, draft_id, timestamp),
    )
    revision_id = conn.execute(
        "INSERT INTO grade_revisions(grade_id, evaluation_id, revision, schema_version, "
        "source, payload, recorded_at) VALUES (?, ?, 1, 1, 'cli', '{}', ?) RETURNING id",
        (grade_id, evaluation_id, timestamp),
    ).fetchone()[0]
    snapshot_id = conn.execute(
        "INSERT INTO feedback_snapshots(scan_id, policy_version, as_of, lookback_days, "
        "max_grades, segment_min_grades, note_max_chars, relevance_token_budget, "
        "reply_draft_token_budget, critic_token_budget, population_count, eligible_count, "
        "excluded_count, created_at) VALUES (?, 'test', ?, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, ?) "
        "RETURNING id",
        (scan_id, timestamp, timestamp),
    ).fetchone()[0]
    phase_id = conn.execute(
        "INSERT INTO feedback_snapshot_phases(snapshot_id, phase, token_budget, "
        "token_estimate, structured_summary, rendered_text, rendered_sha256, created_at) "
        "VALUES (?, 'relevance', 1, 1, '{}', 'test', ?, ?) RETURNING id",
        (snapshot_id, "a" * 64, timestamp),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO evaluation_phase_runs(scan_id, post_id, evaluation_id, "
        "snapshot_phase_id, phase, trace_id, model, status, created_at) "
        "VALUES (?, ?, ?, ?, 'relevance', 'migration-test-trace', 'test', 'complete', ?)",
        (scan_id, post_id, evaluation_id, phase_id, timestamp),
    )
    conn.execute(
        "INSERT INTO human_positive_promotions(source_evaluation_id, source_grade_id, "
        "status, created_at, updated_at) VALUES (?, ?, 'running', ?, ?)",
        (evaluation_id, grade_id, timestamp, timestamp),
    )
    artifact_digest = "b" * 64
    conn.execute(
        "INSERT INTO analysis_artifacts(digest, content, recorded_at) VALUES (?, ?, ?)",
        (artifact_digest, sqlite3.Binary(b"{}"), timestamp),
    )
    conn.execute(
        "INSERT INTO review_dispositions(action_id, queue_digest, evaluation_id, "
        "grade_revision_id, request_json, disposition_json) VALUES "
        "('migration-test', ?, ?, ?, '{}', '{}')",
        (artifact_digest, evaluation_id, revision_id),
    )
    conn.execute(
        "INSERT INTO shadow_relevance_runs(scan_id, post_id, evaluation_id, backend, "
        "model, catalogue_id, catalogue_version, request_id, state_json, status, created_at) "
        "VALUES (?, ?, ?, 'test', 'test', 'test', 'test', 'migration-test', '{}', 'ok', ?)",
        (scan_id, post_id, evaluation_id, timestamp),
    )


def _holdout_objects(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE tbl_name = 'relevance_holdouts' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        )
    ]


def _application_schema(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )
    ]


def test_v48_migration_only_adds_holdout_table_and_indexes(tmp_path: Path) -> None:
    db_path = tmp_path / "v47.db"
    with StateManager(str(db_path)) as seeded, seeded.db.transaction():
        scan_id = seeded.start_scan(environment="migration-test")
        seeded.conn.execute(
            "INSERT INTO posts(platform, platform_msg_id, scan_id) VALUES (?, ?, ?)",
            ("test", "existing-post", scan_id),
        )
        post_id = int(seeded.conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        seeded.conn.execute(
            "INSERT INTO evaluations(post_id, relevant, score, scan_id, surface_status) "
            "VALUES (?, 0, 0.1, ?, 'not_relevant')",
            (post_id, scan_id),
        )
        evaluation_id = int(seeded.conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        _seed_evaluation_dependents(
            seeded.conn,
            evaluation_id=evaluation_id,
            post_id=post_id,
            scan_id=scan_id,
        )
        seeded_tables = {
            table
            for table in EVALUATION_DEPENDENT_TABLES
            if seeded.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        }
        assert seeded_tables == EVALUATION_DEPENDENT_TABLES
        seeded.conn.execute("DROP INDEX relevance_holdouts_batch_idx")
        seeded.conn.execute("DROP INDEX relevance_holdouts_status_idx")
        seeded.conn.execute("DROP TABLE relevance_holdouts")
        seeded.conn.execute("ALTER TABLE evaluations DROP COLUMN relevance_action")
        seeded.conn.execute("ALTER TABLE evaluations DROP COLUMN relevance_classifier")
        seeded.conn.execute("PRAGMA user_version = 47")

    before_conn = sqlite3.connect(db_path)
    before = _application_schema(before_conn)
    before_conn.close()

    upgraded = sqlite3.connect(db_path)
    upgraded.row_factory = sqlite3.Row
    _migrate_to_48(upgraded)
    with StateManager(":memory:") as fresh:
        after = _application_schema(upgraded)
        added = [item for item in after if item not in before]

        assert LATEST_SCHEMA_VERSION == 49
        assert added == _holdout_objects(fresh.conn)
        assert [item for item in before if item not in after] == []
        assert upgraded.execute("PRAGMA foreign_key_check").fetchall() == []
        assert upgraded.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert upgraded.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 1
    upgraded.close()


def test_v49_adds_classifier_columns_and_backfills_human_targets() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE evaluations (id INTEGER PRIMARY KEY);
        CREATE TABLE human_positive_promotions (
            target_evaluation_id INTEGER,
            status TEXT NOT NULL,
            FOREIGN KEY (target_evaluation_id) REFERENCES evaluations(id)
        );
        CREATE TABLE relevance_holdouts (
            target_evaluation_id INTEGER UNIQUE,
            release_action TEXT,
            FOREIGN KEY (target_evaluation_id) REFERENCES evaluations(id)
        );
        INSERT INTO evaluations(id) VALUES (1), (2), (3), (4), (5);
        INSERT INTO human_positive_promotions(target_evaluation_id, status)
        VALUES (2, 'completed'), (3, 'running');
        INSERT INTO relevance_holdouts(target_evaluation_id, release_action)
        VALUES (4, 'review'), (5, 'drop');
        """
    )

    _migrate_to_49(conn)

    rows = conn.execute(
        "SELECT id, relevance_classifier, relevance_action FROM evaluations ORDER BY id"
    ).fetchall()
    assert rows == [
        (1, "llm", None),
        (2, "human", "respond"),
        (3, "llm", None),
        (4, "human", "review"),
        (5, "human", "drop"),
    ]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO evaluations(id, relevance_classifier) VALUES (6, 'zeroshot')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO evaluations(id, relevance_classifier, relevance_action) "
            "VALUES (7, 'llm', 'respond')"
        )
