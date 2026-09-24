"""CLI handlers for `scout holdout`. Read-only export today.

`release` lands beside it and registers its own subparser here.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scout.holdouts.export import (
    HoldoutExportRecord,
    export_holdouts,
    render_blind_jsonl,
    render_holdout_jsonl,
)
from scout.result import Err
from scout.storage.state import StateManager


def add_holdout_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], db_path: str
) -> None:
    """Register `scout holdout export`."""
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


__all__ = ["add_holdout_parser", "export_holdout_population"]
