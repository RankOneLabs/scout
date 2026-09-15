"""Canonical live fetch-owner lifecycle: commit exactly one owner scan
before any platform I/O, bound to the holding lease, and finalize it
through durable coverage evidence on every terminal path — success, empty
success, processing exception, or cancellation.

Thin orchestration over `StateManager`'s coverage/lease primitives
(scout.storage.scans); no platform or scoring knowledge lives here.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from scout.errors import PlatformFetchFailure, SourceFetchOutcome
from scout.result import Err, Ok, Result
from scout.storage.scans import (
    CoverageFinalizationError,
    CoverageFinalizationResult,
    LeaseError,
    ScanRole,
    SourceCheckpoint,
)
from scout.storage.state import StateManager

logger = logging.getLogger("scout.scanning.coverage")


def commit_canonical_owner(
    state: StateManager,
    *,
    environment: str,
    owner_id: str,
    fence: int,
    fetch_started_at: datetime,
) -> Result[int, LeaseError]:
    """Durably commit exactly one canonical live fetch-owner scan,
    immediately before calling the platform clients, in the same
    transaction that re-verifies `owner_id` still holds `environment`'s
    lease at `fence`, unexpired. A crash or cancellation during the
    platform I/O that follows still leaves this row behind as a
    recoverable, auditable attempt; a worker that already lost its lease
    gets `Err` and never becomes an owner."""
    return state.start_canonical_owner_scan(
        environment=environment,
        owner_id=owner_id,
        fence=fence,
        fetch_started_at=fetch_started_at,
    )


def commit_linked_secondary(
    state: StateManager,
    *,
    environment: str,
    fetch_started_at: datetime,
    canonical_scan_id: int,
    run_kind: str = "live",
) -> int:
    """Record a secondary or rescore scan linked back to `canonical_scan_id`
    — a non-advancing pass over the same fetch (e.g. --mode both's second
    scoring pass) or an independent rescore/rescore-failed run. Never
    eligible to advance the watermark: finalize_scan_coverage only ever
    accepts role='canonical_live'."""
    role: ScanRole = "rescore" if run_kind == "rescore" else "secondary"
    scan_id = state.start_scan(
        fetch_started_at=fetch_started_at,
        environment=environment,
        run_kind=run_kind,
        role=role,
    )
    state.link_secondary_scan(scan_id, canonical_scan_id=canonical_scan_id)
    return scan_id


@dataclass(frozen=True, slots=True)
class SourceCoverage:
    """The normalized source-key sets finalization consumes, plus the
    active required checkpoints this fetch never attempted at all."""

    required: frozenset[str]
    covered: frozenset[str]
    unattempted: tuple[SourceCheckpoint, ...]


def register_source_outcomes(
    state: StateManager, source_outcomes: Sequence[SourceFetchOutcome]
) -> SourceCoverage:
    """Get-or-create a checkpoint row for every source the fetch attempted
    (new sources start cold, per the coverage foundation) and derive the
    sets finalization consumes.

    `required` is seeded from every active, required checkpoint row — not
    only from the sources this fetch happened to attempt — so a source
    whose platform is unconfigured this run, or that a scanner skipped,
    still counts against coverage. Such a source is returned in
    `unattempted`; the caller records blocking evidence for it (see
    `unattempted_source_failures`) so the scan finalizes `blocked` rather
    than silently advancing past it. Decommissioned sources must be
    retired explicitly (`retire_source_checkpoint`) to stop counting.
    """
    attempted: set[str] = set()
    required: set[str] = set()
    covered: set[str] = set()
    for outcome in source_outcomes:
        attempted.add(outcome.source_key)
        checkpoint = state.ensure_source_checkpoint(
            outcome.source_key,
            platform=outcome.platform,
            source_kind=outcome.source_kind,
            provider_key=outcome.provider_key,
        )
        if checkpoint.active and checkpoint.required:
            required.add(outcome.source_key)
        if outcome.covered:
            covered.add(outcome.source_key)
    unattempted = tuple(
        checkpoint
        for checkpoint in state.list_source_checkpoints(active_only=True)
        if checkpoint.required and checkpoint.source_key not in attempted
    )
    required.update(checkpoint.source_key for checkpoint in unattempted)
    return SourceCoverage(
        required=frozenset(required), covered=frozenset(covered), unattempted=unattempted,
    )


def unattempted_source_failures(coverage: SourceCoverage) -> list[PlatformFetchFailure]:
    """One blocking fetch failure per active required source the fetch
    never attempted — the durable evidence that turns a missing source
    into a `blocked` coverage outcome instead of a silent advance."""
    return [
        PlatformFetchFailure(
            platform=checkpoint.platform,
            kind="source_unattempted",
            message=(
                f"active required source {checkpoint.source_key} was not attempted by this "
                "fetch; retire it if it has been decommissioned"
            ),
            context=checkpoint.source_key,
            retryable=True,
            operation_phase="fetch",
            blocks_watermark_advance=True,
        )
        for checkpoint in coverage.unattempted
    ]


def record_interruption(
    state: StateManager,
    scan_id: int,
    *,
    messages_scanned: int,
    error_kind: str,
    error_message: str,
) -> None:
    """Persist a terminal, blocking outcome for a canonical owner that hit
    a processing exception or cancellation during platform I/O or
    scoring. Safe to call even after the owner's lease has been lost: the
    row still becomes durably terminal with auditable evidence, and the
    lease-bound finalization gate separately refuses ever advancing the
    watermark from a lost owner's work."""
    state.fail_scan(
        scan_id,
        messages_scanned,
        failure_post_id=None,
        error_kind=error_kind,
        error_message=error_message,
    )


