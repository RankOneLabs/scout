"""Ordered SQLite migration registry for scout's database.

Owns every versioned migration function (2 through LATEST_SCHEMA_VERSION,
see schema.py) and the MIGRATIONS registry that
state_manager.StateManager._init_schema walks to bring an existing
database up to the latest schema. Dependency-light by design: nothing
here imports state_manager, so schema.py, migrations.py, and
storage/state.py form a one-directional import chain rather than a cycle.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from scout.config import RELEVANCE_THRESHOLD

Migration = Callable[[sqlite3.Connection], None]


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    """True when *name* exists as a table.

    Migrations 3 through 24 alter the outbound content tables that
    migration 37 later drops. On a real database that reached them those
    tables always exist, so the guards below never change production
    behaviour; they exist for databases bootstrapped from a post-v37
    SCHEMA and stamped at an older version (the migration test harness),
    which never had the tables at all.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _normalize_legacy_graded_at(value: str) -> str:
    """Normalize a historical graded_at value to the canonical form.

    Legacy writers spelled instants with a literal 'Z', an explicit
    numeric offset, or no offset at all. A 'Z' or offset value is parsed
    as the instant it names; a naive value is treated as UTC, since every
    historical writer intended UTC. Raises ValueError for anything that
    doesn't parse as a datetime at all — migration 20 must abort on those
    rather than fabricate a chronology.
    """
    text = value.strip()
    iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    parsed = datetime.fromisoformat(iso_text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    as_utc = parsed.astimezone(UTC)
    millis = as_utc.microsecond // 1000
    return as_utc.strftime("%Y-%m-%dT%H:%M:%S") + f".{millis:03d}Z"


def _migrate_to_2(conn: sqlite3.Connection) -> None:
    """Migrate from v0 (per-version outbound_drafts) to v2 (singleton + revisions).

    Runs inside a single transaction managed by the caller. Idempotent for
    databases that never had the legacy shape: when the legacy `version`
    column isn't present on `outbound_drafts`, this is a no-op.

    For legacy databases:
      1. Rename outbound_drafts → outbound_drafts_legacy and drop the
         partial unique index it relied on.
      2. Create the singleton outbound_drafts table, plus draft_revisions
         and draft_annotations (IF NOT EXISTS, in their final v22 shape —
         later migrations that add columns to these tables guard each
         ALTER with a column-existence check, so creating them here with
         the full final shape is idempotent with respect to those).
      3. Backfill: one outbound_drafts row per distinct candidate; one
         draft_revisions row per legacy row, all with source='initial'
         and parent_revision_id=NULL.
      4. Repoint publications.draft_id from legacy ids to the singleton id
         for the same candidate.
      5. Drop outbound_drafts_legacy.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(outbound_drafts)")}
    if "version" not in cols:
        # Already singleton-shaped — nothing to migrate.
        return

    # SQLite 3.26+ default behavior rewrites FK references in dependent
    # tables when ALTER TABLE ... RENAME fires. Without legacy_alter_table=ON
    # the FKs in draft_revisions / draft_annotations / publications would
    # follow the rename to outbound_drafts_legacy and then dangle once
    # the legacy table is dropped — breaking future inserts as soon as FK
    # enforcement comes back on. Keeping the FK strings frozen at
    # "outbound_drafts" means they re-bind to the singleton we recreate
    # below.
    conn.execute("PRAGMA legacy_alter_table = ON")
    conn.execute("DROP INDEX IF EXISTS outbound_drafts_current")
    conn.execute("ALTER TABLE outbound_drafts RENAME TO outbound_drafts_legacy")
    conn.execute(
        """
        CREATE TABLE outbound_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL UNIQUE,
            approved_at TEXT,
            rejected_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (candidate_id) REFERENCES content_candidates(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS draft_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            draft_id INTEGER NOT NULL,
            version INTEGER NOT NULL,
            parent_revision_id INTEGER,
            claim TEXT,
            evidence TEXT,
            pushback_angles TEXT,
            edited_text TEXT,
            source TEXT NOT NULL CHECK(source IN (
                'initial', 'regenerate', 'inline_edit', 'editor', 'external'
            )),
            feedback_summary TEXT,
            regenerate_verbs TEXT,
            regenerate_annotation_count INTEGER,
            regenerate_freetext_present INTEGER,
            trace_id TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (draft_id, version),
            FOREIGN KEY (draft_id) REFERENCES outbound_drafts(id),
            FOREIGN KEY (parent_revision_id) REFERENCES draft_revisions(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS draft_annotations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            draft_id INTEGER NOT NULL,
            revision_id INTEGER NOT NULL,
            text_anchor TEXT NOT NULL,
            start_offset INTEGER NOT NULL,
            end_offset INTEGER NOT NULL,
            feedback_verb TEXT NOT NULL,
            feedback_note TEXT,
            origin TEXT,
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            FOREIGN KEY (draft_id) REFERENCES outbound_drafts(id),
            FOREIGN KEY (revision_id) REFERENCES draft_revisions(id)
        )
        """
    )

    # Backfill the singleton: one row per distinct candidate. approved_at /
    # rejected_at are inferred from the candidate's current status — the
    # closest stamp we have to "when this draft reached that state". Precise
    # historical timing isn't load-bearing for legacy rows.
    legacy_candidates = conn.execute(
        """
        SELECT candidate_id,
               MIN(created_at) AS min_created,
               MAX(updated_at) AS max_updated
        FROM outbound_drafts_legacy
        GROUP BY candidate_id
        """
    ).fetchall()
    for row in legacy_candidates:
        cand_id = row["candidate_id"]
        cand = conn.execute(
            "SELECT status, updated_at FROM content_candidates WHERE id = ?",
            (cand_id,),
        ).fetchone()
        if cand is None:
            # Orphaned legacy draft — skip. FK on the new singleton would
            # reject it anyway and a v0 install with orphans is malformed.
            continue
        status = cand["status"]
        approved_at = cand["updated_at"] if status in ("approved", "published") else None
        rejected_at = cand["updated_at"] if status == "rejected" else None
        conn.execute(
            """
            INSERT INTO outbound_drafts
            (candidate_id, approved_at, rejected_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (cand_id, approved_at, rejected_at, row["min_created"], row["max_updated"]),
        )

    # Backfill revisions: each legacy row becomes one draft_revisions row
    # keyed to its candidate's singleton. parent_revision_id=NULL across the
    # board — v0 had no parent-link semantics to preserve. created_at folds
    # the edited_at/drafted_at signal so "when did this revision exist" is
    # still recoverable from the row.
    legacy_rows = conn.execute(
        """
        SELECT id, candidate_id, version, claim, evidence, pushback_angles,
               edited_text, trace_id, drafted_at, edited_at, created_at
        FROM outbound_drafts_legacy
        ORDER BY candidate_id, version
        """
    ).fetchall()
    legacy_to_singleton: dict[int, int] = {}
    for legacy in legacy_rows:
        singleton = conn.execute(
            "SELECT id FROM outbound_drafts WHERE candidate_id = ?",
            (legacy["candidate_id"],),
        ).fetchone()
        if singleton is None:
            continue
        singleton_id = singleton["id"]
        legacy_to_singleton[legacy["id"]] = singleton_id
        revision_created = (
            legacy["edited_at"] or legacy["drafted_at"] or legacy["created_at"]
        )
        conn.execute(
            """
            INSERT INTO draft_revisions
            (draft_id, version, parent_revision_id, claim, evidence,
             pushback_angles, edited_text, source, feedback_summary,
             trace_id, created_at)
            VALUES (?, ?, NULL, ?, ?, ?, ?, 'initial', NULL, ?, ?)
            """,
            (
                singleton_id,
                legacy["version"],
                legacy["claim"],
                legacy["evidence"],
                legacy["pushback_angles"],
                legacy["edited_text"],
                legacy["trace_id"],
                revision_created,
            ),
        )

    # Repoint publications.draft_id at the singleton row. publications was
    # 1:1 with legacy rows; collapsing into a singleton means many legacy
    # ids may map to the same singleton — that's expected and matches the
    # "one publication per (draft, platform)" invariant the unique index
    # already enforces.
    pub_rows = conn.execute("SELECT id, draft_id FROM publications").fetchall()
    for pub in pub_rows:
        new_draft_id = legacy_to_singleton.get(pub["draft_id"])
        if new_draft_id is None:
            continue
        conn.execute(
            "UPDATE publications SET draft_id = ? WHERE id = ?",
            (new_draft_id, pub["id"]),
        )

    conn.execute("DROP TABLE outbound_drafts_legacy")

    # Restore default ALTER semantics so subsequent schema work on this
    # connection isn't surprised by the pragma we flipped above.
    conn.execute("PRAGMA legacy_alter_table = OFF")


def _migrate_to_3(conn: sqlite3.Connection) -> None:
    """Add `outbound_drafts.doc_level_freetext` for cohort 6 round-trips.

    Idempotent: if the column already exists (fresh DB created via the
    baseline SCHEMA, which includes the column), the ALTER is skipped.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(outbound_drafts)")}
    if not cols or "doc_level_freetext" in cols:
        return
    conn.execute("ALTER TABLE outbound_drafts ADD COLUMN doc_level_freetext TEXT")


def _migrate_to_4(conn: sqlite3.Connection) -> None:
    """Add projects, project_keywords, and prompt_templates tables (schema v4).

    Idempotent: the CREATE TABLE/INDEX IF NOT EXISTS statements are no-ops on
    fresh databases that already have these tables from the baseline SCHEMA.
    Uses individual conn.execute() calls (not executescript) so this migration
    participates in the caller-managed transaction in _init_schema.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            key TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            link TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_key TEXT NOT NULL,
            keyword TEXT NOT NULL,
            evaluate_prompt TEXT,
            respond_prompt TEXT,
            critique_prompt TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            priority INTEGER NOT NULL DEFAULT 100,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (project_key) REFERENCES projects(key),
            UNIQUE(project_key, keyword)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS project_keywords_active_idx"
        " ON project_keywords(active, priority, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS project_keywords_project_idx"
        " ON project_keywords(project_key)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prompt_templates (
            name TEXT PRIMARY KEY,
            body TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(
                kind IN ('evaluate', 'respond', 'critique', 'shared', 'custom')
            ),
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def _migrate_to_5(conn: sqlite3.Connection) -> None:
    """Add structured keyword metadata columns for schema v5."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(project_keywords)")}
    if "match_type" not in cols:
        conn.execute(
            "ALTER TABLE project_keywords ADD COLUMN match_type TEXT NOT NULL DEFAULT 'substring'"
        )
    if "intent" not in cols:
        conn.execute("ALTER TABLE project_keywords ADD COLUMN intent TEXT")
    if "positive_context" not in cols:
        conn.execute("ALTER TABLE project_keywords ADD COLUMN positive_context TEXT")
    if "negative_context" not in cols:
        conn.execute("ALTER TABLE project_keywords ADD COLUMN negative_context TEXT")
    if "notes" not in cols:
        conn.execute("ALTER TABLE project_keywords ADD COLUMN notes TEXT")


def _migrate_to_6(conn: sqlite3.Connection) -> None:
    """Add nullable evaluation route persistence for schema v6."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(evaluations)")}
    if "keyword_route_id" in cols:
        return
    conn.execute(
        "ALTER TABLE evaluations ADD COLUMN keyword_route_id INTEGER "
        "REFERENCES project_keywords(id)"
    )


