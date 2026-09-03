"""CLI wrapper around scout.evals.phase1.jig_exporter.rebuild_finalized_grades_to_jig.

Thin by design: argument parsing and exit-code translation only. All
rebuild behavior — the read-only Scout boundary, pure projection, provider
preflight, temporary-database write, verification, and atomic replace —
lives in scout.evals.phase1.jig_exporter.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from scout.evals.phase1.jig_exporter import JigRebuildError, rebuild_finalized_grades_to_jig


async def _run(scout_db_path: str, jig_db_path: str) -> int:
    try:
        result = await rebuild_finalized_grades_to_jig(scout_db_path, jig_db_path)
    except JigRebuildError as exc:
        print(f"export-finalized-grades-to-jig error: {exc}", file=sys.stderr)
        return 2
    except BaseException as exc:  # noqa: BLE001 - includes KeyboardInterrupt/cancellation
        print(
            f"export-finalized-grades-to-jig error: rebuild interrupted or failed "
            f"unexpectedly ({exc!r}); the previous destination database, if any, is "
            "unchanged",
            file=sys.stderr,
        )
        return 2

    print(
        f"Rebuilt {result.destination}: {result.result_count} results, "
        f"{result.score_count} scores"
    )
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="export_finalized_grades_to_jig",
        description=(
            "Rebuild every finalized Scout human grade into a disposable Jig "
            "analysis database."
        ),
    )
    parser.add_argument("--scout-db", required=True, help="Path to the Scout SQLite database")
    parser.add_argument(
        "--jig-db", required=True, help="Destination path for the rebuilt Jig database"
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    raise SystemExit(asyncio.run(_run(args.scout_db, args.jig_db)))


if __name__ == "__main__":
    main()
