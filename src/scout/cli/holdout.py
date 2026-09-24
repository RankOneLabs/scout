"""CLI handlers for `scout holdout`: export a pending population, release it.

`export` is read-only. `release` makes model calls and writes evaluations,
so it takes the same care the scan loop does: one fenced claim per hold,
model calls outside every database transaction, and the target evaluation
and the release completion committed together.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from scout.holdouts.export import (
    HoldoutExportRecord,
    export_holdouts,
    render_blind_jsonl,
    render_holdout_jsonl,
)
from scout.holdouts.release import (
    ReleaseInputs,
    load_release_labels,
    release_pending_holdouts,
)
from scout.replay.runtime import replay_runtime
from scout.result import Err
from scout.storage.state import StateManager


def add_holdout_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], db_path: str
) -> None:
    """Register `scout holdout export` and `scout holdout release`."""
    holdout_parser = subparsers.add_parser(
        "holdout", help="Relevance holdout export and release"
    )
    holdout_sub = holdout_parser.add_subparsers(dest="holdout_command", required=True)

    export_p = holdout_sub.add_parser(
        "export",
        help="Export every unreleased hold as JSON lines",
    )
    export_p.add_argument(
        "--output",
        default=None,
        help="Write JSON lines here instead of stdout",
    )
    export_p.add_argument(
        "--blind",
        action="store_true",
        help="Emit only the allowlisted blind projection (no production decision)",
    )
    export_p.add_argument("--db", default=db_path, help="SQLite database path")

    release_p = holdout_sub.add_parser(
        "release",
        help="Release every eligible hold, acting on stored labels where they exist",
    )
    release_p.add_argument(
        "--labels",
        default=None,
        help="assay.label-packet-labels/v3 file; requires --key",
    )
    release_p.add_argument(
        "--key",
        default=None,
        help="assay.label-packet-key/v2 file resolving each case to a held evaluation",
    )
    release_p.add_argument(
        "--packet",
        default=None,
        help="assay.label-packet/v1 file, verified against the labels and key digests",
    )
    release_p.add_argument(
        "--owner",
        default=None,
        help="Claim owner recorded on each hold (defaults to a generated worker id)",
    )
    release_p.add_argument("--db", default=db_path, help="SQLite database path")


def export_holdout_population(args: argparse.Namespace) -> None:
    """Write the pending holdout population, or fail loudly without a file."""
    with StateManager(db_path=args.db) as state:
        exported = export_holdouts(state.conn)
    if isinstance(exported, Err):
        print(
            f"error: {exported.error.operation}: {exported.error.detail}"
            + (
                f" (holdout {exported.error.holdout_id})"
                if exported.error.holdout_id is not None
                else ""
            ),
            file=sys.stderr,
        )
        raise SystemExit(1)

    records: tuple[HoldoutExportRecord, ...] = exported.value
    payload = render_blind_jsonl(records) if args.blind else render_holdout_jsonl(records)
    if args.output:
        Path(args.output).write_bytes(payload)
        print(f"{len(records)} unreleased holds → {args.output}")
    else:
        sys.stdout.buffer.write(payload)


def release_holdout_population(args: argparse.Namespace) -> None:
    """Attempt every eligible hold, reporting each outcome and its age.

    Exits non-zero when any hold failed, so an operator running this from a
    scheduler sees a partial run rather than a silent one. Every failed hold
    stays pending for the next attempt.
    """
    if (args.labels is None) != (args.key is None):
        print("error: --labels and --key must be given together", file=sys.stderr)
        raise SystemExit(2)

    resolved = load_release_labels(
        ReleaseInputs(
            labels_path=Path(args.labels) if args.labels else None,
            key_path=Path(args.key) if args.key else None,
            packet_path=Path(args.packet) if args.packet else None,
        )
    )
    if isinstance(resolved, Err):
        print(f"error: {resolved.error.operation}: {resolved.error.detail}", file=sys.stderr)
        raise SystemExit(1)

    async def _run() -> None:
        # The resource bundle replay already uses: one owner, cleanup in
        # reverse acquisition order, no try/finally pyramid here.
        async with replay_runtime(db_path=args.db) as runtime:
            released = await release_pending_holdouts(
                state=runtime.state,
                tracer=runtime.tracer,
                feedback=runtime.feedback,
                labels=resolved.value,
                owner=args.owner,
            )
        if isinstance(released, Err):
            # A whole-run refusal: nothing was claimed and nothing changed.
            print(
                f"error: {released.error.operation}: {released.error.detail}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        report = released.value
        print(json.dumps(report.to_json(), indent=2, sort_keys=True))
        if report.failed:
            raise SystemExit(1)

    asyncio.run(_run())


__all__ = [
    "add_holdout_parser",
    "export_holdout_population",
    "release_holdout_population",
]