def _migrate_to_7(conn: sqlite3.Connection) -> None:
    """Add scan durability metadata for schema v7.

    Adds fetch_started_at, safe_watermark_at, and status to scans; creates
    scan_fetch_failures for per-platform failure recording.

    Backfill: fetch_started_at = started_at for all existing rows;
    safe_watermark_at = started_at only for completed scans (so failed/partial
    historical rows don't advance the watermark); status = 'complete' for
    completed scans, NULL otherwise.
    """
    scan_cols = {row["name"] for row in conn.execute("PRAGMA table_info(scans)")}
    if "fetch_started_at" not in scan_cols:
        conn.execute("ALTER TABLE scans ADD COLUMN fetch_started_at TEXT")
    if "safe_watermark_at" not in scan_cols:
        conn.execute("ALTER TABLE scans ADD COLUMN safe_watermark_at TEXT")
    if "status" not in scan_cols:
        conn.execute("ALTER TABLE scans ADD COLUMN status TEXT")

    conn.execute("UPDATE scans SET fetch_started_at = started_at WHERE fetch_started_at IS NULL")
    conn.execute(
        "UPDATE scans SET safe_watermark_at = started_at, status = 'complete' "
        "WHERE completed_at IS NOT NULL AND safe_watermark_at IS NULL"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scan_fetch_failures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            context TEXT,
            kind TEXT NOT NULL,
            message TEXT,
            http_status INTEGER,
            retry_after TEXT,
            retryable INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY (scan_id) REFERENCES scans(id)
        )
        """
    )


def _migrate_to_8(conn: sqlite3.Connection) -> None:
    """Add overflow_count to scans for schema v8."""
    scan_cols = {row["name"] for row in conn.execute("PRAGMA table_info(scans)")}
    if "overflow_count" not in scan_cols:
        conn.execute("ALTER TABLE scans ADD COLUMN overflow_count INTEGER DEFAULT 0")
        # SQLite ADD COLUMN leaves existing rows NULL regardless of DEFAULT;
        # backfill so every row satisfies the non-null semantic.
        conn.execute("UPDATE scans SET overflow_count = 0 WHERE overflow_count IS NULL")


def _migrate_to_9(conn: sqlite3.Connection) -> None:
    """Add approved_revision_id, decision_source, decision_reason to outbound_drafts (v9).

    Also creates the identity-lookup indexes on evaluations(post_id),
    draft_revisions(draft_id), draft_annotations(revision_id), posts(scan_id),
    and draft_comments(scan_id).

    Idempotent: ALTER TABLE is skipped when the column already exists.
    CREATE INDEX IF NOT EXISTS is always safe to repeat.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(outbound_drafts)")}
    if cols and "approved_revision_id" not in cols:
        conn.execute(
            "ALTER TABLE outbound_drafts ADD COLUMN approved_revision_id INTEGER "
            "REFERENCES draft_revisions(id)"
        )
        # Backfill: point approved_revision_id at the MAX(version) revision for
        # every draft that was already approved before this migration.  Uses
        # MAX(version) because approved_revision_id didn't exist yet — this is
        # exactly the heuristic the old export query used, so it preserves the
        # historical signal rather than silently dropping it from eval exports.
        conn.execute(
            "UPDATE outbound_drafts SET approved_revision_id = ("
            "  SELECT r.id FROM draft_revisions r"
            "  WHERE r.draft_id = outbound_drafts.id"
            "  ORDER BY r.version DESC LIMIT 1"
            ") WHERE approved_at IS NOT NULL AND approved_revision_id IS NULL"
        )
    if cols and "decision_source" not in cols:
        conn.execute("ALTER TABLE outbound_drafts ADD COLUMN decision_source TEXT")
    if cols and "decision_reason" not in cols:
        conn.execute("ALTER TABLE outbound_drafts ADD COLUMN decision_reason TEXT")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS evaluations_post_id_idx ON evaluations(post_id)"
    )
    if _has_table(conn, "draft_revisions"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS draft_revisions_draft_id_idx ON draft_revisions(draft_id)"
        )
    if _has_table(conn, "draft_annotations"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS draft_annotations_revision_id_idx"
            " ON draft_annotations(revision_id)"
        )
    conn.execute("CREATE INDEX IF NOT EXISTS posts_scan_id_idx ON posts(scan_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS draft_comments_scan_id_idx ON draft_comments(scan_id)"
    )


def _migrate_to_10(conn: sqlite3.Connection) -> None:
    """Add created_at to evaluations for deterministic latest-evaluation ordering (v10).

    Idempotent: ALTER TABLE is skipped when created_at already exists (fresh
    databases get it from the baseline SCHEMA). Backfills from scans.started_at
    for evaluations that carry a scan_id; leaves NULL for any legacy orphans.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(evaluations)")}
    if "created_at" not in cols:
        conn.execute("ALTER TABLE evaluations ADD COLUMN created_at TEXT")
    conn.execute(
        "UPDATE evaluations SET created_at = ("
        "  SELECT started_at FROM scans WHERE scans.id = evaluations.scan_id"
        ") WHERE created_at IS NULL AND scan_id IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS evaluations_created_at_idx"
        " ON evaluations(created_at, id)"
    )


def _migrate_to_11(conn: sqlite3.Connection) -> None:
    """Persist feedback metadata and make grades evaluation-scoped (v11)."""
    draft_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(outbound_drafts)")
    }
    if draft_cols and "rejected_source" not in draft_cols:
        conn.execute("ALTER TABLE outbound_drafts ADD COLUMN rejected_source TEXT")
    if draft_cols and "rejected_reason" not in draft_cols:
        conn.execute("ALTER TABLE outbound_drafts ADD COLUMN rejected_reason TEXT")

    annotation_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(draft_annotations)")
    }
    if annotation_cols and "origin" not in annotation_cols:
        conn.execute("ALTER TABLE draft_annotations ADD COLUMN origin TEXT")

    revision_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(draft_revisions)")
    }
    if revision_cols and "regenerate_verbs" not in revision_cols:
        conn.execute("ALTER TABLE draft_revisions ADD COLUMN regenerate_verbs TEXT")
    if revision_cols and "regenerate_annotation_count" not in revision_cols:
        conn.execute(
            "ALTER TABLE draft_revisions ADD COLUMN regenerate_annotation_count INTEGER"
        )
    if revision_cols and "regenerate_freetext_present" not in revision_cols:
        conn.execute(
            "ALTER TABLE draft_revisions ADD COLUMN regenerate_freetext_present INTEGER"
        )

    grade_cols = {row["name"] for row in conn.execute("PRAGMA table_info(grades)")}
    if "evaluation_id" not in grade_cols:
        conn.execute("ALTER TABLE grades ADD COLUMN evaluation_id INTEGER")

    conn.execute(
        "UPDATE grades SET evaluation_id = ("
        "  SELECT e.id FROM evaluations e"
        "  WHERE e.post_id = grades.post_id"
        "    AND (grades.scan_id IS NULL OR e.scan_id = grades.scan_id)"
        "  ORDER BY e.created_at DESC, e.id DESC LIMIT 1"
        ") WHERE evaluation_id IS NULL"
    )

    # Rebuild to remove the old UNIQUE(post_id) table constraint. A post can
    # be evaluated more than once, and each evaluation needs its own grade.
    conn.execute("ALTER TABLE grades RENAME TO grades_v10")
    conn.execute(
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
            FOREIGN KEY (evaluation_id) REFERENCES evaluations(id),
            FOREIGN KEY (post_id) REFERENCES posts(id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO grades (
            id, evaluation_id, post_id, scan_id, source, graded_at,
            relevance_judgment, rejection_reason, relevance_note,
            comment_quality, comment_issue, comment_note
        )
        SELECT id, evaluation_id, post_id, scan_id, source, graded_at,
               relevance_judgment, rejection_reason, relevance_note,
               comment_quality, comment_issue, comment_note
        FROM grades_v10
        """
    )
    conn.execute("DROP TABLE grades_v10")

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS grades_evaluation_id_unique"
        " ON grades(evaluation_id) WHERE evaluation_id IS NOT NULL"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS grades_scan_id_idx ON grades(scan_id)")


def _migrate_to_12(conn: sqlite3.Connection) -> None:
    """Add idempotency_key, attempt_count, last_attempt_at to publications (v12).

    These columns support the durable publish invariant: the idempotency key is
    derived once and reused on all retries, attempt_count tracks how many times a
    row has been claimed, and last_attempt_at anchors the stale-publishing timeout.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(publications)")}
    if cols and "idempotency_key" not in cols:
        conn.execute("ALTER TABLE publications ADD COLUMN idempotency_key TEXT")
    if cols and "attempt_count" not in cols:
        conn.execute(
            "ALTER TABLE publications ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0"
        )
        conn.execute("UPDATE publications SET attempt_count = 0 WHERE attempt_count IS NULL")
    if cols and "last_attempt_at" not in cols:
        conn.execute("ALTER TABLE publications ADD COLUMN last_attempt_at TEXT")


def _migrate_to_13(conn: sqlite3.Connection) -> None:
    """Add parent context columns to posts (v13).

    Adds parent_lookup_status (not_applicable|resolved|failed) and nullable flat
    parent_id/author_id/author_name/text/url columns. Backfills existing rows to
    not_applicable so Discord and Farcaster posts stay compatible without re-scanning.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(posts)")}
    if "parent_lookup_status" not in cols:
        conn.execute(
            "ALTER TABLE posts ADD COLUMN parent_lookup_status TEXT NOT NULL "
            "DEFAULT 'not_applicable'"
        )
        conn.execute(
            "UPDATE posts SET parent_lookup_status = 'not_applicable' "
            "WHERE parent_lookup_status IS NULL"
        )
    for col in ("parent_id", "parent_author_id", "parent_author_name", "parent_text", "parent_url"):
        if col not in cols:
            conn.execute(f"ALTER TABLE posts ADD COLUMN {col} TEXT")


