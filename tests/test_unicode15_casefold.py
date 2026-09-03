"""Tests for the vendored Unicode 15.0.0 case-fold and White_Space data."""

from __future__ import annotations

import pytest

from scout.dossiers.unicode_casefold import (
    CASEFOLD_UNICODE_VERSION,
    WHITE_SPACE_UNICODE_VERSION,
    full_case_fold,
    is_unicode_white_space,
)


def test_vendored_version_is_15_0_0() -> None:
    assert CASEFOLD_UNICODE_VERSION == "15.0.0"
    assert WHITE_SPACE_UNICODE_VERSION == "15.0.0"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("MiXeD CaSe", "mixed case"),
        ("straße", "strasse"),
        ("STRASSE", "strasse"),
        ("λόγος", "λόγοσ"),
        ("ΛΌΓΟΣ", "λόγοσ"),
        ("plain", "plain"),
    ],
)
def test_full_case_fold_vectors(text: str, expected: str) -> None:
    assert full_case_fold(text) == expected


@pytest.mark.parametrize(
    ("code_point", "expected"),
    [
        (0x20, True),  # ASCII space
        (0x9, True),  # tab
        (0xA0, True),  # NBSP
        (0x2028, True),  # line separator
        (0x3000, True),  # ideographic space
        (0x41, False),  # 'A'
        (0x0, False),
    ],
)
def test_is_unicode_white_space(code_point: int, expected: bool) -> None:
    assert is_unicode_white_space(code_point) is expected
