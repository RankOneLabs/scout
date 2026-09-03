"""Read-only Phase 1 audit evidence loading.

Opens exactly one query-only SQLite connection and projects the production
population it observes into a materialized, read-only snapshot, so every
criterion evaluated afterward sees the same database state and cannot
trigger a further query against a stale or closed connection.
"""
# ruff: noqa: E501

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def parse_utc(value: str) -> datetime:
    """Parse an explicit offset-bearing timestamp and normalize it to UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a UTC offset")
    return parsed.astimezone(UTC)


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params)]


def complete_grade(row: dict[str, Any]) -> bool:
    if row.get("schema_version") != 2 or row.get("needs_regrade"):
        return False
    action = row.get("action_judgment")
    if action == "accept":
        return True
    if action != "fail":
        return False
    try:
        dimensions = json.loads(row.get("dimensions") or "[]")
    except json.JSONDecodeError:
        return False
    if not isinstance(dimensions, list) or not dimensions or not row.get("failure_note"):
        return False
    if "contextual_understanding" in dimensions and not row.get("context_missing_input"):
        return False
    if "posture" in dimensions and not row.get("posture_should_have_been"):
        return False
    if "factual_support" in dimensions:
        if not row.get("factual_offending_claim") or not row.get("factual_disposition"):
            return False
        if row["factual_disposition"] == "contradicted" and not row.get(
            "factual_contradicting_evidence"
        ):
            return False
    return not (
        "unsupported_implication" in dimensions
        and (not row.get("implication_implied_claim") or not row.get("implication_missing_support"))
    )


@dataclass(frozen=True)
class AuditSnapshot:
    """Materialized, read-only projection of one query-only connection's
    evidence — frozen against reassignment, not a deep-immutable structure;
    callers must not mutate its list/dict fields in place.

    Every field is populated in ``load_snapshot`` before the connection that
    read it closes — nothing downstream re-queries the database, so every
    criterion and replay step observes exactly this one consistent read.
    """

    scans: list[dict[str, Any]]
    qualifying_scans: list[dict[str, Any]]
    excluded_scans: Counter[str]
    active_projects: tuple[str, ...]
    evaluations: list[dict[str, Any]]
    evaluation_ids: set[int]
    grades: list[dict[str, Any]]
    grades_by_eval: dict[int, list[dict[str, Any]]]
    potential_orphans: list[dict[str, Any]]
    blocks: list[dict[str, Any]]
    fetch_failures: list[dict[str, Any]]
    assessments: list[dict[str, Any]]
    events: list[dict[str, Any]]
    drafts: list[dict[str, Any]]
    drafts_by_eval: dict[int, list[dict[str, Any]]]
    events_by_eval: dict[int, list[dict[str, Any]]]


@contextmanager
def open_read_only_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open *db_path* query-only and hold one explicit deferred read
    transaction across the connection's whole lifetime.

    Without an explicit BEGIN, SQLite runs each statement in its own
    autocommit transaction, so a concurrent writer could commit between two
    of load_snapshot's queries and the "one consistent snapshot" the rest
    of this module promises would be false. BEGIN pins one view of the
    database for every query issued through *conn* until it closes; the
    audit never opens a second connection or a write path onto its own
    evidence."""
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.execute("ROLLBACK")
        conn.close()


