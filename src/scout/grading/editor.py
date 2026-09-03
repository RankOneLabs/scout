"""Editor argv resolution shared by every `$EDITOR`-invoking CLI flow.

Owned by neither grading nor the outbound content engine — both resolve
their editor command through `resolve_editor` here, so there is exactly
one implementation of `$VISUAL`/`$EDITOR` parsing to keep in sync.
"""

from __future__ import annotations

import os
import shlex


def resolve_editor() -> list[str]:
    """Pick the editor argv from $VISUAL / $EDITOR, falling back to vi.

    Empty or whitespace-only values are treated as unset. Malformed
    quoting (unbalanced quotes, etc.) raises ValueError naming the
    offending env var so the user knows what to fix.
    """
    for env_var in ("VISUAL", "EDITOR"):
        raw = os.environ.get(env_var, "").strip()
        if not raw:
            continue
        try:
            argv = shlex.split(raw)
        except ValueError as exc:
            raise ValueError(f"${env_var}={raw!r} is not a valid shell command: {exc}") from exc
        if argv:
            return argv
    return ["vi"]
