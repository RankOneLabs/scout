from __future__ import annotations

import argparse
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from scout.cli.analysis import add_analysis_parser, project_study_index, run_analysis
from scout.grading.artifacts import (
    ArtifactBundle,
    ArtifactError,
    ArtifactLineage,
    ArtifactProcess,
    EnvironmentIdentity,
    ProcessId,
    ProducerEnvironment,
    RetainedArtifact,
    TransformKind,
    digest_artifact,
    validate_bundle,
)
from scout.grading.studies import (
    EvidenceObservations,
    InventorySelection,
    assemble_study_evidence,
    inventory_evidence,
    verify_inventory_replay,
)
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


@pytest.mark.parametrize("destination_kind", ["existing_export", "source_database"])
def test_export_overwrite_refusal_is_distinct_and_keeps_existing_bytes(
    tmp_path: Path, destination_kind: str
) -> None:
    source = tmp_path / "source.db"
    with StateManager(str(source)):
        pass
    destination = source if destination_kind == "source_database" else tmp_path / "bundle.json"
    if destination_kind == "existing_export":
        destination.write_bytes(b"previous export")
    before = destination.read_bytes()
    result = run_analysis(arguments("export", "--db-path", str(source), "--out", str(destination)))
    assert result == Err(
        ArtifactError("analysis", None, "Refusing to replace an existing export path")
    )
    assert destination.read_bytes() == before
    assert not list(tmp_path.glob(".scout-export-*"))


def test_export_invalid_parent_keeps_generic_io_error(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    with StateManager(str(source)):
        pass
    result = run_analysis(
        arguments("export", "--db-path", str(source), "--out", str(tmp_path / "absent" / "export"))
    )
    assert result == Err(
        ArtifactError("analysis", None, "Analysis IO failed; verify paths and schema")
    )


@pytest.mark.parametrize("fail_directory_sync", [False, True])
def test_export_syncs_published_directory_and_reports_sync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_directory_sync: bool
) -> None:
    source = tmp_path / "source.db"
    with StateManager(str(source)):
        pass
    destination = tmp_path / "bundle.json"
    synced: list[str] = []
    sync_file = os.fsync

    def sync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            assert destination.is_file()
            assert not list(tmp_path.glob(".scout-export-*"))
            synced.append("directory")
            if fail_directory_sync:
                raise OSError("synthetic directory sync failure")
        else:
            assert not destination.exists()
            synced.append("file")
        sync_file(descriptor)

    monkeypatch.setattr("scout.cli.analysis.os.fsync", sync)
    result = run_analysis(arguments("export", "--db-path", str(source), "--out", str(destination)))
    assert synced == ["file", "directory"]
    if fail_directory_sync:
        assert result == Err(
            ArtifactError("analysis", None, "Analysis IO failed; verify paths and schema")
        )
    else:
        assert isinstance(result, Ok)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert ArtifactBundle.model_validate_json(destination.read_bytes()).artifacts == ()


@pytest.mark.parametrize("has_unreferenced_input", [False, True])
def test_import_rejects_inventory_without_evidence_file_inputs(
    tmp_path: Path, has_unreferenced_input: bool
) -> None:
    selection = InventorySelection(study="empty inventory", files=(), experiment_run_ids=())
    observed = EvidenceObservations(files=(), runs=())
    observations = observed.model_dump_json().encode()
    config = selection.model_dump_json().encode()
    environment = b"synthetic environment"
    output = assemble_study_evidence(observed, selection).model_dump_json().encode()
    inputs = (
        (digest_artifact(environment), digest_artifact(observations))
        if has_unreferenced_input
        else (digest_artifact(observations),)
    )
    bundle = ArtifactBundle(
        artifacts=tuple(
            RetainedArtifact(digest=digest_artifact(content), content=content)
            for content in (observations, config, environment, output)
        ),
        lineages=(
            ArtifactLineage(
                kind=TransformKind("scout.evidence.inventory"),
                inputs=inputs,
                process=ArtifactProcess(
                    id=ProcessId("scout.evidence.inventory"),
                    version="1",
                    config_digest=digest_artifact(config),
                    environment=EnvironmentIdentity(digest_artifact(environment)),
                ),
                outputs=(digest_artifact(output),),
            ),
        ),
    )
    assert isinstance(validate_bundle(bundle), Ok)
    assert isinstance(verify_inventory_replay(bundle), Err)
    bundle_path = tmp_path / "inventory.json"
    bundle_path.write_text(bundle.model_dump_json())
    destination = tmp_path / "must-not-create.db"
    result = run_analysis(
        arguments("import", "--db-path", str(destination), "--bundle", str(bundle_path))
    )
    assert isinstance(result, Err)
    assert not destination.exists()


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
