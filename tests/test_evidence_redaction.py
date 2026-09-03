"""Tests for evidence_redaction.py: the reference-bundle redaction marker
scheme (redact_value / find_redaction_markers).

tests/test_paa_reference_evidence.py and tests/test_evidence_bundle.py cover
these functions end-to-end through create_reference_bundle/verify_bundle;
this file covers the two functions directly.
"""

from __future__ import annotations

import hashlib

from scout.paa.evidence.redaction import find_redaction_markers, redact_value


def test_redact_value_passes_through_none_untouched() -> None:
    redactions: list[dict[str, object]] = []
    marker = redact_value(None, redactions, artifact="source.json", path="url")
    assert marker is None
    assert redactions == []


def test_redact_value_records_hash_and_length_marker() -> None:
    redactions: list[dict[str, object]] = []
    marker = redact_value("secret text", redactions, artifact="source.json", path="content")
    expected_hash = hashlib.sha256(b"secret text").hexdigest()
    assert marker == {
        "__redacted__": True,
        "kind": "hash_length",
        "sha256": expected_hash,
        "length": len("secret text"),
    }
    assert redactions == [
        {
            "artifact": "source.json",
            "path": "content",
            "kind": "hash_length",
            "sha256": expected_hash,
            "length": len("secret text"),
        }
    ]


def test_redact_value_omit_records_omitted_marker_with_no_hash() -> None:
    redactions: list[dict[str, object]] = []
    marker = redact_value("secret text", redactions, artifact="source.json", path="x", omit=True)
    assert marker == {"__redacted__": True, "kind": "omitted", "sha256": None, "length": None}
    assert redactions == [
        {"artifact": "source.json", "path": "x", "kind": "omitted", "sha256": None, "length": None}
    ]


def test_redact_value_coerces_non_string_values_to_text() -> None:
    redactions: list[dict[str, object]] = []
    marker = redact_value(42, redactions, artifact="grade.json", path="score")
    assert marker is not None
    assert marker["length"] == len("42")


def test_find_redaction_markers_locates_nested_dict_and_list_paths() -> None:
    document = {
        "a": {"__redacted__": True, "kind": "omitted", "sha256": None, "length": None},
        "b": [
            "plain",
            {"__redacted__": True, "kind": "hash_length", "sha256": "abc", "length": 3},
        ],
        "c": "untouched",
    }
    found = find_redaction_markers(document)
    paths = {path for path, _marker in found}
    assert paths == {"a", "b[1]"}


def test_find_redaction_markers_returns_empty_for_document_with_no_markers() -> None:
    assert find_redaction_markers({"a": 1, "b": ["x", "y"]}) == []
