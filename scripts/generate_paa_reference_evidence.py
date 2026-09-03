"""Generate or check evidence/paa/reference/.

evidence/paa/reference/ is Scout's checked-in, publication-safe PAA
reference evidence: exact copies of the three PAA task declarations and
the grading schema, a redacted evidence bundle, a redacted outbound
promotion-report snapshot, and a manifest tying every artifact back to
the sources and schema/distribution versions that produced it. See
paa_reference_evidence.py for the generator itself and
docs/runbooks/paa-operations.md for the operator-facing writeup.

Usage:
    uv run python scripts/generate_paa_reference_evidence.py --write
    uv run python scripts/generate_paa_reference_evidence.py --check
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scout.paa.reference_evidence import (  # noqa: E402
    REFERENCE_DIR,
    ReferenceGenerationError,
    build_fixture_source_database,
    check_reference_tree,
    default_generation_inputs,
    render,
    resolve_git_commit,
    write_reference_tree,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--write", action="store_true",
        help="Render and atomically replace evidence/paa/reference/",
    )
    mode.add_argument(
        "--check", action="store_true",
        help="Exit nonzero if evidence/paa/reference/ is stale (never writes)",
    )
    parser.add_argument(
        "--commit",
        help=(
            "The real commit this write pins the manifest's git_revision to "
            "(--write only). Defaults to the current `git rev-parse HEAD` — "
            "never a placeholder."
        ),
    )
    args = parser.parse_args()

    if args.check:
        if args.commit is not None:
            parser.error("--commit only applies to --write")
        problems = check_reference_tree(target_dir=REFERENCE_DIR)
        if problems:
            for problem in problems:
                print(f"evidence/paa/reference/ check failed: {problem}", file=sys.stderr)
            return 1
        print(f"{REFERENCE_DIR} is up to date")
        return 0

    try:
        commit = args.commit or resolve_git_commit()
        inputs = dataclasses.replace(default_generation_inputs(), git_commit=commit)
        with tempfile.TemporaryDirectory() as tmp:
            source_db_path = Path(tmp) / "source.db"
            build_fixture_source_database(source_db_path)
            files = render(inputs, source_db_path=source_db_path)
    except ReferenceGenerationError as exc:
        print(f"failed to generate evidence/paa/reference/: {exc}", file=sys.stderr)
        return 1

    try:
        write_reference_tree(files, target_dir=REFERENCE_DIR)
    except OSError as exc:
        print(f"failed to write {REFERENCE_DIR}: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {REFERENCE_DIR} ({len(files)} files) pinned to commit {commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
