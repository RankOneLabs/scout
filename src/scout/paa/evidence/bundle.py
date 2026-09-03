"""Tamper-evident same-source Phase 1 evidence bundles.

This module is the stable facade for evidence bundle generation and
verification: it implements create_bundle, create_reference_bundle, and
verify_bundle directly, and re-exports the cohesive sub-concerns extracted
into their own modules — ``evidence_bundle_storage`` (JSON/hash/sqlite
primitives and the BundleError/BundleResult types) and
``evidence_redaction`` (the reference-bundle redaction marker scheme) — so
every existing ``from evidence_bundle import ...`` caller keeps working
unchanged.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from scout.paa.audit.runner import REPORT_SCHEMA_VERSION, canonical_json, render_markdown
from scout.paa.evidence.redaction import (
    REFERENCE_REDACTION_SCHEMA,
    find_redaction_markers,
    redact_value,
)
from scout.paa.evidence.storage import (
    BundleError,
    BundleResult,
    fetch_one,
    hash_file,
    load_json,
    load_json_array,
    open_readonly,
    qualifying_block,
)

__all__ = [
    "REFERENCE_BUNDLE_SCHEMA_VERSION",
    "REFERENCE_REDACTION_SCHEMA",
    "BundleError",
    "BundleResult",
    "create_bundle",
    "create_reference_bundle",
    "find_redaction_markers",
    "redact_value",
    "verify_bundle",
]

# Schema-2 "reference" bundles carry redaction metadata a schema-1 runtime
# motion bundle never needs — real evidence bound to an approved motion is
# shown only to operators, never published, so it stays exactly as
# create_bundle has always produced it (schema 1, empty redactions.json).
REFERENCE_BUNDLE_SCHEMA_VERSION = 2


def create_bundle(
    *,
    report_path: Path,
    db_path: Path,
    gate_block_id: int,
    before_path: Path,
    destination: Path,
    code_revision: str,
    model_id: str,
    prompt_revision: str,
    force: bool = False,
) -> BundleResult:
    """Package only persisted, attributable evidence into a new directory."""
    report = load_json(report_path)
    selected = qualifying_block(report, gate_block_id)
    if not before_path.is_file():
        raise BundleError(
            "before artifact must be an existing attributable output or offline replay"
        )
    if not all(value.strip() for value in (code_revision, model_id, prompt_revision)):
        raise BundleError("code revision, model id, and prompt revision are required")
    if destination.exists():
        if not force:
            raise BundleError(f"refusing to replace existing bundle destination: {destination}")
        if not destination.is_dir():
            raise BundleError("--force destination must be a directory")
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=False)
    try:
        conn = open_readonly(db_path)
        try:
            block = fetch_one(
                conn,
                """SELECT gb.*, e.id AS evaluation_id, e.scan_id AS evaluation_scan_id,
                          e.surface_status, e.dossier_summary_id AS evaluation_summary_id,
                          e.dossier_revision AS evaluation_revision, p.id AS source_post_id,
                          p.platform, p.platform_msg_id, p.author_id, p.content, p.url,
                          p.parent_id, p.parent_author_id, p.parent_text, p.parent_url,
                          s.environment, s.run_kind
                     FROM gate_blocks gb JOIN evaluations e ON e.id=gb.evaluation_id
                     JOIN posts p ON p.id=gb.post_id JOIN scans s ON s.id=e.scan_id
                    WHERE gb.id=?""",
                (gate_block_id,),
            )
            if block["environment"] != "production" or block["run_kind"] != "live":
                raise BundleError("selected gate block is not from a production/live scan")
            if (
                block["surface_status"] != "gate_blocked"
                or not str(block.get("offending_text") or "").strip()
            ):
                raise BundleError("selected gate block is not durable gate-blocked evidence")
            if block["evaluation_id"] != selected.get("evaluation_id"):
                raise BundleError("report and database selected block evaluation disagree")
            evaluation = fetch_one(
                conn, "SELECT * FROM evaluations WHERE id=?", (block["evaluation_id"],)
            )
            grades = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM grades WHERE evaluation_id=? ORDER BY id",
                    (block["evaluation_id"],),
                )
            ]
            approval = next(
                (
                    entry.get("approval")
                    for entry in report.get("dossiers", [])
                    if entry.get("summary_id")
                    == (block.get("dossier_summary_id") or block.get("evaluation_summary_id"))
                    and entry.get("revision")
                    == (block.get("dossier_revision") or block.get("evaluation_revision"))
                ),
                None,
            )
            if not isinstance(approval, dict) or not str(approval.get("reference") or "").strip():
                raise BundleError(
                    "selected evidence has no external approval reference for its exact revision"
                )
            source = {
                key: block[key]
                for key in ("source_post_id", "platform", "platform_msg_id", "author_id", "url")
            }
            parent = {
                key: block[key]
                for key in ("parent_id", "parent_author_id", "parent_text", "parent_url")
            }
            source_bytes = str(block.get("content") or "").encode("utf-8")
            if not source_bytes or not str(block.get("author_id") or "").strip():
                raise BundleError("source document or source author is absent")
            artifacts: dict[str, bytes] = {
                "audit.json": canonical_json(report).encode(),
                "audit.md": render_markdown(report).encode(),
                "source.txt": source_bytes,
                "parent.json": canonical_json(parent).encode(),
                "before.bin": before_path.read_bytes(),
                "after-evaluation.json": canonical_json(evaluation).encode(),
                "grade.json": canonical_json({"grades": grades}).encode(),
                "gate-block.json": canonical_json(block).encode(),
                "redactions.json": canonical_json([]).encode(),
            }
            for name, contents in artifacts.items():
                (destination / name).write_bytes(contents)
            manifest = {
                "schema_version": 1,
                "audit_schema_version": REPORT_SCHEMA_VERSION,
                "force_replacement": force,
                "artifacts": {name: hash_file(destination / name) for name in sorted(artifacts)},
                "identities": {
                    "source": source,
                    "parent": parent,
                    "scan": {
                        "id": block["evaluation_scan_id"],
                        "environment": block["environment"],
                        "run_kind": block["run_kind"],
                    },
                    "evaluation": {
                        "id": evaluation["id"],
                        "dossier_summary_id": evaluation.get("dossier_summary_id"),
                        "dossier_revision": evaluation.get("dossier_revision"),
                    },
                    "grade_ids": [grade["id"] for grade in grades],
                    "block": {"id": block["id"], "reason_code": block["reason_code"]},
                    "assessment_ids": [],
                    "model": {"id": model_id},
                    "prompt": {"revision": prompt_revision},
                    "code": {"revision": code_revision},
                    "dossier": {
                        "summary_id": block.get("dossier_summary_id")
                        or block.get("evaluation_summary_id"),
                        "revision": block.get("dossier_revision")
                        or block.get("evaluation_revision"),
                    },
                    "approval": approval,
                },
            }
            (destination / "manifest.json").write_text(canonical_json(manifest))
            (destination / "manifest.sha256").write_text(
                hash_file(destination / "manifest.json") + "  manifest.json\n"
            )
            return BundleResult(destination, manifest)
        finally:
            conn.close()
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


_GRADE_FREE_TEXT_FIELDS: tuple[str, ...] = (
    # rejection_reason/comment_issue are legacy pre-v3 columns save_grade
    # never writes and no schema-v3 grade ever populates — nothing routes
    # sensitive content through them, so they are not redaction targets.
    # posture_should_have_been is a fixed four-value enum (answer/engage/
    # ask/abstain), not free text, so it is likewise excluded.
    "failure_note",
    "factual_offending_claim",
    "factual_contradicting_evidence",
    "context_missing_input",
    "implication_implied_claim",
    "implication_missing_support",
)


def create_reference_bundle(
    *,
    report_path: Path,
    db_path: Path,
    gate_block_id: int,
    before_path: Path,
    destination: Path,
    code_revision: str,
    model_id: str,
    prompt_revision: str,
    force: bool = False,
) -> BundleResult:
    """Like create_bundle, but publication-safe: reference-redaction/v1 is
    applied to every prohibited data class (source and parent text, author
    names and ids, platform ids, URLs, prompt/correction text, and
    free-form grade detail) before any byte is hashed or written, and every
    transformation is recorded in redactions.json. Produces a schema-2
    bundle — see verify_bundle for what schema 2 additionally checks.
    """
    report = load_json(report_path)
    selected = qualifying_block(report, gate_block_id)
    if not before_path.is_file():
        raise BundleError(
            "before artifact must be an existing attributable output or offline replay"
        )
    if not all(value.strip() for value in (code_revision, model_id, prompt_revision)):
        raise BundleError("code revision, model id, and prompt revision are required")
    if destination.exists():
        if not force:
            raise BundleError(f"refusing to replace existing bundle destination: {destination}")
        if not destination.is_dir():
            raise BundleError("--force destination must be a directory")
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=False)
    try:
        conn = open_readonly(db_path)
        try:
            block = fetch_one(
                conn,
                """SELECT gb.*, e.id AS evaluation_id, e.scan_id AS evaluation_scan_id,
                          e.surface_status, e.dossier_summary_id AS evaluation_summary_id,
                          e.dossier_revision AS evaluation_revision,
                          p.id AS source_post_id,
                          p.platform, p.platform_msg_id, p.author_id, p.author_name,
                          p.content, p.url,
                          p.parent_id, p.parent_author_id, p.parent_author_name,
                          p.parent_text, p.parent_url,
                          s.environment, s.run_kind
                     FROM gate_blocks gb JOIN evaluations e ON e.id=gb.evaluation_id
                     JOIN posts p ON p.id=gb.post_id JOIN scans s ON s.id=e.scan_id
                    WHERE gb.id=?""",
                (gate_block_id,),
            )
            if block["environment"] != "production" or block["run_kind"] != "live":
                raise BundleError("selected gate block is not from a production/live scan")
            if (
                block["surface_status"] != "gate_blocked"
                or not str(block.get("offending_text") or "").strip()
            ):
                raise BundleError("selected gate block is not durable gate-blocked evidence")
            if block["evaluation_id"] != selected.get("evaluation_id"):
                raise BundleError("report and database selected block evaluation disagree")
            evaluation = fetch_one(
                conn, "SELECT * FROM evaluations WHERE id=?", (block["evaluation_id"],)
            )
            grades = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM grades WHERE evaluation_id=? ORDER BY id",
                    (block["evaluation_id"],),
                )
            ]
            approval = next(
                (
                    entry.get("approval")
                    for entry in report.get("dossiers", [])
                    if entry.get("summary_id")
                    == (block.get("dossier_summary_id") or block.get("evaluation_summary_id"))
                    and entry.get("revision")
                    == (block.get("dossier_revision") or block.get("evaluation_revision"))
                ),
                None,
            )
            if not isinstance(approval, dict) or not str(approval.get("reference") or "").strip():
                raise BundleError(
                    "selected evidence has no external approval reference for its exact revision"
                )
            source = {
                key: block[key]
                for key in (
                    "source_post_id", "platform", "platform_msg_id", "author_id",
                    "author_name", "url",
                )
            }
            parent = {
                key: block[key]
                for key in (
                    "parent_id", "parent_author_id", "parent_author_name",
                    "parent_text", "parent_url",
                )
            }
            source_bytes = str(block.get("content") or "").encode("utf-8")
            if not source_bytes or not str(block.get("author_id") or "").strip():
                raise BundleError("source document or source author is absent")

            redactions: list[dict[str, Any]] = []

            def redact(value: object, *, artifact: str, path: str) -> dict[str, Any] | None:
                return redact_value(value, redactions, artifact=artifact, path=path)

            redacted_source = {
                "source_post_id": source["source_post_id"],
                "platform": source["platform"],
                "platform_msg_id": redact(
                    source["platform_msg_id"], artifact="source.json", path="platform_msg_id"
                ),
                "author_id": redact(source["author_id"], artifact="source.json", path="author_id"),
                "author_name": redact(
                    source["author_name"], artifact="source.json", path="author_name"
                ),
                "url": redact(source["url"], artifact="source.json", path="url"),
                "content": redact(block.get("content"), artifact="source.json", path="content"),
            }
            redacted_parent = {
                "parent_id": redact(parent["parent_id"], artifact="parent.json", path="parent_id"),
                "parent_author_id": redact(
                    parent["parent_author_id"], artifact="parent.json", path="parent_author_id"
                ),
                "parent_author_name": redact(
                    parent["parent_author_name"],
                    artifact="parent.json", path="parent_author_name",
                ),
                "parent_text": redact(
                    parent["parent_text"], artifact="parent.json", path="parent_text"
                ),
                "parent_url": redact(
                    parent["parent_url"], artifact="parent.json", path="parent_url"
                ),
            }
            redacted_evaluation = {
                key: value for key, value in evaluation.items() if key != "reason"
            }
            redacted_evaluation["reason"] = redact(
                evaluation.get("reason"), artifact="after-evaluation.json", path="reason"
            )
            redacted_block = {
                key: block[key]
                for key in (
                    "id", "reason_code", "segment_index", "project_key",
                    "dossier_summary_id", "dossier_revision", "scan_id", "post_id",
                    "evaluation_id", "created_at",
                )
            }
            redacted_block["offending_text"] = redact(
                block.get("offending_text"), artifact="gate-block.json", path="offending_text"
            )
            redacted_block["context"] = redact(
                block.get("context"), artifact="gate-block.json", path="context"
            )
            redacted_grades = []
            for index, grade in enumerate(grades):
                redacted_grade = {
                    key: value for key, value in grade.items() if key not in _GRADE_FREE_TEXT_FIELDS
                }
                for field in _GRADE_FREE_TEXT_FIELDS:
                    redacted_grade[field] = redact(
                        grade.get(field), artifact="grade.json", path=f"grades[{index}].{field}"
                    )
                redacted_grades.append(redacted_grade)
            identities_source = {
                "source_post_id": source["source_post_id"],
                "platform": source["platform"],
                "platform_msg_id": redact(
                    source["platform_msg_id"],
                    artifact="manifest.json", path="identities.source.platform_msg_id",
                ),
                "author_id": redact(
                    source["author_id"],
                    artifact="manifest.json", path="identities.source.author_id",
                ),
                "author_name": redact(
                    source["author_name"],
                    artifact="manifest.json", path="identities.source.author_name",
                ),
                "url": redact(
                    source["url"], artifact="manifest.json", path="identities.source.url"
                ),
            }
            identities_parent = {
                "parent_id": redact(
                    parent["parent_id"],
                    artifact="manifest.json", path="identities.parent.parent_id",
                ),
                "parent_author_id": redact(
                    parent["parent_author_id"],
                    artifact="manifest.json", path="identities.parent.parent_author_id",
                ),
                "parent_author_name": redact(
                    parent["parent_author_name"],
                    artifact="manifest.json", path="identities.parent.parent_author_name",
                ),
                "parent_text": redact(
                    parent["parent_text"],
                    artifact="manifest.json", path="identities.parent.parent_text",
                ),
                "parent_url": redact(
                    parent["parent_url"],
                    artifact="manifest.json", path="identities.parent.parent_url",
                ),
            }
            if not redactions:
                raise BundleError(
                    "reference bundle produced zero redactions — the selected evidence has "
                    "no prohibited free-text content to protect, which is not a valid "
                    "reference case"
                )

            artifacts: dict[str, bytes] = {
                "audit.json": canonical_json(report).encode(),
                "audit.md": render_markdown(report).encode(),
                "source.json": canonical_json(redacted_source).encode(),
                "parent.json": canonical_json(redacted_parent).encode(),
                "before.bin": before_path.read_bytes(),
                "after-evaluation.json": canonical_json(redacted_evaluation).encode(),
                "grade.json": canonical_json({"grades": redacted_grades}).encode(),
                "gate-block.json": canonical_json(redacted_block).encode(),
                "redactions.json": canonical_json(
                    sorted(redactions, key=lambda r: (r["artifact"], r["path"]))
                ).encode(),
            }
            for name, contents in artifacts.items():
                (destination / name).write_bytes(contents)
            manifest = {
                "schema_version": REFERENCE_BUNDLE_SCHEMA_VERSION,
                "redaction_schema": REFERENCE_REDACTION_SCHEMA,
                "audit_schema_version": REPORT_SCHEMA_VERSION,
                "force_replacement": force,
                "artifacts": {name: hash_file(destination / name) for name in sorted(artifacts)},
                "identities": {
                    "source": identities_source,
                    "parent": identities_parent,
                    "scan": {
                        "id": block["evaluation_scan_id"],
                        "environment": block["environment"],
                        "run_kind": block["run_kind"],
                    },
                    "evaluation": {
                        "id": evaluation["id"],
                        "dossier_summary_id": evaluation.get("dossier_summary_id"),
                        "dossier_revision": evaluation.get("dossier_revision"),
                    },
                    "grade_ids": [grade["id"] for grade in grades],
                    "block": {"id": block["id"], "reason_code": block["reason_code"]},
                    "assessment_ids": [],
                    "model": {"id": model_id},
                    "prompt": {"revision": prompt_revision},
                    "code": {"revision": code_revision},
                    "dossier": {
                        "summary_id": block.get("dossier_summary_id")
                        or block.get("evaluation_summary_id"),
                        "revision": block.get("dossier_revision")
                        or block.get("evaluation_revision"),
                    },
                    "approval": approval,
                },
            }
            (destination / "manifest.json").write_text(canonical_json(manifest))
            (destination / "manifest.sha256").write_text(
                hash_file(destination / "manifest.json") + "  manifest.json\n"
            )
            return BundleResult(destination, manifest)
        finally:
            conn.close()
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def verify_bundle(path: Path, db_path: Path | None = None) -> dict[str, Any]:
    """Read and verify a bundle without writing to it or its source database.

    Accepts both a schema-1 runtime motion bundle (create_bundle) and a
    schema-2 reference bundle (create_reference_bundle); schema 2 adds the
    redaction-metadata cross-check below. db_path is only ever consulted
    for schema 1 — a reference bundle's redacted content has nothing left
    to look up against the source database it came from.
    """
    if not path.is_dir():
        raise BundleError("bundle path is not a directory")
    manifest_path, sidecar = path / "manifest.json", path / "manifest.sha256"
    manifest = load_json(manifest_path)
    schema_version = manifest.get("schema_version")
    if (
        schema_version not in (1, REFERENCE_BUNDLE_SCHEMA_VERSION)
        or manifest.get("audit_schema_version") != REPORT_SCHEMA_VERSION
    ):
        raise BundleError("unsupported bundle or audit report schema")
    expected_sidecar = hash_file(manifest_path) + "  manifest.json\n"
    if not sidecar.is_file() or sidecar.read_text() != expected_sidecar:
        raise BundleError("manifest sidecar hash mismatch")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise BundleError("manifest has no declared artifacts")
    expected_names = set(artifacts) | {"manifest.json", "manifest.sha256"}
    bundle_entries = list(path.iterdir())
    if any(item.is_symlink() for item in bundle_entries):
        raise BundleError("bundle must not contain symlinks")
    if any(not item.is_file() for item in bundle_entries):
        raise BundleError("bundle must not contain directories or special entries")
    actual_names = {item.name for item in bundle_entries if item.is_file()}
    if actual_names != expected_names:
        raise BundleError("bundle has missing or undeclared artifacts")
    for name, expected in artifacts.items():
        artifact = path / str(name)
        if not artifact.is_file() or hash_file(artifact) != expected:
            raise BundleError(f"artifact hash mismatch: {name}")
    audit = load_json(path / "audit.json")
    if audit.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise BundleError("audit artifact has unsupported schema")
    block = load_json(path / "gate-block.json")
    evaluation = load_json(path / "after-evaluation.json")
    identities = manifest.get("identities")
    if not isinstance(identities, dict):
        raise BundleError("manifest identities are missing")
    if block.get("evaluation_id") != evaluation.get("id") or identities.get("block", {}).get(
        "id"
    ) != block.get("id"):
        raise BundleError("bundle cross-artifact evaluation or block identity mismatch")
    dossier = identities.get("dossier", {})
    approval = identities.get("approval", {})
    if not approval or approval.get("revision") != dossier.get("revision"):
        raise BundleError("approval does not bind the dossier revision")

    if schema_version == REFERENCE_BUNDLE_SCHEMA_VERSION:
        if manifest.get("redaction_schema") != REFERENCE_REDACTION_SCHEMA:
            raise BundleError("reference bundle is missing its redaction schema identity")
        redactions = load_json_array(path / "redactions.json")
        if not redactions:
            raise BundleError("reference bundle declares zero redactions")
        declared_keys = [
            (str(r.get("artifact")), str(r.get("path")), str(r.get("kind")),
             r.get("sha256"), r.get("length"))
            for r in redactions
        ]
        declared = set(declared_keys)
        if len(declared_keys) != len(declared):
            duplicates = sorted(
                {key for key in declared_keys if declared_keys.count(key) > 1}
            )
            raise BundleError(f"redactions.json declares duplicate entries: {duplicates}")
        found: set[tuple[str, str, str, Any, Any]] = set()
        for name in sorted(artifacts):
            if name in ("before.bin", "audit.md", "redactions.json"):
                continue
            for json_path, marker in find_redaction_markers(load_json(path / name)):
                found.add(
                    (name, json_path, str(marker.get("kind")),
                     marker.get("sha256"), marker.get("length"))
                )
        for json_path, marker in find_redaction_markers(manifest):
            found.add(
                ("manifest.json", json_path, str(marker.get("kind")),
                 marker.get("sha256"), marker.get("length"))
            )
        if found != declared:
            raise BundleError(
                "reference bundle redaction metadata mismatch: "
                f"missing={sorted(found - declared)} extra={sorted(declared - found)}"
            )
        return {"ok": True, "manifest": manifest}

    if db_path is not None:
        conn = open_readonly(db_path)
        try:
            fetch_one(conn, "SELECT id FROM gate_blocks WHERE id=?", (block["id"],))
            fetch_one(conn, "SELECT id FROM evaluations WHERE id=?", (evaluation["id"],))
        finally:
            conn.close()
    return {"ok": True, "manifest": manifest}
