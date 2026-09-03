from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scout.paa.audit.runner import REPORT_SCHEMA_VERSION
from scout.paa.evidence.bundle import BundleError, create_bundle, verify_bundle
from scout.storage.state import StateManager


def _report(block_id: int, evaluation_id: int) -> dict[str, object]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "gate_blocks": {"qualifying": [{"id": block_id, "evaluation_id": evaluation_id}]},
        "dossiers": [
            {
                "summary_id": "gateway-dossier",
                "revision": "a" * 40,
                "approval": {"revision": "a" * 40, "reference": "APR-123"},
            }
        ],
        "criteria": {},
        "window": {
            "from": "2026-01-01T00:00:00+00:00",
            "to": "2026-01-15T00:00:00+00:00",
            "interval": "[from,to)",
        },
        "counts": {"production_live_scans": 1, "evaluations": 1, "excluded_scans": {}},
    }


def _records(db: Path) -> tuple[int, int]:
    now = datetime.now(UTC).isoformat()
    with StateManager(str(db)) as state:
        state.conn.execute(
            "INSERT INTO scans(started_at, environment, run_kind) VALUES (?, 'production', 'live')",
            (now,),
        )
        state.conn.execute(
            "INSERT INTO posts(platform, platform_msg_id, author_id, content) "
            "VALUES ('bluesky', 'p1', 'author', 'source text')"
        )
        state.conn.execute(
            """INSERT INTO evaluations(post_id, relevant, score, scan_id, created_at,
               surface_status, dossier_summary_id, dossier_revision)
               VALUES (1, 1, 1, 1, ?, 'gate_blocked', 'gateway-dossier', ?)""",
            (now, "a" * 40),
        )
        cursor = state.conn.execute(
            """INSERT INTO gate_blocks(reason_code, offending_text, scan_id, post_id,
               evaluation_id, dossier_summary_id, dossier_revision, created_at)
               VALUES ('safe_phrasing', 'unsupported text', 1, 1, 1, 'gateway-dossier', ?, ?)""",
            ("a" * 40, now),
        )
        state.commit()
        return int(cursor.lastrowid), 1


def test_bundle_hashes_every_artifact_and_rejects_tampering(tmp_path: Path) -> None:
    db = tmp_path / "scout.db"
    block_id, evaluation_id = _records(db)
    report, before = tmp_path / "report.json", tmp_path / "before.txt"
    report.write_text(json.dumps(_report(block_id, evaluation_id)))
    before.write_text("attributable offline replay")
    bundle = create_bundle(
        report_path=report,
        db_path=db,
        gate_block_id=block_id,
        before_path=before,
        destination=tmp_path / "bundle",
        code_revision="b" * 40,
        model_id="model",
        prompt_revision="prompt",
    )
    assert verify_bundle(bundle.path, db)["ok"]
    (bundle.path / "source.txt").write_text("tampered")
    with pytest.raises(BundleError, match="artifact hash mismatch"):
        verify_bundle(bundle.path)


def test_bundle_refuses_existing_destination_without_force(tmp_path: Path) -> None:
    db = tmp_path / "scout.db"
    block_id, evaluation_id = _records(db)
    report, before, destination = (
        tmp_path / "report.json",
        tmp_path / "before.txt",
        tmp_path / "bundle",
    )
    report.write_text(json.dumps(_report(block_id, evaluation_id)))
    before.write_text("before")
    destination.mkdir()
    with pytest.raises(BundleError, match="refusing to replace"):
        create_bundle(
            report_path=report,
            db_path=db,
            gate_block_id=block_id,
            before_path=before,
            destination=destination,
            code_revision="b" * 40,
            model_id="model",
            prompt_revision="prompt",
        )


def test_bundle_rejects_symlinked_artifact(tmp_path: Path) -> None:
    db = tmp_path / "scout.db"
    block_id, evaluation_id = _records(db)
    report, before = tmp_path / "report.json", tmp_path / "before.txt"
    report.write_text(json.dumps(_report(block_id, evaluation_id)))
    before.write_text("attributable offline replay")
    bundle = create_bundle(
        report_path=report,
        db_path=db,
        gate_block_id=block_id,
        before_path=before,
        destination=tmp_path / "bundle",
        code_revision="b" * 40,
        model_id="model",
        prompt_revision="prompt",
    )
    source = bundle.path / "source.txt"
    external = tmp_path / "external-source.txt"
    external.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(external)

    with pytest.raises(BundleError, match="must not contain symlinks"):
        verify_bundle(bundle.path)