def _migrate_to_14(conn: sqlite3.Connection) -> None:
    """Add dossier-grounded drafting schema (v14).

    Idempotent: every ALTER TABLE is guarded by a column-name check;
    CREATE TABLE/INDEX use IF NOT EXISTS.

    Changes:
      - evaluations: +project_key, +posture, +abstain_reason,
        +surface_status (default legacy_unknown), +dossier_summary_id,
        +dossier_revision
      - draft_comments: +posture, +structured_output, +dossier_summary_id,
        +dossier_revision
      - scans: +dossier_revision
      - projects: +dossier_summary_id
      - NEW gate_blocks table + indexes
      - NEW surfaced_events table + index
    """
    eval_cols = {row["name"] for row in conn.execute("PRAGMA table_info(evaluations)")}
    for col, ddl in (
        ("project_key", "TEXT"),
        ("posture", "TEXT"),
        ("abstain_reason", "TEXT"),
        ("dossier_summary_id", "TEXT"),
        ("dossier_revision", "TEXT"),
    ):
        if col not in eval_cols:
            conn.execute(f"ALTER TABLE evaluations ADD COLUMN {col} {ddl}")
    if "surface_status" not in eval_cols:
        conn.execute(
            "ALTER TABLE evaluations ADD COLUMN surface_status TEXT NOT NULL "
            "DEFAULT 'legacy_unknown'"
        )
        conn.execute(
            "UPDATE evaluations SET surface_status = 'legacy_unknown' WHERE surface_status IS NULL"
        )

    draft_cols = {row["name"] for row in conn.execute("PRAGMA table_info(draft_comments)")}
    for col, ddl in (
        ("posture", "TEXT"),
        ("structured_output", "TEXT"),
        ("dossier_summary_id", "TEXT"),
        ("dossier_revision", "TEXT"),
    ):
        if col not in draft_cols:
            conn.execute(f"ALTER TABLE draft_comments ADD COLUMN {col} {ddl}")

    scan_cols = {row["name"] for row in conn.execute("PRAGMA table_info(scans)")}
    if "dossier_revision" not in scan_cols:
        conn.execute("ALTER TABLE scans ADD COLUMN dossier_revision TEXT")

    proj_cols = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
    if "dossier_summary_id" not in proj_cols:
        conn.execute("ALTER TABLE projects ADD COLUMN dossier_summary_id TEXT")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS gate_blocks (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            reason_code      TEXT NOT NULL,
            offending_text   TEXT,
            segment_index    INTEGER,
            project_key      TEXT,
            dossier_summary_id TEXT,
            dossier_revision TEXT,
            scan_id          INTEGER,
            post_id          INTEGER,
            evaluation_id    INTEGER,
            context          TEXT,
            created_at       TEXT NOT NULL,
            FOREIGN KEY (scan_id)       REFERENCES scans(id),
            FOREIGN KEY (post_id)       REFERENCES posts(id),
            FOREIGN KEY (evaluation_id) REFERENCES evaluations(id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS gate_blocks_scan_id_idx ON gate_blocks(scan_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS gate_blocks_post_id_idx ON gate_blocks(post_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS gate_blocks_evaluation_id_idx ON gate_blocks(evaluation_id)"
    )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS surfaced_events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            platform      TEXT NOT NULL,
            author_id     TEXT NOT NULL,
            surfaced_at   TEXT NOT NULL,
            post_id       INTEGER,
            evaluation_id INTEGER,
            draft_id      INTEGER,
            project_key   TEXT,
            created_at    TEXT NOT NULL,
            UNIQUE (platform, author_id, surfaced_at),
            FOREIGN KEY (post_id)       REFERENCES posts(id),
            FOREIGN KEY (evaluation_id) REFERENCES evaluations(id),
            FOREIGN KEY (draft_id)      REFERENCES draft_comments(id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS surfaced_events_author_idx "
        "ON surfaced_events(author_id, created_at)"
    )


def _migrate_to_15(conn: sqlite3.Connection) -> None:
    """Add v2 causal grading columns to grades table (v15).

    Idempotent: every ALTER TABLE is guarded by a column-name check.
    Existing rows are marked schema_version=1, needs_regrade=1 so they are
    excluded from v2 progress counts but remain inspectable.
    """
    grade_cols = {row["name"] for row in conn.execute("PRAGMA table_info(grades)")}

    for col, ddl in (
        ("schema_version", "INTEGER NOT NULL DEFAULT 1"),
        ("needs_regrade", "INTEGER NOT NULL DEFAULT 0"),
        ("action_judgment", "TEXT"),
        ("dimensions", "TEXT"),
        ("failure_note", "TEXT"),
        ("factual_offending_claim", "TEXT"),
        ("factual_disposition", "TEXT"),
        ("factual_contradicting_evidence", "TEXT"),
        ("context_missing_input", "TEXT"),
        ("posture_should_have_been", "TEXT"),
        ("implication_implied_claim", "TEXT"),
        ("implication_missing_support", "TEXT"),
    ):
        if col not in grade_cols:
            conn.execute(f"ALTER TABLE grades ADD COLUMN {col} {ddl}")

    # Mark all existing pre-v2 rows as needing regrade.
    conn.execute(
        "UPDATE grades SET needs_regrade = 1 "
        "WHERE schema_version IS NULL OR schema_version < 2"
    )


def _migrate_to_16(conn: sqlite3.Connection) -> None:
    """Add immutable scan provenance and parent counterfactual annotations."""
    scan_cols = {row["name"] for row in conn.execute("PRAGMA table_info(scans)")}
    if "environment" not in scan_cols:
        conn.execute(
            "ALTER TABLE scans ADD COLUMN environment TEXT NOT NULL DEFAULT 'unknown'"
        )
    if "run_kind" not in scan_cols:
        conn.execute(
            "ALTER TABLE scans ADD COLUMN run_kind TEXT NOT NULL DEFAULT 'unknown'"
        )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS parent_context_assessments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evaluation_id INTEGER NOT NULL UNIQUE,
            assessor TEXT NOT NULL,
            assessed_at TEXT NOT NULL,
            without_parent_relevance TEXT NOT NULL,
            without_parent_posture TEXT NOT NULL,
            explanation TEXT NOT NULL,
            FOREIGN KEY (evaluation_id) REFERENCES evaluations(id)
        )
    """)


def _migrate_to_17(conn: sqlite3.Connection) -> None:
    """Make evaluation outcomes the durable review population (v17).

    SQLite cannot add a CHECK constraint or relax critique.draft_id in place,
    so the affected tables are rebuilt.  Existing rows are classified from
    their durable artifacts before the replacement is installed.
    """
    conn.execute("PRAGMA legacy_alter_table = ON")
    conn.execute("""
        UPDATE evaluations
           SET surface_status = CASE
             WHEN EXISTS (SELECT 1 FROM surfaced_events se
                          WHERE se.evaluation_id = evaluations.id) THEN 'surfaced'
             WHEN EXISTS (SELECT 1 FROM gate_blocks gb
                          WHERE gb.evaluation_id = evaluations.id) THEN 'gate_blocked'
             WHEN posture = 'abstain' THEN 'abstained'
             WHEN EXISTS (SELECT 1 FROM critiques c
                          JOIN draft_comments d ON d.id = c.draft_id
                         WHERE d.evaluation_id = evaluations.id
                           AND c.verdict = 'reject') THEN 'critic_rejected'
             WHEN relevant = 1 AND score < ? THEN 'low_relevance'
             WHEN relevant = 1 AND EXISTS (SELECT 1 FROM draft_comments d
                                           WHERE d.evaluation_id = evaluations.id)
                  THEN 'drafting_failed'
             ELSE 'not_relevant'
           END
    """, (RELEVANCE_THRESHOLD,))

    # Critique rows need to be usable evidence even when a rejected candidate
    # has no persisted draft.  Backfill the owning evaluation from its draft.
    critique_cols = {row["name"] for row in conn.execute("PRAGMA table_info(critiques)")}
    if "evaluation_id" not in critique_cols:
        conn.execute("ALTER TABLE critiques ADD COLUMN evaluation_id INTEGER")
    conn.execute("""
        UPDATE critiques
           SET evaluation_id = (
             SELECT d.evaluation_id FROM draft_comments d WHERE d.id = critiques.draft_id
           )
         WHERE evaluation_id IS NULL
    """)

    conn.execute("ALTER TABLE evaluations RENAME TO evaluations_v16")
    conn.execute("""
        CREATE TABLE evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            relevant INTEGER NOT NULL,
            score REAL NOT NULL,
            reason TEXT,
            relevant_to TEXT,
            keyword_route_id INTEGER,
            scan_id INTEGER,
            created_at TEXT,
            project_key TEXT,
            posture TEXT,
            abstain_reason TEXT,
            surface_status TEXT NOT NULL CHECK(surface_status IN (
              'surfaced', 'low_relevance', 'abstained', 'critic_rejected',
              'gate_blocked', 'not_relevant', 'drafting_failed'
            )),
            failure_reason TEXT,
            dossier_summary_id TEXT,
            dossier_revision TEXT,
            FOREIGN KEY (post_id) REFERENCES posts(id),
            FOREIGN KEY (keyword_route_id) REFERENCES project_keywords(id)
        )
    """)
    conn.execute("""
        INSERT INTO evaluations (
          id, post_id, relevant, score, reason, relevant_to, keyword_route_id,
          scan_id, created_at, project_key, posture, abstain_reason,
          surface_status, failure_reason, dossier_summary_id, dossier_revision
        )
        SELECT id, post_id, relevant, score, reason, relevant_to, keyword_route_id,
               scan_id, created_at, project_key, posture, abstain_reason,
               surface_status, NULL, dossier_summary_id, dossier_revision
          FROM evaluations_v16
    """)
    conn.execute("DROP TABLE evaluations_v16")

    # Replace critiques so evaluation-scoped rejected evidence has an FK.
    conn.execute("ALTER TABLE critiques RENAME TO critiques_v16")
    conn.execute("""
        CREATE TABLE critiques (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            draft_id INTEGER,
            evaluation_id INTEGER,
            verdict TEXT NOT NULL,
            feedback TEXT,
            created_at TEXT,
            scan_id INTEGER,
            FOREIGN KEY (draft_id) REFERENCES draft_comments(id),
            FOREIGN KEY (evaluation_id) REFERENCES evaluations(id),
            CHECK(draft_id IS NOT NULL OR evaluation_id IS NOT NULL)
        )
    """)
    conn.execute("""
        INSERT INTO critiques (id, draft_id, evaluation_id, verdict, feedback, created_at, scan_id)
        SELECT id, draft_id, evaluation_id, verdict, feedback, created_at, scan_id
          FROM critiques_v16
    """)
    conn.execute("DROP TABLE critiques_v16")

    conn.execute("CREATE INDEX IF NOT EXISTS evaluations_scan_id_idx ON evaluations(scan_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS evaluations_post_id_idx ON evaluations(post_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS critiques_scan_id_idx ON critiques(scan_id)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS draft_comments_evaluation_id_unique "
        "ON draft_comments(evaluation_id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS critiques_evaluation_id_unique "
        "ON critiques(evaluation_id) WHERE evaluation_id IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS surfaced_events_evaluation_id_unique "
        "ON surfaced_events(evaluation_id) WHERE evaluation_id IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS surfaced_events_draft_id_unique "
        "ON surfaced_events(draft_id) WHERE draft_id IS NOT NULL"
    )
    conn.execute("PRAGMA legacy_alter_table = OFF")


def _migrate_to_18(conn: sqlite3.Connection) -> None:
    """Repair migration 014's activation damage and seed canonical agent
    dossier assignments (v18).

    Migration 014 deactivated any active project without a
    dossier_summary_id — including agent-evals and agent-ops, which had not
    yet been assigned one — silently converting missing readiness data into
    an activation change. This migration repairs existing rows only and
    creates no new project rows; fresh databases intentionally start with an
    empty registry so project activation remains an explicit operator action:

      - agent-evals: dossier_summary_id='agent-evals-dossier', active=1
      - agent-ops:   dossier_summary_id='agent-ops-dossier', active=1
      - gateway:     active=0, existing dossier_summary_id retained as-is
      - zk-extension: active=0, existing dossier_summary_id retained as-is
      - every other project and keyword row is left unchanged

    Idempotent: each named row is updated only when a value actually
    differs, so updated_at is bumped only on rows whose value changes and a
    second run is a no-op.
    """
    now = datetime.now(UTC).isoformat()

    def _repair(
        key: str, active: int, dossier_summary_id: str | None, *, set_dossier: bool
    ) -> None:
        row = conn.execute(
            "SELECT active, dossier_summary_id FROM projects WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return
        new_dossier = dossier_summary_id if set_dossier else row["dossier_summary_id"]
        if row["active"] == active and row["dossier_summary_id"] == new_dossier:
            return
        conn.execute(
            "UPDATE projects SET active = ?, dossier_summary_id = ?, updated_at = ? WHERE key = ?",
            (active, new_dossier, now, key),
        )

    _repair("agent-evals", 1, "agent-evals-dossier", set_dossier=True)
    _repair("agent-ops", 1, "agent-ops-dossier", set_dossier=True)
    _repair("gateway", 0, None, set_dossier=False)
    _repair("zk-extension", 0, None, set_dossier=False)


def _migrate_to_19(conn: sqlite3.Connection) -> None:
    """Add storage-level CHECK constraints to the grades table (v19).

    SQLite cannot add a CHECK constraint in place, so the table is rebuilt.
    These are defense-in-depth invariants only — schema_version domain,
    boolean needs_regrade, source domain (including the 'migration' source
    used by save_grade_for_migration), relevance/action enum domains with
    legacy null allowance on action_judgment, and non-empty source/
    graded_at. They do not encode validate_grade's conditional causal
    rules.

    Preflights existing rows and aborts with offending IDs instead of
    coercing or deleting them — the corpus audit must count and classify
    violations first, not have the migration silently erase evidence.
    """
    offending = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM grades WHERE NOT ("
            "  source IN ('cli', 'web', 'migration') AND length(trim(source)) > 0"
            "  AND length(trim(graded_at)) > 0"
            "  AND relevance_judgment IN ('correct', 'false_positive', 'false_negative')"
            "  AND schema_version IN (1, 2)"
            "  AND needs_regrade IN (0, 1)"
            "  AND (action_judgment IS NULL OR action_judgment IN ('accept', 'fail'))"
            ")"
        )
    ]
    if offending:
        raise RuntimeError(
            "migration 19 aborted: grades rows violate the new storage "
            f"invariants and must be audited first, offending ids: {offending}"
        )

    conn.execute("PRAGMA legacy_alter_table = ON")
    conn.execute("ALTER TABLE grades RENAME TO grades_v18")
    conn.execute("""
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
            implication_missing_support TEXT,
            FOREIGN KEY (evaluation_id) REFERENCES evaluations(id),
            FOREIGN KEY (post_id) REFERENCES posts(id)
        )
    """)
    conn.execute("""
        INSERT INTO grades (
            id, evaluation_id, post_id, scan_id, source, graded_at,
            relevance_judgment, rejection_reason, relevance_note,
            comment_quality, comment_issue, comment_note,
            schema_version, needs_regrade, action_judgment, dimensions,
            failure_note, factual_offending_claim, factual_disposition,
            factual_contradicting_evidence, context_missing_input,
            posture_should_have_been, implication_implied_claim,
            implication_missing_support
        )
        SELECT
            id, evaluation_id, post_id, scan_id, source, graded_at,
            relevance_judgment, rejection_reason, relevance_note,
            comment_quality, comment_issue, comment_note,
            schema_version, needs_regrade, action_judgment, dimensions,
            failure_note, factual_offending_claim, factual_disposition,
            factual_contradicting_evidence, context_missing_input,
            posture_should_have_been, implication_implied_claim,
            implication_missing_support
        FROM grades_v18
    """)
    conn.execute("DROP TABLE grades_v18")

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS grades_evaluation_id_unique"
        " ON grades(evaluation_id) WHERE evaluation_id IS NOT NULL"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS grades_scan_id_idx ON grades(scan_id)")
    conn.execute("PRAGMA legacy_alter_table = OFF")


def _migrate_to_20(conn: sqlite3.Connection) -> None:
    """Normalize graded_at to the canonical UTC millisecond form and add a
    storage-level CHECK for its fixed 24-character shape and delimiter
    positions (v20).

    Historical rows may carry a literal 'Z', an explicit numeric offset,
    or be timezone-naive (which historical writers always intended as
    UTC). Each row is parsed as an instant via _normalize_legacy_graded_at,
    converted to UTC, and truncated to millisecond precision. Preflights
    every row and aborts with the offending row IDs for any unparseable
    value before updating any row — this migration is deterministic and
    all-or-nothing, never fabricating chronology for corrupt data.

    SQLite cannot add a CHECK constraint in place, so the table is
    rebuilt, as in migration 19. The CHECK only pins the fixed shape and
    delimiter positions — calendar semantics remain application-parsed at
    the StateManager write boundary via format_graded_at/parse_graded_at.
    """
    rows = conn.execute("SELECT id, graded_at FROM grades").fetchall()
    normalized: dict[int, str] = {}
    unparseable: list[int] = []
    for row in rows:
        try:
            normalized[row["id"]] = _normalize_legacy_graded_at(row["graded_at"])
        except ValueError:
            unparseable.append(row["id"])
    if unparseable:
        raise RuntimeError(
            "migration 20 aborted: grades rows have unparseable graded_at "
            f"values and must be audited first, offending ids: {unparseable}"
        )

    conn.executemany(
        "UPDATE grades SET graded_at = ? WHERE id = ?",
        [(value, row_id) for row_id, value in normalized.items()],
    )

    conn.execute("PRAGMA legacy_alter_table = ON")
    conn.execute("ALTER TABLE grades RENAME TO grades_v19")
    conn.execute("""
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
            implication_missing_support TEXT,
            FOREIGN KEY (evaluation_id) REFERENCES evaluations(id),
            FOREIGN KEY (post_id) REFERENCES posts(id)
        )
    """)
    conn.execute("""
        INSERT INTO grades (
            id, evaluation_id, post_id, scan_id, source, graded_at,
            relevance_judgment, rejection_reason, relevance_note,
            comment_quality, comment_issue, comment_note,
            schema_version, needs_regrade, action_judgment, dimensions,
            failure_note, factual_offending_claim, factual_disposition,
            factual_contradicting_evidence, context_missing_input,
            posture_should_have_been, implication_implied_claim,
            implication_missing_support
        )
        SELECT
            id, evaluation_id, post_id, scan_id, source, graded_at,
            relevance_judgment, rejection_reason, relevance_note,
            comment_quality, comment_issue, comment_note,
            schema_version, needs_regrade, action_judgment, dimensions,
            failure_note, factual_offending_claim, factual_disposition,
            factual_contradicting_evidence, context_missing_input,
            posture_should_have_been, implication_implied_claim,
            implication_missing_support
        FROM grades_v19
    """)
    conn.execute("DROP TABLE grades_v19")

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS grades_evaluation_id_unique"
        " ON grades(evaluation_id) WHERE evaluation_id IS NOT NULL"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS grades_scan_id_idx ON grades(scan_id)")
    conn.execute("PRAGMA legacy_alter_table = OFF")


def _migrate_to_21(conn: sqlite3.Connection) -> None:
    """Drop the unused grades.relevance_note and grades.comment_note
    columns (v21).

    No production reader or writer has touched either column since v2
    causal grading replaced the v1 free-text note fields; the sibling
    rejection_reason, comment_quality, and comment_issue columns are kept
    for migration/audit compatibility, but these two note columns were
    identified as safe to drop outright. SQLite cannot drop columns in
    place while preserving the table's CHECK constraints, so the table is
    rebuilt exactly as in migration 20 minus the two columns — same IDs,
    same retained columns, needs_regrade flags, canonical graded_at
    values, CHECK constraints, foreign keys, and the partial unique
    evaluation_id index.
    """
    conn.execute("PRAGMA legacy_alter_table = ON")
    conn.execute("ALTER TABLE grades RENAME TO grades_v20")
    conn.execute("""
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
            comment_quality INTEGER,
            comment_issue TEXT,
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
            implication_missing_support TEXT,
            FOREIGN KEY (evaluation_id) REFERENCES evaluations(id),
            FOREIGN KEY (post_id) REFERENCES posts(id)
        )
    """)
    conn.execute("""
        INSERT INTO grades (
            id, evaluation_id, post_id, scan_id, source, graded_at,
            relevance_judgment, rejection_reason,
            comment_quality, comment_issue,
            schema_version, needs_regrade, action_judgment, dimensions,
            failure_note, factual_offending_claim, factual_disposition,
            factual_contradicting_evidence, context_missing_input,
            posture_should_have_been, implication_implied_claim,
            implication_missing_support
        )
        SELECT
            id, evaluation_id, post_id, scan_id, source, graded_at,
            relevance_judgment, rejection_reason,
            comment_quality, comment_issue,
            schema_version, needs_regrade, action_judgment, dimensions,
            failure_note, factual_offending_claim, factual_disposition,
            factual_contradicting_evidence, context_missing_input,
            posture_should_have_been, implication_implied_claim,
            implication_missing_support
        FROM grades_v20
    """)
    conn.execute("DROP TABLE grades_v20")

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS grades_evaluation_id_unique"
        " ON grades(evaluation_id) WHERE evaluation_id IS NOT NULL"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS grades_scan_id_idx ON grades(scan_id)")
    conn.execute("PRAGMA legacy_alter_table = OFF")


def _migrate_to_22(conn: sqlite3.Connection) -> None:
    """Resolve terminal orphan annotations (v22).

    Prior to the lifecycle enforcement added alongside this migration, an
    annotation could commit in the instant before a candidate's terminal
    decision (approve/publish/reject) and be left permanently unresolved
    — inflating unresolved-annotation counts (C-009) for candidates
    review had already closed. This migration repairs existing rows: it
    stamps resolved_at on every unresolved `draft_annotations` row whose
    candidate is in `ANNOTATION_CLOSED_STATUSES` (defined beside migration 37)
    (approved, published, rejected), using one instant for the whole
    migration run, formatted as an aware UTC ISO string with a +00:00
    offset.

    No row is deleted — resolution repairs the count while preserving
    the review audit trail. Idempotent: only `resolved_at IS NULL` rows
    are touched, so active-candidate annotations and already-resolved
    timestamps are left untouched, and a rerun after a clean database is
    a no-op.
    """
    if not _has_table(conn, "draft_annotations"):
        return
    now = datetime.now(UTC).isoformat()
    placeholders = ",".join("?" * len(ANNOTATION_CLOSED_STATUSES))
    conn.execute(
        "UPDATE draft_annotations SET resolved_at = ? "
        "WHERE resolved_at IS NULL AND draft_id IN ("
        "  SELECT d.id FROM outbound_drafts d "
        "  JOIN content_candidates c ON c.id = d.candidate_id "
        f"  WHERE c.status IN ({placeholders})"
        ")",
        (now, *sorted(ANNOTATION_CLOSED_STATUSES)),
    )


def _migrate_to_23(conn: sqlite3.Connection) -> None:
    """Add the append-only autonomy_events table for the PAA control plane (v23).

    No existing table changes and no backfill: cohort 1 declarations exist
    but no autonomy event has ever been written, so this is a pure
    additive migration. Idempotent via CREATE TABLE/INDEX/TRIGGER IF NOT
    EXISTS — a fresh database never runs this (it gets the table from the
    baseline SCHEMA directly).
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_events (
            id                  TEXT PRIMARY KEY,
            motion_id           TEXT NOT NULL,
            task                TEXT NOT NULL,
            declaration_version INTEGER NOT NULL,
            scope               TEXT NOT NULL,
            event               TEXT NOT NULL CHECK(event IN (
                'motion_proposed', 'motion_approved', 'motion_rejected', 'position_changed'
            )),
            from_position       TEXT NOT NULL CHECK(from_position IN (
                'manual', 'hitl', 'hotl', 'autonomous'
            )),
            to_position         TEXT NOT NULL CHECK(to_position IN (
                'manual', 'hitl', 'hotl', 'autonomous'
            )),
            evidence_ref        TEXT NOT NULL,
            evidence_sha256     TEXT NOT NULL,
            actor               TEXT NOT NULL,
            reason              TEXT NOT NULL,
            created_at          TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS autonomy_events_scope_idx "
        "ON autonomy_events(task, declaration_version, scope, created_at, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS autonomy_events_motion_idx "
        "ON autonomy_events(motion_id, created_at, id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS autonomy_events_motion_event_unique "
        "ON autonomy_events(motion_id, event)"
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS autonomy_events_no_update
        BEFORE UPDATE ON autonomy_events
        BEGIN
            SELECT RAISE(ABORT, 'autonomy_events is append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS autonomy_events_no_delete
        BEFORE DELETE ON autonomy_events
        BEGIN
            SELECT RAISE(ABORT, 'autonomy_events is append-only');
        END
        """
    )


def _migrate_to_24(conn: sqlite3.Connection) -> None:
    """Add outbound provenance, verification, and review persistence (v24).

    Pure additive: a nullable `publications.revision_id` FK for pinning the
    exact claimed revision, plus four brand-new tables — immutable
    `outbound_revision_provenance`, append-only
    `outbound_publication_verifications`, and the immutable
    `outbound_publish_reviews` / `outbound_publish_review_decisions` queue
    pair. No existing row is rewritten; historical `publications` rows keep
    `revision_id IS NULL` and are reported as incomplete rather than
    backfilled. Idempotent via column-existence checks and CREATE ... IF NOT
    EXISTS — safe to rerun.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(publications)")}
    if cols and "revision_id" not in cols:
        conn.execute(
            "ALTER TABLE publications ADD COLUMN revision_id INTEGER "
            "REFERENCES draft_revisions(id)"
        )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS outbound_revision_provenance (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            draft_revision_id   INTEGER NOT NULL UNIQUE,
            project_key         TEXT NOT NULL,
            dossier_summary_id  TEXT NOT NULL,
            dossier_revision    TEXT NOT NULL,
            fact_ids            TEXT NOT NULL,
            attached_by         TEXT NOT NULL,
            attached_at         TEXT NOT NULL,
            FOREIGN KEY (draft_revision_id) REFERENCES draft_revisions(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS outbound_revision_provenance_no_update
        BEFORE UPDATE ON outbound_revision_provenance
        BEGIN
            SELECT RAISE(ABORT, 'outbound_revision_provenance is immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS outbound_revision_provenance_no_delete
        BEFORE DELETE ON outbound_revision_provenance
        BEGIN
            SELECT RAISE(ABORT, 'outbound_revision_provenance is immutable');
        END
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS outbound_publication_verifications (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            publication_id      INTEGER NOT NULL,
            attempt_count       INTEGER NOT NULL,
            draft_revision_id   INTEGER NOT NULL,
            platform            TEXT NOT NULL,
            evaluator_id        TEXT NOT NULL,
            evaluator_version   TEXT NOT NULL,
            status              TEXT NOT NULL CHECK(status IN ('pass', 'fail', 'error')),
            reason_codes        TEXT NOT NULL,
            dossier_summary_id  TEXT,
            dossier_revision    TEXT,
            result_json         TEXT NOT NULL,
            created_at          TEXT NOT NULL,
            FOREIGN KEY (publication_id) REFERENCES publications(id),
            FOREIGN KEY (draft_revision_id) REFERENCES draft_revisions(id)
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS outbound_publication_verifications_attempt_unique "
        "ON outbound_publication_verifications(publication_id, attempt_count)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS outbound_publication_verifications_publication_idx "
        "ON outbound_publication_verifications(publication_id, created_at, id)"
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS outbound_publication_verifications_no_update
        BEFORE UPDATE ON outbound_publication_verifications
        BEGIN
            SELECT RAISE(ABORT, 'outbound_publication_verifications is append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS outbound_publication_verifications_no_delete
        BEFORE DELETE ON outbound_publication_verifications
        BEGIN
            SELECT RAISE(ABORT, 'outbound_publication_verifications is append-only');
        END
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS outbound_publish_reviews (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            publication_id      INTEGER NOT NULL UNIQUE,
            draft_revision_id   INTEGER NOT NULL,
            evaluator_id        TEXT NOT NULL,
            evaluator_version   TEXT NOT NULL,
            queued_at           TEXT NOT NULL,
            FOREIGN KEY (publication_id) REFERENCES publications(id),
            FOREIGN KEY (draft_revision_id) REFERENCES draft_revisions(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS outbound_publish_reviews_draft_revision_idx "
        "ON outbound_publish_reviews(draft_revision_id)"
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS outbound_publish_reviews_no_update
        BEFORE UPDATE ON outbound_publish_reviews
        BEGIN
            SELECT RAISE(ABORT, 'outbound_publish_reviews is immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS outbound_publish_reviews_no_delete
        BEFORE DELETE ON outbound_publish_reviews
        BEGIN
            SELECT RAISE(ABORT, 'outbound_publish_reviews is immutable');
        END
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS outbound_publish_review_decisions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id    INTEGER NOT NULL UNIQUE,
            reviewer     TEXT NOT NULL,
            decision     TEXT NOT NULL CHECK(decision IN ('pass', 'fail')),
            reason       TEXT NOT NULL CHECK(length(trim(reason)) > 0),
            reviewed_at  TEXT NOT NULL,
            FOREIGN KEY (review_id) REFERENCES outbound_publish_reviews(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS outbound_publish_review_decisions_reviewed_at_idx "
        "ON outbound_publish_review_decisions(reviewed_at, id)"
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS outbound_publish_review_decisions_no_update
        BEFORE UPDATE ON outbound_publish_review_decisions
        BEGIN
            SELECT RAISE(ABORT, 'outbound_publish_review_decisions is immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS outbound_publish_review_decisions_no_delete
        BEFORE DELETE ON outbound_publish_review_decisions
        BEGIN
            SELECT RAISE(ABORT, 'outbound_publish_review_decisions is immutable');
        END
        """
    )


def _migrate_to_25(conn: sqlite3.Connection) -> None:
    """Add the operator-managed author denylist (v25)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS blocked_authors (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            platform    TEXT NOT NULL,
            author_id   TEXT NOT NULL,
            author_name TEXT,
            reason      TEXT,
            active      INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            UNIQUE(platform, author_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS blocked_authors_active_idx "
        "ON blocked_authors(active, platform, author_id)"
    )


GradeConvergenceStatus = Literal["converged", "missing_revision", "divergent_revision"]


def grade_revision_comparison_shape(row: sqlite3.Row) -> dict[str, object]:
    """Build the immutable grade_revisions payload from a complete grades
    row — identity, schema, judgments, dimensions, notes, source,
    timestamp, and regrade state. `dimensions` is re-embedded as a nested
    JSON value (not the raw double-encoded string) so the payload can be
    read back with a single `json.loads`.

    This is also the one shared canonicalization function for revision
    convergence: since every grade_revisions.payload was built by this
    same function at write time, comparing a fresh call against
    `json.loads(revision["payload"])` is a same-shape structural equality
    check — used by both the read-only convergence audit
    (scripts/grade_corpus_audit.py) and
    StateManager.converge_grade_revision_for_remediation, so the two can
    never disagree about what "converged" means.

    `edited_text` is not a `grades` column — it lives in
    `reply_draft_revisions.reply_text`, addressed by `reply_revision_id` —
    so callers that want it in the payload must supply `row` from a query
    that already joins it in under that alias (see
    StateManager._GRADE_WITH_EDITED_TEXT_SELECT); callers that select
    plain `grades` columns get `None` here, same as any pre-migration-34
    row that has no `reply_revision_id` column at all.
    """
    dims_raw = row["dimensions"]
    dims = json.loads(dims_raw) if dims_raw else None
    keys = row.keys()
    return {
        "id": row["id"],
        "evaluation_id": row["evaluation_id"],
        "post_id": row["post_id"],
        "scan_id": row["scan_id"],
        "source": row["source"],
        "graded_at": row["graded_at"],
        "relevance_judgment": row["relevance_judgment"],
        "rejection_reason": row["rejection_reason"],
        "comment_quality": row["comment_quality"],
        "comment_issue": row["comment_issue"],
        "schema_version": row["schema_version"],
        "needs_regrade": row["needs_regrade"],
        "action_judgment": row["action_judgment"],
        "dimensions": dims,
        "failure_note": row["failure_note"],
        "factual_offending_claim": row["factual_offending_claim"],
        "factual_disposition": row["factual_disposition"],
        "factual_contradicting_evidence": row["factual_contradicting_evidence"],
        "context_missing_input": row["context_missing_input"],
        "posture_should_have_been": row["posture_should_have_been"],
        "implication_implied_claim": row["implication_implied_claim"],
        "implication_missing_support": row["implication_missing_support"],
        "reply_revision_id": (
            row["reply_revision_id"] if "reply_revision_id" in keys else None
        ),
        "edited_text": row["edited_text"] if "edited_text" in keys else None,
    }


def _migrate_to_26(conn: sqlite3.Connection) -> None:
    """Add immutable grade_revisions and current-state grade_usage_overrides,
    and backfill exactly one migration_snapshot revision per pre-existing
    grade (v26).

    The backfill is convergent — it only inserts for grades with no
    existing grade_revisions row — so re-running this migration never
    duplicates history. No existing grades row is modified; unlinked
    (evaluation_id IS NULL) grades get a revision too, preserving their id
    as the stable audit identity for later adoption. Idempotent via
    CREATE ... IF NOT EXISTS for every new table/index/trigger.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS grade_revisions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            grade_id      INTEGER NOT NULL,
            evaluation_id INTEGER,
            revision      INTEGER NOT NULL,
            source        TEXT NOT NULL CHECK(
                source IN ('cli', 'web', 'migration', 'migration_snapshot')
                AND length(trim(source)) > 0
            ),
            payload       TEXT NOT NULL,
            recorded_at   TEXT NOT NULL,
            FOREIGN KEY (grade_id) REFERENCES grades(id),
            FOREIGN KEY (evaluation_id) REFERENCES evaluations(id)
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS grade_revisions_grade_id_revision_unique "
        "ON grade_revisions(grade_id, revision)"
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS grade_revisions_no_update
        BEFORE UPDATE ON grade_revisions
        BEGIN
            SELECT RAISE(ABORT, 'grade_revisions is immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS grade_revisions_no_delete
        BEFORE DELETE ON grade_revisions
        BEGIN
            SELECT RAISE(ABORT, 'grade_revisions is immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS grade_usage_overrides (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            grade_id   INTEGER NOT NULL UNIQUE,
            mode       TEXT NOT NULL CHECK(mode IN ('auto', 'exclude')),
            reason     TEXT CHECK(
                (mode = 'auto' AND reason IS NULL)
                OR (mode = 'exclude' AND reason IS NOT NULL AND length(trim(reason)) > 0)
            ),
            updated_at TEXT NOT NULL,
            FOREIGN KEY (grade_id) REFERENCES grades(id)
        )
        """
    )

    now = datetime.now(UTC).isoformat()
    pending_rows = conn.execute(
        "SELECT * FROM grades g WHERE NOT EXISTS "
        "(SELECT 1 FROM grade_revisions r WHERE r.grade_id = g.id)"
    ).fetchall()
    for row in pending_rows:
        payload = json.dumps(grade_revision_comparison_shape(row))
        conn.execute(
            "INSERT INTO grade_revisions "
            "(grade_id, evaluation_id, revision, source, payload, recorded_at) "
            "VALUES (?, ?, 1, 'migration_snapshot', ?, ?)",
            (row["id"], row["evaluation_id"], payload, now),
        )


def _migrate_to_27(conn: sqlite3.Connection) -> None:
    """Add the immutable evaluation-feedback/v1 shadow snapshot tables (v27):
    feedback_snapshots (one row per scan), feedback_snapshot_phases (one row
    per snapshot per relevance/reply_draft/critic phase), and
    feedback_snapshot_items (one pinned-revision row per grade per outcome
    per phase). Fresh tables only — no backfill, since the policy did not
    exist before this migration. Idempotent via CREATE ... IF NOT EXISTS for
    every new table/index/trigger.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback_snapshots (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id                  INTEGER NOT NULL UNIQUE,
            policy_version           TEXT NOT NULL,
            mode                     TEXT NOT NULL DEFAULT 'shadow'
                                          CHECK(mode IN ('shadow', 'active')),
            as_of                    TEXT NOT NULL,
            lookback_days            INTEGER NOT NULL,
            max_grades               INTEGER NOT NULL,
            segment_min_grades       INTEGER NOT NULL,
            note_max_chars           INTEGER NOT NULL,
            relevance_token_budget   INTEGER NOT NULL,
            reply_draft_token_budget INTEGER NOT NULL,
            critic_token_budget      INTEGER NOT NULL,
            population_count         INTEGER NOT NULL,
            eligible_count           INTEGER NOT NULL,
            excluded_count           INTEGER NOT NULL,
            created_at               TEXT NOT NULL,
            FOREIGN KEY (scan_id) REFERENCES scans(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS feedback_snapshots_no_update
        BEFORE UPDATE ON feedback_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'feedback_snapshots is immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS feedback_snapshots_no_delete
        BEFORE DELETE ON feedback_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'feedback_snapshots is immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback_snapshot_phases (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id         INTEGER NOT NULL,
            phase               TEXT NOT NULL
                                    CHECK(phase IN ('relevance', 'reply_draft', 'critic')),
            token_budget        INTEGER NOT NULL,
            token_estimate      INTEGER NOT NULL,
            truncated           INTEGER NOT NULL DEFAULT 0 CHECK(truncated IN (0, 1)),
            structured_summary  TEXT NOT NULL,
            rendered_text       TEXT NOT NULL,
            rendered_sha256     TEXT NOT NULL,
            created_at          TEXT NOT NULL,
            UNIQUE(snapshot_id, phase),
            FOREIGN KEY (snapshot_id) REFERENCES feedback_snapshots(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS feedback_snapshot_phases_no_update
        BEFORE UPDATE ON feedback_snapshot_phases
        BEGIN
            SELECT RAISE(ABORT, 'feedback_snapshot_phases is immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS feedback_snapshot_phases_no_delete
        BEFORE DELETE ON feedback_snapshot_phases
        BEGIN
            SELECT RAISE(ABORT, 'feedback_snapshot_phases is immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback_snapshot_items (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_phase_id  INTEGER NOT NULL,
            grade_id           INTEGER NOT NULL,
            grade_revision_id  INTEGER NOT NULL,
            role               TEXT NOT NULL CHECK(role IN ('excluded', 'aggregate', 'example')),
            reason             TEXT,
            created_at         TEXT NOT NULL,
            UNIQUE(snapshot_phase_id, grade_id, role),
            FOREIGN KEY (snapshot_phase_id) REFERENCES feedback_snapshot_phases(id),
            FOREIGN KEY (grade_id) REFERENCES grades(id),
            FOREIGN KEY (grade_revision_id) REFERENCES grade_revisions(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS feedback_snapshot_items_phase_idx "
        "ON feedback_snapshot_items(snapshot_phase_id, role)"
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS feedback_snapshot_items_no_update
        BEFORE UPDATE ON feedback_snapshot_items
        BEGIN
            SELECT RAISE(ABORT, 'feedback_snapshot_items is immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS feedback_snapshot_items_no_delete
        BEFORE DELETE ON feedback_snapshot_items
        BEGIN
            SELECT RAISE(ABORT, 'feedback_snapshot_items is immutable');
        END
        """
    )


def _migrate_to_28(conn: sqlite3.Connection) -> None:
    """Add explicit immutable snapshot-selection metadata and revision
    schema identity (v28), while enforcing JSON validity at the database
    boundary.

    SQLite cannot add CHECK constraints to existing tables without a table
    rebuild. These append-only tables can preserve their stable row IDs and
    foreign-key relationships by adding columns in place and enforcing the
    equivalent insert invariants with triggers. Existing rows are backfilled
    deterministically before immutability is restored.
    """
    revision_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(grade_revisions)")
    }
    conn.execute("DROP TRIGGER IF EXISTS grade_revisions_no_update")
    if "schema_version" not in revision_columns:
        conn.execute(
            "ALTER TABLE grade_revisions "
            "ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1"
        )
    conn.execute(
        "UPDATE grade_revisions "
        "SET schema_version = COALESCE("
        "  CAST(json_extract(payload, '$.schema_version') AS INTEGER),"
        "  (SELECT g.schema_version FROM grades g WHERE g.id = grade_revisions.grade_id),"
        "  1"
        ")"
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS grade_revisions_no_update
        BEFORE UPDATE ON grade_revisions
        BEGIN
            SELECT RAISE(ABORT, 'grade_revisions is immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS grade_revisions_valid_insert
        BEFORE INSERT ON grade_revisions
        WHEN json_valid(NEW.payload) = 0
        BEGIN
            SELECT RAISE(ABORT, 'grade_revisions.payload must be valid JSON');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS feedback_snapshot_phases_valid_insert
        BEFORE INSERT ON feedback_snapshot_phases
        WHEN json_valid(NEW.structured_summary) = 0
        BEGIN
            SELECT RAISE(
                ABORT,
                'feedback_snapshot_phases.structured_summary must be valid JSON'
            );
        END
        """
    )

    item_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(feedback_snapshot_items)")
    }
    conn.execute("DROP TRIGGER IF EXISTS feedback_snapshot_items_no_update")
    if "selection_reason" not in item_columns:
        conn.execute(
            "ALTER TABLE feedback_snapshot_items "
            "ADD COLUMN selection_reason TEXT NOT NULL DEFAULT 'legacy_backfill'"
        )
    if "rank" not in item_columns:
        conn.execute("ALTER TABLE feedback_snapshot_items ADD COLUMN rank INTEGER")
    conn.execute(
        "UPDATE feedback_snapshot_items "
        "SET selection_reason = CASE role "
        "  WHEN 'excluded' THEN COALESCE(NULLIF(trim(reason), ''), 'excluded') "
        "  WHEN 'aggregate' THEN 'phase_population' "
        "  WHEN 'example' THEN 'selected_recent_note' "
        "END"
    )
    conn.execute(
        "UPDATE feedback_snapshot_items AS item "
        "SET rank = CASE WHEN role = 'example' THEN ("
        "  SELECT COUNT(*) FROM feedback_snapshot_items AS peer "
        "  WHERE peer.snapshot_phase_id = item.snapshot_phase_id "
        "    AND peer.role = 'example' AND peer.id <= item.id"
        ") ELSE NULL END"
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS feedback_snapshot_items_no_update
        BEFORE UPDATE ON feedback_snapshot_items
        BEGIN
            SELECT RAISE(ABORT, 'feedback_snapshot_items is immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS feedback_snapshot_items_valid_insert
        BEFORE INSERT ON feedback_snapshot_items
        WHEN NEW.selection_reason = 'legacy_backfill'
          OR length(trim(NEW.selection_reason)) = 0
          OR (NEW.role = 'example' AND (NEW.rank IS NULL OR NEW.rank <= 0))
          OR (NEW.role != 'example' AND NEW.rank IS NOT NULL)
        BEGIN
            SELECT RAISE(
                ABORT,
                'feedback_snapshot_items selection metadata is invalid'
            );
        END
        """
    )


def _migrate_to_29(conn: sqlite3.Connection) -> None:
    """Add evaluation_phase_runs (v29): one row per phase attempt (relevance,
    reply_draft, critic) recording the durable AGENT_RUN Jig trace it
    produced. Fresh table only — no backfill, since historical attempts
    have no stored trace identity to link. Idempotent via
    CREATE ... IF NOT EXISTS for the table, its indexes, and its triggers.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS evaluation_phase_runs (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id            INTEGER NOT NULL,
            post_id            INTEGER NOT NULL,
            evaluation_id      INTEGER,
            snapshot_phase_id  INTEGER NOT NULL,
            phase              TEXT NOT NULL CHECK(phase IN ('relevance', 'reply_draft', 'critic')),
            trace_id           TEXT NOT NULL UNIQUE,
            model              TEXT NOT NULL,
            status             TEXT NOT NULL CHECK(status IN ('complete', 'error', 'cancelled')),
            created_at         TEXT NOT NULL,
            FOREIGN KEY (scan_id) REFERENCES scans(id),
            FOREIGN KEY (post_id) REFERENCES posts(id),
            FOREIGN KEY (evaluation_id) REFERENCES evaluations(id),
            FOREIGN KEY (snapshot_phase_id) REFERENCES feedback_snapshot_phases(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS evaluation_phase_runs_evaluation_idx "
        "ON evaluation_phase_runs(evaluation_id, created_at, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS evaluation_phase_runs_snapshot_phase_idx "
        "ON evaluation_phase_runs(snapshot_phase_id, created_at, id)"
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS evaluation_phase_runs_link_once
        BEFORE UPDATE ON evaluation_phase_runs
        BEGIN
            SELECT RAISE(
                ABORT,
                'evaluation_phase_runs rows are append-only except a one-time evaluation_id link'
            )
            WHERE OLD.evaluation_id IS NOT NULL
               OR NEW.evaluation_id IS NULL
               OR NEW.scan_id IS NOT OLD.scan_id
               OR NEW.post_id IS NOT OLD.post_id
               OR NEW.snapshot_phase_id IS NOT OLD.snapshot_phase_id
               OR NEW.phase IS NOT OLD.phase
               OR NEW.trace_id IS NOT OLD.trace_id
               OR NEW.model IS NOT OLD.model
               OR NEW.status IS NOT OLD.status
               OR NEW.created_at IS NOT OLD.created_at;
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS evaluation_phase_runs_no_delete
        BEFORE DELETE ON evaluation_phase_runs
        BEGIN
            SELECT RAISE(ABORT, 'evaluation_phase_runs is append-only');
        END
        """
    )


def _migrate_to_30(conn: sqlite3.Connection) -> None:
    """Add evaluation_experiments and trace_comparisons (v30): the CLI-only
    offline replay domain's append-oriented experiment lifecycle and its
    immutable comparison evidence. Fresh tables only — no backfill, since
    replay experiments are a new activity with no historical rows. See the
    matching block in SCHEMA for the full column/trigger rationale;
    idempotent via CREATE ... IF NOT EXISTS throughout.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS evaluation_experiments (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            phase_run_id             INTEGER NOT NULL,
            name                     TEXT NOT NULL,
            status                   TEXT NOT NULL
                CHECK(status IN ('queued', 'running', 'complete', 'failed')),
            candidate_config         TEXT NOT NULL,
            candidate_trace_id       TEXT UNIQUE,
            candidate_llm_call_count INTEGER
                CHECK(candidate_llm_call_count IS NULL OR candidate_llm_call_count >= 0),
            candidate_cost           REAL CHECK(candidate_cost IS NULL OR candidate_cost >= 0),
            error_detail             TEXT
                CHECK(error_detail IS NULL OR length(error_detail) <= 2000),
            created_at               TEXT NOT NULL,
            completed_at             TEXT,
            FOREIGN KEY (phase_run_id) REFERENCES evaluation_phase_runs(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS evaluation_experiments_phase_run_idx "
        "ON evaluation_experiments(phase_run_id, created_at, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS evaluation_experiments_status_idx "
        "ON evaluation_experiments(status, created_at, id)"
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS evaluation_experiments_valid_insert
        BEFORE INSERT ON evaluation_experiments
        WHEN json_valid(NEW.candidate_config) = 0
          OR NEW.status != 'queued'
          OR NEW.candidate_trace_id IS NOT NULL
          OR NEW.candidate_llm_call_count IS NOT NULL
          OR NEW.candidate_cost IS NOT NULL
          OR NEW.error_detail IS NOT NULL
          OR NEW.completed_at IS NOT NULL
        BEGIN
            SELECT RAISE(
                ABORT,
                'evaluation_experiments must insert as a clean queued row with valid JSON config'
            );
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS evaluation_experiments_lifecycle
        BEFORE UPDATE ON evaluation_experiments
        BEGIN
            SELECT RAISE(
                ABORT,
                'evaluation_experiments only allows queued->running->complete/failed CAS updates'
            )
            WHERE OLD.status IN ('complete', 'failed')
               OR NEW.phase_run_id IS NOT OLD.phase_run_id
               OR NEW.name IS NOT OLD.name
               OR NEW.candidate_config IS NOT OLD.candidate_config
               OR NEW.created_at IS NOT OLD.created_at
               OR (OLD.candidate_trace_id IS NOT NULL
                   AND NEW.candidate_trace_id IS NOT OLD.candidate_trace_id)
               OR (OLD.candidate_llm_call_count IS NOT NULL
                   AND NEW.candidate_llm_call_count IS NOT OLD.candidate_llm_call_count)
               OR (OLD.candidate_cost IS NOT NULL AND NEW.candidate_cost IS NOT OLD.candidate_cost)
               OR NOT (
                    (OLD.status = 'queued' AND NEW.status = 'running')
                 OR (OLD.status = 'running' AND NEW.status IN ('running', 'complete', 'failed'))
               );
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS evaluation_experiments_no_delete
        BEFORE DELETE ON evaluation_experiments
        BEGIN
            SELECT RAISE(ABORT, 'evaluation_experiments rows cannot be deleted');
        END
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trace_comparisons (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id INTEGER NOT NULL UNIQUE,
            jig_revision  TEXT NOT NULL,
            trace_diff    TEXT NOT NULL,
            domain_diff   TEXT NOT NULL,
            created_at    TEXT NOT NULL,
            FOREIGN KEY (experiment_id) REFERENCES evaluation_experiments(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trace_comparisons_valid_insert
        BEFORE INSERT ON trace_comparisons
        WHEN json_valid(NEW.trace_diff) = 0 OR json_valid(NEW.domain_diff) = 0
        BEGIN
            SELECT RAISE(ABORT, 'trace_comparisons JSON columns must be valid JSON');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trace_comparisons_no_update
        BEFORE UPDATE ON trace_comparisons
        BEGIN
            SELECT RAISE(ABORT, 'trace_comparisons is immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trace_comparisons_no_delete
        BEFORE DELETE ON trace_comparisons
        BEGIN
            SELECT RAISE(ABORT, 'trace_comparisons is immutable');
        END
        """
    )


def _migrate_to_31(conn: sqlite3.Connection) -> None:
    """Make comparison trace identities first-class and enforce that they
    agree with both the immutable TraceDiff document and the experiment's
    exact baseline/candidate trace evidence.

    Existing v30 rows are accepted only when all three authorities already
    agree. A corrupt or incomplete row aborts the migration rather than
    inventing an identity or silently preserving an untrustworthy backlink.
    """
    invalid = conn.execute(
        """
        SELECT tc.id
        FROM trace_comparisons tc
        LEFT JOIN evaluation_experiments e ON e.id = tc.experiment_id
        LEFT JOIN evaluation_phase_runs pr ON pr.id = e.phase_run_id
        WHERE CASE
            WHEN json_valid(tc.trace_diff) = 0 THEN 1
            WHEN json_type(tc.trace_diff, '$.trace_a_id') IS NOT 'text' THEN 1
            WHEN json_type(tc.trace_diff, '$.trace_b_id') IS NOT 'text' THEN 1
            WHEN json_extract(tc.trace_diff, '$.trace_a_id') IS NOT pr.trace_id THEN 1
            WHEN json_extract(tc.trace_diff, '$.trace_b_id') IS NOT e.candidate_trace_id THEN 1
            WHEN e.status IS NOT 'complete' THEN 1
            ELSE 0
        END = 1
        LIMIT 1
        """
    ).fetchone()
    if invalid is not None:
        raise sqlite3.IntegrityError(
            f"trace_comparisons row {invalid[0]} has untrustworthy trace identities"
        )

    conn.execute("DROP TRIGGER IF EXISTS trace_comparisons_valid_insert")
    conn.execute("DROP TRIGGER IF EXISTS trace_comparisons_no_update")
    conn.execute("DROP TRIGGER IF EXISTS trace_comparisons_no_delete")
    conn.execute("ALTER TABLE trace_comparisons RENAME TO trace_comparisons_v30")
    conn.execute(
        """
        CREATE TABLE trace_comparisons (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id INTEGER NOT NULL UNIQUE,
            trace_a_id    TEXT NOT NULL,
            trace_b_id    TEXT NOT NULL,
            jig_revision  TEXT NOT NULL,
            trace_diff    TEXT NOT NULL,
            domain_diff   TEXT NOT NULL,
            created_at    TEXT NOT NULL,
            FOREIGN KEY (experiment_id) REFERENCES evaluation_experiments(id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO trace_comparisons (
            id, experiment_id, trace_a_id, trace_b_id,
            jig_revision, trace_diff, domain_diff, created_at
        )
        SELECT
            id, experiment_id,
            json_extract(trace_diff, '$.trace_a_id'),
            json_extract(trace_diff, '$.trace_b_id'),
            jig_revision, trace_diff, domain_diff, created_at
        FROM trace_comparisons_v30
        """
    )
    conn.execute("DROP TABLE trace_comparisons_v30")
    conn.execute(
        "CREATE INDEX trace_comparisons_trace_a_idx "
        "ON trace_comparisons(trace_a_id, created_at, id)"
    )
    conn.execute(
        "CREATE INDEX trace_comparisons_trace_b_idx "
        "ON trace_comparisons(trace_b_id, created_at, id)"
    )
    conn.execute(
        """
        CREATE TRIGGER trace_comparisons_valid_insert
        BEFORE INSERT ON trace_comparisons
        WHEN json_valid(NEW.trace_diff) = 0 OR json_valid(NEW.domain_diff) = 0
        BEGIN
            SELECT RAISE(ABORT, 'trace_comparisons JSON columns must be valid JSON');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER trace_comparisons_identity_insert
        BEFORE INSERT ON trace_comparisons
        WHEN json_valid(NEW.trace_diff) = 1
         AND (
             json_extract(NEW.trace_diff, '$.trace_a_id') IS NOT NEW.trace_a_id
          OR json_extract(NEW.trace_diff, '$.trace_b_id') IS NOT NEW.trace_b_id
          OR NOT EXISTS (
              SELECT 1
              FROM evaluation_experiments e
              JOIN evaluation_phase_runs pr ON pr.id = e.phase_run_id
              WHERE e.id = NEW.experiment_id
                AND e.status = 'running'
                AND pr.trace_id = NEW.trace_a_id
                AND e.candidate_trace_id = NEW.trace_b_id
          ))
        BEGIN
            SELECT RAISE(
                ABORT,
                'trace_comparisons trace identities do not match experiment evidence'
            );
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER trace_comparisons_no_update
        BEFORE UPDATE ON trace_comparisons
        BEGIN
            SELECT RAISE(ABORT, 'trace_comparisons is immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER trace_comparisons_no_delete
        BEFORE DELETE ON trace_comparisons
        BEGIN
            SELECT RAISE(ABORT, 'trace_comparisons is immutable');
        END
        """
    )


def _migrate_to_32(conn: sqlite3.Connection) -> None:
    """Add durable human-positive -> draft promotion workflow state."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS human_positive_promotions (
            source_evaluation_id INTEGER PRIMARY KEY,
            source_grade_id      INTEGER NOT NULL,
            scan_id              INTEGER,
            target_evaluation_id INTEGER,
            status               TEXT NOT NULL
                CHECK(status IN ('running', 'completed', 'failed')),
            error_detail         TEXT
                CHECK(error_detail IS NULL OR length(error_detail) <= 2000),
            created_at           TEXT NOT NULL,
            updated_at           TEXT NOT NULL,
            completed_at         TEXT,
            CHECK(
                (status = 'completed' AND scan_id IS NOT NULL
                    AND target_evaluation_id IS NOT NULL AND completed_at IS NOT NULL
                    AND error_detail IS NULL)
                OR status IN ('running', 'failed')
            ),
            FOREIGN KEY (source_evaluation_id) REFERENCES evaluations(id),
            FOREIGN KEY (source_grade_id) REFERENCES grades(id),
            FOREIGN KEY (scan_id) REFERENCES scans(id),
            FOREIGN KEY (target_evaluation_id) REFERENCES evaluations(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS human_positive_promotions_status_idx "
        "ON human_positive_promotions(status, updated_at, source_evaluation_id)"
    )


class AutonomyEventsNotEmptyError(RuntimeError):
    """Raised when migration 33 finds rows in autonomy_events.

    Migration 33 rebuilds the table rather than altering it, so it is only
    safe while the table is empty. It has always been empty in every
    environment Scout runs — the two declarations that would write to it
    are `deployment: disabled`, and outbound_content_publish has never
    recorded a position change — but "has always been" is not a guarantee,
    and an append-only governance audit trail is the last thing that
    should be discarded on an assumption. If this raises, the migration
    aborts inside its transaction and the database is untouched.
    """


def _migrate_to_33(conn: sqlite3.Connection) -> None:
    """Align autonomy_events with the paa_runtime EventStore contract (v33).

    Scout is about to serve paa_runtime's EventStore protocol out of this
    table (see paa_event_store.ScoutEventStore), and the shape differs in
    two places:

    * `event_schema` is absent here. It stamps each row with the contract
      version it was written under, and it is NOT NULL with no sensible
      default — a row's schema should be recorded, not inherited from
      whatever the code happened to believe at read time.
    * `scope` is NOT NULL here. A declaration that omits `scopes:`
      resolves at scope None, and both canonical_promotion and
      inbound_reply_surfacing do exactly that.

    Neither is reachable with ALTER TABLE: SQLite rejects ADD COLUMN with
    a NOT NULL constraint and no default, and it has no ALTER COLUMN at
    all for relaxing the second. So this drops and recreates, which is
    only defensible because the table is empty — asserted, not assumed.

    DROP TABLE takes the indexes and the append-only triggers with it and
    does not fire the BEFORE DELETE trigger, which guards row deletion
    rather than table removal.
    """
    (row_count,) = conn.execute("SELECT COUNT(*) FROM autonomy_events").fetchone()
    if row_count:
        raise AutonomyEventsNotEmptyError(
            f"migration 33 rebuilds autonomy_events and refuses to run with "
            f"{row_count} row(s) present; the append-only history would be "
            f"lost. Export the rows and migrate them deliberately instead."
        )

    conn.execute("DROP TABLE autonomy_events")
    conn.execute(
        """
        CREATE TABLE autonomy_events (
            event_schema        TEXT NOT NULL,
            id                  TEXT PRIMARY KEY,
            motion_id           TEXT NOT NULL,
            task                TEXT NOT NULL,
            declaration_version INTEGER NOT NULL,
            scope               TEXT,
            event               TEXT NOT NULL CHECK(event IN (
                'motion_proposed', 'motion_approved', 'motion_rejected', 'position_changed'
            )),
            from_position       TEXT NOT NULL CHECK(from_position IN (
                'manual', 'hitl', 'hotl', 'autonomous'
            )),
            to_position         TEXT NOT NULL CHECK(to_position IN (
                'manual', 'hitl', 'hotl', 'autonomous'
            )),
            evidence_ref        TEXT NOT NULL,
            evidence_sha256     TEXT NOT NULL,
            actor               TEXT NOT NULL,
            reason              TEXT NOT NULL,
            created_at          TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS autonomy_events_scope_idx "
        "ON autonomy_events(task, declaration_version, scope, created_at, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS autonomy_events_motion_idx "
        "ON autonomy_events(motion_id, created_at, id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS autonomy_events_motion_event_unique "
        "ON autonomy_events(motion_id, event)"
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS autonomy_events_no_update
        BEFORE UPDATE ON autonomy_events
        BEGIN
            SELECT RAISE(ABORT, 'autonomy_events is append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS autonomy_events_no_delete
        BEFORE DELETE ON autonomy_events
        BEGIN
            SELECT RAISE(ABORT, 'autonomy_events is append-only');
        END
        """
    )


def _migrate_to_34(conn: sqlite3.Connection) -> None:
    """Add immutable reply_draft_revisions lineage and grades.reply_revision_id (v34).

    reply_draft_revisions is the reply-pipeline counterpart to
    draft_revisions: an ordered, immutable lineage of corrected reply text
    for a draft_comments row, keyed by (draft_comment_id, version) with a
    nullable self-referential parent_revision_id and the same
    grade_revisions-style append-only UPDATE/DELETE triggers. No existing
    table changes shape and no backfill runs — draft_comments rows carry
    no correction history before this migration, so there is nothing to
    reconstruct.

    grades.reply_revision_id is a nullable pointer added to the existing
    grades table, added with the same inline-REFERENCES ALTER TABLE
    pattern migration 6 used for evaluations.keyword_route_id. Every
    pre-migration-34 grade keeps reply_revision_id NULL rather than
    fabricating correction lineage that never existed.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reply_draft_revisions (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            draft_comment_id   INTEGER NOT NULL,
            version            INTEGER NOT NULL,
            parent_revision_id INTEGER,
            reply_text         TEXT NOT NULL,
            source             TEXT NOT NULL CHECK(
                source IN ('cli', 'web', 'migration') AND length(trim(source)) > 0
            ),
            created_at         TEXT NOT NULL,
            UNIQUE (draft_comment_id, version),
            FOREIGN KEY (draft_comment_id) REFERENCES draft_comments(id),
            FOREIGN KEY (parent_revision_id) REFERENCES reply_draft_revisions(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS reply_draft_revisions_draft_comment_id_idx "
        "ON reply_draft_revisions(draft_comment_id)"
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS reply_draft_revisions_no_update
        BEFORE UPDATE ON reply_draft_revisions
        BEGIN
            SELECT RAISE(ABORT, 'reply_draft_revisions is immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS reply_draft_revisions_no_delete
        BEFORE DELETE ON reply_draft_revisions
        BEGIN
            SELECT RAISE(ABORT, 'reply_draft_revisions is immutable');
        END
        """
    )

    grade_cols = {row["name"] for row in conn.execute("PRAGMA table_info(grades)")}
    if "reply_revision_id" not in grade_cols:
        conn.execute(
            "ALTER TABLE grades ADD COLUMN reply_revision_id INTEGER "
            "REFERENCES reply_draft_revisions(id)"
        )


def _migrate_to_35(conn: sqlite3.Connection) -> None:
    """Widen grades.schema_version's CHECK to admit 3 (v35).

    HUMAN_GRADE_SCHEMA_VERSION becomes 3 as of this release (nullable
    edited_text joins the causal grade contract); SQLite cannot alter a
    CHECK constraint in place, so the table is rebuilt exactly as in
    migration 21 plus migration 34's reply_revision_id column — same IDs,
    same columns, same other CHECK constraints, foreign keys, and the
    partial unique evaluation_id index. No data changes: every existing
    row is schema_version 1 or 2 already, both still valid.
    """
    conn.execute("PRAGMA legacy_alter_table = ON")
    conn.execute("ALTER TABLE grades RENAME TO grades_v34")
    conn.execute("""
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
            comment_quality INTEGER,
            comment_issue TEXT,
            schema_version INTEGER NOT NULL DEFAULT 1 CHECK(schema_version IN (1, 2, 3)),
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
            implication_missing_support TEXT,
            reply_revision_id INTEGER,
            FOREIGN KEY (evaluation_id) REFERENCES evaluations(id),
            FOREIGN KEY (post_id) REFERENCES posts(id),
            FOREIGN KEY (reply_revision_id) REFERENCES reply_draft_revisions(id)
        )
    """)
    conn.execute("""
        INSERT INTO grades (
            id, evaluation_id, post_id, scan_id, source, graded_at,
            relevance_judgment, rejection_reason,
            comment_quality, comment_issue,
            schema_version, needs_regrade, action_judgment, dimensions,
            failure_note, factual_offending_claim, factual_disposition,
            factual_contradicting_evidence, context_missing_input,
            posture_should_have_been, implication_implied_claim,
            implication_missing_support, reply_revision_id
        )
        SELECT
            id, evaluation_id, post_id, scan_id, source, graded_at,
            relevance_judgment, rejection_reason,
            comment_quality, comment_issue,
            schema_version, needs_regrade, action_judgment, dimensions,
            failure_note, factual_offending_claim, factual_disposition,
            factual_contradicting_evidence, context_missing_input,
            posture_should_have_been, implication_implied_claim,
            implication_missing_support, reply_revision_id
        FROM grades_v34
    """)
    conn.execute("DROP TABLE grades_v34")

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS grades_evaluation_id_unique"
        " ON grades(evaluation_id) WHERE evaluation_id IS NOT NULL"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS grades_scan_id_idx ON grades(scan_id)")
    conn.execute("PRAGMA legacy_alter_table = OFF")


def _migrate_to_36(conn: sqlite3.Connection) -> None:
    """Version experiment evidence (v36): split the v30-v35 one-off
    evaluation_experiments row into a versioned experiment_runs parent
    (candidate-only config v2) and an immutable per-baseline-case
    evaluation_experiments attempt child (baseline_evidence v2), and add
    trace_comparisons.score_evidence for Scout-native reply_draft
    correction grading.

    Every existing row becomes exactly one experiment_runs parent with
    exactly one linked child attempt (attempt_number=1, no
    supersedes_experiment_id), preserving the child's original id so
    trace_comparisons.experiment_id foreign keys resolve unchanged and no
    comparison evidence needs touching beyond gaining a NULL
    score_evidence column. candidate_config v1 is split deterministically:
    phase/model/system_prompt/system_prompt_sha256/grader_attached move to
    the new parent's v2 config; recorded_input_sha256/baseline_prompt_reused
    move to the child's v2 baseline_evidence. v1's recorded_input_reused
    was always literally true and carries no information, so it is
    dropped rather than migrated. No migrated row was ever graded —
    grader_attached stays false and baseline_evidence never gains the
    reply-correction provenance fields (reply_revision_id, correction
    hash, dossier pin, baseline model/prompt hash, grader/assembler
    version), which only new writes populate; retaining v1's evidence
    as-is here is deliberate — nothing here fabricates a score or a
    correction that was never recorded.

    evaluation_experiments is the only table other tables foreign-key
    into that changes shape, so it is rebuilt first (rename, recreate,
    migrate rows) and trace_comparisons is rebuilt second, with a fresh
    FOREIGN KEY declaration written directly against the new
    evaluation_experiments table — rebuilding trace_comparisons before
    dropping the renamed evaluation_experiments_v35 avoids ever leaving a
    foreign key pointed at a table this migration is about to drop.
    """
    # experiment_runs is a genuinely new table with no prior migration —
    # IF NOT EXISTS throughout, matching migration 30's original pattern
    # for evaluation_experiments/trace_comparisons, so this stays a no-op
    # against a test harness that bootstraps the latest SCHEMA (which
    # already creates experiment_runs) before replaying migrations from an
    # older stamped user_version.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS experiment_runs (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT NOT NULL,
            status           TEXT NOT NULL
                CHECK(status IN ('queued', 'running', 'complete', 'partial', 'failed')),
            candidate_config TEXT NOT NULL,
            created_at       TEXT NOT NULL,
            completed_at     TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS experiment_runs_status_idx "
        "ON experiment_runs(status, created_at, id)"
    )
    # Triggers are created only after the legacy backfill loop below, not
    # here: experiment_runs_valid_insert requires status='queued', but the
    # backfill inserts each migrated parent directly in its already-terminal
    # historical status (matching migration 17/20/31's "rebuild, backfill,
    # then constrain" ordering).

    conn.execute("DROP TRIGGER IF EXISTS evaluation_experiments_valid_insert")
    conn.execute("DROP TRIGGER IF EXISTS evaluation_experiments_lifecycle")
    conn.execute("DROP TRIGGER IF EXISTS evaluation_experiments_no_delete")
    # Index names are schema-global in SQLite, not per-table — renaming the
    # table leaves its indexes attached to the renamed table under their
    # original names, so they must be dropped explicitly before the new
    # evaluation_experiments table can reuse those same index names.
    # evaluation_experiments_run_idx is new in v36, but a fixture that
    # bootstraps the latest SCHEMA before replaying migrations from an
    # older stamped user_version will already have created it on the
    # about-to-be-renamed table too.
    conn.execute("DROP INDEX IF EXISTS evaluation_experiments_run_idx")
    conn.execute("DROP INDEX IF EXISTS evaluation_experiments_phase_run_idx")
    conn.execute("DROP INDEX IF EXISTS evaluation_experiments_status_idx")
    conn.execute("ALTER TABLE evaluation_experiments RENAME TO evaluation_experiments_v35")

    conn.execute(
        """
        CREATE TABLE evaluation_experiments (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_run_id        INTEGER NOT NULL,
            phase_run_id             INTEGER NOT NULL,
            attempt_number           INTEGER NOT NULL CHECK(attempt_number >= 1),
            supersedes_experiment_id INTEGER,
            status                   TEXT NOT NULL
                CHECK(status IN ('queued', 'running', 'complete', 'failed')),
            baseline_evidence        TEXT NOT NULL,
            candidate_trace_id       TEXT UNIQUE,
            candidate_llm_call_count INTEGER
                CHECK(candidate_llm_call_count IS NULL OR candidate_llm_call_count >= 0),
            candidate_cost           REAL CHECK(candidate_cost IS NULL OR candidate_cost >= 0),
            error_detail             TEXT
                CHECK(error_detail IS NULL OR length(error_detail) <= 2000),
            created_at               TEXT NOT NULL,
            completed_at             TEXT,
            UNIQUE(experiment_run_id, phase_run_id, attempt_number),
            FOREIGN KEY (experiment_run_id) REFERENCES experiment_runs(id),
            FOREIGN KEY (phase_run_id) REFERENCES evaluation_phase_runs(id),
            FOREIGN KEY (supersedes_experiment_id) REFERENCES evaluation_experiments(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX evaluation_experiments_run_idx "
        "ON evaluation_experiments(experiment_run_id, created_at, id)"
    )
    conn.execute(
        "CREATE INDEX evaluation_experiments_phase_run_idx "
        "ON evaluation_experiments(phase_run_id, created_at, id)"
    )
    conn.execute(
        "CREATE INDEX evaluation_experiments_status_idx "
        "ON evaluation_experiments(status, created_at, id)"
    )

    # A handful of older migration tests build their fixture by bootstrapping
    # the latest SCHEMA (already v36-shaped) for every table they don't care
    # about, then stamp an old user_version and replay migrations forward
    # from there — so evaluation_experiments_v35 is occasionally already in
    # the *new* shape rather than the v1-v35 shape this migration expects to
    # split. Detect that case and pass rows through unchanged (always 0 rows
    # in practice, since none of those fixtures seed this table) instead of
    # reading v1-only columns that no longer exist.
    old_cols = {r["name"] for r in conn.execute("PRAGMA table_info(evaluation_experiments_v35)")}
    if "candidate_config" not in old_cols:
        conn.execute(
            "INSERT INTO evaluation_experiments SELECT * FROM evaluation_experiments_v35"
        )
        legacy_rows: list[sqlite3.Row] = []
    else:
        legacy_rows = conn.execute(
            "SELECT id, phase_run_id, name, status, candidate_config, candidate_trace_id, "
            "candidate_llm_call_count, candidate_cost, error_detail, created_at, completed_at "
            "FROM evaluation_experiments_v35 ORDER BY id"
        ).fetchall()
    for row in legacy_rows:
        v1_config = json.loads(row["candidate_config"])
        v2_config = json.dumps(
            {
                "version": 2,
                "phase": v1_config["phase"],
                "model": v1_config["model"],
                "system_prompt": v1_config["system_prompt"],
                "system_prompt_sha256": v1_config["system_prompt_sha256"],
                "grader_attached": v1_config.get("grader_attached", False),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        run_cursor = conn.execute(
            "INSERT INTO experiment_runs "
            "(name, status, candidate_config, created_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (row["name"], row["status"], v2_config, row["created_at"], row["completed_at"]),
        )
        experiment_run_id = run_cursor.lastrowid
        baseline_evidence = json.dumps(
            {
                "version": 2,
                "recorded_input_sha256": v1_config["recorded_input_sha256"],
                "baseline_prompt_reused": v1_config["baseline_prompt_reused"],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        conn.execute(
            "INSERT INTO evaluation_experiments "
            "(id, experiment_run_id, phase_run_id, attempt_number, supersedes_experiment_id, "
            "status, baseline_evidence, candidate_trace_id, candidate_llm_call_count, "
            "candidate_cost, error_detail, created_at, completed_at) "
            "VALUES (?, ?, ?, 1, NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"],
                experiment_run_id,
                row["phase_run_id"],
                row["status"],
                baseline_evidence,
                row["candidate_trace_id"],
                row["candidate_llm_call_count"],
                row["candidate_cost"],
                row["error_detail"],
                row["created_at"],
                row["completed_at"],
            ),
        )

    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS experiment_runs_valid_insert
        BEFORE INSERT ON experiment_runs
        WHEN json_valid(NEW.candidate_config) = 0
          OR NEW.status != 'queued'
          OR NEW.completed_at IS NOT NULL
        BEGIN
            SELECT RAISE(
                ABORT, 'experiment_runs must insert as a clean queued row with valid JSON config'
            );
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS experiment_runs_immutable_identity
        BEFORE UPDATE ON experiment_runs
        BEGIN
            SELECT RAISE(ABORT, 'experiment_runs name/candidate_config/created_at cannot change')
            WHERE NEW.name IS NOT OLD.name
               OR NEW.candidate_config IS NOT OLD.candidate_config
               OR NEW.created_at IS NOT OLD.created_at;
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS experiment_runs_no_delete
        BEFORE DELETE ON experiment_runs
        BEGIN
            SELECT RAISE(ABORT, 'experiment_runs rows cannot be deleted');
        END
        """
    )

    conn.execute(
        """
        CREATE TRIGGER evaluation_experiments_valid_insert
        BEFORE INSERT ON evaluation_experiments
        WHEN json_valid(NEW.baseline_evidence) = 0
          OR NEW.status != 'queued'
          OR NEW.candidate_trace_id IS NOT NULL
          OR NEW.candidate_llm_call_count IS NOT NULL
          OR NEW.candidate_cost IS NOT NULL
          OR NEW.error_detail IS NOT NULL
          OR NEW.completed_at IS NOT NULL
        BEGIN
            SELECT RAISE(
                ABORT,
                'evaluation_experiments must insert as a clean queued attempt with valid JSON'
            );
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER evaluation_experiments_lifecycle
        BEFORE UPDATE ON evaluation_experiments
        BEGIN
            SELECT RAISE(
                ABORT,
                'evaluation_experiments attempts only allow queued->running->complete/failed'
            )
            WHERE OLD.status IN ('complete', 'failed')
               OR NEW.experiment_run_id IS NOT OLD.experiment_run_id
               OR NEW.phase_run_id IS NOT OLD.phase_run_id
               OR NEW.attempt_number IS NOT OLD.attempt_number
               OR NEW.supersedes_experiment_id IS NOT OLD.supersedes_experiment_id
               OR NEW.baseline_evidence IS NOT OLD.baseline_evidence
               OR NEW.created_at IS NOT OLD.created_at
               OR (OLD.candidate_trace_id IS NOT NULL
                   AND NEW.candidate_trace_id IS NOT OLD.candidate_trace_id)
               OR (OLD.candidate_llm_call_count IS NOT NULL
                   AND NEW.candidate_llm_call_count IS NOT OLD.candidate_llm_call_count)
               OR (OLD.candidate_cost IS NOT NULL AND NEW.candidate_cost IS NOT OLD.candidate_cost)
               OR NOT (
                    (OLD.status = 'queued' AND NEW.status = 'running')
                 OR (OLD.status = 'running' AND NEW.status IN ('running', 'complete', 'failed'))
               );
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER evaluation_experiments_no_delete
        BEFORE DELETE ON evaluation_experiments
        BEGIN
            SELECT RAISE(ABORT, 'evaluation_experiments rows cannot be deleted');
        END
        """
    )

    # trace_comparisons is rebuilt (not ALTER TABLE ADD COLUMN) so its
    # FOREIGN KEY is re-authored directly against the new
    # evaluation_experiments table rather than inheriting SQLite's
    # automatic rename-tracking rewrite (which would otherwise leave it
    # pointed at evaluation_experiments_v35, the table dropped below).
    conn.execute("DROP TRIGGER IF EXISTS trace_comparisons_valid_insert")
    conn.execute("DROP TRIGGER IF EXISTS trace_comparisons_identity_insert")
    conn.execute("DROP TRIGGER IF EXISTS trace_comparisons_no_update")
    conn.execute("DROP TRIGGER IF EXISTS trace_comparisons_no_delete")
    conn.execute("DROP INDEX IF EXISTS trace_comparisons_trace_a_idx")
    conn.execute("DROP INDEX IF EXISTS trace_comparisons_trace_b_idx")
    conn.execute("ALTER TABLE trace_comparisons RENAME TO trace_comparisons_v35")
    conn.execute(
        """
        CREATE TABLE trace_comparisons (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id  INTEGER NOT NULL UNIQUE,
            trace_a_id     TEXT NOT NULL,
            trace_b_id     TEXT NOT NULL,
            jig_revision   TEXT NOT NULL,
            trace_diff     TEXT NOT NULL,
            domain_diff    TEXT NOT NULL,
            score_evidence TEXT,
            created_at     TEXT NOT NULL,
            FOREIGN KEY (experiment_id) REFERENCES evaluation_experiments(id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO trace_comparisons (
            id, experiment_id, trace_a_id, trace_b_id,
            jig_revision, trace_diff, domain_diff, score_evidence, created_at
        )
        SELECT
            id, experiment_id, trace_a_id, trace_b_id,
            jig_revision, trace_diff, domain_diff, NULL, created_at
        FROM trace_comparisons_v35
        """
    )
    conn.execute("DROP TABLE trace_comparisons_v35")
    conn.execute("DROP TABLE evaluation_experiments_v35")
    conn.execute(
        "CREATE INDEX trace_comparisons_trace_a_idx "
        "ON trace_comparisons(trace_a_id, created_at, id)"
    )
    conn.execute(
        "CREATE INDEX trace_comparisons_trace_b_idx "
        "ON trace_comparisons(trace_b_id, created_at, id)"
    )
    conn.execute(
        """
        CREATE TRIGGER trace_comparisons_valid_insert
        BEFORE INSERT ON trace_comparisons
        WHEN json_valid(NEW.trace_diff) = 0
          OR json_valid(NEW.domain_diff) = 0
          OR (NEW.score_evidence IS NOT NULL AND json_valid(NEW.score_evidence) = 0)
        BEGIN
            SELECT RAISE(ABORT, 'trace_comparisons JSON columns must be valid JSON');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER trace_comparisons_identity_insert
        BEFORE INSERT ON trace_comparisons
        WHEN json_valid(NEW.trace_diff) = 1
         AND (
             json_extract(NEW.trace_diff, '$.trace_a_id') IS NOT NEW.trace_a_id
          OR json_extract(NEW.trace_diff, '$.trace_b_id') IS NOT NEW.trace_b_id
          OR NOT EXISTS (
              SELECT 1
              FROM evaluation_experiments e
              JOIN evaluation_phase_runs pr ON pr.id = e.phase_run_id
              WHERE e.id = NEW.experiment_id
                AND e.status = 'running'
                AND pr.trace_id = NEW.trace_a_id
                AND e.candidate_trace_id = NEW.trace_b_id
          ))
        BEGIN
            SELECT RAISE(
                ABORT,
                'trace_comparisons trace identities do not match experiment evidence'
            );
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER trace_comparisons_no_update
        BEFORE UPDATE ON trace_comparisons
        BEGIN
            SELECT RAISE(ABORT, 'trace_comparisons is immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER trace_comparisons_no_delete
        BEFORE DELETE ON trace_comparisons
        BEGIN
            SELECT RAISE(ABORT, 'trace_comparisons is immutable');
        END
        """
    )


# Candidate statuses migration 22 treats as closing every open annotation.
# Formerly content_contracts.ANNOTATION_CLOSED_STATUSES; inlined when the
# content engine left this repository (migration 37) so the historical
# migration keeps replaying identically without the module it came from.
ANNOTATION_CLOSED_STATUSES: frozenset[str] = frozenset({"approved", "published", "rejected"})


def _migrate_to_37(conn: sqlite3.Connection) -> None:
    """Drop the outbound content engine's nine tables (v37).

    The content lane — candidates, outbound drafts, draft revisions and
    annotations, publications, publish reviews and decisions, revision
    provenance, publication verifications — moved to its own repository.
    Its rows are deliberately not migrated: the schemas travelled and the
    rows did not (Spec 3 A1, decided 2026-09-01), and the deploy
    procedure takes a full backup before this runs. Dropped child-first so
    the order is valid whether or not foreign_keys is enforced during
    migration; each DROP is IF EXISTS so the migration is a no-op against
    a database bootstrapped from a SCHEMA that never created them. Their
    indexes and triggers drop with them.
    """
    for table in (
        "outbound_publish_review_decisions",
        "outbound_publish_reviews",
        "outbound_publication_verifications",
        "outbound_revision_provenance",
        "publications",
        "draft_annotations",
        "draft_revisions",
        "outbound_drafts",
        "content_candidates",
    ):
        conn.execute(f"DROP TABLE IF EXISTS {table}")


def _migrate_to_38(conn: sqlite3.Connection) -> None:
    """Retain exact analysis bytes and append-only lineage in scout.db."""
    from scout.storage.schema import ARTIFACT_SCHEMA_STATEMENTS

    for statement in ARTIFACT_SCHEMA_STATEMENTS:
        conn.execute(statement)


def _migrate_to_39(conn: sqlite3.Connection) -> None:
    """Protect both revision uniqueness keys against SQLite REPLACE deletion."""
    from scout.storage.schema import GRADE_REVISION_NO_REPLACE

    conn.execute(GRADE_REVISION_NO_REPLACE)


MIGRATIONS: dict[int, Migration] = {
    2: _migrate_to_2,
    3: _migrate_to_3,
    4: _migrate_to_4,
    5: _migrate_to_5,
    6: _migrate_to_6,
    7: _migrate_to_7,
    8: _migrate_to_8,
    9: _migrate_to_9,
    10: _migrate_to_10,
    11: _migrate_to_11,
    12: _migrate_to_12,
    13: _migrate_to_13,
    14: _migrate_to_14,
    15: _migrate_to_15,
    16: _migrate_to_16,
    17: _migrate_to_17,
    18: _migrate_to_18,
    19: _migrate_to_19,
    20: _migrate_to_20,
    21: _migrate_to_21,
    22: _migrate_to_22,
    23: _migrate_to_23,
    24: _migrate_to_24,
    25: _migrate_to_25,
    26: _migrate_to_26,
    27: _migrate_to_27,
    28: _migrate_to_28,
    29: _migrate_to_29,
    30: _migrate_to_30,
    31: _migrate_to_31,
    32: _migrate_to_32,
    33: _migrate_to_33,
    34: _migrate_to_34,
    35: _migrate_to_35,
    36: _migrate_to_36,
    37: _migrate_to_37,
    38: _migrate_to_38,
    39: _migrate_to_39,
}
