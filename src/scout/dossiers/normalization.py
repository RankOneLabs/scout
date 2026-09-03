"""Unicode normalization for the dossier-source dossier contract.

Ported from the upstream producer implementation (pinned producer commit
44a70aa86d470e99c6315126ffdad5e1640d3f1c):
  packages/core/src/unicode/normalize.ts -> canonical_normalize / normalize_subject_for_regex

See docs/dossier-contract.md and docs/unicode-normalization.md in
the producer repository for the authoritative contract this module implements.
Re-exported by ``dossier_contract`` for backward-compatible callers.
"""

from __future__ import annotations

import re
import unicodedata

from scout.dossiers.unicode_casefold import full_case_fold, is_unicode_white_space

_CRLF_CR_RE = re.compile(r"\r\n|\r")
# Not a raw string: \u2028/\u2029 must be resolved to real code points by
# Python's own string-literal parser, since `re`'s pattern language has no
# \uXXXX escape of its own (unlike JavaScript's). Explicit escapes here
# (rather than the literal, near-invisible U+2028/U+2029 characters) keep
# the pattern's intent legible and robust across editors/diff tooling.
_LINE_ENDING_RE = re.compile("\r\n|\r|\u2028|\u2029")


def normalize_subject_for_regex(text: str) -> str:
    """Line-normalize a subject for portable-regex matching only.

    CRLF, bare CR, U+2028, and U+2029 all become LF. No NFKC, no case
    folding, no whitespace collapsing.
    """
    return _LINE_ENDING_RE.sub("\n", text)


def canonical_normalize(text: str) -> str:
    """Canonical phrase normalization: CRLF/CR->LF, NFKC, full Unicode 15.0.0
    case fold, collapse maximal White_Space runs to one U+0020, trim.

    Used for ``normalized_phrase`` prohibition matching and safe/forbidden
    phrasing comparisons. Order is fixed per the dossier-source contract.
    """
    line_normalized = _CRLF_CR_RE.sub("\n", text)
    nfkc = unicodedata.normalize("NFKC", line_normalized)
    folded = full_case_fold(nfkc)

    collapsed_chars: list[str] = []
    in_whitespace_run = False
    for ch in folded:
        if is_unicode_white_space(ord(ch)):
            if not in_whitespace_run:
                collapsed_chars.append(" ")
                in_whitespace_run = True
        else:
            collapsed_chars.append(ch)
            in_whitespace_run = False

    return "".join(collapsed_chars).strip(" ")


__all__ = [
    "canonical_normalize",
    "normalize_subject_for_regex",
]
