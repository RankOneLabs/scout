"""Reference-bundle redaction primitives: replace one prohibited free-text
value with a typed hash/length (or omission) marker, and find every such
marker embedded in a document.

Extracted from paa/evidence/bundle.py, which uses these to build
publication-safe reference bundles (create_reference_bundle) and to
cross-check a bundle's declared redactions against its artifacts
(verify_bundle). Re-exported by ``evidence_bundle`` for backward-compatible
callers.
"""

from __future__ import annotations

import hashlib
from typing import Any

# Schema-2 "reference" bundles carry redaction metadata a schema-1 runtime
# motion bundle never needs — real evidence bound to an approved motion is
# shown only to operators, never published, so it stays exactly as
# create_bundle has always produced it (schema 1, empty redactions.json).
REFERENCE_REDACTION_SCHEMA = "reference-redaction/v1"

_REDACTED_MARKER_KEY = "__redacted__"


def _redaction_marker(*, kind: str, sha256: str | None, length: int | None) -> dict[str, Any]:
    return {_REDACTED_MARKER_KEY: True, "kind": kind, "sha256": sha256, "length": length}


def redact_value(
    value: object,
    redactions: list[dict[str, Any]],
    *,
    artifact: str,
    path: str,
    omit: bool = False,
) -> dict[str, Any] | None:
    """Replace one prohibited free-text value with a typed hash/length (or
    omission) marker and append the transformation to *redactions*.

    A ``None`` source value is passed through untouched — there is
    nothing to protect and nothing to log. Every other value is recorded
    exactly once, in the same shape as its marker minus the boolean flag,
    so ``redactions.json`` and the in-place marker can be cross-checked
    field-for-field by ``verify_bundle`` without re-deriving either side
    from the other.
    """
    if value is None:
        return None
    if omit:
        marker = _redaction_marker(kind="omitted", sha256=None, length=None)
    else:
        text = str(value)
        marker = _redaction_marker(
            kind="hash_length",
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            length=len(text),
        )
    record = {k: v for k, v in marker.items() if k != _REDACTED_MARKER_KEY}
    redactions.append({"artifact": artifact, "path": path, **record})
    return marker


def find_redaction_markers(value: Any, *, path: str = "") -> list[tuple[str, dict[str, Any]]]:
    """Every redaction marker embedded anywhere in *value*, paired with its
    dotted/indexed path from the document root — the same path shape
    ``redact_value`` records in ``redactions.json``."""
    found: list[tuple[str, dict[str, Any]]] = []
    if isinstance(value, dict):
        if value.get(_REDACTED_MARKER_KEY) is True:
            found.append((path, value))
            return found
        for key in sorted(value):
            found.extend(find_redaction_markers(value[key], path=f"{path}.{key}" if path else key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(find_redaction_markers(item, path=f"{path}[{index}]"))
    return found


__all__ = [
    "REFERENCE_REDACTION_SCHEMA",
    "find_redaction_markers",
    "redact_value",
]
