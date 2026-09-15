"""Canonical live fetch-owner lifecycle: commit exactly one owner scan
before any platform I/O, and finalize it through durable coverage evidence
on every terminal path — success, empty success, processing exception, or
cancellation.

Thin orchestration over `StateManager`'s coverage/lease primitives
(scout.storage.scans); no platform or scoring knowledge lives here.
"""

from __future__ import annotations

import logging
from datetime import datetime

from scout.result import Err, Ok, Result
from scout.storage.scans import CoverageFinalizationError, CoverageFinalizationResult, ScanRole
from scout.storage.state import StateManager

logger = logging.getLogger("scout.scanning.coverage")


def commit_canonical_owner(
    state: StateManager,
    *,
    environment: str,
    fetch_started_at: datetime,
) -> int:
    """Durably commit exactly one canonical live fetch-owner scan,
    immediately before calling the platform clients. The returned scan_id
    already carries the environment's current lease fence — a crash or
    cancellation during the platform I/O that follows still leaves this
    row behind as a recoverable, auditable attempt."""
    return state.start_scan(
        fetch_started_at=fetch_started_at,
        environment=environment,
        run_kind="live",
        role="canonical_live",
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
    scoring. Safe to call even after the owner's lease fence has been
    superseded (fenced out): the row still becomes durably terminal with
    auditable evidence, and finalize_scan_coverage will separately refuse
    it on the stale-fence check rather than ever advancing the watermark
    from a lost owner's work."""
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
    advance_watermark: bool,
    coverage_classifier_version: int = 1,
) -> Result[CoverageFinalizationResult, CoverageFinalizationError]:
    """Finalize a canonical owner's durable coverage, forcing the scan to
    `status='failed'` with auditable evidence if finalization itself is
    refused (a stale fence, a non-terminal status, or any other
    eligibility gate) — never leaving a scan silently uncommitted between
    `complete_scan` and coverage finalization."""
    result = state.finalize_scan_coverage(
        scan_id,
        environment=environment,
        advance_watermark=advance_watermark,
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
    advance_watermark: bool,
) -> Result[CoverageFinalizationResult, CoverageFinalizationError]:
    """Complete and finalize a zero-message, fully covered live fetch.

    The caller must not have constructed a tracer, feedback loop, model
    client, or digest for this scan — a fully empty fetch is a valid
    advancing owner on its own, with no scoring resources needed.
    """
    state.complete_scan(scan_id, 0, 0, status="complete", overflow_count=0)
    return finalize_owner(
        state, scan_id, environment=environment, advance_watermark=advance_watermark,
    )
