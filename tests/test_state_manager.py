"""Tests for StateManager database operations."""

from __future__ import annotations

import pathlib
import sqlite3
from datetime import UTC, datetime

import pytest

from scout.config import Message
from scout.storage.migrations import AutonomyEventsNotEmptyError
from scout.storage.state import (
    LATEST_SCHEMA_VERSION,
    MIGRATIONS,
    StateManager,
)
from tests.legacy_schema_fixtures import (
    LEGACY_SCHEMA,
)
from tests.legacy_schema_fixtures import (
    build_legacy_conn_at_version as _build_legacy_conn_at_version,
)
from tests.legacy_schema_fixtures import (
    schema_snapshot as _schema_snapshot,
)


class TestContextManager:
    def test_exit_closes_connection(self, tmp_path) -> None:
        db_path = str(tmp_path / "cm.db")
        with StateManager(db_path=db_path) as state:
            assert state.conn is not None

        import sqlite3

        try:
            state.conn.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            return
        raise AssertionError("connection should be closed after __exit__")

    def test_clean_exit_commits_without_an_explicit_commit_call(self, tmp_path) -> None:
        db_path = str(tmp_path / "clean-exit.db")
        with StateManager(db_path=db_path) as state:
            # A raw write outside any Db context, with no state.commit() —
            # a clean __exit__ must commit it on its own.
            state.conn.execute(
                "INSERT INTO scans (started_at) VALUES ('2026-01-01T00:00:00+00:00')"
            )

        fresh = sqlite3.connect(db_path)
        try:
            assert fresh.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 1
        finally:
            fresh.close()

    def test_exceptional_exit_rolls_back_without_an_explicit_rollback_call(self, tmp_path) -> None:
        db_path = str(tmp_path / "exceptional-exit.db")
        with pytest.raises(RuntimeError, match="boom"), StateManager(db_path=db_path) as state:
            state.conn.execute(
                "INSERT INTO scans (started_at) VALUES ('2026-01-01T00:00:00+00:00')"
            )
            raise RuntimeError("boom")

        fresh = sqlite3.connect(db_path)
        try:
            assert fresh.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 0
        finally:
            fresh.close()


class TestWalMode:
    def test_wal_mode_active(self, tmp_path) -> None:
        db_path = str(tmp_path / "wal.db")
        with StateManager(db_path=db_path) as state:
            mode = state.conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode == "wal"


class TestInitSchemaSkip:
    """init_schema=False models the sidecar's per-request StateManager: it
    must reuse a schema bootstrapped elsewhere without re-running the DDL,
    while still applying per-connection PRAGMAs on every open connection."""

    def test_skips_ddl_against_a_database_with_no_schema(self, tmp_path) -> None:
        db_path = str(tmp_path / "no_schema.db")
        # No prior StateManager has touched this path — a fresh file has no
        # tables at all until something runs the DDL.
        with (
            StateManager(db_path=db_path, init_schema=False) as state,
            pytest.raises(sqlite3.OperationalError, match="no such table"),
        ):
            state.conn.execute("SELECT * FROM scans").fetchone()

    def test_reuses_schema_bootstrapped_by_a_prior_connection(self, tmp_path) -> None:
        db_path = str(tmp_path / "bootstrapped.db")
        with StateManager(db_path=db_path) as bootstrap:
            scan_id = bootstrap.start_scan()
            bootstrap.complete_scan(scan_id, messages_scanned=0, relevant_found=0)
            bootstrap.commit()

        with StateManager(db_path=db_path, init_schema=False) as state:
            assert state.get_latest_completed_scan_id() == scan_id

    def test_still_applies_per_connection_pragmas(self, tmp_path) -> None:
        db_path = str(tmp_path / "pragmas.db")
        with StateManager(db_path=db_path):
            pass  # bootstrap the schema and WAL mode once

        with StateManager(db_path=db_path, init_schema=False) as state:
            assert state.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert state.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert state.conn.execute("PRAGMA synchronous").fetchone()[0] == 1

    def test_default_still_runs_schema_init(self, tmp_path) -> None:
        """The default (init_schema unset) is unchanged: every existing
        caller that just does StateManager(db_path) keeps working exactly
        as before this parameter was added."""
        db_path = str(tmp_path / "default.db")
        with StateManager(db_path=db_path) as state:
            row = state.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='scans'"
            ).fetchone()
            assert row is not None


class TestCommit:
    def test_save_post_is_durable_without_an_explicit_commit_call(self, tmp_path) -> None:
        """save_post opens its own Db.transaction() and commits on return —
        a fresh connection sees the row immediately, with no
        state.commit() call needed."""
        import sqlite3

        db_path = str(tmp_path / "commit.db")
        with StateManager(db_path=db_path) as state:
            scan_id = state.start_scan()
            msg = Message(
                platform="discord",
                platform_id="commit-1",
                channel_name="general",
                channel_id="ch-1",
                author_name="alice",
                author_id="user-1",
                content="pending commit",
                created_at=datetime.now(UTC),
            )
            state.save_post(msg, scan_id=scan_id)
            assert not state.db.in_transaction

            fresh = sqlite3.connect(db_path)
            fresh.row_factory = sqlite3.Row
            row = fresh.execute(
                "SELECT id FROM posts WHERE platform_msg_id = ?",
                ("commit-1",),
            ).fetchone()
            fresh.close()
            assert row is not None

    def test_commit_is_a_harmless_noop_after_self_durable_writes(self, tmp_path) -> None:
        """StateManager.commit() remains a compatibility delegate: calling
        it after a write that already committed itself is a no-op, not an
        error."""
        db_path = str(tmp_path / "commit_noop.db")
        with StateManager(db_path=db_path) as state:
            scan_id = state.start_scan()
            state.commit()
            assert not state.db.in_transaction
            row = state.conn.execute("SELECT id FROM scans WHERE id = ?", (scan_id,)).fetchone()
            assert row is not None


