"""CLI adapter for the read-only typesafe shadow report."""

from __future__ import annotations

import argparse
from datetime import datetime

from scout.storage.db import read_only_connection
from scout.storage.shadow_relevance import ShadowRelevanceStore
from scout.typesafe.reporting import (
    FixtureReplayUnavailableError,
    build_report,
    render_text,
    replay_acceptance_fixture,
)


def _iso8601(value: str) -> str:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO-8601 timestamp") from exc
    return value


def add_typesafe_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], db_path: str
) -> None:
    parser = subparsers.add_parser("typesafe", help="Typesafe shadow operator commands")
    commands = parser.add_subparsers(dest="typesafe_command", required=True)
    report = commands.add_parser(
        "report", help="Compare shadow, LLM, and finalized human decisions"
    )
    selector = report.add_mutually_exclusive_group(required=True)
    selector.add_argument("--since", type=_iso8601)
    selector.add_argument("--scan-id", type=int)
    report.add_argument("--json", action="store_true")
    report.add_argument("--db-path", default=db_path)


def run_typesafe(args: argparse.Namespace) -> int:
    with read_only_connection(args.db_path) as conn:
        if not ShadowRelevanceStore.table_exists(conn):
            print("shadow_relevance_runs is not present; this database predates schema v47.")
            return 0
        rows = ShadowRelevanceStore.report_rows(
            conn, since=args.since, scan_id=args.scan_id
        )
    try:
        fixture_check = replay_acceptance_fixture()
    except FixtureReplayUnavailableError as exc:
        print(f"Cannot produce typesafe report: {exc}")
        return 1
    report = build_report(rows, fixture_check)
    print(report.model_dump_json(indent=2) if args.json else render_text(report))
    return 0
