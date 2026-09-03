"""Tests for evidence_bundle_storage.py: JSON artifact loading, content
hashing, and read-only sqlite access for evidence bundles.

tests/test_evidence_bundle.py and tests/test_paa_reference_evidence.py cover
these through create_bundle/create_reference_bundle/verify_bundle; this file
covers the storage primitives directly.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from scout.paa.audit.runner import REPORT_SCHEMA_VERSION
from scout.paa.evidence.storage import (
    BundleError,
    fetch_one,
    hash_file,
    load_json,
    load_json_array,
    open_readonly,
    qualifying_block,
)


def test_load_json_parses_a_well_formed_object(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    path.write_text(json.dumps({"a": 1}))
    assert load_json(path) == {"a": 1}


def test_load_json_rejects_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    path.write_text("{not json")
    with pytest.raises(BundleError, match="invalid JSON artifact"):
        load_json(path)


def test_load_json_rejects_a_non_object_document(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    path.write_text(json.dumps([1, 2, 3]))
    with pytest.raises(BundleError, match="must be an object"):
        load_json(path)


def test_load_json_array_parses_a_well_formed_array(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    path.write_text(json.dumps([1, 2, 3]))
    assert load_json_array(path) == [1, 2, 3]


def test_load_json_array_rejects_a_non_array_document(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    path.write_text(json.dumps({"a": 1}))
    with pytest.raises(BundleError, match="must be an array"):
        load_json_array(path)


def test_hash_file_returns_sha256_hex_digest(tmp_path: Path) -> None:
    path = tmp_path / "artifact.bin"
    path.write_bytes(b"hello world")
    assert hash_file(path) == hashlib.sha256(b"hello world").hexdigest()


def test_open_readonly_connection_rejects_writes(tmp_path: Path) -> None:
    db_path = tmp_path / "scout.db"
    with sqlite3.connect(db_path) as setup_conn:
        setup_conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
        setup_conn.execute("INSERT INTO t(v) VALUES ('x')")
        setup_conn.commit()

    conn = open_readonly(db_path)
    try:
        row = conn.execute("SELECT v FROM t WHERE id=1").fetchone()
        assert row["v"] == "x"
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t(v) VALUES ('y')")
    finally:
        conn.close()


def test_fetch_one_returns_matching_row_as_dict(tmp_path: Path) -> None:
    db_path = tmp_path / "scout.db"
    with sqlite3.connect(db_path) as setup_conn:
        setup_conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
        setup_conn.execute("INSERT INTO t(v) VALUES ('x')")
        setup_conn.commit()

    conn = open_readonly(db_path)
    try:
        assert fetch_one(conn, "SELECT * FROM t WHERE id=?", (1,)) == {"id": 1, "v": "x"}
    finally:
        conn.close()


def test_fetch_one_raises_bundle_error_when_no_row_matches(tmp_path: Path) -> None:
    db_path = tmp_path / "scout.db"
    with sqlite3.connect(db_path) as setup_conn:
        setup_conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
        setup_conn.commit()

    conn = open_readonly(db_path)
    try:
        with pytest.raises(BundleError, match="does not exist"):
            fetch_one(conn, "SELECT * FROM t WHERE id=?", (99,))
    finally:
        conn.close()


def _report(**gate_blocks_qualifying: object) -> dict[str, object]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "gate_blocks": {"qualifying": [gate_blocks_qualifying]},
    }


def test_qualifying_block_returns_the_matching_block() -> None:
    report = _report(id=7, evaluation_id=3)
    assert qualifying_block(report, 7) == {"id": 7, "evaluation_id": 3}


def test_qualifying_block_rejects_unsupported_report_schema() -> None:
    report = {"schema_version": REPORT_SCHEMA_VERSION - 1, "gate_blocks": {"qualifying": []}}
    with pytest.raises(BundleError, match="unsupported audit report schema"):
        qualifying_block(report, 7)


def test_qualifying_block_rejects_a_block_id_not_present() -> None:
    report = _report(id=7, evaluation_id=3)
    with pytest.raises(BundleError, match="not a qualifying"):
        qualifying_block(report, 99)