class TestSchemaConvergence:
    """T-007: a fresh SCHEMA bootstrap, the oldest-supported legacy
    database upgraded only through the ordered Python MIGRATIONS must
    converge on the identical realized schema — proving both creation
    paths agree, not just that each individually "looks latest"."""

    def test_fresh_bootstrap_and_legacy_upgrade_converge(self, tmp_path) -> None:
        from scout.storage.state import SCHEMA

        fresh_conn = sqlite3.connect(":memory:")
        fresh_conn.row_factory = sqlite3.Row
        fresh_conn.executescript(SCHEMA)

        legacy_db_path = str(tmp_path / "legacy.db")
        legacy_conn = sqlite3.connect(legacy_db_path)
        legacy_conn.executescript(LEGACY_SCHEMA)
        legacy_conn.commit()
        legacy_conn.close()
        with StateManager(db_path=legacy_db_path) as legacy_state:
            legacy_snapshot, legacy_uv = _schema_snapshot(legacy_state.conn)

        fresh_snapshot, fresh_uv = _schema_snapshot(fresh_conn)
        fresh_conn.close()

        assert fresh_uv == LATEST_SCHEMA_VERSION
        assert legacy_uv == LATEST_SCHEMA_VERSION

        assert legacy_snapshot == fresh_snapshot, (
            "the legacy-upgrade migration path diverged from the fresh SCHEMA bootstrap"
        )


# The nine outbound content-engine tables migration 37 drops. No migrated
# or freshly bootstrapped database may still carry any of them.
_CONTENT_ENGINE_TABLES: frozenset[str] = frozenset({
    "content_candidates", "outbound_drafts", "draft_revisions", "draft_annotations",
    "publications", "outbound_publication_verifications", "outbound_publish_reviews",
    "outbound_publish_review_decisions", "outbound_revision_provenance",
})


def _build_legacy_db(db_path: str) -> None:
    """Hand-build a v0 scout.db with two candidates exercising both edge cases.

    candidate 1: single outbound_drafts row at status='drafted'.
    candidate 2: two rows (one superseded, one current) at status='approved',
    with one publications row pointing at the current legacy draft.
    """
    import sqlite3 as sq

    conn = sq.connect(db_path)
    conn.executescript(LEGACY_SCHEMA)
    # user_version stays at the default 0 — this is what a real v0 DB looks like.
    now = "2026-05-01T00:00:00+00:00"

    conn.execute(
        "INSERT INTO content_candidates "
        "(id, source, raw_payload, target_channels, status, root_trace_id, "
        "created_at, updated_at) VALUES (1, 'idea_dump', 'dump1', "
        "'[\"bluesky\"]', 'drafted', 'trace-1', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO outbound_drafts "
        "(candidate_id, version, claim, evidence, pushback_angles, edited_text, "
        "trace_id, drafted_at, created_at, updated_at) "
        "VALUES (1, 1, 'c1', 'e1', '[\"a\",\"b\",\"c\"]', 'first', 'trace-1', ?, ?, ?)",
        (now, now, now),
    )

    conn.execute(
        "INSERT INTO content_candidates "
        "(id, source, raw_payload, target_channels, status, root_trace_id, "
        "created_at, updated_at) VALUES (2, 'idea_dump', 'dump2', "
        "'[\"bluesky\"]', 'approved', 'trace-2', ?, ?)",
        (now, now),
    )
    # Superseded v1
    conn.execute(
        "INSERT INTO outbound_drafts "
        "(id, candidate_id, version, superseded_at, claim, edited_text, "
        "trace_id, created_at, updated_at) "
        "VALUES (10, 2, 1, '2026-05-02T00:00:00+00:00', 'old', 'old-text', "
        "'trace-2', ?, ?)",
        (now, now),
    )
    # Current v2
    conn.execute(
        "INSERT INTO outbound_drafts "
        "(id, candidate_id, version, claim, edited_text, trace_id, "
        "drafted_at, edited_at, created_at, updated_at) "
        "VALUES (11, 2, 2, 'new', 'new-text', 'trace-2', ?, ?, ?, ?)",
        (now, now, now, now),
    )
    # Publication points at the current legacy draft.
    conn.execute(
        "INSERT INTO publications "
        "(id, draft_id, platform, status, trace_id, created_at, updated_at) "
        "VALUES (1, 11, 'bluesky', 'published', 'trace-2', ?, ?)",
        (now, now),
    )

    conn.commit()
    conn.close()


def _build_v4_keyword_db(db_path: str) -> None:
    """A genuine v4-era database: LEGACY_SCHEMA upgraded through the real
    migrate_to_2..4 (so projects/project_keywords have exactly the v4
    shape — no match_type/intent/etc, those arrive at v5), seeded with one
    project and one keyword, stamped at user_version=4.
    """
    conn = _build_legacy_conn_at_version(db_path, 4)
    now = "2026-06-08T00:00:00+00:00"
    conn.execute(
        "INSERT INTO projects (key, name, description, link, active, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 1, ?, ?)",
        ("p", "Project", "Desc", "https://example.com", now, now),
    )
    conn.execute(
        "INSERT INTO project_keywords "
        "(project_key, keyword, evaluate_prompt, respond_prompt, critique_prompt, "
        "active, priority, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 1, 42, ?, ?)",
        ("p", "meta", None, None, None, now, now),
    )
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()


