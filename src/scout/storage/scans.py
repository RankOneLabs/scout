"""Scan aggregate: scan lifecycle, per-platform fetch failures, coverage
finalization, per-source checkpoints, the author blocklist, and author
classifications. Owns the `scans`, `scan_fetch_failures`,
`scan_watermark_blockers`, `environment_leases`, `source_checkpoints`,
`blocked_authors`, and `author_classifications` tables.

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

from scout.result import Err, Ok, Result
from scout.storage.unit_of_work import UnitOfWork

logger = logging.getLogger(__name__)

ScanStatus = Literal["complete", "partial", "failed", "interrupted"]

# A scan's role in coverage finalization (decision 10). Only 'canonical_live'
# scans are eligible to advance the durable watermark; 'secondary' (e.g.
# scoring/digest-only passes) and 'rescore' runs never do, regardless of
# their own processing status.
ScanRole = Literal["canonical_live", "secondary", "rescore"]

# Coverage outcome is independent of processing `status` (decision 4): a
# scan can be `status='partial'` (degraded by non-primary enrichment) while
# `coverage_outcome='complete'` (primary coverage fully in), or vice versa.
CoverageOutcome = Literal["complete", "partial", "blocked"]


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
    operation_phase: str
    blocks_watermark_advance: bool


@dataclass(frozen=True, slots=True)
class CoverageFinalizationError:
    """A coverage finalization attempt failed an eligibility gate before
    any mutation — trace context for why a scan's watermark did not
    advance and why no coverage_outcome was recorded."""

    operation: str
    scan_id: int
    detail: str


@dataclass(frozen=True, slots=True)
class CoverageFinalizationResult:
    """The durable outcome of one `finalize_scan_coverage` call."""

    scan_id: int
    coverage_outcome: CoverageOutcome
    watermark_advanced: bool
    safe_watermark_at: datetime | None
    coverage_classifier_version: int
    blocking_failure_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class WatermarkAdvanceError:
    """`advance_watermark` could not derive a candidate timestamp."""

    operation: str
    scan_id: int
    detail: str


@dataclass(frozen=True, slots=True)
class SourceCheckpoint:
    """One `source_checkpoints` row — an independent per-source cursor."""

    source_key: str
    platform: str
    source_kind: str
    provider_key: str
    required: bool
    active: bool
    checkpoint_at: datetime | None
    bootstrapped_from_legacy: bool


@dataclass(frozen=True, slots=True)
class SourceCheckpointError:
    """A source-checkpoint operation violated a lifecycle invariant."""

    operation: str
    source_key: str
    detail: str


@dataclass(frozen=True, slots=True)
class StoredAuthorClassification:
    """One `author_classifications` row, as returned by `get_author_classification`."""

    platform: str
    author_id: str
    author_class: str
    rule_version: int
    matched_text: str | None
    classified_at: str


class ScanStore:
    """Owns scan lifecycle, fetch-failure recording, author blocking and
    author classification."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    @property
    def _conn(self) -> sqlite3.Connection:
        return self._uow.conn

    def get_last_scan_timestamp(self, *, environment: str | None = None) -> datetime | None:
        """Return the safe watermark from the most recent eligible scan, or None.

        Returns safe_watermark_at rather than completed_at so the next scan's
        since boundary is anchored to when fetching started on the prior scan,
        not when processing finished — avoiding a lossy gap for messages that
        arrive during the processing window.

        When `environment` is given, only scans whose stored `environment`
        matches it exactly are eligible — development and unknown-environment
        rows never influence a caller reading a specific (e.g. production)
        cursor (decision 5). `environment=None` preserves this method's
        historical any-environment behavior for call sites outside this
        cohort's edit scope (e.g. scout.scanning.runner) that do not yet pass
        one; see docs/known deviations in the coverage-foundation cohort
        report for why this parameter is optional rather than required.
        """
        if environment is None:
            row = self._conn.execute(
                "SELECT safe_watermark_at FROM scans "
                "WHERE safe_watermark_at IS NOT NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT safe_watermark_at FROM scans "
                "WHERE safe_watermark_at IS NOT NULL AND environment = ? "
                "ORDER BY id DESC LIMIT 1",
                (environment,),
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
        role: ScanRole = "canonical_live",
    ) -> int:
        """Record the start of a new scan. Returns scan ID.

        fetch_started_at anchors the safe watermark for this scan. Pass it
        as the timestamp captured immediately before calling the platform
        clients so the watermark covers the full fetch window including any
        messages that arrive while the previous scan is still processing.

        The scan captures `environment`'s current lease fence at start time
        (0 when no lease has ever been taken for that environment) —
        finalize_scan_coverage later checks this stored value against the
        environment's fence at finalization time, so a fence bump between
        start and finalization (a new canonical owner taking over) fails the
        scan closed rather than silently letting a stale owner advance the
        watermark (decision 10).
        """
        now = datetime.now(UTC).isoformat()
        fsa = (fetch_started_at or datetime.now(UTC)).isoformat()
        with self._uow.begin():
            lease_fence = self._get_environment_lease_fence(environment)
            cursor = self._conn.execute(
                "INSERT INTO scans "
                "(started_at, fetch_started_at, environment, run_kind, role, lease_fence) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now, fsa, environment, run_kind, role, lease_fence),
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
        *,
        status: ScanStatus = "complete",
        overflow_count: int = 0,
        advance_watermark: bool = True,
    ) -> None:
        """Mark a scan as complete, partial, failed, or interrupted.

        For complete scans with advance_watermark=True, safe_watermark_at is
        set to the scan's own stored fetch_started_at — the only candidate
        timestamp, with no caller-supplied override and no fallback to "now"
        (decision 3): if a scan somehow has no stored fetch_started_at, the
        watermark is left unset rather than silently advancing to the
        current time. Non-fetch scans such as rescore should pass
        advance_watermark=False so they do not move live platform cursors.

        This method predates and is independent of `finalize_scan_coverage`
        / `advance_watermark`: it is the existing status/watermark surface
        every current caller uses, and continues to gate watermark movement
        on `status == "complete"` exactly as before. New code that needs the
        durable, evidence-derived coverage_outcome and the atomic
        finalization gates from decision 10 should call
        `finalize_scan_coverage` (optionally followed by `advance_watermark`)
        instead.
        """
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            if status == "complete" and advance_watermark:
                row = self._conn.execute(
                    "SELECT fetch_started_at FROM scans WHERE id = ?", (scan_id,)
                ).fetchone()
                wm = row["fetch_started_at"] if row and row["fetch_started_at"] else None
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
        operation_phase: str = "unknown",
        blocks_watermark_advance: bool = True,
    ) -> int:
        """Persist a per-platform fetch failure for operator visibility and retry.

        `operation_phase`/`blocks_watermark_advance` mirror
        `scout.errors.PlatformFetchFailure` and default to the same
        fail-closed "unclassified" pairing so a caller outside this cohort's
        edit scope that has not been updated yet still persists a failure
        `finalize_scan_coverage` treats as blocking.
        """
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            cursor = self._conn.execute(
                "INSERT INTO scan_fetch_failures "
                "(scan_id, platform, context, kind, message, http_status, retry_after, "
                "retryable, operation_phase, blocks_watermark_advance, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (scan_id, platform, context, kind, message, http_status, retry_after,
                 int(retryable), operation_phase, int(blocks_watermark_advance), now),
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
                "(scan_id, platform, context, kind, message, retryable, "
                "operation_phase, blocks_watermark_advance, created_at) "
                "VALUES (?, 'scan_runner', ?, ?, ?, 0, 'scan', 1, ?)",
                (scan_id, context, error_kind, error_message, now),
            )
        logger.error(
            "Scan #%d failed (non-clean end): post_id=%s kind=%s message=%s",
            scan_id, failure_post_id, error_kind, error_message,
        )

    def get_scan_fetch_failures(self, scan_id: int) -> list[ScanFetchFailure]:
        """Return all fetch failures recorded for a scan."""
        rows = self._conn.execute(
            "SELECT platform, context, kind, message, http_status, retry_after, retryable, "
            "operation_phase, blocks_watermark_advance "
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
                operation_phase=row["operation_phase"],
                blocks_watermark_advance=bool(row["blocks_watermark_advance"]),
            )
            for row in rows
        ]

    # --- Environment lease fence ---

    def _get_environment_lease_fence(self, environment: str) -> int:
        """Return the current fence for `environment`, or 0 if none exists yet."""
        row = self._conn.execute(
            "SELECT fence FROM environment_leases WHERE environment = ?",
            (environment,),
        ).fetchone()
        return int(row["fence"]) if row is not None else 0

    def get_environment_lease_fence(self, environment: str) -> int:
        """Public read of `environment`'s current lease fence."""
        return self._get_environment_lease_fence(environment)

    def bump_environment_lease_fence(self, environment: str) -> int:
        """Advance `environment`'s lease fence and return the new value.

        A new canonical live owner calls this when it takes over, fencing
        out any scan that started under the prior fence: that scan's stored
        `lease_fence` will no longer match, so `finalize_scan_coverage`
        fails it closed instead of letting a stale owner advance the
        watermark (decision 10).
        """
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            self._conn.execute(
                "INSERT INTO environment_leases (environment, fence, updated_at) "
                "VALUES (?, 1, ?) "
                "ON CONFLICT(environment) DO UPDATE SET "
                "fence = fence + 1, updated_at = excluded.updated_at",
                (environment, now),
            )
            fence = self._get_environment_lease_fence(environment)
        logger.info("Bumped lease fence for environment=%s to %d", environment, fence)
        return fence

    # --- Watermark advance ---

    def advance_watermark(self, scan_id: int) -> Result[datetime, WatermarkAdvanceError]:
        """Advance scan_id's durable watermark from its own stored
        fetch_started_at — the single candidate timestamp (decision 3).

        Takes no timestamp of any kind: there is no caller-supplied override
        and no fallback to "now" when fetch_started_at is missing — either
        the scan's own recorded fetch start is used verbatim, or the call
        fails closed with `Err` rather than fabricate a chronology.
        """
        operation = "advance_watermark"
        with self._uow.begin_immediate():
            row = self._conn.execute(
                "SELECT fetch_started_at FROM scans WHERE id = ?", (scan_id,)
            ).fetchone()
            if row is None:
                return Err(WatermarkAdvanceError(
                    operation=operation, scan_id=scan_id, detail="scan not found",
                ))
            if not row["fetch_started_at"]:
                return Err(WatermarkAdvanceError(
                    operation=operation,
                    scan_id=scan_id,
                    detail="scan has no stored fetch_started_at",
                ))
            fetch_started_at = datetime.fromisoformat(row["fetch_started_at"])
            self._conn.execute(
                "UPDATE scans SET safe_watermark_at = ?, watermark_advanced = 1 WHERE id = ?",
                (row["fetch_started_at"], scan_id),
            )
        logger.info("Advanced watermark for scan #%d to %s", scan_id, fetch_started_at)
        return Ok(fetch_started_at)

    # --- Coverage finalization ---

    def finalize_scan_coverage(
        self,
        scan_id: int,
        *,
        environment: str,
        required_source_keys: frozenset[str] = frozenset(),
        covered_source_keys: frozenset[str] = frozenset(),
        coverage_classifier_version: int,
        now: datetime | None = None,
        expected_coverage_outcome: CoverageOutcome | None = None,
    ) -> Result[CoverageFinalizationResult, CoverageFinalizationError]:
        """Atomically derive and record whether scan_id's primary coverage
        is durably safe to advance the watermark from (decision 1, 10).

        Runs entirely inside one `BEGIN IMMEDIATE` transaction: every
        eligibility fact (terminal status, canonical live role, exact
        non-unknown environment match, current lease fence, non-future
        stored fetch_started_at, and complete required-source coverage) is
        checked against durable state, and the blocking set is computed
        exclusively from persisted `scan_fetch_failures` rows — callers
        cannot submit a coverage boolean or arbitrary blocker IDs. A
        deliberately unclassified/unknown failure
        (`operation_phase='unknown'`) is always treated as blocking,
        regardless of its own `blocks_watermark_advance` value, so a
        misclassification can never accidentally read as safe.

        Returns `Err` for a structural/eligibility violation (missing scan,
        non-terminal or failed/interrupted status, non-canonical-live role,
        non-live run kind, unknown/mismatched environment, stale lease
        fence, future fetch_started_at, incomplete required-source
        coverage, or caller/blocker disagreement) — none of these advance
        the watermark or record a coverage_outcome. Returns `Ok` for both a
        genuinely blocked outcome (persisted failures exist) and a complete
        one; only the complete case also advances the watermark.
        """
        operation = "finalize_scan_coverage"

        def _err(detail: str) -> Result[CoverageFinalizationResult, CoverageFinalizationError]:
            return Err(
                CoverageFinalizationError(operation=operation, scan_id=scan_id, detail=detail)
            )

        with self._uow.begin_immediate():
            row = self._conn.execute(
                "SELECT status, role, environment, run_kind, fetch_started_at, lease_fence "
                "FROM scans WHERE id = ?",
                (scan_id,),
            ).fetchone()
            if row is None:
                return _err("scan not found")
            if row["status"] not in ("complete", "partial"):
                return _err(f"non-eligible processing status: {row['status']!r}")
            if row["role"] != "canonical_live":
                return _err(f"non-canonical-live role: {row['role']!r}")
            if row["run_kind"] != "live":
                return _err(f"non-live run kind: {row['run_kind']!r}")
            if environment == "unknown" or row["environment"] != environment:
                return _err(
                    f"environment mismatch: scan={row['environment']!r} caller={environment!r}"
                )
            current_fence = self._get_environment_lease_fence(environment)
            stored_fence = row["lease_fence"]
            if stored_fence is None or int(stored_fence) != current_fence:
                return _err(
                    f"stale lease fence: scan={stored_fence!r} current={current_fence!r}"
                )
            if not row["fetch_started_at"]:
                return _err("scan has no stored fetch_started_at")
            fetch_started_at = datetime.fromisoformat(row["fetch_started_at"])
            reference_now = now if now is not None else datetime.now(UTC)
            if fetch_started_at > reference_now:
                return _err(
                    f"future fetch_started_at: {fetch_started_at.isoformat()} "
                    f"> now={reference_now.isoformat()}"
                )
            missing_required = required_source_keys - covered_source_keys
            if missing_required:
                return _err(
                    "incomplete required-source coverage: "
                    f"{sorted(missing_required)}"
                )

            blocking_rows = self._conn.execute(
                "SELECT id FROM scan_fetch_failures WHERE scan_id = ? "
                "AND (blocks_watermark_advance = 1 OR operation_phase = 'unknown') "
                "ORDER BY id",
                (scan_id,),
            ).fetchall()
            blocking_failure_ids = tuple(int(r["id"]) for r in blocking_rows)
            derived_outcome: CoverageOutcome = "blocked" if blocking_failure_ids else "complete"

            if (
                expected_coverage_outcome is not None
                and expected_coverage_outcome != derived_outcome
            ):
                return _err(
                    "caller/blocker disagreement: caller expected "
                    f"{expected_coverage_outcome!r}, derived {derived_outcome!r}"
                )

            created_at = datetime.now(UTC).isoformat()
            for failure_id in blocking_failure_ids:
                self._conn.execute(
                    "INSERT INTO scan_watermark_blockers (scan_id, failure_id, created_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(scan_id, failure_id) DO NOTHING",
                    (scan_id, failure_id, created_at),
                )

            watermark_advanced = False
            safe_watermark_at: datetime | None = None
            if derived_outcome == "complete":
                safe_watermark_at = fetch_started_at
                watermark_advanced = True

            self._conn.execute(
                "UPDATE scans SET coverage_outcome = ?, coverage_classifier_version = ?, "
                "watermark_advanced = ?, safe_watermark_at = COALESCE(?, safe_watermark_at) "
                "WHERE id = ?",
                (
                    derived_outcome,
                    coverage_classifier_version,
                    int(watermark_advanced),
                    safe_watermark_at.isoformat() if safe_watermark_at else None,
                    scan_id,
                ),
            )

        logger.info(
            "Finalized coverage for scan #%d: outcome=%s watermark_advanced=%s blockers=%s",
            scan_id, derived_outcome, watermark_advanced, blocking_failure_ids,
        )
        return Ok(CoverageFinalizationResult(
            scan_id=scan_id,
            coverage_outcome=derived_outcome,
            watermark_advanced=watermark_advanced,
            safe_watermark_at=safe_watermark_at,
            coverage_classifier_version=coverage_classifier_version,
            blocking_failure_ids=blocking_failure_ids,
        ))

    # --- Source checkpoints ---

    def get_source_checkpoint(self, source_key: str) -> SourceCheckpoint | None:
        row = self._conn.execute(
            "SELECT source_key, platform, source_kind, provider_key, required, active, "
            "checkpoint_at, bootstrapped_from_legacy FROM source_checkpoints "
            "WHERE source_key = ?",
            (source_key,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_checkpoint(row)

    @staticmethod
    def _row_to_checkpoint(row: sqlite3.Row) -> SourceCheckpoint:
        return SourceCheckpoint(
            source_key=row["source_key"],
            platform=row["platform"],
            source_kind=row["source_kind"],
            provider_key=row["provider_key"],
            required=bool(row["required"]),
            active=bool(row["active"]),
            checkpoint_at=(
                datetime.fromisoformat(row["checkpoint_at"])
                if row["checkpoint_at"]
                else None
            ),
            bootstrapped_from_legacy=bool(row["bootstrapped_from_legacy"]),
        )

    def ensure_source_checkpoint(
        self,
        source_key: str,
        *,
        platform: str,
        source_kind: str,
        provider_key: str,
        required: bool = True,
    ) -> SourceCheckpoint:
        """Get-or-create a source's checkpoint row.

        A newly created row starts uninitialized (checkpoint_at=None) —
        every source introduced after rollout begins cold rather than
        silently seeded at the current time (decision 7). Idempotent: an
        existing row's identity fields and checkpoint are never overwritten.
        """
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            self._conn.execute(
                "INSERT INTO source_checkpoints "
                "(source_key, platform, source_kind, provider_key, required, active, "
                "checkpoint_at, bootstrapped_from_legacy, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 1, NULL, 0, ?, ?) "
                "ON CONFLICT(source_key) DO NOTHING",
                (source_key, platform, source_kind, provider_key, int(required), now, now),
            )
            row = self._conn.execute(
                "SELECT source_key, platform, source_kind, provider_key, required, active, "
                "checkpoint_at, bootstrapped_from_legacy FROM source_checkpoints "
                "WHERE source_key = ?",
                (source_key,),
            ).fetchone()
        assert row is not None
        return self._row_to_checkpoint(row)

    def bootstrap_legacy_source_checkpoint(
        self,
        source_key: str,
        *,
        platform: str,
        source_kind: str,
        provider_key: str,
        legacy_checkpoint_at: datetime,
        required: bool = True,
    ) -> Result[SourceCheckpoint, SourceCheckpointError]:
        """One-time, auditable rollout bootstrap from a legacy cursor
        (decision 7).

        Callers are expected to have already resolved `legacy_checkpoint_at`
        from the exact-environment eligible legacy cursor (e.g.
        `get_last_scan_timestamp(environment=...)`). Fails with `Err` if the
        source already has a row — bootstrap is one-time and auditable via
        `bootstrapped_from_legacy=1`; a source that already exists (cold or
        previously bootstrapped) must go through `ensure_source_checkpoint`/
        `reactivate_source_checkpoint` instead, never a silent re-seed.
        """
        operation = "bootstrap_legacy_source_checkpoint"
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            existing = self._conn.execute(
                "SELECT 1 FROM source_checkpoints WHERE source_key = ?", (source_key,)
            ).fetchone()
            if existing is not None:
                return Err(SourceCheckpointError(
                    operation=operation,
                    source_key=source_key,
                    detail="source checkpoint already exists; bootstrap is one-time",
                ))
            self._conn.execute(
                "INSERT INTO source_checkpoints "
                "(source_key, platform, source_kind, provider_key, required, active, "
                "checkpoint_at, bootstrapped_from_legacy, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?, 1, ?, ?)",
                (
                    source_key, platform, source_kind, provider_key, int(required),
                    legacy_checkpoint_at.isoformat(), now, now,
                ),
            )
        logger.info("Bootstrapped legacy source checkpoint for %s", source_key)
        row = self.get_source_checkpoint(source_key)
        assert row is not None
        return Ok(row)

    def retire_source_checkpoint(self, source_key: str) -> bool:
        """Deactivate a source, retaining its checkpoint for a later
        reactivation to resume from (decision 7)."""
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            cursor = self._conn.execute(
                "UPDATE source_checkpoints SET active = 0, updated_at = ? "
                "WHERE source_key = ? AND active = 1",
                (now, source_key),
            )
        return cursor.rowcount > 0

    def reactivate_source_checkpoint(
        self, source_key: str, *, reset: bool = False
    ) -> Result[SourceCheckpoint, SourceCheckpointError]:
        """Reactivate a retired source.

        Resumes from the retained checkpoint by default; `reset=True` is an
        explicit, audited request to drop the retained checkpoint and start
        cold again (decision 7) — never the implicit default.
        """
        operation = "reactivate_source_checkpoint"
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            existing = self._conn.execute(
                "SELECT 1 FROM source_checkpoints WHERE source_key = ?", (source_key,)
            ).fetchone()
            if existing is None:
                return Err(SourceCheckpointError(
                    operation=operation,
                    source_key=source_key,
                    detail="source checkpoint not found",
                ))
            if reset:
                self._conn.execute(
                    "UPDATE source_checkpoints SET active = 1, checkpoint_at = NULL, "
                    "updated_at = ? WHERE source_key = ?",
                    (now, source_key),
                )
            else:
                self._conn.execute(
                    "UPDATE source_checkpoints SET active = 1, updated_at = ? "
                    "WHERE source_key = ?",
                    (now, source_key),
                )
        row = self.get_source_checkpoint(source_key)
        assert row is not None
        return Ok(row)

    def update_source_checkpoint(self, source_key: str, *, checkpoint_at: datetime) -> None:
        """Advance a source's checkpoint after a scan actually covers it."""
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            self._conn.execute(
                "UPDATE source_checkpoints SET checkpoint_at = ?, updated_at = ? "
                "WHERE source_key = ?",
                (checkpoint_at.isoformat(), now, source_key),
            )

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

    def record_author_classification(
        self,
        *,
        platform: str,
        author_id: str,
        author_class: str,
        rule_version: int,
        matched_text: str | None,
    ) -> bool:
        """Upsert an author's class; return whether a row was inserted or changed.

        A rerun of the same rule with the same outcome is a no-op, so
        classified_at records when the current class was first decided.
        """
        identity = self._author_identity(platform, author_id)
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            cursor = self._conn.execute(
                "INSERT INTO author_classifications "
                "(platform, author_id, author_class, rule_version, matched_text, classified_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(platform, author_id) DO UPDATE SET "
                "author_class = excluded.author_class, rule_version = excluded.rule_version, "
                "matched_text = excluded.matched_text, classified_at = excluded.classified_at "
                "WHERE author_class IS NOT excluded.author_class "
                "OR rule_version IS NOT excluded.rule_version",
                (*identity, author_class, rule_version, matched_text, now),
            )
        return cursor.rowcount > 0

    def get_author_classification(
        self, *, platform: str, author_id: str
    ) -> StoredAuthorClassification | None:
        identity = self._author_identity(platform, author_id)
        row = self._conn.execute(
            "SELECT platform, author_id, author_class, rule_version, matched_text, classified_at "
            "FROM author_classifications WHERE platform = ? AND author_id = ?",
            identity,
        ).fetchone()
        if row is None:
            return None
        return StoredAuthorClassification(
            platform=row["platform"],
            author_id=row["author_id"],
            author_class=row["author_class"],
            rule_version=int(row["rule_version"]),
            matched_text=row["matched_text"],
            classified_at=row["classified_at"],
        )

    def count_scans(self) -> int:
        """Total number of scans ever recorded — one leg of ScanStats."""
        return int(self._conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0])
