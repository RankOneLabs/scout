"""Scan aggregate: scan lifecycle, per-platform fetch failures, and the
author blocklist. Owns the `scans`, `scan_fetch_failures`, and
`blocked_authors` tables.

Constructed with the same `UnitOfWork` `StateManager` and every sibling
store share — see `unit_of_work.py` for why no store opens its own
connection.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from scout.storage.unit_of_work import UnitOfWork

logger = logging.getLogger(__name__)

ScanStatus = Literal["complete", "partial", "failed", "interrupted"]


@dataclass(frozen=True, slots=True)
class ScanFetchFailure:
    """One `scan_fetch_failures` row, as returned by `get_scan_fetch_failures`."""

    platform: str
    context: str | None
    kind: str
    message: str | None
    http_status: int | None
    retry_after: str | None
    retryable: bool


class ScanStore:
    """Owns scan lifecycle, fetch-failure recording, and author blocking."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    @property
    def _conn(self) -> sqlite3.Connection:
        return self._uow.conn

    def get_last_scan_timestamp(self) -> datetime | None:
        """Return the safe watermark from the most recent scan that set one, or None.

        Returns safe_watermark_at rather than completed_at so the next scan's
        since boundary is anchored to when fetching started on the prior scan,
        not when processing finished — avoiding a lossy gap for messages that
        arrive during the processing window.
        """
        row = self._conn.execute(
            "SELECT safe_watermark_at FROM scans "
            "WHERE safe_watermark_at IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row and row["safe_watermark_at"]:
            return datetime.fromisoformat(row["safe_watermark_at"])
        return None

    def get_latest_completed_scan_id(self) -> int | None:
        """Return the id of the most recent completed scan, or None."""
        row = self._conn.execute(
            "SELECT id FROM scans WHERE completed_at IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return int(row["id"])

    def start_scan(
        self,
        fetch_started_at: datetime | None = None,
        *,
        environment: str = "development",
        run_kind: str = "live",
    ) -> int:
        """Record the start of a new scan. Returns scan ID.

        fetch_started_at anchors the safe watermark for this scan. Pass it
        as the timestamp captured immediately before calling the platform
        clients so the watermark covers the full fetch window including any
        messages that arrive while the previous scan is still processing.
        """
        now = datetime.now(UTC).isoformat()
        fsa = (fetch_started_at or datetime.now(UTC)).isoformat()
        with self._uow.begin():
            cursor = self._conn.execute(
                "INSERT INTO scans (started_at, fetch_started_at, environment, run_kind) "
                "VALUES (?, ?, ?, ?)",
                (now, fsa, environment, run_kind),
            )
            scan_id = cursor.lastrowid
        assert scan_id is not None
        logger.info("Started scan #%d", scan_id)
        return scan_id

    def complete_scan(
        self,
        scan_id: int,
        messages_scanned: int,
        relevant_found: int,
        status: ScanStatus = "complete",
        safe_watermark_at: datetime | None = None,
        overflow_count: int = 0,
        advance_watermark: bool = True,
    ) -> None:
        """Mark a scan as complete, partial, failed, or interrupted.

        safe_watermark_at is the timestamp the next scan will use as its
        since boundary. For complete scans it defaults to fetch_started_at
        from the scan row when advance_watermark is true. Non-fetch scans
        such as rescore should pass advance_watermark=False so they do not
        move live platform cursors.
        """
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            if safe_watermark_at is not None:
                wm = safe_watermark_at.isoformat()
            elif status == "complete" and advance_watermark:
                row = self._conn.execute(
                    "SELECT fetch_started_at FROM scans WHERE id = ?", (scan_id,)
                ).fetchone()
                wm = row["fetch_started_at"] if row and row["fetch_started_at"] else now
            else:
                wm = None
            self._conn.execute(
                "UPDATE scans SET completed_at = ?, messages_scanned = ?, relevant_found = ?, "
                "status = ?, safe_watermark_at = ?, overflow_count = ? WHERE id = ?",
                (now, messages_scanned, relevant_found, status, wm, overflow_count, scan_id),
            )
        logger.info(
            "Completed scan #%d (%s): %d scanned, %d relevant, %d overflow",
            scan_id,
            status,
            messages_scanned,
            relevant_found,
            overflow_count,
        )

    def save_fetch_failure(
        self,
        scan_id: int,
        platform: str,
        kind: str,
        message: str,
        context: str | None = None,
        http_status: int | None = None,
        retry_after: str | None = None,
        retryable: bool = True,
    ) -> int:
        """Persist a per-platform fetch failure for operator visibility and retry."""
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            cursor = self._conn.execute(
                "INSERT INTO scan_fetch_failures "
                "(scan_id, platform, context, kind, message, http_status, retry_after, "
                "retryable, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (scan_id, platform, context, kind, message, http_status, retry_after,
                 int(retryable), now),
            )
            failure_id = cursor.lastrowid
        assert failure_id is not None
        return failure_id

    def fail_scan(
        self,
        scan_id: int,
        messages_scanned: int,
        *,
        failure_post_id: int | None,
        error_kind: str,
        error_message: str,
    ) -> None:
        """Mark an active scan as failed and record its triggering error.

        Called after a per-post outcome-persistence failure or
        cancellation: by the time this runs, the failing post's own
        outcome transaction has already rolled back on its own (via
        `Db.begin_immediate()`'s exception handling) — this method never
        touches that unit. It only stamps the scan row `status='failed'`
        and records a `scan_fetch_failures` row carrying
        `failure_post_id` (as `context`) and the error classification, so
        the failure is auditable without a dedicated column. Callers are
        expected to re-raise after this returns; it never converts a
        failed scan into a successful-looking return on its own.

        Idempotent once the scan has a ``completed_at`` value: a higher
        orchestration layer may safely call this while propagating an error
        already recorded by a lower per-post boundary without overwriting
        the terminal status or inserting a duplicate failure row.
        """
        now = datetime.now(UTC).isoformat()
        context = f"post_id:{failure_post_id}" if failure_post_id is not None else None
        with self._uow.begin():
            cursor = self._conn.execute(
                "UPDATE scans SET completed_at = ?, messages_scanned = ?, "
                "status = 'failed' WHERE id = ? AND completed_at IS NULL",
                (now, messages_scanned, scan_id),
            )
            if cursor.rowcount == 0:
                return
            self._conn.execute(
                "INSERT INTO scan_fetch_failures "
                "(scan_id, platform, context, kind, message, retryable, created_at) "
                "VALUES (?, 'scan_runner', ?, ?, ?, 0, ?)",
                (scan_id, context, error_kind, error_message, now),
            )
        logger.error(
            "Scan #%d failed (non-clean end): post_id=%s kind=%s message=%s",
            scan_id, failure_post_id, error_kind, error_message,
        )

    def get_scan_fetch_failures(self, scan_id: int) -> list[ScanFetchFailure]:
        """Return all fetch failures recorded for a scan."""
        rows = self._conn.execute(
            "SELECT platform, context, kind, message, http_status, retry_after, retryable "
            "FROM scan_fetch_failures WHERE scan_id = ? ORDER BY id",
            (scan_id,),
        ).fetchall()
        return [
            ScanFetchFailure(
                platform=row["platform"],
                context=row["context"],
                kind=row["kind"],
                message=row["message"],
                http_status=row["http_status"],
                retry_after=row["retry_after"],
                retryable=bool(row["retryable"]),
            )
            for row in rows
        ]

    @staticmethod
    def _author_identity(platform: str, author_id: str) -> tuple[str, str]:
        normalized_platform = platform.strip().casefold()
        normalized_author_id = author_id.strip()
        if not normalized_platform:
            raise ValueError("platform must be non-empty")
        if not normalized_author_id:
            raise ValueError("author_id must be non-empty")
        return normalized_platform, normalized_author_id

    def block_author(
        self,
        *,
        platform: str,
        author_id: str,
        author_name: str | None = None,
        reason: str | None = None,
    ) -> int:
        """Create or reactivate an author block and return its stable row ID."""
        identity = self._author_identity(platform, author_id)
        now = datetime.now(UTC).isoformat()
        normalized_name = author_name.strip() if author_name and author_name.strip() else None
        normalized_reason = reason.strip() if reason and reason.strip() else None
        with self._uow.begin():
            self._conn.execute(
                "INSERT INTO blocked_authors "
                "(platform, author_id, author_name, reason, active, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 1, ?, ?) "
                "ON CONFLICT(platform, author_id) DO UPDATE SET "
                "author_name = excluded.author_name, reason = excluded.reason, "
                "active = 1, updated_at = excluded.updated_at",
                (*identity, normalized_name, normalized_reason, now, now),
            )
            row = self._conn.execute(
                "SELECT id FROM blocked_authors WHERE platform = ? AND author_id = ?",
                identity,
            ).fetchone()
        assert row is not None
        return int(row["id"])

    def unblock_author(self, *, platform: str, author_id: str) -> bool:
        """Deactivate an author block, returning whether an active row changed."""
        identity = self._author_identity(platform, author_id)
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            cursor = self._conn.execute(
                "UPDATE blocked_authors SET active = 0, updated_at = ? "
                "WHERE platform = ? AND author_id = ? AND active = 1",
                (now, *identity),
            )
        return cursor.rowcount > 0

    def get_blocked_author_keys(self) -> frozenset[tuple[str, str]]:
        """Return active platform/author identities for bulk prefiltering."""
        rows = self._conn.execute(
            "SELECT platform, author_id FROM blocked_authors WHERE active = 1"
        ).fetchall()
        return frozenset((row["platform"].casefold(), row["author_id"]) for row in rows)

    def is_author_blocked(self, *, platform: str, author_id: str) -> bool:
        """Check the live denylist, including blocks added during a scan."""
        identity = self._author_identity(platform, author_id)
        row = self._conn.execute(
            "SELECT 1 FROM blocked_authors "
            "WHERE platform = ? AND author_id = ? AND active = 1",
            identity,
        ).fetchone()
        return row is not None

    def count_scans(self) -> int:
        """Total number of scans ever recorded — one leg of ScanStats."""
        return int(self._conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0])
