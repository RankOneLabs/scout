"""Audited watermark recovery operations: a read-only six-hour probe,
correctness-first bounded backfill (the default recovery path), controlled
cutover (an explicitly accepted gap), and a stale-watermark diagnostic.

Every mutating operation here acquires environment's fenced lease as an
exclusive recovery lock before touching anything, and every backfill/
cutover attempt — accepted or refused — is appended to the immutable
`recovery_operations` audit trail (scout.storage.scans). No command here
performs any host-level action on willie, the production host (stopping a
worker, deploying, restarting a service); that is an external operator
responsibility documented in docs/runbooks/watermark-recovery.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import scout.config as _config
from scout.config import build_search_queries
from scout.errors import SourceFetchOutcome
from scout.result import Err, Ok
from scout.scanning import coverage as coverage_lifecycle
from scout.scanning import lease as lease_lifecycle
from scout.scanning.runner import PlatformsFetch, build_platform_scanners, fetch_messages
from scout.storage.scans import CutoverRefusalReason
from scout.storage.state import StateManager

# Exit codes are deliberately distinct per failure class so automation can
# branch on them without parsing stderr text.
EXIT_OK = 0
EXIT_LOCK_CONTENTION = 2
EXIT_STALE_EXPECTED_OLD = 3
EXIT_SOURCE_OR_PROBE_FAILURE = 4
EXIT_MISSING_METADATA = 5
EXIT_POSTCONDITION_MISMATCH = 6
EXIT_STALE_WATERMARK = 7

_CUTOVER_EXIT_CODES: dict[CutoverRefusalReason, int] = {
    "lock_not_held": EXIT_LOCK_CONTENTION,
    "stale_expected_old": EXIT_STALE_EXPECTED_OLD,
    "missing_probe": EXIT_SOURCE_OR_PROBE_FAILURE,
    "missing_metadata": EXIT_MISSING_METADATA,
    "invalid_accepted_new": EXIT_MISSING_METADATA,
    "postcondition_mismatch": EXIT_POSTCONDITION_MISMATCH,
}

PROBE_WINDOW_HOURS = 6.0


def positive_hours(raw: str) -> float:
    """argparse type: a strictly positive window. A zero or negative window
    would put `since` at or past now, letting an empty fetch finalize as
    clean coverage of history it never looked at."""
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a number of hours") from exc
    if not value > 0:
        raise argparse.ArgumentTypeError(f"--hours must be > 0, got {raw!r}")
    return value


def add_watermark_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], db_path: str
) -> None:
    p = subparsers.add_parser(
        "watermark", help="Audited scan-watermark recovery operations"
    )
    sub = p.add_subparsers(dest="watermark_command", required=True)

    probe_p = sub.add_parser(
        "probe", help="Read-only six-hour probe of every active normalized source"
    )
    probe_p.add_argument("--environment", required=True)
    probe_p.add_argument("--db-path", default=db_path)
    probe_p.add_argument(
        "--hours", type=positive_hours, default=PROBE_WINDOW_HOURS,
        help="Probe window in hours (default: 6). A cutover only accepts a probe "
        "whose window was at least six hours.",
    )

    stale_p = sub.add_parser(
        "stale-check", help="Report whether environment's watermark is stale"
    )
    stale_p.add_argument("--environment", required=True)
    stale_p.add_argument("--db-path", default=db_path)
    stale_p.add_argument(
        "--hours", type=positive_hours, default=None, help="Override staleness threshold"
    )

    backfill_p = sub.add_parser(
        "backfill", help="Correctness-first bounded backfill (the default recovery path)"
    )
    backfill_p.add_argument("--environment", required=True)
    backfill_p.add_argument("--db-path", default=db_path)
    backfill_p.add_argument("--operator", required=True)
    backfill_p.add_argument("--rationale", required=True)
    backfill_p.add_argument(
        "--hours", type=positive_hours, required=True,
        help="How far back to widen the fetch window; every source is paginated "
        "under SCOUT_BACKFILL_MAX_PAGES_PER_SOURCE rather than the production ceiling",
    )

    cutover_p = sub.add_parser(
        "cutover", help="Controlled cutover — an explicitly accepted watermark gap"
    )
    cutover_p.add_argument("--environment", required=True)
    cutover_p.add_argument("--db-path", default=db_path)
    cutover_p.add_argument("--operator", required=True)
    cutover_p.add_argument("--rationale", required=True)
    cutover_p.add_argument("--policy", required=True)
    cutover_p.add_argument("--source-evidence", required=True)
    cutover_p.add_argument(
        "--accepted-new", required=True,
        help="Timezone-aware ISO8601 timestamp, not in the future, to accept as the new cursor",
    )
    cutover_p.add_argument(
        "--expected-old", default=None,
        help="ISO8601 timestamp the caller observed as the current cursor before "
        "requesting cutover (compare-and-set); omit only when the cursor is "
        "currently unset",
    )


def _print_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def run_watermark(args: argparse.Namespace) -> int:
    if args.watermark_command == "probe":
        return asyncio.run(_run_probe(args))
    if args.watermark_command == "stale-check":
        return _run_stale_check(args)
    if args.watermark_command == "backfill":
        return asyncio.run(_run_backfill(args))
    if args.watermark_command == "cutover":
        return _run_cutover(args)
    print(f"unknown watermark command: {args.watermark_command!r}", file=sys.stderr)
    return EXIT_MISSING_METADATA


def _production_limits() -> dict[str, int]:
    """The exact page/result limits a probe runs under — persisted with the
    probe so a cutover can verify it was a normal-limit probe."""
    return {
        "discord_max_pages": _config.DISCORD_MAX_PAGES,
        "discord_max_messages_per_channel": _config.MAX_MESSAGES_PER_CHANNEL,
        "farcaster_max_pages": _config.FARCASTER_MAX_PAGES,
        "farcaster_max_results_per_query": _config.FARCASTER_MAX_RESULTS_PER_QUERY,
        "bluesky_max_pages": _config.BLUESKY_MAX_PAGES,
        "bluesky_max_results_per_query": _config.BLUESKY_MAX_RESULTS_PER_QUERY,
    }


def _outcome_evidence(outcome: SourceFetchOutcome) -> dict[str, object]:
    return {
        "source_key": outcome.source_key,
        "page_count": outcome.page_count,
        "termination": outcome.termination,
        "message_count": outcome.message_count,
        "covered": outcome.covered,
        "failure": (
            {"kind": outcome.failure.kind, "message": outcome.failure.message}
            if outcome.failure is not None else None
        ),
    }


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    """What a probe or backfill learned about every active normalized
    source: the per-source outcomes it attempted, the active checkpoint
    rows it never reached, and whole-platform failures with no source
    granularity."""

    outcomes: tuple[SourceFetchOutcome, ...]
    unattempted_active_sources: tuple[str, ...]
    platform_failures: tuple[str, ...]

    @property
    def page_count(self) -> int:
        return sum(o.page_count for o in self.outcomes)

    @property
    def all_covered(self) -> bool:
        return (
            not self.unattempted_active_sources
            and not self.platform_failures
            and all(o.covered for o in self.outcomes)
        )

    def to_json(self) -> dict[str, object]:
        return {
            "sources": [_outcome_evidence(o) for o in self.outcomes],
            "unattempted_active_sources": list(self.unattempted_active_sources),
            "platform_failures": list(self.platform_failures),
        }


def _source_evidence(state: StateManager, fetched: PlatformsFetch) -> SourceEvidence:
    attempted = fetched.attempted_source_keys
    active = {c.source_key for c in state.list_source_checkpoints(active_only=True)}
    # A whole-platform failure carries no source outcomes at all.
    outcome_failure_ids = {id(o.failure) for o in fetched.source_outcomes if o.failure}
    platform_failures = tuple(
        f"{f.platform}:{f.kind}" for f in fetched.failures if id(f) not in outcome_failure_ids
    )
    return SourceEvidence(
        outcomes=fetched.source_outcomes,
        unattempted_active_sources=tuple(sorted(active - attempted)),
        platform_failures=platform_failures,
    )


@dataclass(frozen=True, slots=True)
class RecoveryLock:
    handle: lease_lifecycle.EnvironmentLeaseHandle

    @property
    def owner_id(self) -> str:
        return self.handle.owner_id

    @property
    def fence(self) -> int:
        return self.handle.fence


@asynccontextmanager
async def _recovery_lock(
    state: StateManager, db_path: str, environment: str
) -> AsyncIterator[RecoveryLock | None]:
    """Hold environment's lease as the recovery lock for the duration of a
    long-running probe/backfill, heartbeating on a dedicated connection so
    a slow network operation cannot outlive the TTL unnoticed. Yields None
    on contention. Always stops the heartbeat, releases (unless lost), and
    closes the heartbeat connection."""
    heartbeat_state = StateManager(db_path=db_path, init_schema=False)
    owner_id = lease_lifecycle.generate_owner_id()
    match lease_lifecycle.acquire_lease(
        state,
        environment=environment,
        owner_id=owner_id,
        heartbeat_state=heartbeat_state,
        ttl_seconds=_config.SCOUT_RECOVERY_LOCK_TTL_SECONDS,
        heartbeat_interval_seconds=_config.SCOUT_LEASE_HEARTBEAT_SECONDS,
    ):
        case Err(_):
            with suppress(Exception):
                heartbeat_state.close()
            yield None
            return
        case Ok(handle):
            pass
    handle.start_heartbeat()
    try:
        yield RecoveryLock(handle=handle)
    finally:
        await handle.stop(release=False)
        if not handle.lost:
            with suppress(Exception):
                state.release_environment_lease(environment, owner_id, handle.fence)
        with suppress(Exception):
            heartbeat_state.close()


async def _run_probe(args: argparse.Namespace) -> int:
    with StateManager(db_path=args.db_path) as state:
        async with _recovery_lock(state, args.db_path, args.environment) as lock:
            if lock is None:
                _print_json({"ok": False, "reason": "lock_contention"})
                return EXIT_LOCK_CONTENTION

            discord_scanner, farcaster_scanner, bluesky_scanner = build_platform_scanners()
            registry = state.load_runtime_registry()
            search_queries = build_search_queries(registry.keywords)
            since = datetime.now(UTC) - timedelta(hours=args.hours)
            active_before = state.list_source_checkpoints(active_only=True)

            probe_run_id = state.start_probe_run(
                args.environment,
                source_count=len(active_before),
                window_hours=args.hours,
                limits_json=json.dumps(_production_limits(), sort_keys=True),
            )
            fetched = await fetch_messages(
                discord_scanner, farcaster_scanner, bluesky_scanner, since,
                queries=search_queries,
            )
            # Evidence gathered after losing the lock is not this run's to
            # record: another worker may already be probing or cutting over.
            lock.handle.check()
            evidence = _source_evidence(state, fetched)
            passed = evidence.all_covered
            state.complete_probe_run(
                probe_run_id,
                passed=passed,
                page_count=evidence.page_count,
                detail_json=json.dumps(
                    {"message_count": len(fetched.messages), **evidence.to_json()},
                    sort_keys=True,
                ),
            )
            _print_json({
                "ok": passed,
                "probe_run_id": probe_run_id,
                "environment": args.environment,
                "window_hours": args.hours,
                "source_count": len(evidence.outcomes),
                "page_count": evidence.page_count,
                "message_count": len(fetched.messages),
                **evidence.to_json(),
            })
            return EXIT_OK if passed else EXIT_SOURCE_OR_PROBE_FAILURE


def _run_stale_check(args: argparse.Namespace) -> int:
    threshold_hours = (
        args.hours if args.hours is not None else _config.SCOUT_STALE_WATERMARK_HOURS
    )
    with StateManager(db_path=args.db_path) as state:
        watermark = state.get_last_scan_timestamp(environment=args.environment)

    now = datetime.now(UTC)
    age_hours = (now - watermark).total_seconds() / 3600 if watermark else None
    stale = watermark is None or age_hours is None or age_hours > threshold_hours

    _print_json({
        "environment": args.environment,
        "watermark": watermark,
        "age_hours": age_hours,
        "threshold_hours": threshold_hours,
        "stale": stale,
    })
    return EXIT_STALE_WATERMARK if stale else EXIT_OK


async def _run_backfill(args: argparse.Namespace) -> int:
    with StateManager(db_path=args.db_path) as state:

        def _refuse(detail: str) -> None:
            state.record_recovery_operation(
                environment=args.environment, operation="backfill",
                operator=args.operator, rationale=args.rationale,
                outcome="refused", detail=detail,
            )

        async with _recovery_lock(state, args.db_path, args.environment) as lock:
            if lock is None:
                _refuse("lock_contention")
                _print_json({"ok": False, "reason": "lock_contention"})
                return EXIT_LOCK_CONTENTION

            reconciled = state.reconcile_abandoned_canonical_owners(
                args.environment, lock.fence
            )
            discord_scanner, farcaster_scanner, bluesky_scanner = build_platform_scanners(
                max_pages=_config.SCOUT_BACKFILL_MAX_PAGES_PER_SOURCE
            )
            registry = state.load_runtime_registry()
            search_queries = build_search_queries(registry.keywords)
            since = datetime.now(UTC) - timedelta(hours=args.hours)
            fetch_started_at = datetime.now(UTC)

            match coverage_lifecycle.commit_canonical_owner(
                state, environment=args.environment, owner_id=lock.owner_id,
                fence=lock.fence, fetch_started_at=fetch_started_at,
            ):
                case Ok(canonical_scan_id):
                    pass
                case Err(lease_error):
                    _refuse(f"lock_not_held: {lease_error.detail}")
                    _print_json({
                        "ok": False, "reason": "lock_not_held", "detail": lease_error.detail,
                    })
                    return EXIT_LOCK_CONTENTION

            try:
                fetched = await fetch_messages(
                    discord_scanner, farcaster_scanner, bluesky_scanner, since,
                    queries=search_queries,
                )
                lock.handle.check()
                evidence = _source_evidence(state, fetched)
                source_coverage = coverage_lifecycle.register_source_outcomes(
                    state, fetched.source_outcomes
                )
                failures = [
                    *fetched.failures,
                    *coverage_lifecycle.unattempted_source_failures(source_coverage),
                ]
                unseen = [
                    m for m in fetched.messages
                    if not state.has_seen_message(m.platform, m.platform_id)
                ]
                for msg in unseen:
                    state.save_post(msg, canonical_scan_id)
                for failure in failures:
                    state.save_fetch_failure(
                        canonical_scan_id,
                        platform=failure.platform, kind=failure.kind, message=failure.message,
                        context=failure.context, http_status=failure.http_status,
                        retry_after=failure.retry_after, retryable=failure.retryable,
                        operation_phase=failure.operation_phase,
                        blocks_watermark_advance=failure.blocks_watermark_advance,
                    )
                scan_status: Literal["complete", "partial"] = (
                    "partial" if failures else "complete"
                )
                state.complete_scan(
                    canonical_scan_id, len(unseen), 0, status=scan_status, overflow_count=0,
                )
            except BaseException as exc:
                # The owner is already durable; make it terminal with evidence
                # and audit the refused attempt before the lock goes away —
                # cancellation included.
                with suppress(Exception):
                    coverage_lifecycle.record_interruption(
                        state, canonical_scan_id, messages_scanned=0,
                        error_kind="backfill_error",
                        error_message=f"{type(exc).__name__}: {exc}",
                    )
                with suppress(Exception):
                    _refuse(f"backfill_error: {type(exc).__name__}: {exc}")
                raise

            # Finalization (watermark + checkpoints) and its audit row commit
            # together or not at all.
            outcome: Literal["accepted", "refused"]
            with state.db.begin_immediate():
                result = coverage_lifecycle.finalize_owner(
                    state, canonical_scan_id, environment=args.environment,
                    owner_id=lock.owner_id, advance_watermark=True,
                    required_source_keys=source_coverage.required,
                    covered_source_keys=source_coverage.covered,
                )
                match result:
                    case Ok(finalized):
                        outcome = (
                            "accepted" if finalized.coverage_outcome == "complete" else "refused"
                        )
                        detail = json.dumps({
                            "coverage_outcome": finalized.coverage_outcome,
                            "advanced_source_keys": list(finalized.advanced_source_keys),
                            "fetched": len(fetched.messages),
                            "unseen": len(unseen),
                            "reconciled_abandoned": reconciled,
                            "max_pages_per_source": _config.SCOUT_BACKFILL_MAX_PAGES_PER_SOURCE,
                            **evidence.to_json(),
                        }, sort_keys=True)
                    case Err(finalize_error):
                        outcome = "refused"
                        detail = finalize_error.detail
                state.record_recovery_operation(
                    environment=args.environment, operation="backfill",
                    operator=args.operator, rationale=args.rationale,
                    outcome=outcome, detail=detail,
                )

            _print_json({
                "ok": outcome == "accepted",
                "scan_id": canonical_scan_id,
                "fetched": len(fetched.messages),
                "unevaluated_posts_persisted": len(unseen),
                "reconciled_abandoned_owners": reconciled,
                "max_pages_per_source": _config.SCOUT_BACKFILL_MAX_PAGES_PER_SOURCE,
                "page_count": evidence.page_count,
                **evidence.to_json(),
                "detail": detail,
            })
            return EXIT_OK if outcome == "accepted" else EXIT_SOURCE_OR_PROBE_FAILURE


def _run_cutover(args: argparse.Namespace) -> int:
    owner_id = lease_lifecycle.generate_owner_id()
    with StateManager(db_path=args.db_path) as state:
        try:
            accepted_new = datetime.fromisoformat(args.accepted_new)
            expected_old = (
                datetime.fromisoformat(args.expected_old) if args.expected_old else None
            )
        except ValueError as exc:
            # Never reached storage: record the refusal here so an unparseable
            # request is exactly as auditable as a refused one.
            detail = f"invalid timestamp: {exc}"
            state.record_recovery_operation(
                environment=args.environment, operation="cutover",
                operator=args.operator, rationale=args.rationale,
                policy=args.policy, source_evidence=args.source_evidence,
                outcome="refused", detail=f"missing_metadata: {detail}",
            )
            _print_json({"ok": False, "reason": "missing_metadata", "detail": detail})
            return EXIT_MISSING_METADATA

        # Cutover is one short transaction that re-verifies the lease itself;
        # no heartbeat is needed for its duration.
        lease_result = state.acquire_environment_lease(
            args.environment, owner_id, ttl_seconds=_config.SCOUT_RECOVERY_LOCK_TTL_SECONDS,
        )
        match lease_result:
            case Err(error):
                state.record_recovery_operation(
                    environment=args.environment, operation="cutover",
                    operator=args.operator, rationale=args.rationale,
                    policy=args.policy, source_evidence=args.source_evidence,
                    expected_old_watermark=expected_old, accepted_new_watermark=accepted_new,
                    outcome="refused", detail=f"lock_not_held: {error.detail}",
                )
                _print_json({"ok": False, "reason": "lock_contention", "detail": error.detail})
                return EXIT_LOCK_CONTENTION
            case Ok(lease):
                pass

        try:
            result = state.cutover_watermark(
                environment=args.environment, owner_id=owner_id, fence=lease.fence,
                operator=args.operator, rationale=args.rationale, policy=args.policy,
                source_evidence=args.source_evidence,
                expected_old_watermark=expected_old, accepted_new_watermark=accepted_new,
                probe_max_age_seconds=_config.SCOUT_CUTOVER_PROBE_MAX_AGE_SECONDS,
                probe_min_window_hours=PROBE_WINDOW_HOURS,
            )
            match result:
                case Err(refusal):
                    _print_json({
                        "ok": False, "reason": refusal.reason, "detail": refusal.detail,
                        "audit_id": refusal.audit_id,
                    })
                    return _CUTOVER_EXIT_CODES[refusal.reason]
                case Ok(cutover):
                    _print_json({
                        "ok": True,
                        "scan_id": cutover.scan_id,
                        "probe_run_id": cutover.probe_run_id,
                        "audit_id": cutover.audit_id,
                        "accepted_new_watermark": cutover.accepted_new_watermark,
                    })
                    return EXIT_OK
        finally:
            state.release_environment_lease(args.environment, owner_id, lease.fence)
