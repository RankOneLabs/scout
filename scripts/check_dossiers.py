"""Validate dossiers for all active projects.

Reads active projects from the DB and, for each project that has a
dossier_summary_id set, calls resolve_dossier to verify the dossier
resolves against the pinned dossier-source schema contract and is
sufficiently populated and not stale.

This script is read-only: it opens the database in read-only mode and
never writes to it.

Usage:
    uv run python scripts/check_dossiers.py [--db-path scout.db] [--root /path/to/dossier-root]

Exit codes:
    0  All active projects with dossier IDs are ready (or none exist).
    1  One or more projects are not ready.
"""
# ruff: noqa: E501

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

# Allow running directly from the repo root or via uv run
sys.path.insert(0, str(Path(__file__).parent.parent))

from scout.config import (  # noqa: E402
    DB_PATH as DEFAULT_DB_PATH,
)
from scout.config import (
    SCOUT_DOSSIER_MAX_AGE_DAYS,
    SCOUT_DOSSIER_MIN_ENTRIES,
    SCOUT_DOSSIER_ROOT,
)
from scout.dossiers.resolver import (  # noqa: E402
    DossierResolutionError,
    get_dossier_revision,
    get_pinned_dossier_revision,
    resolve_dossier,
)

logger = logging.getLogger(__name__)


def _has_dossier_column(conn: sqlite3.Connection) -> bool:
    """Return True when the projects table has a dossier_summary_id column."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
    return "dossier_summary_id" in cols


def _load_active_projects(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Return active projects as plain dicts.

    If the dossier_summary_id column is absent (migration not yet applied),
    all projects are returned with dossier_summary_id set to None.
    """
    has_col = _has_dossier_column(conn)
    if has_col:
        rows = conn.execute(
            "SELECT key, name, dossier_summary_id FROM projects WHERE active = 1 ORDER BY key"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT key, name FROM projects WHERE active = 1 ORDER BY key"
        ).fetchall()

    projects = []
    for row in rows:
        projects.append(
            {
                "key": row[0],
                "name": row[1],
                "dossier_summary_id": row[2] if has_col else None,
            }
        )
    return projects


def check(db_path: str, root: str, min_entries: int, max_age_days: int) -> int:
    """Run dossier checks; return 0 if all ready, 1 if any not ready."""
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as exc:
        print(f"ERROR: cannot open database {db_path!r}: {exc}", file=sys.stderr)
        return 1

    with conn:
        projects = _load_active_projects(conn)

    ready: list[str] = []
    not_ready: list[tuple[str, str]] = []  # (key, error)

    if not projects:
        print("no active projects; nothing to check.")
        print("\n0/0 dossiers ready.")
        return 0

    root_path = Path(root)
    try:
        revision = get_pinned_dossier_revision(root_path)
        print(f"pinned revision:  {revision}")
    except RuntimeError as exc:
        print(f"pinned revision:  (unavailable: {exc})")
        return 1

    # Reported alongside, never resolved against. The checkout belongs to
    # the producer repository and sits at its own HEAD, so drift here is expected and not
    # an error — but it is the difference between what an operator sees in
    # the working tree and what Scout actually read, and that is worth
    # saying out loud rather than leaving them to infer it.
    try:
        head = get_dossier_revision(root_path)
        note = "same as pin" if head == revision else "AHEAD OF PIN — Scout is not reading this"
        print(f"checkout HEAD:    {head}  ({note})")
    except RuntimeError as exc:
        print(f"checkout HEAD:    (unavailable: {exc})")

    for project in projects:
        key = str(project["key"])
        summary_id = project["dossier_summary_id"]
        if not isinstance(summary_id, str) or not summary_id.strip():
            not_ready.append((key, "dossier_summary_id is missing"))
            continue
        try:
            resolve_dossier(root_path, revision, key, summary_id, max_age_days=max_age_days,
                            min_entries=min_entries)
            ready.append(key)
        except DossierResolutionError as exc:
            not_ready.append((key, str(exc)))

    for key in ready:
        print(f"  OK      {key}")

    for key, error in not_ready:
        print(f"  NOT READY  {key}: {error}")

    total_checked = len(projects)
    print(
        f"\n{len(ready)}/{total_checked} dossier{'s' if total_checked != 1 else ''} ready."
    )

    return 1 if not_ready else 0


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        default=DEFAULT_DB_PATH,
        help="Path to the SQLite database (default: %(default)s)",
    )
    parser.add_argument(
        "--root",
        default=SCOUT_DOSSIER_ROOT or None,
        help=(
            "Root directory of the dossier repo checkout "
            "(default: $SCOUT_DOSSIER_ROOT)"
        ),
    )
    args = parser.parse_args()

    if not args.root:
        print(
            "ERROR: dossier root not set. "
            "Pass --root or set SCOUT_DOSSIER_ROOT.",
            file=sys.stderr,
        )
        sys.exit(1)

    sys.exit(
        check(
            db_path=args.db_path,
            root=args.root,
            min_entries=SCOUT_DOSSIER_MIN_ENTRIES,
            max_age_days=SCOUT_DOSSIER_MAX_AGE_DAYS,
        )
    )


if __name__ == "__main__":
    main()
