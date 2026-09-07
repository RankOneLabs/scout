from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest

from scout.grading.wire import JSON_VALUE, encode_wire_v1, record_wire


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, b"null"),
        (True, b"true"),
        (1, b"1"),
        (1.0, b"1.0"),
        (-0.0, b"-0.0"),
        (1e-5, b"0.00001"),
        (1e-6, b"1e-6"),
        (1.23e-7, b"1.23e-7"),
        (1e16, b"1e16"),
        (float("inf"), b"null"),
        (date(2026, 1, 1), b'"2026-01-01"'),
        ('é\n"', '"é\\n\\""'.encode()),
    ],
)
def test_v1_scalar_spelling_is_fixed(value: object, expected: bytes) -> None:
    assert encode_wire_v1(value, JSON_VALUE) == expected


def test_wire_projection_ignores_operational_order_and_extra_fields() -> None:
    @dataclass(frozen=True)
    class OperationalRecord:
        value: int
        extra: str
        label: str

    record = OperationalRecord(value=7, extra="not in the wire contract", label="synthetic")
    assert encode_wire_v1(record, record_wire("label value")) == b'{"label":"synthetic","value":7}'