class TestFreshVsLegacyClassification:
    """A database is fresh only when user_version=0 AND sqlite_schema has
    no application objects; that's the only case that runs SCHEMA instead
    of the ordered MIGRATIONS path."""

    def test_fresh_empty_database_never_invokes_migrations(self, tmp_path, monkeypatch) -> None:
        import scout.storage.state as sm

        called = []
        original = sm.MIGRATIONS[2]

        def spy(conn: sqlite3.Connection) -> None:
            called.append(True)
            original(conn)

        monkeypatch.setitem(sm.MIGRATIONS, 2, spy)

        db_path = str(tmp_path / "fresh.db")
        with StateManager(db_path=db_path) as state:
            assert state.conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
        assert called == [], "a genuinely fresh database must never run migrations"

    def test_legacy_v0_with_application_objects_is_not_treated_as_fresh(
        self, tmp_path, monkeypatch
    ) -> None:
        import scout.storage.state as sm

        called = []
        original = sm.MIGRATIONS[2]

        def spy(conn: sqlite3.Connection) -> None:
            called.append(True)
            original(conn)

        monkeypatch.setitem(sm.MIGRATIONS, 2, spy)

        db_path = str(tmp_path / "legacy.db")
        _build_legacy_db(db_path)  # user_version=0, but real application tables exist
        with StateManager(db_path=db_path):
            pass
        assert called == [True], (
            "a v0 database carrying real application objects must run the "
            "MIGRATIONS path, not be mistaken for an empty database"
        )

    def test_newer_than_latest_version_is_rejected(self, tmp_path) -> None:
        from scout.storage.state import SCHEMA, UnsupportedSchemaVersionError

        db_path = str(tmp_path / "future.db")
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION + 1}")
        conn.commit()
        conn.close()

        with pytest.raises(UnsupportedSchemaVersionError):
            StateManager(db_path=db_path)


