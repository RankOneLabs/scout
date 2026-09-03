"""Tests for portable_regex.py: the portable regex grammar and compiler.

The full manifest-driven corpus (dossier-source's conformance/v1
prohibition-vectors.json and manifest.json fixtures) exercises this module
against real producer vectors via tests/test_dossier_conformance.py — see
docs/dossier-contract.md's "Running the shared conformance corpus" section.
This file keeps only the small, explicitly labeled set of Python-engine-
specific edge regressions whose value is independent of that corpus: they
exercise Python/JS regex-engine divergences the manifest doesn't and can't
express.
"""

from __future__ import annotations

from scout.dossiers.regex import ascii_fold, compile_portable_regex

# ---------------------------------------------------------------------------
# JS/Python engine-parity edge case: non-multiline `$` must not match before
# a trailing newline the way Python's bare `$` normally does.
# ---------------------------------------------------------------------------


def test_end_anchor_without_multiline_is_strict_like_js() -> None:
    compiled = compile_portable_regex("bar$")
    assert compiled.pattern.search("bar") is not None
    assert compiled.pattern.search("bar\n") is None


def test_end_anchor_with_multiline_matches_before_newline() -> None:
    compiled = compile_portable_regex("bar$", m=True)
    assert compiled.pattern.search("bar\nbaz") is not None


def test_ascii_fold_only_folds_ascii_letters() -> None:
    assert ascii_fold("ABCxyz123") == "abcxyz123"
    assert ascii_fold("STRASSE") == "strasse"
    assert ascii_fold("É") == "É"  # non-ASCII É untouched
