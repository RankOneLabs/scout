"""Tests for dossier_normalization.py: canonical Unicode normalization and
the narrower line-ending normalization used only for regex matching.

The full manifest-driven corpus (dossier-source's conformance/v1
normalization-vectors.json) runs canonical_normalize against real producer
vectors via tests/test_dossier_conformance.py. normalize_subject_for_regex
is not exercised by that corpus (the vectors target canonical_normalize
specifically), so it gets its own direct unit coverage here.
"""

from __future__ import annotations

from scout.dossiers.normalization import normalize_subject_for_regex


def test_normalize_subject_for_regex_only_touches_line_endings() -> None:
    assert normalize_subject_for_regex("a\r\nb\rc\u2028d\u2029e") == "a\nb\nc\nd\ne"
    # No NFKC/casefold/whitespace-collapse: unlike canonical_normalize.
    assert normalize_subject_for_regex("A B") == "A B"
