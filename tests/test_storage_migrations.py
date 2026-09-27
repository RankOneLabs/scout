"""Focused migration tests for storage changes after the v47 baseline."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from scout.storage.schema import LATEST_SCHEMA_VERSION
from scout.storage.state import StateManager


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
        seeded.conn.execute("DROP INDEX relevance_holdouts_batch_idx")
        seeded.conn.execute("DROP INDEX relevance_holdouts_status_idx")
        seeded.conn.execute("DROP TABLE relevance_holdouts")
        seeded.conn.execute("PRAGMA user_version = 47")

    before_conn = sqlite3.connect(db_path)
    before = _application_schema(before_conn)
    before_conn.close()

    with StateManager(str(db_path)) as upgraded, StateManager(":memory:") as fresh:
        after = _application_schema(upgraded.conn)
        added = [item for item in after if item not in before]

        assert upgraded.conn.execute("PRAGMA user_version").fetchone()[0] == 48
        assert LATEST_SCHEMA_VERSION == 48
        assert added == _holdout_objects(fresh.conn)
        assert [item for item in before if item not in after] == []
        assert upgraded.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert upgraded.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert upgraded.conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 1
