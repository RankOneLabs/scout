"""Tests for prohibition_matching.py: the dossier prohibition matcher
dispatch.

The full manifest-driven corpus (dossier-source's conformance/v1
prohibition-vectors.json) exercises prohibition_matches against real
producer vectors via tests/test_dossier_conformance.py. This file covers
the flags boundary the function validates itself, independent of the
schema enum callers going through resolve_dossier already satisfy.
"""

from __future__ import annotations

import pytest

from scout.dossiers.prohibitions import prohibition_matches


def test_prohibition_matches_rejects_unknown_flag_letter() -> None:
    with pytest.raises(ValueError, match="unsupported letters"):
        prohibition_matches("regex", "foo", "x", "foo")


def test_prohibition_matches_rejects_duplicate_flag_letter() -> None:
    with pytest.raises(ValueError, match="duplicate letter"):
        prohibition_matches("regex", "foo", "ii", "foo")
