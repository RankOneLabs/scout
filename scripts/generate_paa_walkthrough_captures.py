"""Generate or check docs/assets/paa-walkthrough/.

docs/assets/paa-walkthrough/ holds the captured `scout paa` output that
docs/paa-reviewer-walkthrough.md links to: one reference execution of the
autonomy control plane against Scout's checked-in inbound_reply_surfacing
declaration and the redacted artifacts under evidence/paa/reference/, run
in a throwaway database and evidence root. Every capture is labeled
"Reference execution"; none of it is production output. See
scout.paa.walkthrough_captures for what is pinned and why.

Usage:
    uv run python scripts/generate_paa_walkthrough_captures.py --write
    uv run python scripts/generate_paa_walkthrough_captures.py --check
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scout.paa.walkthrough_captures import (  # noqa: E402
    CAPTURES_DIR,
    CaptureGenerationError,
    check_captures,
    write_captures,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--write", action="store_true",
        help="Render and replace the *.txt captures under docs/assets/paa-walkthrough/",
    )
    mode.add_argument(
        "--check", action="store_true",
        help="Exit nonzero if docs/assets/paa-walkthrough/ is stale (never writes)",
    )
    args = parser.parse_args()

    try:
        if args.check:
            problems = check_captures(CAPTURES_DIR)
            for problem in problems:
                print(f"docs/assets/paa-walkthrough/ check failed: {problem}", file=sys.stderr)
            if problems:
                return 1
            print(f"{CAPTURES_DIR} is up to date")
            return 0
        written = write_captures(CAPTURES_DIR)
    except CaptureGenerationError as exc:
        print(f"failed to generate docs/assets/paa-walkthrough/: {exc}", file=sys.stderr)
        return 1
    for name in written:
        print(f"wrote {CAPTURES_DIR / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
