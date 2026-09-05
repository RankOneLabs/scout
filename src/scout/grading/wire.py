"""Frozen v1 JSON layouts for retained Scout records, not operational models.

Layouts below each producer name the original source fields explicitly. Reading
attributes in that order isolates identity from dataclass/Pydantic field order,
added operational fields, and model_dump_json renderer settings.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class JsonValueWire:
    """An original JSON scalar or arbitrary JSON value (e.g. grade dimensions)."""


@dataclass(frozen=True, slots=True)
class ArrayWire:
    item: WireLayout


@dataclass(frozen=True, slots=True)
class ObjectWire:
    fields: tuple[WireField, ...]


@dataclass(frozen=True, slots=True)
class WireField:
    name: str
    layout: WireLayout


type WireLayout = JsonValueWire | ArrayWire | ObjectWire

JSON_VALUE = JsonValueWire()


def record_wire(names: str, **nested: WireLayout) -> ObjectWire:
    """Declare a frozen ordered source projection; nested overrides are explicit."""
    return ObjectWire(
        tuple(WireField(name, nested.get(name, JSON_VALUE)) for name in names.split())
    )


def _json_v1(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # Match the original v1 renderer, including compact exponent spelling.
        if not math.isfinite(value):
            return "null"
        rendered = repr(value)
        if "e" in rendered:
            if 1e-5 <= abs(value) < 1e16:
                return format(Decimal(rendered), "f")
            mantissa, exponent = rendered.split("e")
            return f"{mantissa}e{int(exponent)}"
        return rendered
    if isinstance(value, date):
        return _json_v1(value.isoformat())
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (tuple, list)):
        return "[" + ",".join(_json_v1(item) for item in value) + "]"
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return (
            "{"
            + ",".join(_json_v1(key) + ":" + _json_v1(item) for key, item in value.items())
            + "}"
        )
    raise ValueError("Unsupported v1 JSON value")


def _render_wire(value: object, layout: WireLayout) -> str:
    if value is None:
        return "null"
    match layout:
        case JsonValueWire():
            return _json_v1(value)
        case ArrayWire(item):
            if not isinstance(value, (tuple, list)):
                raise ValueError("Expected a wire array")
            return "[" + ",".join(_render_wire(child, item) for child in value) + "]"
        case ObjectWire(fields):
            return (
                "{"
                + ",".join(
                    _json_v1(field.name)
                    + ":"
                    + _render_wire(getattr(value, field.name), field.layout)
                    for field in fields
                )
                + "}"
            )


def encode_wire_v1(value: object, layout: WireLayout) -> bytes:
    """Serialization boundary for a validated record and its frozen wire layout."""
    return _render_wire(value, layout).encode("utf-8")
