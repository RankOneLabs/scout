"""Shared legacy-schema DB builders for migration-path tests.

LEGACY_SCHEMA is the real v0 install shape; build_legacy_conn_at_version
runs it through the actual ordered MIGRATIONS up to a target version, so
tests across test_state_manager.py, test_evaluation_store.py, and
test_grade_store.py exercise a genuine historical shape rather than a
hand-maintained approximation. schema_snapshot/extract_check_clauses are
the structural-comparison helpers migration-convergence tests use to diff
two independently produced databases.
"""

from __future__ import annotations

import sqlite3

LEGACY_SCHEMA = """
CREATE TABLE scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    messages_scanned INTEGER DEFAULT 0,
    relevant_found INTEGER DEFAULT 0
);

CREATE TABLE posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    platform_msg_id TEXT NOT NULL,
    channel_name TEXT,
    channel_id TEXT,
    author_name TEXT,
    author_id TEXT,
    content TEXT,
    url TEXT,
    created_at TEXT,
    scan_id INTEGER,
    UNIQUE(platform, platform_msg_id)
);

CREATE TABLE evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL,
    relevant INTEGER NOT NULL,
    score REAL NOT NULL,
    reason TEXT,
    relevant_to TEXT,
    scan_id INTEGER,
    FOREIGN KEY (post_id) REFERENCES posts(id)
);

CREATE TABLE draft_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL,
    evaluation_id INTEGER NOT NULL,
    project_key TEXT,
    comment_text TEXT,
    created_at TEXT,
    scan_id INTEGER,
    FOREIGN KEY (post_id) REFERENCES posts(id),
    FOREIGN KEY (evaluation_id) REFERENCES evaluations(id)
);

CREATE TABLE critiques (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    feedback TEXT,
    created_at TEXT,
    scan_id INTEGER,
    FOREIGN KEY (draft_id) REFERENCES draft_comments(id)
);

CREATE TABLE grades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    FOREIGN KEY (post_id) REFERENCES posts(id),
    UNIQUE(post_id)
);

CREATE TABLE content_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_ref TEXT,
    raw_payload TEXT NOT NULL,
    title TEXT,
    target_channels TEXT NOT NULL,
    target_audience TEXT,
    target_length INTEGER,
    status TEXT NOT NULL,
    root_trace_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE outbound_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    superseded_at TEXT,
    claim TEXT,
    evidence TEXT,
    pushback_angles TEXT,
    edited_text TEXT,
    trace_id TEXT,
    drafted_at TEXT,
    edited_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (candidate_id, version),
    FOREIGN KEY (candidate_id) REFERENCES content_candidates(id)
);

CREATE UNIQUE INDEX outbound_drafts_current
    ON outbound_drafts (candidate_id) WHERE superseded_at IS NULL;

CREATE TABLE publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    platform_post_id TEXT,
    published_at TEXT,
    status TEXT NOT NULL,
    error_detail TEXT,
    trace_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (draft_id) REFERENCES outbound_drafts(id)
);

CREATE UNIQUE INDEX publications_unique_per_draft
    ON publications(draft_id, platform);
"""



def build_legacy_conn_at_version(db_path: str, version: int) -> sqlite3.Connection:
    """Return a connection to `db_path` at a genuine `version`-era shape.

    Runs the real LEGACY_SCHEMA (the v0 install shape) through the actual
    ordered MIGRATIONS up to `version`, so every table/column a later
    migration or index-creation statement expects is exactly what a real
    database at that version would have — no SCHEMA pre-run backfills it
    anymore. `user_version` is left unset; callers stamp it themselves once
    any extra seed data is inserted, mirroring a real never-restamped v0
    history. The connection is left open (uncommitted) for the caller to
    seed further data before committing.
    """
    from scout.storage.state import MIGRATIONS

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(LEGACY_SCHEMA)
    for v in sorted(v for v in MIGRATIONS if v <= version):
        MIGRATIONS[v](conn)
    return conn


def extract_check_clauses(sql: str) -> list[str]:
    """Extract every balanced-paren `CHECK(...)` clause body from a CREATE
    TABLE statement's realized DDL, whitespace-normalized.

    Column order (and therefore raw text layout) legitimately differs
    between a table built by CREATE TABLE and one built by successive
    ALTER TABLE ADD COLUMN calls, so comparing full statement text is too
    strict. CHECK constraint wording is not — this pulls just that
    substring out so it can be compared on its own, order-insensitively.
    """
    clauses = []
    marker = "CHECK("
    start = 0
    while True:
        idx = sql.find(marker, start)
        if idx == -1:
            break
        depth = 0
        i = idx + len(marker) - 1  # position of the opening '('
        end = i
        for j in range(i, len(sql)):
            if sql[j] == "(":
                depth += 1
            elif sql[j] == ")":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        clause = sql[idx + len(marker) : end]
        clauses.append(" ".join(clause.split()))
        start = end + 1
    return clauses


def schema_snapshot(conn: sqlite3.Connection) -> tuple[dict[str, object], int]:
    """A normalized structural snapshot of every application object in
    `conn`, plus its PRAGMA user_version — the comparison T-007 runs
    across independently produced databases.

    Column order is intentionally erased (compared as a set) since
    ALTER-built and CREATE-built tables order columns differently for the
    same logical shape; index key order is intentionally preserved since
    it is semantically load-bearing.
    """
    tables = sorted(
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    )
    snapshot: dict[str, object] = {}
    for table in tables:
        cols = {
            (row["name"], row["type"], row["notnull"], row["dflt_value"], row["pk"])
            for row in conn.execute(f"PRAGMA table_xinfo({table})")
        }
        fks = {
            (row["table"], row["from"], row["to"], row["on_update"], row["on_delete"])
            for row in conn.execute(f"PRAGMA foreign_key_list({table})")
        }
        idx_list = conn.execute(f"PRAGMA index_list({table})").fetchall()
        indexes = {}
        for idx in idx_list:
            key_cols = tuple(
                (row["name"], row["desc"])
                for row in conn.execute(f"PRAGMA index_xinfo({idx['name']})")
                if row["key"]
            )
            indexes[idx["name"]] = (idx["unique"], idx["partial"], key_cols)
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()["sql"]
        snapshot[table] = {
            "cols": cols,
            "fks": fks,
            "indexes": indexes,
            "checks": sorted(extract_check_clauses(table_sql)),
        }
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    return snapshot, user_version