class TestMigrations:
    def test_v0_db_upgrades_cleanly_to_latest(self, tmp_path) -> None:
        """A real v0 install (per-version outbound drafts, no user_version)
        walks the whole migration chain: the content-engine tables it
        started with are carried through every historical migration and
        then dropped by migration 37, and every retained table is present
        at the end."""
        import sqlite3 as sq

        db_path = str(tmp_path / "legacy.db")
        _build_legacy_db(db_path)

        # Pre-condition: legacy DB has user_version=0 and the old shape.
        pre = sq.connect(db_path)
        assert pre.execute("PRAGMA user_version").fetchone()[0] == 0
        pre.close()

        with StateManager(db_path=db_path) as state:
            assert state.conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
            tables = {
                row["name"]
                for row in state.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert not tables & _CONTENT_ENGINE_TABLES
            assert "outbound_drafts_legacy" not in tables
            assert {"scans", "posts", "evaluations", "draft_comments", "grades"} <= tables

    def test_fresh_db_lands_at_v6(self, tmp_path) -> None:
        db_path = str(tmp_path / "fresh.db")
        with StateManager(db_path=db_path) as state:
            assert state.conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
            # The new tables exist and are empty.
            tables = {
                row["name"]
                for row in state.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert not tables & _CONTENT_ENGINE_TABLES
            assert "evaluations" in tables
            cols = {row["name"] for row in state.conn.execute("PRAGMA table_info(evaluations)")}
            assert "keyword_route_id" in cols


    def test_v4_keyword_db_upgrades_to_v6(self, tmp_path) -> None:
        db_path = str(tmp_path / "legacy-keyword.db")
        _build_v4_keyword_db(db_path)

        with StateManager(db_path=db_path) as state:
            assert state.conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
            cols = {
                row["name"] for row in state.conn.execute("PRAGMA table_info(project_keywords)")
            }
            assert {
                "match_type",
                "intent",
                "positive_context",
                "negative_context",
                "notes",
            }.issubset(cols)
            row = state.conn.execute(
                "SELECT match_type, intent, positive_context, negative_context, notes "
                "FROM project_keywords WHERE keyword = ?",
                ("meta",),
            ).fetchone()
            assert row is not None
            assert row["match_type"] == "substring"
            assert row["intent"] is None
            assert row["positive_context"] is None
            assert row["negative_context"] is None
            assert row["notes"] is None
            eval_cols = {
                row["name"] for row in state.conn.execute("PRAGMA table_info(evaluations)")
            }
            assert "keyword_route_id" in eval_cols


class TestScanDurability:
    """Regression tests for scan durability: watermark anchoring and failure metadata."""

    def test_get_last_scan_timestamp_reads_safe_watermark_at(
        self, in_memory_state: StateManager
    ) -> None:
        """get_last_scan_timestamp must return safe_watermark_at, not completed_at."""
        scan_id = in_memory_state.start_scan()
        in_memory_state.complete_scan(scan_id, 0, 0, status="complete")
        ts = in_memory_state.get_last_scan_timestamp()
        assert ts is not None
        row = in_memory_state.conn.execute(
            "SELECT safe_watermark_at, fetch_started_at FROM scans WHERE id = ?",
            (scan_id,),
        ).fetchone()
        assert row["safe_watermark_at"] is not None
        assert row["fetch_started_at"] is not None
        assert ts == datetime.fromisoformat(row["safe_watermark_at"])

    def test_partial_scan_does_not_advance_watermark(self, in_memory_state: StateManager) -> None:
        """Partial scans must not set safe_watermark_at (watermark must not advance)."""
        scan1 = in_memory_state.start_scan()
        in_memory_state.complete_scan(scan1, 0, 0, status="complete")
        first_watermark = in_memory_state.get_last_scan_timestamp()

        scan2 = in_memory_state.start_scan()
        in_memory_state.complete_scan(scan2, 0, 0, status="partial")

        watermark_after_partial = in_memory_state.get_last_scan_timestamp()
        assert watermark_after_partial == first_watermark

    def test_failed_scan_does_not_advance_watermark(self, in_memory_state: StateManager) -> None:
        scan1 = in_memory_state.start_scan()
        in_memory_state.complete_scan(scan1, 0, 0, status="complete")
        first_watermark = in_memory_state.get_last_scan_timestamp()

        scan2 = in_memory_state.start_scan()
        in_memory_state.complete_scan(scan2, 0, 0, status="failed")

        assert in_memory_state.get_last_scan_timestamp() == first_watermark

    def test_save_fetch_failure_persists_metadata(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()
        failure_id = in_memory_state.save_fetch_failure(
            scan_id=scan_id,
            platform="bluesky",
            kind="rate_limited",
            message="429 Too Many Requests",
            http_status=429,
            retry_after="30",
            retryable=True,
        )
        assert failure_id >= 1
        row = in_memory_state.conn.execute(
            "SELECT * FROM scan_fetch_failures WHERE id = ?", (failure_id,)
        ).fetchone()
        assert row is not None
        assert row["scan_id"] == scan_id
        assert row["platform"] == "bluesky"
        assert row["kind"] == "rate_limited"
        assert row["message"] == "429 Too Many Requests"
        assert row["http_status"] == 429
        assert row["retry_after"] == "30"
        assert row["retryable"] == 1

    def test_save_fetch_failure_with_context(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()
        failure_id = in_memory_state.save_fetch_failure(
            scan_id=scan_id,
            platform="farcaster",
            kind="network_error",
            message="connection timeout",
            context="channel:ai",
        )
        row = in_memory_state.conn.execute(
            "SELECT context FROM scan_fetch_failures WHERE id = ?", (failure_id,)
        ).fetchone()
        assert row["context"] == "channel:ai"

    def test_start_scan_records_fetch_started_at(self, in_memory_state: StateManager) -> None:
        before = datetime.now(UTC)
        scan_id = in_memory_state.start_scan()
        after = datetime.now(UTC)

        row = in_memory_state.conn.execute(
            "SELECT fetch_started_at FROM scans WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["fetch_started_at"] is not None
        fsa = datetime.fromisoformat(row["fetch_started_at"])
        assert before <= fsa <= after

    def test_start_scan_accepts_explicit_fetch_started_at(
        self, in_memory_state: StateManager
    ) -> None:
        explicit = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        scan_id = in_memory_state.start_scan(fetch_started_at=explicit)
        row = in_memory_state.conn.execute(
            "SELECT fetch_started_at FROM scans WHERE id = ?", (scan_id,)
        ).fetchone()
        assert datetime.fromisoformat(row["fetch_started_at"]) == explicit

    def test_complete_scan_uses_fetch_started_at_as_watermark(
        self, in_memory_state: StateManager
    ) -> None:
        """For complete scans, safe_watermark_at defaults to fetch_started_at."""
        explicit_fsa = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
        scan_id = in_memory_state.start_scan(fetch_started_at=explicit_fsa)
        in_memory_state.complete_scan(scan_id, 5, 2, status="complete")

        row = in_memory_state.conn.execute(
            "SELECT safe_watermark_at FROM scans WHERE id = ?", (scan_id,)
        ).fetchone()
        assert datetime.fromisoformat(row["safe_watermark_at"]) == explicit_fsa

    def test_migration_v6_to_v7_backfills_existing_scans(self, tmp_path) -> None:
        """Migration 007 backfills fetch_started_at from started_at and sets
        safe_watermark_at only for completed scans."""
        db_path = str(tmp_path / "v6.db")
        # A genuine v6-era database: LEGACY_SCHEMA upgraded through the real
        # migrate_to_2..6, with two scans: one completed, one in-progress.
        conn = _build_legacy_conn_at_version(db_path, 6)
        conn.execute(
            "INSERT INTO scans (started_at, completed_at, messages_scanned, relevant_found) "
            "VALUES ('2026-01-01T00:00:00+00:00', '2026-01-01T01:00:00+00:00', 10, 2)"
        )
        conn.execute("INSERT INTO scans (started_at) VALUES ('2026-01-02T00:00:00+00:00')")
        conn.execute("PRAGMA user_version = 6")
        conn.commit()
        conn.close()

        with StateManager(db_path=db_path) as state:
            assert state.conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION

            rows = state.conn.execute(
                "SELECT id, started_at, completed_at, fetch_started_at, safe_watermark_at, status "
                "FROM scans ORDER BY id"
            ).fetchall()
            assert len(rows) == 2

            # Completed scan: fetch_started_at = started_at, safe_watermark_at = started_at
            assert rows[0]["fetch_started_at"] == "2026-01-01T00:00:00+00:00"
            assert rows[0]["safe_watermark_at"] == "2026-01-01T00:00:00+00:00"
            assert rows[0]["status"] == "complete"

            # In-progress scan: fetch_started_at = started_at, safe_watermark_at = NULL
            assert rows[1]["fetch_started_at"] == "2026-01-02T00:00:00+00:00"
            assert rows[1]["safe_watermark_at"] is None
            assert rows[1]["status"] is None

            # scan_fetch_failures table must exist.
            tables = {
                r["name"]
                for r in state.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert "scan_fetch_failures" in tables

    def test_v9_migration_backfills_approved_revision_id(self, tmp_path) -> None:
        """Migration to v9 must backfill approved_revision_id for pre-existing
        approved rows so export_content_eval_cases doesn't silently drop them."""
        import sqlite3 as sq

        from scout.storage.migrations import _migrate_to_9

        conn = sq.connect(tmp_path / "pre9.db")
        conn.row_factory = sq.Row
        conn.execute("PRAGMA foreign_keys = OFF")

        # Minimal pre-v9 schema: one approved draft with two revisions.
        conn.executescript(
            """
            CREATE TABLE content_candidates (
                id INTEGER PRIMARY KEY,
                source TEXT, raw_payload TEXT, title TEXT,
                target_channels TEXT, target_audience TEXT,
                target_length INTEGER, status TEXT,
                root_trace_id TEXT, created_at TEXT, updated_at TEXT
            );
            CREATE TABLE outbound_drafts (
                id INTEGER PRIMARY KEY,
                candidate_id INTEGER,
                approved_at TEXT,
                rejected_at TEXT,
                updated_at TEXT,
                created_at TEXT
            );
            CREATE TABLE draft_revisions (
                id INTEGER PRIMARY KEY,
                draft_id INTEGER,
                version INTEGER,
                source TEXT,
                edited_text TEXT,
                trace_id TEXT,
                parent_revision_id INTEGER,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS evaluations (post_id TEXT);
            CREATE TABLE IF NOT EXISTS draft_annotations (revision_id INTEGER);
            CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY, scan_id INTEGER);
            CREATE TABLE IF NOT EXISTS draft_comments (id INTEGER PRIMARY KEY, scan_id INTEGER);
            """
        )
        conn.execute(
            "INSERT INTO content_candidates VALUES (1,'idea_dump','raw','t','[\"bluesky\"]',"
            "NULL,NULL,'approved','tr1','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO outbound_drafts VALUES (10, 1, '2026-01-01T12:00:00+00:00', "
            "NULL, '2026-01-01T12:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
        # Two revisions — MAX(version) is revision id=2.
        conn.execute(
            "INSERT INTO draft_revisions VALUES (1, 10, 1, 'initial', 'v1', 'tr1', NULL, "
            "'2026-01-01T10:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO draft_revisions VALUES (2, 10, 2, 'editor', 'v2', 'tr1', 1, "
            "'2026-01-01T11:00:00+00:00')"
        )
        conn.commit()

        _migrate_to_9(conn)
        conn.commit()

        row = conn.execute(
            "SELECT approved_revision_id FROM outbound_drafts WHERE id = 10"
        ).fetchone()
        assert row["approved_revision_id"] == 2  # MAX(version) revision

        conn.close()


class TestStandaloneMigration14:
    def test_dossier_schema_migration_preserves_operator_activation(self, tmp_path) -> None:
        """_migrate_to_14 never touches projects.active — only migration 18's
        later repair logic does. This guards against reintroducing the kind
        of silent-deactivation bug migration 18 had to repair in production."""
        from scout.storage.migrations import _migrate_to_14

        db_path = tmp_path / "migration-14.db"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE evaluations (id INTEGER PRIMARY KEY);
            CREATE TABLE draft_comments (id INTEGER PRIMARY KEY);
            CREATE TABLE scans (id INTEGER PRIMARY KEY);
            CREATE TABLE posts (id INTEGER PRIMARY KEY);
            CREATE TABLE projects (
                key TEXT PRIMARY KEY,
                active INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO projects (key, active) VALUES ('operator-project', 1);
            """
        )

        _migrate_to_14(conn)
        conn.commit()

        row = conn.execute(
            "SELECT active, dossier_summary_id FROM projects WHERE key = 'operator-project'"
        ).fetchone()
        assert tuple(row) == (1, None)
        conn.close()


# ---------------------------------------------------------------------------
# Publication claim tests
# ---------------------------------------------------------------------------


def _seed_approved_draft(state: StateManager) -> tuple[int, int, int]:
    """Create an approved candidate and return (candidate_id, draft_id, revision_id)."""
    cand_id, trace = state.add_candidate(
        source="idea_dump",
        raw_payload="payload",
        target_channels=["bluesky"],
        status="drafted",
    )
    revision_id = state.add_revision(cand_id, source="initial", edited_text="text", trace_id=trace)
    state.approve_candidate(cand_id, source="test")
    draft = state.get_or_create_draft(cand_id)
    state.commit()
    return cand_id, draft["id"], revision_id


def _seed_drafted_candidate(state: StateManager) -> tuple[int, int, int]:
    """Create a drafted (not approved) candidate. Returns (candidate_id, draft_id, revision_id)."""
    cand_id, trace = state.add_candidate(
        source="idea_dump",
        raw_payload="payload",
        target_channels=["bluesky"],
        status="drafted",
    )
    revision_id = state.add_revision(cand_id, source="initial", edited_text="text", trace_id=trace)
    draft = state.get_or_create_draft(cand_id)
    state.commit()
    return cand_id, draft["id"], revision_id


_VALID_SHA = "a" * 40


# ---------------------------------------------------------------------------
# Parent context persistence and healing
# ---------------------------------------------------------------------------


def _build_pre23_db(db_path: str) -> None:
    """Build a genuine pre-migration-23 database: the full latest SCHEMA
    minus every autonomy_events object, stamped at user_version=22.

    Unlike migration 22 (a same-shape data repair), migration 23 adds a
    brand-new table, so a fixture that leaves autonomy_events in place
    would not exercise the CREATE TABLE path at all — it must be genuinely
    absent going in.
    """
    from scout.storage.state import SCHEMA

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("DROP TRIGGER autonomy_events_no_update")
    conn.execute("DROP TRIGGER autonomy_events_no_delete")
    conn.execute("DROP INDEX autonomy_events_motion_event_unique")
    conn.execute("DROP INDEX autonomy_events_motion_idx")
    conn.execute("DROP INDEX autonomy_events_scope_idx")
    conn.execute("DROP TABLE autonomy_events")
    conn.execute("PRAGMA user_version = 22")
    conn.commit()
    conn.close()


class TestMigration23AutonomyEvents:
    """Migration 23 adds the append-only autonomy_events table, its two
    indexes, its unique (motion_id, event) index, and its append-only
    triggers — with no change to any existing table."""

    def test_migrates_v22_db_creates_empty_autonomy_events(self, tmp_path: pathlib.Path) -> None:
        db_path = str(tmp_path / "pre23.db")
        _build_pre23_db(db_path)

        with StateManager(db_path=db_path) as state:
            assert state.conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
            assert state.conn.execute("SELECT COUNT(*) FROM autonomy_events").fetchone()[0] == 0

    def test_idempotent_on_rerun(self, tmp_path: pathlib.Path) -> None:
        db_path = str(tmp_path / "pre23-idem.db")
        _build_pre23_db(db_path)

        with StateManager(db_path=db_path) as state:
            from scout.storage.migrations import _migrate_to_23

            _migrate_to_23(state.conn)
            _migrate_to_23(state.conn)
            state.conn.commit()
            assert state.conn.execute("SELECT COUNT(*) FROM autonomy_events").fetchone()[0] == 0

    def test_fresh_db_has_autonomy_events_table_at_latest_version(
        self, in_memory_state: StateManager
    ) -> None:
        assert (
            in_memory_state.conn.execute("PRAGMA user_version").fetchone()[0]
            == LATEST_SCHEMA_VERSION
        )
        assert (
            in_memory_state.conn.execute("SELECT COUNT(*) FROM autonomy_events").fetchone()[0] == 0
        )


_MIGRATION_24_NEW_TABLES = (
    "outbound_revision_provenance",
    "outbound_publication_verifications",
    "outbound_publish_reviews",
    "outbound_publish_review_decisions",
)


def _build_pre24_db(db_path: str) -> None:
    """Build a genuine pre-migration-24 database: the full latest SCHEMA
    minus publications.revision_id and every v24 table/index/trigger,
    stamped at user_version=23.

    Migration 24 adds a nullable column to an existing table plus four
    brand-new tables, so a fixture that left any of that in place would not
    exercise the real ALTER TABLE / CREATE TABLE paths — they must be
    genuinely absent going in.
    """
    from scout.storage.state import SCHEMA

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("DROP TRIGGER outbound_revision_provenance_no_update")
    conn.execute("DROP TRIGGER outbound_revision_provenance_no_delete")
    conn.execute("DROP TRIGGER outbound_publication_verifications_no_update")
    conn.execute("DROP TRIGGER outbound_publication_verifications_no_delete")
    conn.execute("DROP TRIGGER outbound_publish_reviews_no_update")
    conn.execute("DROP TRIGGER outbound_publish_reviews_no_delete")
    conn.execute("DROP TRIGGER outbound_publish_review_decisions_no_update")
    conn.execute("DROP TRIGGER outbound_publish_review_decisions_no_delete")
    conn.execute("DROP INDEX outbound_publication_verifications_attempt_unique")
    conn.execute("DROP INDEX outbound_publication_verifications_publication_idx")
    conn.execute("DROP INDEX outbound_publish_reviews_draft_revision_idx")
    conn.execute("DROP INDEX outbound_publish_review_decisions_reviewed_at_idx")
    for table in _MIGRATION_24_NEW_TABLES:
        conn.execute(f"DROP TABLE {table}")
    # SQLite refuses ALTER TABLE ... DROP COLUMN when the column appears in
    # the table's own foreign key definition, so drop and recreate the
    # (still-empty) table in its pre-v24 shape instead.
    conn.execute("DROP TABLE publications")
    conn.execute(
        """
        CREATE TABLE publications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            draft_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            platform_post_id TEXT,
            published_at TEXT,
            status TEXT NOT NULL,
            idempotency_key TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT,
            error_detail TEXT,
            trace_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (draft_id) REFERENCES outbound_drafts(id)
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX publications_unique_per_draft ON publications(draft_id, platform)"
    )
    conn.execute("PRAGMA user_version = 23")
    conn.commit()
    conn.close()


def _insert_event_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        event_id="evt-1",
        motion_id="mot-1",
        task="outbound_content_publish",
        declaration_version=1,
        scope="publish:bluesky",
        event="motion_proposed",
        from_position="hitl",
        to_position="hotl",
        evidence_ref="evidence/paa/" + "a" * 64 + "/evidence.json",
        evidence_sha256="a" * 64,
        actor="operator",
        reason="requested transition hitl to hotl",
        created_at="2026-01-01T00:00:00.000000Z",
    )
    base.update(overrides)
    return base


class TestAutonomyEventsConstraints:
    """SQL-level invariants on the append-only autonomy_events table:
    event/position value CHECKs, required non-null positions, the unique
    (motion_id, event) index, and the append-only UPDATE/DELETE triggers."""

    def test_valid_insert_succeeds(self, in_memory_state: StateManager) -> None:
        in_memory_state.insert_autonomy_event(**_insert_event_kwargs())  # type: ignore[arg-type]
        in_memory_state.db.commit()
        rows = in_memory_state.get_autonomy_events_for_motion("mot-1")
        assert len(rows) == 1
        assert rows[0]["event"] == "motion_proposed"

    def test_invalid_event_value_rejected(self, in_memory_state: StateManager) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.insert_autonomy_event(  # type: ignore[arg-type]
                **_insert_event_kwargs(event="not_a_real_event")
            )

    def test_invalid_from_position_rejected(self, in_memory_state: StateManager) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.insert_autonomy_event(  # type: ignore[arg-type]
                **_insert_event_kwargs(from_position="bogus")
            )

    def test_invalid_to_position_rejected(self, in_memory_state: StateManager) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.insert_autonomy_event(  # type: ignore[arg-type]
                **_insert_event_kwargs(to_position="bogus")
            )

    def test_null_position_rejected(self, in_memory_state: StateManager) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "INSERT INTO autonomy_events "
                "(id, motion_id, task, declaration_version, scope, event, "
                " from_position, to_position, evidence_ref, evidence_sha256, "
                " actor, reason, created_at) "
                "VALUES ('e', 'm', 't', 1, 's', 'motion_proposed', NULL, 'hotl', "
                "'r', 'h', 'a', 'r', 'c')"
            )

    def test_duplicate_motion_event_pair_rejected(self, in_memory_state: StateManager) -> None:
        in_memory_state.insert_autonomy_event(**_insert_event_kwargs())  # type: ignore[arg-type]
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.insert_autonomy_event(  # type: ignore[arg-type]
                **_insert_event_kwargs(event_id="evt-2")
            )

    def test_same_motion_different_event_allowed(self, in_memory_state: StateManager) -> None:
        in_memory_state.insert_autonomy_event(**_insert_event_kwargs())  # type: ignore[arg-type]
        in_memory_state.insert_autonomy_event(  # type: ignore[arg-type]
            **_insert_event_kwargs(event_id="evt-2", event="motion_approved")
        )
        in_memory_state.db.commit()
        assert len(in_memory_state.get_autonomy_events_for_motion("mot-1")) == 2

    def test_update_is_rejected_by_trigger(self, in_memory_state: StateManager) -> None:
        in_memory_state.insert_autonomy_event(**_insert_event_kwargs())  # type: ignore[arg-type]
        in_memory_state.db.commit()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            in_memory_state.conn.execute(
                "UPDATE autonomy_events SET actor = 'someone-else' WHERE id = 'evt-1'"
            )

    def test_delete_is_rejected_by_trigger(self, in_memory_state: StateManager) -> None:
        in_memory_state.insert_autonomy_event(**_insert_event_kwargs())  # type: ignore[arg-type]
        in_memory_state.db.commit()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            in_memory_state.conn.execute("DELETE FROM autonomy_events WHERE id = 'evt-1'")


class TestAutonomyEventsReadMethods:
    def test_get_latest_position_changed_event_scoped_exactly(
        self, in_memory_state: StateManager
    ) -> None:
        in_memory_state.insert_autonomy_event(  # type: ignore[arg-type]
            **_insert_event_kwargs(
                event_id="e1",
                motion_id="m1",
                event="position_changed",
                scope="publish:bluesky",
                created_at="2026-01-01T00:00:00.000000Z",
            )
        )
        in_memory_state.insert_autonomy_event(  # type: ignore[arg-type]
            **_insert_event_kwargs(
                event_id="e2",
                motion_id="m2",
                event="position_changed",
                scope="publish:farcaster",
                created_at="2026-01-02T00:00:00.000000Z",
            )
        )
        in_memory_state.db.commit()

        bluesky = in_memory_state.get_latest_position_changed_event(
            task="outbound_content_publish",
            declaration_version=1,
            scope="publish:bluesky",
        )
        assert bluesky is not None
        assert bluesky["id"] == "e1"

        farcaster = in_memory_state.get_latest_position_changed_event(
            task="outbound_content_publish",
            declaration_version=1,
            scope="publish:farcaster",
        )
        assert farcaster is not None
        assert farcaster["id"] == "e2"

        other_version = in_memory_state.get_latest_position_changed_event(
            task="outbound_content_publish",
            declaration_version=2,
            scope="publish:bluesky",
        )
        assert other_version is None

    def test_get_autonomy_events_filters_by_task(self, in_memory_state: StateManager) -> None:
        in_memory_state.insert_autonomy_event(  # type: ignore[arg-type]
            **_insert_event_kwargs(event_id="e1", motion_id="m1", task="outbound_content_publish")
        )
        in_memory_state.insert_autonomy_event(  # type: ignore[arg-type]
            **_insert_event_kwargs(event_id="e2", motion_id="m2", task="inbound_reply_surfacing")
        )
        in_memory_state.db.commit()

        assert len(in_memory_state.get_autonomy_events()) == 2
        assert len(in_memory_state.get_autonomy_events(task="outbound_content_publish")) == 1

    def test_get_position_changed_event_before_excludes_ties_and_later_rows(
        self, in_memory_state: StateManager
    ) -> None:
        in_memory_state.insert_autonomy_event(  # type: ignore[arg-type]
            **_insert_event_kwargs(
                event_id="e1",
                motion_id="m1",
                event="position_changed",
                created_at="2026-01-01T00:00:00.000000Z",
            )
        )
        in_memory_state.insert_autonomy_event(  # type: ignore[arg-type]
            **_insert_event_kwargs(
                event_id="e2",
                motion_id="m2",
                event="position_changed",
                created_at="2026-01-02T00:00:00.000000Z",
            )
        )
        in_memory_state.db.commit()

        # Strictly before e2's (created_at, id): finds e1.
        before_e2 = in_memory_state.get_position_changed_event_before(
            task="outbound_content_publish",
            declaration_version=1,
            scope="publish:bluesky",
            created_at="2026-01-02T00:00:00.000000Z",
            event_id="e2",
        )
        assert before_e2 is not None
        assert before_e2["id"] == "e1"

        # Strictly before e1's own (created_at, id): finds nothing — e1
        # itself must not be returned as "before" itself (id tie).
        before_e1 = in_memory_state.get_position_changed_event_before(
            task="outbound_content_publish",
            declaration_version=1,
            scope="publish:bluesky",
            created_at="2026-01-01T00:00:00.000000Z",
            event_id="e1",
        )
        assert before_e1 is None

        # Before an even earlier point: nothing.
        before_all = in_memory_state.get_position_changed_event_before(
            task="outbound_content_publish",
            declaration_version=1,
            scope="publish:bluesky",
            created_at="2025-12-31T00:00:00.000000Z",
            event_id="e0",
        )
        assert before_all is None


class TestMigration33AutonomyEventsContractShape:
    """Migration 33 rebuilds autonomy_events to match the paa_runtime
    EventStore contract: event_schema added, scope relaxed to nullable.

    Neither delta is reachable with ALTER TABLE, so the migration drops
    and recreates — defensible only while the table is empty, which it
    asserts rather than assumes.
    """

    def _v32_conn(self, db_path: str) -> sqlite3.Connection:
        return _build_legacy_conn_at_version(db_path, 32)

    def test_v32_shape_lacks_event_schema(self, tmp_path: pathlib.Path) -> None:
        """Guards the premise: if a future edit gives v32 the column, this
        migration is pointless and should be revisited rather than kept."""
        conn = self._v32_conn(str(tmp_path / "s.db"))
        cols = {row[1] for row in conn.execute("PRAGMA table_info(autonomy_events)")}
        conn.close()
        assert "event_schema" not in cols

    def test_migration_adds_event_schema(self, tmp_path: pathlib.Path) -> None:
        db_path = str(tmp_path / "s.db")
        conn = self._v32_conn(db_path)
        MIGRATIONS[33](conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(autonomy_events)")}
        conn.close()
        assert "event_schema" in cols

    def test_migration_relaxes_scope_to_nullable(self, tmp_path: pathlib.Path) -> None:
        db_path = str(tmp_path / "s.db")
        conn = self._v32_conn(db_path)
        MIGRATIONS[33](conn)
        notnull = {row[1]: row[3] for row in conn.execute("PRAGMA table_info(autonomy_events)")}
        conn.close()
        assert notnull["scope"] == 0

    def test_migration_preserves_append_only_triggers(self, tmp_path: pathlib.Path) -> None:
        """DROP TABLE takes the triggers with it, so they have to come
        back — an append-only table without them is append-only by
        convention, which is exactly what the protocol forbids."""
        db_path = str(tmp_path / "s.db")
        conn = self._v32_conn(db_path)
        MIGRATIONS[33](conn)
        triggers = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name = 'autonomy_events'"
            )
        }
        conn.close()
        assert triggers == {"autonomy_events_no_update", "autonomy_events_no_delete"}

    def test_migration_preserves_indexes(self, tmp_path: pathlib.Path) -> None:
        db_path = str(tmp_path / "s.db")
        conn = self._v32_conn(db_path)
        MIGRATIONS[33](conn)
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'autonomy_events' AND name NOT LIKE 'sqlite_%'"
            )
        }
        conn.close()
        assert indexes == {
            "autonomy_events_scope_idx",
            "autonomy_events_motion_idx",
            "autonomy_events_motion_event_unique",
        }

    def test_migration_refuses_to_run_with_rows_present(self, tmp_path: pathlib.Path) -> None:
        """The one place in this migration where silent data loss is
        possible. It must abort rather than discard governance history."""
        db_path = str(tmp_path / "s.db")
        conn = self._v32_conn(db_path)
        conn.execute(
            "INSERT INTO autonomy_events (id, motion_id, task, declaration_version, scope, "
            "event, from_position, to_position, evidence_ref, evidence_sha256, actor, "
            "reason, created_at) VALUES "
            "('e1', 'm1', 'outbound_content_publish', 1, 'publish:bluesky', "
            "'position_changed', 'hitl', 'hotl', 'ref', 'a', 'operator', 'why', "
            "'2026-01-01T00:00:00.000000Z')"
        )
        with pytest.raises(AutonomyEventsNotEmptyError):
            MIGRATIONS[33](conn)
        conn.close()

    def test_refusal_leaves_the_existing_row_intact(self, tmp_path: pathlib.Path) -> None:
        db_path = str(tmp_path / "s.db")
        conn = self._v32_conn(db_path)
        conn.execute(
            "INSERT INTO autonomy_events (id, motion_id, task, declaration_version, scope, "
            "event, from_position, to_position, evidence_ref, evidence_sha256, actor, "
            "reason, created_at) VALUES "
            "('e1', 'm1', 'outbound_content_publish', 1, 'publish:bluesky', "
            "'position_changed', 'hitl', 'hotl', 'ref', 'a', 'operator', 'why', "
            "'2026-01-01T00:00:00.000000Z')"
        )
        with pytest.raises(AutonomyEventsNotEmptyError):
            MIGRATIONS[33](conn)
        (count,) = conn.execute("SELECT COUNT(*) FROM autonomy_events").fetchone()
        conn.close()
        assert count == 1

    def test_null_scope_is_insertable_after_migration(self, tmp_path: pathlib.Path) -> None:
        """The behavioural point of the whole migration: a declaration
        with no `scopes:` resolves at scope None and must be recordable."""
        db_path = str(tmp_path / "s.db")
        conn = self._v32_conn(db_path)
        MIGRATIONS[33](conn)
        conn.execute(
            "INSERT INTO autonomy_events (event_schema, id, motion_id, task, "
            "declaration_version, scope, event, from_position, to_position, evidence_ref, "
            "evidence_sha256, actor, reason, created_at) VALUES "
            "('paa-autonomy-event/0.1.0-draft', 'e1', 'm1', 'canonical_promotion', 1, NULL, "
            "'position_changed', 'manual', 'hitl', 'ref', 'a', 'operator', 'why', "
            "'2026-01-01T00:00:00.000000Z')"
        )
        (scope,) = conn.execute("SELECT scope FROM autonomy_events WHERE id = 'e1'").fetchone()
        conn.close()
        assert scope is None


# ---------------------------------------------------------------------------
# Migration 26: grade_revisions + grade_usage_overrides
# ---------------------------------------------------------------------------

