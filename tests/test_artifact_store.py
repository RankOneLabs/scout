from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scout.cli.analysis import project_study_index
from scout.grading.artifacts import (
    ArtifactBundle,
    ArtifactLineage,
    ArtifactProcess,
    EnvironmentIdentity,
    ProcessId,
    RetainedArtifact,
    TransformKind,
    decode_bundle,
    digest_artifact,
)
from scout.result import Err, Ok
from scout.storage.schema import LATEST_SCHEMA_VERSION
from scout.storage.state import StateManager


@pytest.fixture
def bundle() -> ArtifactBundle:
    contents = (b"synthetic input", b"synthetic config", b"pinned environment", b"output\x00\xff")
    artifacts = tuple(
        RetainedArtifact(digest=digest_artifact(value), content=value) for value in contents
    )
    lineage = ArtifactLineage(
        kind=TransformKind("test.transform"),
        inputs=(artifacts[0].digest,),
        process=ArtifactProcess(
            id=ProcessId("test.producer"),
            version="1",
            config_digest=artifacts[1].digest,
            environment=EnvironmentIdentity(artifacts[2].digest),
        ),
        outputs=(artifacts[3].digest,),
    )
    return ArtifactBundle(artifacts=artifacts, lineages=(lineage,))


def test_binary_bundle_survives_json_boundary(bundle: ArtifactBundle) -> None:
    assert decode_bundle(bundle.model_dump_json().encode()) == Ok(bundle)


def test_store_round_trip_is_idempotent_and_self_contained(bundle: ArtifactBundle) -> None:
    with StateManager(":memory:") as source, StateManager(":memory:") as restored:
        assert source.artifacts.import_bundle(bundle) == Ok(None)
        exported = source.artifacts.export_bundle()
        assert isinstance(exported, Ok)
        assert restored.artifacts.import_bundle(exported.value) == Ok(None)
        assert restored.artifacts.import_bundle(exported.value) == Ok(None)
        assert restored.artifacts.export_bundle() == exported


def test_legacy_lineage_identity_survives_export_import_and_index(bundle: ArtifactBundle) -> None:
    lineage = bundle.lineages[0]
    # Recreate a retained row written by the original untagged v1 encoder,
    # without using the new writer to manufacture the compatibility fixture.
    legacy = (
        '{"kind":"test.transform","inputs":["'
        + lineage.inputs[0]
        + '"],"process":{"id":"test.producer","version":"1","config_digest":"'
        + lineage.process.config_digest
        + '","environment":"'
        + lineage.process.environment
        + '"},"outputs":["'
        + lineage.outputs[0]
        + '"]}'
    ).encode("utf-8")
    legacy_digest = digest_artifact(legacy)
    with StateManager(":memory:") as source, StateManager(":memory:") as restored:
        with source.db.transaction():
            for artifact in (
                *bundle.artifacts,
                RetainedArtifact(digest=legacy_digest, content=legacy),
            ):
                source.conn.execute(
                    "INSERT INTO analysis_artifacts(digest, content) VALUES (?, ?)",
                    (artifact.digest, artifact.content),
                )
            source.conn.execute("INSERT INTO analysis_lineage(digest) VALUES (?)", (legacy_digest,))
        exported = source.artifacts.export_bundle()
        assert isinstance(exported, Ok)
        parsed = decode_bundle(exported.value.model_dump_json().encode())
        assert isinstance(parsed, Ok)
        assert restored.artifacts.import_bundle(parsed.value) == Ok(None)
        assert restored.artifacts.import_bundle(parsed.value) == Ok(None)
        assert restored.artifacts.export_bundle() == exported
        assert restored.artifacts.get(legacy_digest) == Ok(legacy)
        assert [row[0] for row in restored.conn.execute("SELECT digest FROM analysis_lineage")] == [
            legacy_digest
        ]
        index = project_study_index(parsed.value)
        assert isinstance(index, Ok)
        assert index.value.entries[0].lineage_digest == legacy_digest


@pytest.mark.parametrize("missing", [0, 1, 2, 3])
def test_missing_input_config_environment_or_output_refuses_entire_import(
    bundle: ArtifactBundle,
    missing: int,
) -> None:
    incomplete = bundle.model_copy(
        update={
            "artifacts": tuple(
                value for index, value in enumerate(bundle.artifacts) if index != missing
            ),
        }
    )
    with StateManager(":memory:") as state:
        assert isinstance(state.artifacts.import_bundle(incomplete), Err)
        assert state.conn.execute("SELECT count(*) FROM analysis_artifacts").fetchone()[0] == 0


def test_tampered_bytes_are_rejected_before_writing(bundle: ArtifactBundle) -> None:
    corrupt = RetainedArtifact(digest=bundle.artifacts[0].digest, content=b"altered")
    invalid = bundle.model_copy(update={"artifacts": (corrupt, *bundle.artifacts[1:])})
    with StateManager(":memory:") as state:
        assert isinstance(state.artifacts.import_bundle(invalid), Err)
        assert state.conn.execute("SELECT count(*) FROM analysis_artifacts").fetchone()[0] == 0


