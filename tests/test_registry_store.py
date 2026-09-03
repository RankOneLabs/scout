"""Tests for RegistryStore: projects, keyword routes, and prompt templates."""

from __future__ import annotations

import sqlite3

from scout.storage.schema import LATEST_SCHEMA_VERSION
from scout.storage.state import StateManager


def _build_registry_db_at_version(
    db_path: str, version: int, project_rows: list[tuple[str, str, str, str, int, str | None, str]]
) -> None:
    """Build a DB stamped at `version` with the given projects pre-seeded.

    Uses the full latest SCHEMA (idempotent CREATE TABLE IF NOT EXISTS) as the
    base, since every migration touching the projects table (14+) is a no-op
    once the target column already exists. This isolates migration 18's own
    data-repair logic from unrelated schema-shape changes in 14-17, which are
    covered by their own tests.

    A genuine v13/v17 database still carries `grades.relevance_note` /
    `grades.comment_note` — migration 21 doesn't drop them until v21 — so
    those columns are added back after the latest-shape bootstrap; migration
    19's rebuild (which runs for every version below 19) selects them by
    name and fails on a `SCHEMA`-only base that no longer carries them.
    """
    from scout.storage.state import SCHEMA

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("ALTER TABLE grades ADD COLUMN relevance_note TEXT")
    conn.execute("ALTER TABLE grades ADD COLUMN comment_note TEXT")
    for key, name, description, link, active, dossier_summary_id, updated_at in project_rows:
        conn.execute(
            "INSERT INTO projects "
            "(key, name, description, link, active, dossier_summary_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (key, name, description, link, active, dossier_summary_id, updated_at, updated_at),
        )
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    conn.close()



class TestMigration18RegistryActivationRepair:
    """Migration 18 repairs migration 014's silent deactivation damage and
    seeds the canonical agent-evals/agent-ops dossier assignments."""

    _UNTOUCHED_UPDATED_AT = "2026-01-01T00:00:00+00:00"

    # A fresh, never-migrated v13 install: all four named projects still
    # active with no dossier assigned, plus one unrelated project.
    _V13_PROJECTS = [
        ("agent-evals", "Agent Evals", "d", "l", 1, None, _UNTOUCHED_UPDATED_AT),
        ("agent-ops", "Agent Ops", "d", "l", 1, None, _UNTOUCHED_UPDATED_AT),
        ("gateway", "Gateway", "d", "l", 1, None, _UNTOUCHED_UPDATED_AT),
        ("zk-extension", "ZK Extension", "d", "l", 1, None, _UNTOUCHED_UPDATED_AT),
        ("unrelated", "Unrelated", "d", "l", 1, "unrelated-dossier", _UNTOUCHED_UPDATED_AT),
    ]

    # A v17 install that already carries migration 014's real production
    # damage: every active-without-dossier project silently deactivated.
    _V17_PROJECTS = [
        ("agent-evals", "Agent Evals", "d", "l", 0, None, _UNTOUCHED_UPDATED_AT),
        ("agent-ops", "Agent Ops", "d", "l", 0, None, _UNTOUCHED_UPDATED_AT),
        ("gateway", "Gateway", "d", "l", 0, None, _UNTOUCHED_UPDATED_AT),
        ("zk-extension", "ZK Extension", "d", "l", 0, None, _UNTOUCHED_UPDATED_AT),
        ("unrelated", "Unrelated", "d", "l", 1, "unrelated-dossier", _UNTOUCHED_UPDATED_AT),
    ]

    def _assert_repaired(self, state: StateManager) -> None:
        assert state.conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
        rows = {
            row["key"]: row
            for row in state.conn.execute(
                "SELECT key, active, dossier_summary_id, updated_at FROM projects"
            )
        }
        assert rows["agent-evals"]["active"] == 1
        assert rows["agent-evals"]["dossier_summary_id"] == "agent-evals-dossier"
        assert rows["agent-ops"]["active"] == 1
        assert rows["agent-ops"]["dossier_summary_id"] == "agent-ops-dossier"
        assert rows["gateway"]["active"] == 0
        assert rows["gateway"]["dossier_summary_id"] is None
        assert rows["zk-extension"]["active"] == 0
        assert rows["zk-extension"]["dossier_summary_id"] is None
        # The unrelated project is left completely untouched, including
        # updated_at — proof that migration 18 only writes named rows.
        assert rows["unrelated"]["active"] == 1
        assert rows["unrelated"]["dossier_summary_id"] == "unrelated-dossier"
        assert rows["unrelated"]["updated_at"] == self._UNTOUCHED_UPDATED_AT
        assert len(rows) == 5

    def test_v13_to_v18_lands_at_repaired_rollout_state(self, tmp_path) -> None:
        db_path = str(tmp_path / "v13.db")
        _build_registry_db_at_version(db_path, 13, self._V13_PROJECTS)
        with StateManager(db_path=db_path) as state:
            self._assert_repaired(state)

    def test_v17_to_v18_repairs_migration_14_damage(self, tmp_path) -> None:
        db_path = str(tmp_path / "v17.db")
        _build_registry_db_at_version(db_path, 17, self._V17_PROJECTS)
        with StateManager(db_path=db_path) as state:
            self._assert_repaired(state)

    def test_v13_and_v17_paths_converge_on_the_same_named_project_state(self, tmp_path) -> None:
        v13_path = str(tmp_path / "v13.db")
        _build_registry_db_at_version(v13_path, 13, self._V13_PROJECTS)
        v17_path = str(tmp_path / "v17.db")
        _build_registry_db_at_version(v17_path, 17, self._V17_PROJECTS)

        def _named(state: StateManager) -> dict[str, tuple[int, str | None]]:
            return {
                row["key"]: (row["active"], row["dossier_summary_id"])
                for row in state.conn.execute(
                    "SELECT key, active, dossier_summary_id FROM projects "
                    "WHERE key IN ('agent-evals', 'agent-ops', 'gateway', 'zk-extension')"
                )
            }

        with StateManager(db_path=v13_path) as v13_state:
            v13_named = _named(v13_state)
        with StateManager(db_path=v17_path) as v17_state:
            v17_named = _named(v17_state)
        assert v13_named == v17_named

    def test_migration_18_is_idempotent(self, tmp_path) -> None:
        from scout.storage.migrations import _migrate_to_18

        db_path = str(tmp_path / "rerun.db")
        _build_registry_db_at_version(db_path, 17, self._V17_PROJECTS)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        _migrate_to_18(conn)
        conn.commit()
        first_pass = {
            row["key"]: (row["active"], row["dossier_summary_id"], row["updated_at"])
            for row in conn.execute(
                "SELECT key, active, dossier_summary_id, updated_at FROM projects"
            )
        }

        _migrate_to_18(conn)
        conn.commit()
        second_pass = {
            row["key"]: (row["active"], row["dossier_summary_id"], row["updated_at"])
            for row in conn.execute(
                "SELECT key, active, dossier_summary_id, updated_at FROM projects"
            )
        }

        assert first_pass == second_pass
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 5
        conn.close()
