"""Dossier prohibition matching for the dossier-source dossier contract.

Ported from the upstream producer implementation (pinned producer commit
44a70aa86d470e99c6315126ffdad5e1640d3f1c):
  packages/core/src/prohibition.ts -> prohibition_matches

See the producer's docs/dossier-contract.md for the authoritative contract
this module implements. Re-exported by ``dossier_contract`` for
backward-compatible callers.
"""

from __future__ import annotations

from typing import Literal

from scout.dossiers.normalization import canonical_normalize, normalize_subject_for_regex
from scout.dossiers.regex import ascii_fold, compile_portable_regex

ProhibitionMode = Literal["exact_phrase", "normalized_phrase", "regex"]

# The only regex flag letters the contract permits, each authored at most
# once (see docs/regex-grammar.md). Callers that go through resolve_dossier
# already have this enforced structurally by the summary schema's flags
# enum; prohibition_matches validates it independently as a boundary
# defense for any caller that constructs a prohibition without going
# through schema validation first.
_ALLOWED_REGEX_FLAG_CHARS: frozenset[str] = frozenset("ims")


def prohibition_matches(mode: ProhibitionMode, pattern: str, flags: str, text: str) -> bool:
    """Evaluate a single dossier prohibition's matcher against a piece of text.

    ``exact_phrase``      — case-sensitive literal substring of the original text.
    ``normalized_phrase``  — literal substring after canonical normalization of
                              both the pattern and the text.
    ``regex``               — portable-regex search (not full match) on the
                              line-normalized original text, honoring only the
                              authored i/m/s flags.
    """
    if mode == "exact_phrase":
        return pattern in text
    if mode == "normalized_phrase":
        return canonical_normalize(pattern) in canonical_normalize(text)
    if mode == "regex":
        flag_set = set(flags)
        if len(flag_set) != len(flags):
            raise ValueError(f"prohibition regex flags contain a duplicate letter: {flags!r}")
        unknown_flags = flag_set - _ALLOWED_REGEX_FLAG_CHARS
        if unknown_flags:
            raise ValueError(
                f"prohibition regex flags {flags!r} contain unsupported letters "
                f"{sorted(unknown_flags)!r} — only i, m, s are permitted"
            )
        compiled = compile_portable_regex(
            pattern, i="i" in flag_set, m="m" in flag_set, s="s" in flag_set
        )
        subject = normalize_subject_for_regex(text)
        if compiled.fold_subject:
            subject = ascii_fold(subject)
        return compiled.pattern.search(subject) is not None
    raise ValueError(f"unknown prohibition mode: {mode!r}")


__all__ = [
    "ProhibitionMode",
    "prohibition_matches",
]