def load_snapshot(
    conn: sqlite3.Connection, window_from: datetime, window_to: datetime
) -> AuditSnapshot:
    """Load every table this audit reads through *conn*, exactly once each."""
    start, end = window_from.isoformat(), window_to.isoformat()
    scans = _rows(
        conn,
        "SELECT * FROM scans WHERE started_at >= ? AND started_at < ? ORDER BY id",
        (start, end),
    )
    qualifying_scans = [
        s for s in scans if s.get("environment") == "production" and s.get("run_kind") == "live"
    ]
    active_projects = tuple(
        str(row["key"])
        for row in _rows(conn, "SELECT key FROM projects WHERE active = 1 ORDER BY key")
    )
    excluded_scans = Counter(
        f"{s.get('environment') or 'missing'}:{s.get('run_kind') or 'missing'}"
        for s in scans
        if s not in qualifying_scans
    )
    scan_ids = [int(s["id"]) for s in qualifying_scans]
    marks = ",".join("?" for _ in scan_ids) or "NULL"
    evaluations = _rows(
        conn,
        f"""SELECT e.*, p.platform, p.platform_msg_id, p.author_id, p.content, p.url,
                   p.parent_lookup_status, p.parent_id
              FROM evaluations e JOIN posts p ON p.id=e.post_id
             WHERE e.scan_id IN ({marks}) ORDER BY e.id""",
        tuple(scan_ids),
    )
    evaluation_ids = {int(row["id"]) for row in evaluations}
    grades = _rows(
        conn,
        f"SELECT * FROM grades WHERE evaluation_id IN (SELECT id FROM evaluations WHERE scan_id IN ({marks})) ORDER BY id",
        tuple(scan_ids),
    )
    grades_by_eval: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for grade in grades:
        if grade.get("evaluation_id") is not None:
            grades_by_eval[int(grade["evaluation_id"])].append(grade)
    potential_orphans = _rows(
        conn,
        f"SELECT * FROM grades WHERE scan_id IN ({marks}) AND (evaluation_id IS NULL OR evaluation_id NOT IN (SELECT id FROM evaluations WHERE scan_id IN ({marks})))",
        tuple(scan_ids) * 2,
    )
    blocks = _rows(
        conn,
        f"SELECT gb.*, e.surface_status, p.platform, p.platform_msg_id, p.author_id, p.content, p.url FROM gate_blocks gb JOIN evaluations e ON e.id=gb.evaluation_id JOIN posts p ON p.id=gb.post_id WHERE gb.scan_id IN ({marks}) ORDER BY gb.id",
        tuple(scan_ids),
    )
    fetch_failures = _rows(
        conn,
        f"SELECT * FROM scan_fetch_failures WHERE scan_id IN ({marks}) ORDER BY id",
        tuple(scan_ids),
    )
    assessments = _rows(
        conn,
        f"SELECT a.*, e.relevant, e.posture, p.platform, p.parent_lookup_status, p.parent_id FROM parent_context_assessments a JOIN evaluations e ON e.id=a.evaluation_id JOIN posts p ON p.id=e.post_id WHERE e.scan_id IN ({marks})",
        tuple(scan_ids),
    )
    events = _rows(
        conn,
        f"SELECT se.* FROM surfaced_events se JOIN evaluations e ON e.id=se.evaluation_id WHERE e.scan_id IN ({marks}) ORDER BY se.author_id, se.surfaced_at, se.id",
        tuple(scan_ids),
    )
    drafts = _rows(
        conn,
        f"SELECT d.*, e.surface_status, e.post_id AS evaluation_post_id, p.platform, p.author_id "
        f"FROM draft_comments d JOIN evaluations e ON e.id=d.evaluation_id "
        f"JOIN posts p ON p.id=d.post_id WHERE e.scan_id IN ({marks}) ORDER BY d.id",
        tuple(scan_ids),
    )
    drafts_by_eval: dict[int, list[dict[str, Any]]] = defaultdict(list)
    events_by_eval: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for draft in drafts:
        drafts_by_eval[int(draft["evaluation_id"])].append(draft)
    for event in events:
        events_by_eval[int(event["evaluation_id"])].append(event)

    return AuditSnapshot(
        scans=scans,
        qualifying_scans=qualifying_scans,
        excluded_scans=excluded_scans,
        active_projects=active_projects,
        evaluations=evaluations,
        evaluation_ids=evaluation_ids,
        grades=grades,
        grades_by_eval=grades_by_eval,
        potential_orphans=potential_orphans,
        blocks=blocks,
        fetch_failures=fetch_failures,
        assessments=assessments,
        events=events,
        drafts=drafts,
        drafts_by_eval=drafts_by_eval,
        events_by_eval=events_by_eval,
    )
