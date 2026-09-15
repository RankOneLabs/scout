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
from datetime import UTC, datetime, timedelta
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

# Mirrors scout.errors.OperationPhase. finalize_scan_coverage treats any
# stored value outside this set — not just the literal 'unknown' — as
# blocking: an invalid or unrecognized phase carries no evidence that it is
# safe to ignore, so it must fail closed rather than silently pass through
# as non-blocking.
KNOWN_OPERATION_PHASES = frozenset({"fetch", "parent_lookup", "scan", "digest", "unknown"})


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
    # Source checkpoints moved forward in the same transaction as the
    # watermark advance — empty when nothing advanced.
    advanced_source_keys: tuple[str, ...] = ()


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
class EnvironmentLease:
    """The current holder of one environment's fenced lease, as returned by
    acquire/renew/takeover. `fence` is the same monotonic token
    `finalize_scan_coverage` already fences a canonical owner's scans
    against — acquiring or taking over bumps it; renewing does not."""

    environment: str
    owner_id: str
    fence: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class LeaseError:
    """A lease operation was refused — contention, a stale caller, or an
    unknown environment/owner/fence combination."""

    operation: str
    environment: str
    detail: str


@dataclass(frozen=True, slots=True)
class ProbeRunResult:
    """One `source_probe_runs` row."""

    probe_run_id: int
    environment: str
    started_at: datetime
    completed_at: datetime | None
    passed: bool | None
    source_count: int
    page_count: int
    window_hours: float
    limits_json: str
    detail_json: str


CutoverRefusalReason = Literal[
    "lock_not_held",
    "missing_probe",
    "missing_metadata",
    "invalid_accepted_new",
    "stale_expected_old",
    "postcondition_mismatch",
]


@dataclass(frozen=True, slots=True)
class CutoverRefusal:
    """A cutover was refused inside its own transaction. The refusal is
    already durably recorded as an append-only `recovery_operations` row
    (`audit_id`) with `outcome='refused'` by the time this is returned."""

    reason: CutoverRefusalReason
    detail: str
    audit_id: int


