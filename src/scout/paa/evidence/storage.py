"""Low-level storage primitives for Phase 1 evidence bundles: JSON artifact
loading, content hashing, and read-only queries against the production
database a bundle's evidence is drawn from.

Extracted from paa/evidence/bundle.py, which composes these into
create_bundle/create_reference_bundle (write a bundle directory) and
verify_bundle (read and cross-check one). Re-exported by ``evidence_bundle``
for backward-compatible callers.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scout.paa.audit.runner import REPORT_SCHEMA_VERSION


class BundleError(ValueError):
    """Raised when evidence is absent, inconsistent, or tampered with."""


@dataclass(frozen=True, slots=True)
class BundleResult:
    path: Path
    manifest: dict[str, Any]


def load_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError(f"invalid JSON artifact {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise BundleError(f"JSON artifact {path} must be an object")
    return dict(loaded)


def load_json_array(path: Path) -> list[Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError(f"invalid JSON artifact {path}: {exc}") from exc
    if not isinstance(loaded, list):
        raise BundleError(f"JSON artifact {path} must be an array")
    return list(loaded)


def hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def open_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def fetch_one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise BundleError("referenced production record does not exist")
    return dict(row)


def qualifying_block(report: dict[str, Any], block_id: int) -> dict[str, Any]:
    if report.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise BundleError(f"unsupported audit report schema {report.get('schema_version')!r}")
    for block in report.get("gate_blocks", {}).get("qualifying", []):
        if block.get("id") == block_id:
            return dict(block)
    raise BundleError("selected block is not a qualifying production deterministic gate block")


__all__ = [
    "BundleError",
    "BundleResult",
    "fetch_one",
    "hash_file",
    "load_json",
    "load_json_array",
    "open_readonly",
    "qualifying_block",
]