def finalize_owner(
    state: StateManager,
    scan_id: int,
    *,
    environment: str,
    owner_id: str,
    advance_watermark: bool,
    required_source_keys: frozenset[str] = frozenset(),
    covered_source_keys: frozenset[str] = frozenset(),
    coverage_classifier_version: int = 1,
) -> Result[CoverageFinalizationResult, CoverageFinalizationError]:
    """Finalize a canonical owner's durable coverage — watermark and every
    covered source checkpoint move together, atomically, only while
    `owner_id` still holds the lease — forcing the scan to
    `status='failed'` with auditable evidence if finalization itself is
    refused, never leaving a scan silently uncommitted between
    `complete_scan` and coverage finalization."""
    result = state.finalize_scan_coverage(
        scan_id,
        environment=environment,
        advance_watermark=advance_watermark,
        owner_id=owner_id,
        required_source_keys=required_source_keys,
        covered_source_keys=covered_source_keys,
        coverage_classifier_version=coverage_classifier_version,
    )
    match result:
        case Err(error):
            state.mark_coverage_finalization_failed(scan_id, detail=error.detail)
        case Ok():
            pass
    return result


def finalize_empty_success(
    state: StateManager,
    scan_id: int,
    *,
    environment: str,
    owner_id: str,
    advance_watermark: bool,
    required_source_keys: frozenset[str] = frozenset(),
    covered_source_keys: frozenset[str] = frozenset(),
) -> Result[CoverageFinalizationResult, CoverageFinalizationError]:
    """Complete and finalize a zero-message, fully covered live fetch.

    The caller must not have constructed a tracer, feedback loop, model
    client, or digest for this scan — a fully empty fetch is a valid
    advancing owner on its own, and its covered sources' checkpoints
    advance in the same transaction as the watermark.
    """
    state.complete_scan(scan_id, 0, 0, status="complete", overflow_count=0)
    return finalize_owner(
        state,
        scan_id,
        environment=environment,
        owner_id=owner_id,
        advance_watermark=advance_watermark,
        required_source_keys=required_source_keys,
        covered_source_keys=covered_source_keys,
    )
