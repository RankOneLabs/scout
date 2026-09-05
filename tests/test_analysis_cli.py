from __future__ import annotations

import argparse
import sqlite3
import stat
from pathlib import Path

import pytest

from scout.cli.analysis import add_analysis_parser, project_study_index, run_analysis
from scout.grading.artifacts import ProducerEnvironment, digest_artifact
from scout.grading.studies import InventorySelection, inventory_evidence, verify_inventory_replay
from scout.result import Err, Ok
from scout.storage.db import read_only_connection
from scout.storage.state import StateManager


def arguments(*values: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_analysis_parser(parser.add_subparsers(), "unused.db")
    return parser.parse_args(["analysis", *values])


@pytest.mark.parametrize("command", ["preview", "export", "index", "verify"])
def test_read_commands_never_create_missing_database(tmp_path: Path, command: str) -> None:
    path = tmp_path / "absent?#.db"
    values = [command, "--db-path", str(path)]
    if command == "preview":
        values.extend(["--project", "synthetic", "--dossier-root", str(tmp_path)])
    if command == "export":
        values.extend(["--out", str(tmp_path / "bundle.json")])
    assert isinstance(run_analysis(arguments(*values)), Err)
    assert not path.exists()


def test_preview_does_not_migrate_or_change_source(tmp_path: Path) -> None:
    path = tmp_path / "scout?#.db"
    with StateManager(str(path)) as state, state.db.transaction():
        state.conn.execute("DROP TABLE analysis_lineage")
        state.conn.execute("DROP TABLE analysis_artifacts")
        state.conn.execute("PRAGMA user_version = 37")
    before = path.read_bytes()
    result = run_analysis(
        arguments(
            "preview",
            "--db-path",
            str(path),
            "--project",
            "synthetic",
            "--dossier-root",
            str(tmp_path),
        )
    )
    assert isinstance(result, Ok)
    assert path.read_bytes() == before


def test_read_connection_rejects_writes(tmp_path: Path) -> None:
    path = tmp_path / "source.db"
    with StateManager(str(path)):
        pass
    with read_only_connection(str(path)) as conn, pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM posts")


def test_export_is_private_atomic_and_cannot_overwrite_source(tmp_path: Path) -> None:
    path = tmp_path / "source.db"
    with StateManager(str(path)) as state:
        assert isinstance(state.artifacts.put(b"synthetic observation"), Ok)
    destination = tmp_path / "bundle.json"
    assert isinstance(
        run_analysis(arguments("export", "--db-path", str(path), "--out", str(destination))), Ok
    )
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    before = path.read_bytes()
    assert isinstance(
        run_analysis(arguments("export", "--db-path", str(path), "--out", str(path))), Err
    )
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".scout-export-*"))


def test_import_and_verify_retain_source_only_observations(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "restored.db"
    bundle = tmp_path / "bundle.json"
    with StateManager(str(source)) as state:
        assert isinstance(state.artifacts.put(b"synthetic observation"), Ok)
    assert isinstance(
        run_analysis(arguments("export", "--db-path", str(source), "--out", str(bundle))), Ok
    )
    assert isinstance(
        run_analysis(arguments("import", "--db-path", str(destination), "--bundle", str(bundle))),
        Ok,
    )
    assert isinstance(run_analysis(arguments("verify", "--db-path", str(destination))), Ok)


def test_inventory_links_existing_run_without_promoting_invalid_evidence(tmp_path: Path) -> None:
    path = tmp_path / "source.db"
    report = tmp_path / "report.json"
    report.write_bytes(b'{"synthetic":true}')
    with StateManager(str(path)) as state:
        with state.db.transaction():
            state.conn.execute(
                "INSERT INTO experiment_runs(id, name, status, candidate_config, created_at) "
                "VALUES (1, 'synthetic run', 'queued', '{}', '2026-01-01')"
            )
        with state.db.read_transaction():
            result = inventory_evidence(
                state.conn,
                InventorySelection(
                    study="synthetic study",
                    files=(str(report),),
                    experiment_run_ids=(1,),
                    usability="invalid",
                    reason="Known invalid configuration",
                ),
                b"synthetic environment",
            )
        assert isinstance(result, Ok)
        assert verify_inventory_replay(result.value) == Ok(1)
        assert state.artifacts.import_bundle(result.value) == Ok(None)
        index = project_study_index(result.value)
        assert isinstance(index, Ok)
        assert index.value.entries[0].experiment_run_ids == (1,)
        assert index.value.entries[0].usability == "invalid"
        assert (
            state.conn.execute("SELECT status FROM experiment_runs WHERE id = 1").fetchone()[0]
            == "queued"
        )


def test_inventory_replays_after_original_report_changes(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_bytes(b"synthetic original report")
    with StateManager(":memory:") as state:
        with state.db.read_transaction():
            result = inventory_evidence(
                state.conn,
                InventorySelection(study="test", files=(str(report),), experiment_run_ids=()),
                b"environment",
            )
        assert isinstance(result, Ok)
        report.write_bytes(b"different later report")
        assert verify_inventory_replay(result.value) == Ok(1)


def test_inventory_cli_records_explicit_files_and_environment(tmp_path: Path) -> None:
    path = tmp_path / "source.db"
    with StateManager(str(path)):
        pass
    report = tmp_path / "report.json"
    report.write_bytes(b"synthetic report")
    environment = tmp_path / "environment.json"
    environment.write_text(
        ProducerEnvironment(
            code_revision="a" * 40,
            dependency_lock_digest=digest_artifact(b"synthetic lock"),
            python_version="3.12.3",
        ).model_dump_json()
    )
    result = run_analysis(
        arguments(
            "inventory",
            "--db-path",
            str(path),
            "--study",
            "test",
            "--file",
            str(report),
            "--environment",
            str(environment),
        )
    )
    assert isinstance(result, Ok)
    assert isinstance(run_analysis(arguments("verify", "--db-path", str(path))), Ok)