def test_database_failure_rolls_back_partial_import(bundle: ArtifactBundle) -> None:
    with StateManager(":memory:") as state:
        state.conn.execute("""CREATE TRIGGER fail_lineage BEFORE INSERT ON analysis_lineage
                            BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END""")
        assert isinstance(state.artifacts.import_bundle(bundle), Err)
        assert state.conn.execute("SELECT count(*) FROM analysis_artifacts").fetchone()[0] == 0


def test_failed_import_composes_as_savepoint_without_rolling_back_outer_work(
    bundle: ArtifactBundle,
) -> None:
    with StateManager(":memory:") as state:
        state.conn.execute("""CREATE TRIGGER fail_lineage BEFORE INSERT ON analysis_lineage
                            BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END""")
        with state.db.begin_immediate():
            assert isinstance(state.artifacts.put(b"outer retained observation"), Ok)
            assert isinstance(state.artifacts.import_bundle(bundle), Err)
        assert state.conn.execute("SELECT count(*) FROM analysis_artifacts").fetchone()[0] == 1


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE analysis_artifacts SET content = x'00'",
        "DELETE FROM analysis_artifacts",
        "INSERT OR REPLACE INTO analysis_artifacts(digest, content) "
        "SELECT digest, x'00' FROM analysis_artifacts",
        "UPDATE analysis_lineage SET digest = digest",
        "DELETE FROM analysis_lineage",
        "INSERT OR REPLACE INTO analysis_lineage SELECT * FROM analysis_lineage",
    ],
)
def test_database_enforces_immutability(bundle: ArtifactBundle, statement: str) -> None:
    with StateManager(":memory:") as state:
        assert state.artifacts.import_bundle(bundle) == Ok(None)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"), state.db.transaction():
            state.conn.execute(statement)


def test_corrupted_stored_bytes_fail_export(bundle: ArtifactBundle) -> None:
    with StateManager(":memory:") as state:
        assert state.artifacts.import_bundle(bundle) == Ok(None)
        # Simulate on-disk damage, not an allowed application mutation.
        with state.db.transaction():
            state.conn.execute("DROP TRIGGER analysis_artifacts_no_update")
            state.conn.execute(
                "UPDATE analysis_artifacts SET content = x'00' WHERE digest = ?",
                (bundle.artifacts[0].digest,),
            )
        assert isinstance(state.artifacts.export_bundle(), Err)


def test_sqlite_backup_preserves_artifacts_and_lineage(
    bundle: ArtifactBundle, tmp_path: Path
) -> None:
    backup = tmp_path / "backup.db"
    with StateManager(":memory:") as source:
        assert source.artifacts.import_bundle(bundle) == Ok(None)
        with sqlite3.connect(backup) as destination:
            source.conn.backup(destination)
        with StateManager(str(backup)) as restored:
            assert restored.artifacts.export_bundle() == source.artifacts.export_bundle()


def test_v37_upgrade_is_additive_and_matches_bootstrap(tmp_path: Path) -> None:
    # Build a v37-shaped database by omitting only the new objects from bootstrap.
    path = tmp_path / "v37.db"
    with StateManager(str(path)) as before, before.db.transaction():
        before.conn.execute("DROP TABLE analysis_lineage")
        before.conn.execute("DROP TABLE analysis_artifacts")
        before.conn.execute("PRAGMA user_version = 37")
        before.conn.execute("INSERT INTO posts(platform, platform_msg_id) VALUES ('test', 'kept')")
    with StateManager(str(path)) as upgraded, StateManager(":memory:") as fresh:
        assert upgraded.conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
        assert upgraded.conn.execute("SELECT platform_msg_id FROM posts").fetchone()[0] == "kept"
        query = (
            "SELECT type, name, sql FROM sqlite_master WHERE name LIKE 'analysis_%' ORDER BY name"
        )
        assert [tuple(row) for row in upgraded.conn.execute(query)] == [
            tuple(row) for row in fresh.conn.execute(query)
        ]


@pytest.mark.parametrize("mode", ["read", "write"])
def test_export_borrows_outer_transaction_without_ending_it(
    bundle: ArtifactBundle, mode: str
) -> None:
    with StateManager(":memory:") as state:
        assert state.artifacts.import_bundle(bundle) == Ok(None)
        expected = state.artifacts.export_bundle()
        context = state.db.read_transaction() if mode == "read" else state.db.begin_immediate()
        with context:
            assert state.artifacts.export_bundle() == expected
            assert state.conn.in_transaction
            assert bool(state.conn.execute("PRAGMA query_only").fetchone()[0]) == (mode == "read")


def test_export_does_not_commit_an_unmanaged_caller_transaction(bundle: ArtifactBundle) -> None:
    with StateManager(":memory:") as state:
        assert state.artifacts.import_bundle(bundle) == Ok(None)
        state.conn.execute("BEGIN")
        try:
            assert isinstance(state.artifacts.export_bundle(), Ok)
            assert state.conn.in_transaction
        finally:
            state.conn.rollback()
