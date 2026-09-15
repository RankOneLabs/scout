"""Audited watermark recovery operations: a read-only six-hour probe,
correctness-first bounded backfill (the default recovery path), controlled
cutover (an explicitly accepted gap), and a stale-watermark diagnostic.

Every mutating operation here acquires environment's fenced lease as an
exclusive recovery lock before touching anything, and every backfill/
cutover attempt — accepted or refused — is appended to the immutable
`recovery_operations` audit trail (scout.storage.scans). No command here
performs any host-level willie action (stopping a worker, deploying,
restarting a service); that is an external operator responsibility
documented in docs/runbooks/watermark-recovery.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from typing import Literal

import scout.config as _config
from scout.config import build_search_queries
from scout.result import Err, Ok
from scout.scanning import coverage as coverage_lifecycle
from scout.scanning import lease as lease_lifecycle
from scout.scanning.runner import build_platform_scanners, fetch_messages
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


def add_watermark_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], db_path: str
) -> None:
    p = subparsers.add_parser(
        "watermark", help="Audited scan-watermark recovery operations"
    )
    sub = p.add_subparsers(dest="watermark_command", required=True)

    probe_p = sub.add_parser(
        "probe", help="Read-only six-hour probe of every active source"
    )
    probe_p.add_argument("--environment", required=True)
    probe_p.add_argument("--db-path", default=db_path)
    probe_p.add_argument(
        "--hours", type=float, default=6.0,
        help="Probe window in hours (default: 6, matching the decision name)",
    )

    stale_p = sub.add_parser(
        "stale-check", help="Report whether environment's watermark is stale"
    )
    stale_p.add_argument("--environment", required=True)
    stale_p.add_argument("--db-path", default=db_path)
    stale_p.add_argument("--hours", type=float, default=None, help="Override staleness threshold")

    backfill_p = sub.add_parser(
        "backfill", help="Correctness-first bounded backfill (the default recovery path)"
    )
    backfill_p.add_argument("--environment", required=True)
    backfill_p.add_argument("--db-path", default=db_path)
    backfill_p.add_argument("--operator", required=True)
    backfill_p.add_argument("--rationale", required=True)
    backfill_p.add_argument(
        "--hours", type=float, required=True,
        help="How far back to widen the fetch window, bounded by "
        "SCOUT_BACKFILL_MAX_PAGES_PER_SOURCE",
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
        "--accepted-new", required=True, help="ISO8601 timestamp to accept as the new cursor"
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


async def _run_probe(args: argparse.Namespace) -> int:
    owner_id = lease_lifecycle.generate_owner_id()
    with StateManager(db_path=args.db_path) as state:
        lease_result = state.acquire_environment_lease(
            args.environment, owner_id, ttl_seconds=_config.SCOUT_RECOVERY_LOCK_TTL_SECONDS,
        )
        match lease_result:
            case Err(error):
                _print_json({"ok": False, "reason": "lock_contention", "detail": error.detail})
                return EXIT_LOCK_CONTENTION
            case Ok(lease):
                pass

        try:
            discord_scanner, farcaster_scanner, bluesky_scanner = build_platform_scanners()
            source_count = sum(
                1 for s in (discord_scanner, farcaster_scanner, bluesky_scanner) if s is not None
            )
            registry = state.load_runtime_registry()
            search_queries = build_search_queries(registry.keywords)
            since = datetime.now(UTC) - timedelta(hours=args.hours)

            probe_run_id = state.start_probe_run(args.environment, source_count=source_count)
            fetched = await fetch_messages(
                discord_scanner, farcaster_scanner, bluesky_scanner, since,
                queries=search_queries,
            )
            messages, failures = fetched.messages, list(fetched.failures)
            passed = not failures
            # fetch_messages' PlatformFetchSuccess/Failure contract does not
            # currently expose a raw per-source page count (only whether a
            # page ceiling was reached) — page_count records the number of
            # sources that reported hitting their ceiling as a lower-bound
            # signal, not a true page total. See known_issues in the build
            # result for the full-fidelity page counter this stands in for.
            page_ceiling_hits = sum(1 for f in failures if f.kind == "page_ceiling")
            state.complete_probe_run(
                probe_run_id,
                passed=passed,
                page_count=page_ceiling_hits,
                detail_json=json.dumps({
                    "message_count": len(messages),
                    "failure_count": len(failures),
                    "failures": [f.kind for f in failures],
                }),
            )
            _print_json({
                "ok": passed,
                "probe_run_id": probe_run_id,
                "environment": args.environment,
                "source_count": source_count,
                "message_count": len(messages),
                "failures": [{"platform": f.platform, "kind": f.kind} for f in failures],
            })
            return EXIT_OK if passed else EXIT_SOURCE_OR_PROBE_FAILURE
        finally:
            state.release_environment_lease(args.environment, owner_id, lease.fence)


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
    owner_id = lease_lifecycle.generate_owner_id()
    with StateManager(db_path=args.db_path) as state:
        lease_result = state.acquire_environment_lease(
            args.environment, owner_id, ttl_seconds=_config.SCOUT_RECOVERY_LOCK_TTL_SECONDS,
        )
        match lease_result:
            case Err(error):
                state.record_recovery_operation(
                    environment=args.environment, operation="backfill",
                    operator=args.operator, rationale=args.rationale,
                    outcome="refused", detail=f"lock_contention: {error.detail}",
                )
                _print_json({"ok": False, "reason": "lock_contention", "detail": error.detail})
                return EXIT_LOCK_CONTENTION
            case Ok(lease):
                pass

        try:
            reconciled = state.reconcile_abandoned_canonical_owners(
                args.environment, lease.fence
            )
            discord_scanner, farcaster_scanner, bluesky_scanner = build_platform_scanners()
            registry = state.load_runtime_registry()
            search_queries = build_search_queries(registry.keywords)
            since = datetime.now(UTC) - timedelta(hours=args.hours)
            fetch_started_at = datetime.now(UTC)

            canonical_scan_id = coverage_lifecycle.commit_canonical_owner(
                state, environment=args.environment, fetch_started_at=fetch_started_at,
            )
            fetched = await fetch_messages(
                discord_scanner, farcaster_scanner, bluesky_scanner, since,
                queries=search_queries,
            )
            messages, failures = fetched.messages, list(fetched.failures)
            unseen = [
                m for m in messages if not state.has_seen_message(m.platform, m.platform_id)
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
            scan_status: Literal["complete", "partial"] = "partial" if failures else "complete"
            state.complete_scan(
                canonical_scan_id, len(unseen), 0, status=scan_status, overflow_count=0,
            )
            result = coverage_lifecycle.finalize_owner(
                state, canonical_scan_id, environment=args.environment, advance_watermark=True,
            )
            outcome: Literal["accepted", "refused"]
            match result:
                case Ok(finalized):
                    outcome = "accepted"
                    detail = (
                        f"fetched={len(messages)} unseen={len(unseen)} "
                        f"coverage_outcome={finalized.coverage_outcome} "
                        f"reconciled_abandoned={reconciled}"
                    )
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
                "fetched": len(messages),
                "unevaluated_posts_persisted": len(unseen),
                "reconciled_abandoned_owners": reconciled,
                "detail": detail,
            })
            if outcome == "accepted" and not failures:
                return EXIT_OK
            return EXIT_SOURCE_OR_PROBE_FAILURE
        finally:
            state.release_environment_lease(args.environment, owner_id, lease.fence)


def _run_cutover(args: argparse.Namespace) -> int:
    owner_id = lease_lifecycle.generate_owner_id()
    with StateManager(db_path=args.db_path) as state:
        lease_result = state.acquire_environment_lease(
            args.environment, owner_id, ttl_seconds=_config.SCOUT_RECOVERY_LOCK_TTL_SECONDS,
        )
        match lease_result:
            case Err(error):
                state.record_recovery_operation(
                    environment=args.environment, operation="cutover",
                    operator=args.operator, rationale=args.rationale,
                    policy=args.policy, source_evidence=args.source_evidence,
                    outcome="refused", detail=f"lock_contention: {error.detail}",
                )
                _print_json({"ok": False, "reason": "lock_contention", "detail": error.detail})
                return EXIT_LOCK_CONTENTION
            case Ok(lease):
                pass

        try:
            probe = state.get_latest_passed_probe(
                args.environment, max_age_seconds=_config.SCOUT_CUTOVER_PROBE_MAX_AGE_SECONDS,
            )
            if probe is None:
                detail = "no recent passed six-hour probe for this environment"
                state.record_recovery_operation(
                    environment=args.environment, operation="cutover",
                    operator=args.operator, rationale=args.rationale,
                    policy=args.policy, source_evidence=args.source_evidence,
                    outcome="refused", detail=detail,
                )
                _print_json({"ok": False, "reason": "missing_probe", "detail": detail})
                return EXIT_SOURCE_OR_PROBE_FAILURE

            try:
                accepted_new = datetime.fromisoformat(args.accepted_new)
                expected_old = (
                    datetime.fromisoformat(args.expected_old) if args.expected_old else None
                )
            except ValueError as exc:
                detail = f"invalid timestamp: {exc}"
                state.record_recovery_operation(
                    environment=args.environment, operation="cutover",
                    operator=args.operator, rationale=args.rationale,
                    policy=args.policy, source_evidence=args.source_evidence,
                    outcome="refused", detail=detail,
                )
                _print_json({"ok": False, "reason": "missing_metadata", "detail": detail})
                return EXIT_MISSING_METADATA

            result = state.cutover_watermark(
                environment=args.environment, owner_id=owner_id, fence=lease.fence,
                expected_old_watermark=expected_old, accepted_new_watermark=accepted_new,
            )
            match result:
                case Err(cutover_error):
                    state.record_recovery_operation(
                        environment=args.environment, operation="cutover",
                        operator=args.operator, rationale=args.rationale,
                        policy=args.policy, source_evidence=args.source_evidence,
                        probe_run_id=probe.probe_run_id,
                        expected_old_watermark=expected_old,
                        accepted_new_watermark=accepted_new,
                        outcome="refused", detail=cutover_error.detail,
                    )
                    _print_json({
                        "ok": False, "reason": "stale_expected_old", "detail": cutover_error.detail,
                    })
                    return EXIT_STALE_EXPECTED_OLD
                case Ok(scan_id):
                    pass

            postcondition = state.get_last_scan_timestamp(environment=args.environment)
            if postcondition != accepted_new:
                detail = f"postcondition mismatch: expected={accepted_new} actual={postcondition}"
                state.record_recovery_operation(
                    environment=args.environment, operation="cutover",
                    operator=args.operator, rationale=args.rationale,
                    policy=args.policy, source_evidence=args.source_evidence,
                    probe_run_id=probe.probe_run_id,
                    expected_old_watermark=expected_old, accepted_new_watermark=accepted_new,
                    outcome="refused", detail=detail,
                )
                _print_json({"ok": False, "reason": "postcondition_mismatch", "detail": detail})
                return EXIT_POSTCONDITION_MISMATCH

            state.record_recovery_operation(
                environment=args.environment, operation="cutover",
                operator=args.operator, rationale=args.rationale,
                policy=args.policy, source_evidence=args.source_evidence,
                probe_run_id=probe.probe_run_id,
                expected_old_watermark=expected_old, accepted_new_watermark=accepted_new,
                outcome="accepted", detail=f"scan_id={scan_id}",
            )
            _print_json({
                "ok": True, "scan_id": scan_id, "accepted_new_watermark": accepted_new,
            })
            return EXIT_OK
        finally:
            state.release_environment_lease(args.environment, owner_id, lease.fence)