@dataclass(frozen=True, slots=True)
class CutoverResult:
    """A cutover that committed: the synthetic canonical scan row now read
    as the cursor, the probe that gated it, and its accepted audit row."""

    scan_id: int
    probe_run_id: int
    audit_id: int
    accepted_new_watermark: datetime


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

    def get_last_scan_timestamp(self, *, environment: str) -> datetime | None:
        """Return the safe watermark from the most recent eligible scan, or None.

        Returns safe_watermark_at rather than completed_at so the next scan's
        since boundary is anchored to when fetching started on the prior scan,
        not when processing finished — avoiding a lossy gap for messages that
        arrive during the processing window.

        `environment` is required and must be an exact, non-'unknown' value:
        development and unknown-environment rows must never influence a
        production cursor read, and a caller with no real environment to
        supply has no safe reading to perform (decision 5). Only rows with
        `role='canonical_live'` are eligible — a secondary or rescore scan's
        watermark can never leak into the cursor another owner reads.
        """
        if not environment or environment == "unknown":
            raise ValueError(
                "get_last_scan_timestamp requires a specific, non-'unknown' environment"
            )
        row = self._conn.execute(
            "SELECT safe_watermark_at FROM scans "
            "WHERE safe_watermark_at IS NOT NULL AND environment = ? "
            "AND role = 'canonical_live' "
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
    ) -> None:
        """Mark a scan as complete, partial, failed, or interrupted.

        This method only ever touches processing status and counters — it
        never writes `safe_watermark_at` or `watermark_advanced`. The only
        legitimate path that can move the watermark is
        `finalize_scan_coverage(scan_id, *, advance_watermark=...)`, which
        derives the decision from durable evidence inside one atomic
        transaction (decisions 1, 3, 10). A caller that wants to advance the
        watermark for this scan must call `finalize_scan_coverage`
        separately with an explicit `advance_watermark` intent.
        """
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            self._conn.execute(
                "UPDATE scans SET completed_at = ?, messages_scanned = ?, relevant_found = ?, "
                "status = ?, overflow_count = ? WHERE id = ?",
                (now, messages_scanned, relevant_found, status, overflow_count, scan_id),
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
        *,
        operation_phase: str,
        blocks_watermark_advance: bool,
    ) -> int:
        """Persist a per-platform fetch failure for operator visibility and retry.

        `operation_phase`/`blocks_watermark_advance` mirror
        `scout.errors.PlatformFetchFailure` and are required with no
        default: every caller must state the actual classification it
        received from the platform/processing layer that raised the
        failure, rather than this method silently substituting the
        fail-closed "unclassified" pairing on their behalf. A caller with
        no real classification to pass should construct the failure with
        `operation_phase="unknown", blocks_watermark_advance=True`
        explicitly — `finalize_scan_coverage` always treats a stored
        `operation_phase` outside `KNOWN_OPERATION_PHASES` as blocking
        regardless of the stored `blocks_watermark_advance` value.

        Raises `ValueError` for a value outside `KNOWN_OPERATION_PHASES`
        (validated at the write boundary, not just read back later), and
        for a scan whose coverage was already finalized — a failure
        recorded after `finalize_scan_coverage` ran could invalidate a
        durable `coverage_outcome`/`watermark_advanced` decision that
        nothing would otherwise recompute, so the write is rejected rather
        than silently coexisting with a now-stale outcome. Call
        `finalize_scan_coverage` again after resolving/recording the
        failure to re-derive coverage from the full evidence.
        """
        if operation_phase not in KNOWN_OPERATION_PHASES:
            raise ValueError(
                f"operation_phase {operation_phase!r} is not in KNOWN_OPERATION_PHASES"
            )
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            scan_row = self._conn.execute(
                "SELECT coverage_outcome FROM scans WHERE id = ?", (scan_id,)
            ).fetchone()
            if scan_row is not None and scan_row["coverage_outcome"] is not None:
                raise ValueError(
                    f"cannot record a fetch failure for scan {scan_id}: its coverage was "
                    "already finalized by finalize_scan_coverage; call finalize_scan_coverage "
                    "again after recording this failure to re-derive coverage_outcome"
                )
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

    # --- Environment lease (owned lifecycle) ---

    def acquire_environment_lease(
        self, environment: str, owner_id: str, *, ttl_seconds: float
    ) -> Result[EnvironmentLease, LeaseError]:
        """Acquire environment's fenced lease for owner_id, or take over an
        expired one.

        Fails closed with `Err` when the lease is currently held by anyone
        (including `owner_id` itself) and not yet expired — an already-held
        lease must be extended via `renew_environment_lease`, never
        silently re-acquired. When no one holds it, or the holder's
        `expires_at` has passed, this bumps `environment`'s fence (the same
        token `finalize_scan_coverage` fences a canonical owner's scans
        against) and installs `owner_id` as the new holder — this is the
        takeover path: an expired holder's fence is left behind, so any
        scan it started under the old fence can never finalize (decision
        10). Acquiring after a clean `release_environment_lease` also
        bumps the fence, which is conservative but always safe: monotonic
        is the only requirement.
        """
        operation = "acquire_environment_lease"
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=ttl_seconds)
        with self._uow.begin_immediate():
            row = self._conn.execute(
                "SELECT owner_id, expires_at FROM environment_leases WHERE environment = ?",
                (environment,),
            ).fetchone()
            held_unexpired = (
                row is not None
                and row["owner_id"] is not None
                and row["expires_at"] is not None
                and datetime.fromisoformat(row["expires_at"]) > now
            )
            if held_unexpired:
                return Err(LeaseError(
                    operation=operation, environment=environment,
                    detail=f"lease already held by {row['owner_id']!r} until {row['expires_at']}",
                ))
            self._conn.execute(
                "INSERT INTO environment_leases "
                "(environment, fence, updated_at, owner_id, expires_at) "
                "VALUES (?, 1, ?, ?, ?) "
                "ON CONFLICT(environment) DO UPDATE SET "
                "fence = fence + 1, updated_at = excluded.updated_at, "
                "owner_id = excluded.owner_id, expires_at = excluded.expires_at",
                (environment, now.isoformat(), owner_id, expires_at.isoformat()),
            )
            fence = self._get_environment_lease_fence(environment)
        logger.info(
            "Acquired lease for environment=%s owner=%s fence=%d expires_at=%s",
            environment, owner_id, fence, expires_at.isoformat(),
        )
        return Ok(EnvironmentLease(
            environment=environment, owner_id=owner_id, fence=fence, expires_at=expires_at,
        ))

    def renew_environment_lease(
        self, environment: str, owner_id: str, fence: int, *, ttl_seconds: float
    ) -> Result[EnvironmentLease, LeaseError]:
        """Heartbeat an already-held lease. A compare-and-set on
        (environment, owner_id, fence, unexpired) — a stale generation or an
        owner whose lease already expired cannot renew, refill its own
        expiry, or otherwise resurrect its hold."""
        operation = "renew_environment_lease"
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=ttl_seconds)
        with self._uow.begin_immediate():
            cursor = self._conn.execute(
                "UPDATE environment_leases SET expires_at = ?, updated_at = ? "
                "WHERE environment = ? AND owner_id = ? AND fence = ? AND expires_at > ?",
                (
                    expires_at.isoformat(), now.isoformat(), environment, owner_id,
                    fence, now.isoformat(),
                ),
            )
            if cursor.rowcount == 0:
                return Err(LeaseError(
                    operation=operation, environment=environment,
                    detail=f"no unexpired lease for owner={owner_id!r} fence={fence} to renew",
                ))
        logger.debug(
            "Renewed lease for environment=%s owner=%s fence=%d expires_at=%s",
            environment, owner_id, fence, expires_at.isoformat(),
        )
        return Ok(EnvironmentLease(
            environment=environment, owner_id=owner_id, fence=fence, expires_at=expires_at,
        ))

    def release_environment_lease(self, environment: str, owner_id: str, fence: int) -> bool:
        """Clear an owned, matching-fence lease's holder. Never bumps the
        fence — a released lease's fence is left as-is; the next acquire
        bumps it regardless of whether it finds a released or expired
        holder, which is always monotonic and always safe. Returns whether
        a row actually matched and was cleared."""
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            cursor = self._conn.execute(
                "UPDATE environment_leases SET owner_id = NULL, expires_at = NULL, updated_at = ? "
                "WHERE environment = ? AND owner_id = ? AND fence = ?",
                (now, environment, owner_id, fence),
            )
        released = cursor.rowcount > 0
        if released:
            logger.info(
                "Released lease for environment=%s owner=%s fence=%d",
                environment, owner_id, fence,
            )
        return released

    def _lease_held_by(
        self, environment: str, owner_id: str, fence: int, now: datetime
    ) -> str | None:
        """Return None when `owner_id` holds `environment`'s lease at exactly
        `fence`, unexpired as of `now`; otherwise a human-readable reason.
        Callers run this inside their own transaction so the check and the
        mutation it guards are one atomic unit."""
        row = self._conn.execute(
            "SELECT owner_id, fence, expires_at FROM environment_leases WHERE environment = ?",
            (environment,),
        ).fetchone()
        if row is None:
            return f"no lease exists for environment={environment!r}"
        if row["owner_id"] != owner_id:
            return f"lease held by {row['owner_id']!r}, not {owner_id!r}"
        if int(row["fence"]) != fence:
            return f"stale generation: caller fence={fence} current={int(row['fence'])}"
        if row["expires_at"] is None or datetime.fromisoformat(row["expires_at"]) <= now:
            return f"lease expired at {row['expires_at']}"
        return None

    def start_canonical_owner_scan(
        self,
        *,
        environment: str,
        owner_id: str,
        fence: int,
        fetch_started_at: datetime,
    ) -> Result[int, LeaseError]:
        """Create the canonical live fetch-owner scan, bound to the caller's
        lease in the same transaction: the row is inserted only if
        `owner_id` still holds `environment`'s lease at exactly `fence`,
        unexpired. A worker that lost its lease between acquiring it and
        reaching this point gets `Err` and no row — it never becomes an
        owner of anything."""
        operation = "start_canonical_owner_scan"
        now = datetime.now(UTC)
        with self._uow.begin_immediate():
            reason = self._lease_held_by(environment, owner_id, fence, now)
            if reason is not None:
                return Err(LeaseError(operation=operation, environment=environment, detail=reason))
            cursor = self._conn.execute(
                "INSERT INTO scans "
                "(started_at, fetch_started_at, environment, run_kind, role, lease_fence) "
                "VALUES (?, ?, ?, 'live', 'canonical_live', ?)",
                (now.isoformat(), fetch_started_at.isoformat(), environment, fence),
            )
            scan_id = cursor.lastrowid
        assert scan_id is not None
        logger.info(
            "Started canonical owner scan #%d for environment=%s owner=%s fence=%d",
            scan_id, environment, owner_id, fence,
        )
        return Ok(int(scan_id))

    def reconcile_abandoned_canonical_owners(
        self, environment: str, current_fence: int
    ) -> list[int]:
        """Interrupt every non-terminal canonical-live scan in `environment`
        started under a fence older than `current_fence` — abandoned by a
        worker that died or was fenced out mid-flight before it could
        finalize its own coverage.

        Scoped to this exact environment and strictly older fences only:
        the current lease holder's own in-flight scan (`lease_fence ==
        current_fence`) and every other environment are left untouched.
        Each interrupted scan gets a durable `status='interrupted'` and a
        blocking `scan_fetch_failures` row, so a later
        `finalize_scan_coverage` call against it (if ever attempted) is
        already fenced out by its stale `lease_fence`, and it carries
        auditable evidence either way. Returns the reconciled scan ids.
        """
        now = datetime.now(UTC).isoformat()
        with self._uow.begin_immediate():
            rows = self._conn.execute(
                "SELECT id FROM scans WHERE environment = ? AND role = 'canonical_live' "
                "AND completed_at IS NULL AND lease_fence IS NOT NULL AND lease_fence < ? "
                "ORDER BY id",
                (environment, current_fence),
            ).fetchall()
            reconciled_ids = [int(r["id"]) for r in rows]
            for scan_id in reconciled_ids:
                self._conn.execute(
                    "UPDATE scans SET completed_at = ?, status = 'interrupted' WHERE id = ?",
                    (now, scan_id),
                )
                self._conn.execute(
                    "INSERT INTO scan_fetch_failures "
                    "(scan_id, platform, context, kind, message, retryable, "
                    "operation_phase, blocks_watermark_advance, created_at) "
                    "VALUES (?, 'scan_runner', 'reconciliation', 'abandoned_owner', "
                    "'canonical owner abandoned under a stale lease fence; reconciled at startup', "
                    "0, 'scan', 1, ?)",
                    (scan_id, now),
                )
        if reconciled_ids:
            logger.warning(
                "Reconciled %d abandoned canonical owner(s) in environment=%s: %s",
                len(reconciled_ids), environment, reconciled_ids,
            )
        return reconciled_ids

    # --- Coverage finalization ---

    def finalize_scan_coverage(
        self,
        scan_id: int,
        *,
        environment: str,
        advance_watermark: bool,
        owner_id: str | None = None,
        required_source_keys: frozenset[str] = frozenset(),
        covered_source_keys: frozenset[str] = frozenset(),
        coverage_classifier_version: int,
        expected_coverage_outcome: CoverageOutcome | None = None,
    ) -> Result[CoverageFinalizationResult, CoverageFinalizationError]:
        """Atomically derive and record whether scan_id's primary coverage
        is durably safe to advance the watermark from (decision 1, 10).

        `advance_watermark` is a required, keyword-only expression of
        caller intent: this is the only path in the codebase that can ever
        set `safe_watermark_at`/`watermark_advanced`, and it only does so
        when the caller explicitly asks for it AND the derived coverage
        outcome is `'complete'`. Passing `advance_watermark=False` still
        computes and durably records `coverage_outcome` and the blocking
        set, without touching the watermark — useful for a caller that only
        wants an eligibility/coverage read (e.g. a rescore or secondary
        pass must never advance a live cursor).

        Runs entirely inside one `BEGIN IMMEDIATE` transaction: every
        eligibility fact (terminal status, canonical live role, exact
        non-unknown environment match, current lease fence, non-future
        stored fetch_started_at, and complete required-source coverage) is
        checked against durable state, and the blocking set is computed
        exclusively from persisted `scan_fetch_failures` rows — callers
        cannot submit a coverage boolean or arbitrary blocker IDs. A
        deliberately unclassified/unknown failure, or any value outside
        `KNOWN_OPERATION_PHASES`, is always treated as blocking regardless
        of its own `blocks_watermark_advance` value, so a misclassification
        can never accidentally read as safe. Calling this again after a
        prior finalization only ever raises `watermark_advanced` — it can
        never clear a durable advancement recorded by an earlier call.

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
                "SELECT status, role, environment, run_kind, fetch_started_at, lease_fence, "
                "watermark_advanced FROM scans WHERE id = ?",
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
            reference_now = datetime.now(UTC)
            if advance_watermark:
                # Advancement is bound to the holding owner in this same
                # transaction — not merely to a matching fence number. A
                # caller whose lease expired or was taken over cannot
                # advance even if the fence still happens to match.
                if owner_id is None:
                    return _err("advancing the watermark requires the holding owner_id")
                lease_reason = self._lease_held_by(
                    environment, owner_id, current_fence, reference_now
                )
                if lease_reason is not None:
                    return _err(f"lease not held for advancement: {lease_reason}")
            if not row["fetch_started_at"]:
                return _err("scan has no stored fetch_started_at")
            fetch_started_at = datetime.fromisoformat(row["fetch_started_at"])
            if fetch_started_at > reference_now:
                return _err(
                    f"future fetch_started_at: {fetch_started_at.isoformat()} "
                    f"> now={reference_now.isoformat()}"
                )

            valid_non_blocking_phases = KNOWN_OPERATION_PHASES - {"unknown"}
            placeholders = ", ".join("?" for _ in valid_non_blocking_phases)
            blocking_rows = self._conn.execute(
                "SELECT id FROM scan_fetch_failures WHERE scan_id = ? "
                f"AND (blocks_watermark_advance = 1 OR operation_phase NOT IN ({placeholders})) "
                "ORDER BY id",
                (scan_id, *valid_non_blocking_phases),
            ).fetchall()
            blocking_failure_ids = tuple(int(r["id"]) for r in blocking_rows)

            # A required source the fetch did not fully cover is only a
            # coherent claim when persisted failure evidence explains it —
            # then the outcome is simply 'blocked'. Missing coverage with no
            # evidence at all is a caller/evidence disagreement, refused.
            missing_required = required_source_keys - covered_source_keys
            if missing_required and not blocking_failure_ids:
                return _err(
                    "incomplete required-source coverage without failure evidence: "
                    f"{sorted(missing_required)}"
                )
            derived_outcome: CoverageOutcome = (
                "blocked" if blocking_failure_ids or missing_required else "complete"
            )

            if (
                expected_coverage_outcome is not None
                and expected_coverage_outcome != derived_outcome
            ):
                return _err(
                    "caller/blocker disagreement: caller expected "
                    f"{expected_coverage_outcome!r}, derived {derived_outcome!r}"
                )

            created_at = reference_now.isoformat()
            for failure_id in blocking_failure_ids:
                self._conn.execute(
                    "INSERT INTO scan_watermark_blockers (scan_id, failure_id, created_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(scan_id, failure_id) DO NOTHING",
                    (scan_id, failure_id, created_at),
                )

            already_advanced = bool(row["watermark_advanced"])
            newly_advanced = derived_outcome == "complete" and advance_watermark
            safe_watermark_at: datetime | None = fetch_started_at if newly_advanced else None
            watermark_advanced = already_advanced or newly_advanced

            self._conn.execute(
                "UPDATE scans SET coverage_outcome = ?, coverage_classifier_version = ?, "
                "watermark_advanced = MAX(watermark_advanced, ?), "
                "safe_watermark_at = COALESCE(?, safe_watermark_at) "
                "WHERE id = ?",
                (
                    derived_outcome,
                    coverage_classifier_version,
                    int(watermark_advanced),
                    safe_watermark_at.isoformat() if safe_watermark_at else None,
                    scan_id,
                ),
            )

            # Every covered source's checkpoint moves to this scan's fetch
            # start atomically with the watermark advance — never on a
            # blocked outcome, never backwards, never for a retired source.
            advanced_source_keys: list[str] = []
            if newly_advanced:
                for source_key in sorted(covered_source_keys):
                    moved = self._conn.execute(
                        "UPDATE source_checkpoints SET checkpoint_at = ?, updated_at = ? "
                        "WHERE source_key = ? AND active = 1 "
                        "AND (checkpoint_at IS NULL OR checkpoint_at < ?)",
                        (
                            fetch_started_at.isoformat(), created_at, source_key,
                            fetch_started_at.isoformat(),
                        ),
                    )
                    if moved.rowcount > 0:
                        advanced_source_keys.append(source_key)

        logger.info(
            "Finalized coverage for scan #%d: outcome=%s watermark_advanced=%s blockers=%s "
            "advanced_sources=%s",
            scan_id, derived_outcome, watermark_advanced, blocking_failure_ids,
            advanced_source_keys,
        )
        return Ok(CoverageFinalizationResult(
            scan_id=scan_id,
            coverage_outcome=derived_outcome,
            watermark_advanced=watermark_advanced,
            safe_watermark_at=safe_watermark_at,
            coverage_classifier_version=coverage_classifier_version,
            blocking_failure_ids=blocking_failure_ids,
            advanced_source_keys=tuple(advanced_source_keys),
        ))

    def mark_coverage_finalization_failed(self, scan_id: int, *, detail: str) -> None:
        """Force scan_id to `status='failed'` after `finalize_scan_coverage`
        rejected it post-completion, and record the rejection as an
        auditable, blocking `scan_fetch_failures` row.

        Unlike `fail_scan`, this updates a scan that already has
        `completed_at` set — a `finalize_scan_coverage` `Err` means no
        `coverage_outcome` was ever durably recorded for this scan, so it
        must not be left looking like an ordinary complete/partial run with
        silently unmoved coverage.
        """
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            self._conn.execute("UPDATE scans SET status = 'failed' WHERE id = ?", (scan_id,))
            self._conn.execute(
                "INSERT INTO scan_fetch_failures "
                "(scan_id, platform, context, kind, message, retryable, "
                "operation_phase, blocks_watermark_advance, created_at) "
                "VALUES (?, 'scan_runner', 'finalize_scan_coverage', "
                "'coverage_finalization_rejected', ?, 0, 'scan', 1, ?)",
                (scan_id, detail, now),
            )
        logger.error("Scan #%d coverage finalization rejected: %s", scan_id, detail)

    def link_secondary_scan(self, scan_id: int, *, canonical_scan_id: int) -> None:
        """Record that `scan_id` (a secondary or rescore pass) is a linked,
        non-advancing pass of `canonical_scan_id` — e.g. --mode both's
        second scoring pass over the same fetch."""
        with self._uow.begin():
            self._conn.execute(
                "UPDATE scans SET canonical_scan_id = ? WHERE id = ?",
                (canonical_scan_id, scan_id),
            )

    # --- Six-hour probe evidence ---

    def start_probe_run(
        self,
        environment: str,
        *,
        source_count: int,
        window_hours: float,
        limits_json: str,
    ) -> int:
        """Record the start of a read-only probe run with the exact window
        and page/result limits it is about to use, so a later cutover can
        verify it was a genuine six-hour, normal-limit probe. Never touches
        a source_checkpoints cursor — this is diagnostic evidence only."""
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            cursor = self._conn.execute(
                "INSERT INTO source_probe_runs "
                "(environment, started_at, source_count, page_count, window_hours, "
                "limits_json, detail_json, created_at) "
                "VALUES (?, ?, ?, 0, ?, ?, '{}', ?)",
                (environment, now, source_count, window_hours, limits_json, now),
            )
            probe_run_id = cursor.lastrowid
        assert probe_run_id is not None
        return probe_run_id

    def complete_probe_run(
        self,
        probe_run_id: int,
        *,
        passed: bool,
        page_count: int,
        detail_json: str,
    ) -> None:
        """Persist a probe run's terminal pass/fail diagnostic."""
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            self._conn.execute(
                "UPDATE source_probe_runs SET completed_at = ?, passed = ?, "
                "page_count = ?, detail_json = ? WHERE id = ?",
                (now, int(passed), page_count, detail_json, probe_run_id),
            )
        logger.info(
            "Probe run #%d complete: passed=%s page_count=%d", probe_run_id, passed, page_count,
        )

    def get_probe_run(self, probe_run_id: int) -> ProbeRunResult | None:
        row = self._conn.execute(
            "SELECT id, environment, started_at, completed_at, passed, source_count, "
            "page_count, window_hours, limits_json, detail_json "
            "FROM source_probe_runs WHERE id = ?",
            (probe_run_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_probe_run(row)

    def get_latest_passed_probe(
        self, environment: str, *, max_age_seconds: float
    ) -> ProbeRunResult | None:
        """Return the most recent passed probe for `environment` that
        completed within `max_age_seconds`, or None. Used to gate cutover
        on a recent successful probe (decision 8)."""
        cutoff = (datetime.now(UTC) - timedelta(seconds=max_age_seconds)).isoformat()
        row = self._conn.execute(
            "SELECT id, environment, started_at, completed_at, passed, source_count, "
            "page_count, window_hours, limits_json, detail_json FROM source_probe_runs "
            "WHERE environment = ? AND passed = 1 AND completed_at IS NOT NULL "
            "AND completed_at >= ? ORDER BY id DESC LIMIT 1",
            (environment, cutoff),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_probe_run(row)

    @staticmethod
    def _row_to_probe_run(row: sqlite3.Row) -> ProbeRunResult:
        return ProbeRunResult(
            probe_run_id=int(row["id"]),
            environment=row["environment"],
            started_at=datetime.fromisoformat(row["started_at"]),
            completed_at=(
                datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None
            ),
            passed=bool(row["passed"]) if row["passed"] is not None else None,
            source_count=int(row["source_count"]),
            page_count=int(row["page_count"]),
            window_hours=float(row["window_hours"]),
            limits_json=row["limits_json"],
            detail_json=row["detail_json"],
        )

    # --- Recovery audit (bounded backfill / controlled cutover) ---

    def record_recovery_operation(
        self,
        *,
        environment: str,
        operation: Literal["backfill", "cutover", "stale_check"],
        operator: str,
        rationale: str,
        policy: str | None,
        source_evidence: str | None,
        probe_run_id: int | None,
        expected_old_watermark: datetime | None,
        accepted_new_watermark: datetime | None,
        outcome: Literal["accepted", "refused"],
        detail: str | None,
    ) -> int:
        """Append one immutable audit row. Called for both accepted and
        refused attempts (decision 9) — a refusal is still evidence of what
        was requested and why it did not proceed."""
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            return self._insert_recovery_operation(
                environment=environment, operation=operation, operator=operator,
                rationale=rationale, policy=policy, source_evidence=source_evidence,
                probe_run_id=probe_run_id, expected_old_watermark=expected_old_watermark,
                accepted_new_watermark=accepted_new_watermark, outcome=outcome,
                detail=detail, created_at=now,
            )

    def _insert_recovery_operation(
        self,
        *,
        environment: str,
        operation: Literal["backfill", "cutover", "stale_check"],
        operator: str,
        rationale: str,
        policy: str | None,
        source_evidence: str | None,
        probe_run_id: int | None,
        expected_old_watermark: datetime | None,
        accepted_new_watermark: datetime | None,
        outcome: Literal["accepted", "refused"],
        detail: str | None,
        created_at: str,
    ) -> int:
        """Raw append of one audit row; the caller owns the transaction."""
        cursor = self._conn.execute(
            "INSERT INTO recovery_operations "
            "(environment, operation, operator, rationale, policy, source_evidence, "
            "probe_run_id, expected_old_watermark, accepted_new_watermark, outcome, "
            "detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                environment, operation, operator, rationale, policy, source_evidence,
                probe_run_id,
                expected_old_watermark.isoformat() if expected_old_watermark else None,
                accepted_new_watermark.isoformat() if accepted_new_watermark else None,
                outcome, detail, created_at,
            ),
        )
        audit_id = cursor.lastrowid
        assert audit_id is not None
        return int(audit_id)

    def cutover_watermark(
        self,
        *,
        environment: str,
        owner_id: str,
        fence: int,
        operator: str,
        rationale: str,
        policy: str,
        source_evidence: str,
        expected_old_watermark: datetime | None,
        accepted_new_watermark: datetime,
        probe_max_age_seconds: float,
        probe_min_window_hours: float = 6.0,
    ) -> Result[CutoverResult, CutoverRefusal]:
        """Atomically accept a gap: install `accepted_new_watermark` as
        `environment`'s cursor via a synthetic, already-finalized
        canonical-live scan row.

        One `BEGIN IMMEDIATE` covers every gate and every write, in order:
        the caller's lease must be the current, unexpired holder at
        exactly `fence` (the recovery lock); every metadata field must be
        non-blank; `accepted_new_watermark` must be timezone-aware, not in
        the future, and strictly after `expected_old_watermark` when one is
        given; a probe for this environment must have passed within
        `probe_max_age_seconds` with a window of at least
        `probe_min_window_hours`; and `expected_old_watermark` must still
        equal the live cursor at commit time (compare-and-set, so a race
        with another writer refuses instead of silently overwriting). Only
        then is the scan row inserted, the cursor re-read as a
        postcondition, and the accepted audit row appended. A refusal at
        any gate appends a `refused` audit row in the same transaction and
        returns `Err` — nothing else is written.
        """
        now = datetime.now(UTC)
        created_at = now.isoformat()

        with self._uow.begin_immediate():
            probe_run_id: int | None = None

            def _refuse(
                reason: CutoverRefusalReason, detail: str
            ) -> Result[CutoverResult, CutoverRefusal]:
                audit_id = self._insert_recovery_operation(
                    environment=environment, operation="cutover",
                    operator=operator.strip() or "<missing>",
                    rationale=rationale.strip() or "<missing>",
                    policy=policy, source_evidence=source_evidence,
                    probe_run_id=probe_run_id,
                    expected_old_watermark=expected_old_watermark,
                    accepted_new_watermark=(
                        accepted_new_watermark if accepted_new_watermark.tzinfo else None
                    ),
                    outcome="refused", detail=f"{reason}: {detail}", created_at=created_at,
                )
                logger.error(
                    "Cutover refused for environment=%s (%s): %s", environment, reason, detail
                )
                return Err(CutoverRefusal(reason=reason, detail=detail, audit_id=audit_id))

            lease_reason = self._lease_held_by(environment, owner_id, fence, now)
            if lease_reason is not None:
                return _refuse("lock_not_held", lease_reason)

            blank = [
                name for name, value in (
                    ("operator", operator), ("rationale", rationale),
                    ("policy", policy), ("source_evidence", source_evidence),
                ) if not value.strip()
            ]
            if blank:
                return _refuse("missing_metadata", f"blank metadata: {blank}")

            if accepted_new_watermark.tzinfo is None:
                return _refuse("invalid_accepted_new", "accepted-new must be timezone-aware")
            if accepted_new_watermark > now:
                return _refuse(
                    "invalid_accepted_new",
                    f"accepted-new {accepted_new_watermark.isoformat()} is in the future",
                )
            if expected_old_watermark is not None:
                if expected_old_watermark.tzinfo is None:
                    return _refuse("missing_metadata", "expected-old must be timezone-aware")
                if accepted_new_watermark <= expected_old_watermark:
                    return _refuse(
                        "invalid_accepted_new",
                        "accepted-new must move the cursor forward past expected-old",
                    )

            probe_cutoff = (now - timedelta(seconds=probe_max_age_seconds)).isoformat()
            probe_row = self._conn.execute(
                "SELECT id, window_hours FROM source_probe_runs "
                "WHERE environment = ? AND passed = 1 AND completed_at IS NOT NULL "
                "AND completed_at >= ? AND window_hours >= ? ORDER BY id DESC LIMIT 1",
                (environment, probe_cutoff, probe_min_window_hours),
            ).fetchone()
            if probe_row is None:
                return _refuse(
                    "missing_probe",
                    f"no passed probe with window >= {probe_min_window_hours}h completed "
                    f"within {probe_max_age_seconds:.0f}s",
                )
            probe_run_id = int(probe_row["id"])

            current_row = self._conn.execute(
                "SELECT safe_watermark_at FROM scans "
                "WHERE safe_watermark_at IS NOT NULL AND environment = ? "
                "AND role = 'canonical_live' ORDER BY id DESC LIMIT 1",
                (environment,),
            ).fetchone()
            current_watermark = (
                datetime.fromisoformat(current_row["safe_watermark_at"])
                if current_row and current_row["safe_watermark_at"]
                else None
            )
            if current_watermark != expected_old_watermark:
                return _refuse(
                    "stale_expected_old",
                    f"expected={expected_old_watermark} actual={current_watermark}",
                )

            cursor = self._conn.execute(
                "INSERT INTO scans "
                "(started_at, completed_at, fetch_started_at, safe_watermark_at, status, "
                "environment, run_kind, role, lease_fence, coverage_outcome, "
                "watermark_advanced, coverage_classifier_version) "
                "VALUES (?, ?, ?, ?, 'complete', ?, 'recovery_cutover', 'canonical_live', ?, "
                "'complete', 1, 1)",
                (
                    created_at, created_at, accepted_new_watermark.isoformat(),
                    accepted_new_watermark.isoformat(), environment, fence,
                ),
            )
            scan_id = cursor.lastrowid
            assert scan_id is not None

            postcondition_row = self._conn.execute(
                "SELECT safe_watermark_at FROM scans "
                "WHERE safe_watermark_at IS NOT NULL AND environment = ? "
                "AND role = 'canonical_live' ORDER BY id DESC LIMIT 1",
                (environment,),
            ).fetchone()
            readable = (
                datetime.fromisoformat(postcondition_row["safe_watermark_at"])
                if postcondition_row else None
            )
            if readable != accepted_new_watermark:
                # Roll the insert back by raising out of the transaction —
                # but record the refusal first so the audit survives. This
                # branch is unreachable with a correct read path; it exists
                # so a regression fails closed rather than committing a
                # cursor that is not what the operator accepted.
                self._conn.execute("DELETE FROM scans WHERE id = ?", (scan_id,))
                return _refuse(
                    "postcondition_mismatch",
                    f"re-read cursor {readable} != accepted {accepted_new_watermark}",
                )

            audit_id = self._insert_recovery_operation(
                environment=environment, operation="cutover",
                operator=operator.strip(), rationale=rationale.strip(),
                policy=policy.strip(), source_evidence=source_evidence.strip(),
                probe_run_id=probe_run_id,
                expected_old_watermark=expected_old_watermark,
                accepted_new_watermark=accepted_new_watermark,
                outcome="accepted", detail=f"scan_id={scan_id}", created_at=created_at,
            )

        logger.warning(
            "Cutover accepted for environment=%s: %s -> %s (scan #%d, audit #%d)",
            environment, expected_old_watermark, accepted_new_watermark, scan_id, audit_id,
        )
        return Ok(CutoverResult(
            scan_id=int(scan_id), probe_run_id=probe_run_id, audit_id=audit_id,
            accepted_new_watermark=accepted_new_watermark,
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

    def list_source_checkpoints(self, *, active_only: bool = True) -> list[SourceCheckpoint]:
        rows = self._conn.execute(
            "SELECT source_key, platform, source_kind, provider_key, required, active, "
            "checkpoint_at, bootstrapped_from_legacy FROM source_checkpoints "
            + ("WHERE active = 1 " if active_only else "")
            + "ORDER BY source_key"
        ).fetchall()
        return [self._row_to_checkpoint(row) for row in rows]

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

    def update_source_checkpoint(
        self, source_key: str, *, checkpoint_at: datetime
    ) -> Result[SourceCheckpoint, SourceCheckpointError]:
        """Advance an active source's checkpoint after a scan actually
        covers it.

        Rejects a `source_key` that doesn't name an existing, active source
        — a typo or a retired source must not silently no-op — and rejects
        a `checkpoint_at` that would move the cursor backwards or sideways
        relative to its current stored value, since a late or stale scan
        must never undo a source's already-recorded progress.
        """
        operation = "update_source_checkpoint"
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            row = self._conn.execute(
                "SELECT active, checkpoint_at FROM source_checkpoints WHERE source_key = ?",
                (source_key,),
            ).fetchone()
            if row is None:
                return Err(SourceCheckpointError(
                    operation=operation, source_key=source_key,
                    detail="source checkpoint not found",
                ))
            if not row["active"]:
                return Err(SourceCheckpointError(
                    operation=operation, source_key=source_key,
                    detail="source checkpoint is retired",
                ))
            current = (
                datetime.fromisoformat(row["checkpoint_at"]) if row["checkpoint_at"] else None
            )
            if current is not None and checkpoint_at <= current:
                return Err(SourceCheckpointError(
                    operation=operation, source_key=source_key,
                    detail=(
                        f"non-monotonic checkpoint: new={checkpoint_at.isoformat()} "
                        f"current={current.isoformat()}"
                    ),
                ))
            self._conn.execute(
                "UPDATE source_checkpoints SET checkpoint_at = ?, updated_at = ? "
                "WHERE source_key = ?",
                (checkpoint_at.isoformat(), now, source_key),
            )
        updated = self.get_source_checkpoint(source_key)
        assert updated is not None
        return Ok(updated)

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
