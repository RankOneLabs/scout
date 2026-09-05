from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import fields, make_dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import get_type_hints

import pytest
from pydantic import create_model

from scout.config import GradeRecord
from scout.dossiers.resolver import DossierResolution, DossierSummary, ResolutionMetadata
from scout.grading.artifacts import ArtifactBundle, decode_bundle
from scout.grading.corpus_export import export_grading_corpus
from scout.grading.feedback import GradePopulationRow
from scout.grading.snapshots import (
    POPULATION_WIRE_V1,
    CorpusSelection,
    FrozenGradeInput,
    FrozenGradePopulation,
    preview_corpus,
    read_grade_population,
    select_corpus,
    verify_snapshot_replay,
)
from scout.grading.snapshots import (
    build_snapshot_bundle as try_build_snapshot_bundle,
)
from scout.grading.wire import encode_wire_v1
from scout.result import Err, Ok
from scout.storage.state import StateManager


def build_snapshot_bundle(
    population: FrozenGradePopulation, selection: CorpusSelection, environment: bytes
) -> ArtifactBundle:
    result = try_build_snapshot_bundle(population, selection, environment)
    assert isinstance(result, Ok), result
    return result.value


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[StateManager]:
    def resolve(
        repository: Path, revision: str, project_key: str, summary_id: str
    ) -> DossierResolution:
        return DossierResolution(
            summary=DossierSummary(
                project_key=project_key, last_reviewed=date(2026, 1, 1), reviewer="synthetic"
            ),
            metadata=ResolutionMetadata(
                project_key=project_key,
                summary_id=summary_id,
                revision=revision,
                path="synthetic.yaml",
            ),
            known_gaps=(),
        )

    monkeypatch.setattr("scout.grading.snapshots.resolve_dossier", resolve)
    with StateManager(str(tmp_path / "scout.db")) as value:
        with value.db.transaction():
            value.conn.execute(
                "INSERT INTO posts(id, platform, platform_msg_id, content) "
                "VALUES (1, 'bluesky', 'synthetic-post', 'A graded source post')"
            )
            value.conn.execute(
                "INSERT INTO evaluations(id, post_id, relevant, score, surface_status, "
                "project_key, posture, dossier_revision, dossier_summary_id) "
                "VALUES (1, 1, 1, 0.9, 'surfaced', 'synthetic', 'answer', ?, 'summary')",
                ("b" * 40,),
            )
        value.save_grade(
            GradeRecord(
                post_id=1,
                evaluation_id=1,
                source="web",
                graded_at=datetime(2020, 1, 1, tzinfo=UTC),
                relevance_judgment="correct",
                action_judgment="accept",
                schema_version=3,
            )
        )
        yield value


def capture(state: StateManager) -> FrozenGradePopulation:
    with state.db.read_transaction():
        result = read_grade_population(state.conn, Path("/unused-synthetic-dossier"))
    assert isinstance(result, Ok)
    return result.value


def test_old_grade_is_not_excluded_by_prompt_lookback(state: StateManager) -> None:
    snapshot = select_corpus(capture(state), CorpusSelection(project_key="synthetic"))
    assert len(snapshot.members) == 1


@pytest.mark.parametrize("original_decision", [0, 1])
def test_recorded_decision_preserves_integer_encoding(
    state: StateManager, original_decision: int
) -> None:
    with state.db.transaction():
        state.conn.execute("UPDATE evaluations SET relevant = ?", (original_decision,))
    population = capture(state)
    evaluation = population.items[0].evaluation
    assert evaluation is not None
    assert type(evaluation.relevant) is int
    assert evaluation.relevant == original_decision


@pytest.mark.parametrize("original_decision", [0, 1])
def test_legacy_boolean_population_encoding_round_trips_and_replays(
    state: StateManager, original_decision: int
) -> None:
    with state.db.transaction():
        state.conn.execute("UPDATE evaluations SET relevant = ?", (original_decision,))
    # Before RecordedEvaluation, v1 capture encoded EvaluationRow.relevant as
    # a JSON boolean. Preserving that spelling is necessary for member digests.
    boolean_json = "true" if original_decision else "false"
    legacy_json = (
        capture(state)
        .model_dump_json()
        .replace(f'"relevant":{original_decision}', f'"relevant":{boolean_json}')
    )
    assert f'"relevant":{boolean_json}' in legacy_json
    population = FrozenGradePopulation.model_validate_json(legacy_json)
    assert population.model_dump_json() == legacy_json
    bundle = build_snapshot_bundle(
        population, CorpusSelection(project_key="synthetic"), b"environment"
    )
    assert verify_snapshot_replay(bundle) == Ok(1)


@pytest.mark.parametrize("original_decision", [2, -1])
@pytest.mark.parametrize("project_key", ["synthetic", "unrelated"])
def test_invalid_evaluation_is_retained_and_excluded_without_blocking_valid_members(
    state: StateManager, original_decision: int, project_key: str
) -> None:
    with state.db.transaction():
        state.conn.execute(
            "INSERT INTO posts(id, platform, platform_msg_id, content) "
            "VALUES (2, 'bluesky', 'invalid-decision-post', 'Another graded post')"
        )
        state.conn.execute(
            "INSERT INTO evaluations(id, post_id, relevant, score, surface_status, "
            "project_key, posture, dossier_revision, dossier_summary_id) "
            "VALUES (2, 2, 1, 0.9, 'surfaced', ?, 'answer', ?, 'summary')",
            (project_key, "b" * 40),
        )
    state.save_grade(
        GradeRecord(
            post_id=2,
            evaluation_id=2,
            source="web",
            graded_at=datetime(2020, 1, 1, tzinfo=UTC),
            relevance_judgment="correct",
            action_judgment="accept",
            schema_version=3,
        )
    )
    with state.db.transaction():
        state.conn.execute("UPDATE evaluations SET relevant = ? WHERE id = 2", (original_decision,))

    population = capture(state)
    recorded = population.items[1]
    assert recorded.evaluation is not None
    assert recorded.evaluation.relevant == original_decision
    assert recorded.grade.evaluation_relevant == original_decision
    selection = CorpusSelection(project_key="synthetic")
    snapshot = select_corpus(population, selection)
    assert [member.evaluation_id for member in snapshot.members] == [1]
    expected_reason = (
        "invalid_original_decision" if project_key == "synthetic" else "outside_project"
    )
    assert [(item.grade_id, item.reason) for item in snapshot.exclusions] == [
        (recorded.grade.grade_id, expected_reason)
    ]
    preview = preview_corpus(snapshot)
    assert (preview.population_count, preview.eligible_count) == (2, 1)

    bundle = build_snapshot_bundle(population, selection, b"environment")
    assert state.artifacts.import_bundle(bundle) == Ok(None)
    exported = state.artifacts.export_bundle()
    assert isinstance(exported, Ok)
    assert verify_snapshot_replay(exported.value) == Ok(1)


def test_snapshot_replays_after_live_post_and_grade_change(state: StateManager) -> None:
    population = capture(state)
    bundle = build_snapshot_bundle(
        population, CorpusSelection(project_key="synthetic"), b"pinned environment"
    )
    assert state.artifacts.import_bundle(bundle) == Ok(None)
    with state.db.transaction():
        state.conn.execute("UPDATE posts SET content = 'later text' WHERE id = 1")
    state.save_grade(
        GradeRecord(
            post_id=1,
            evaluation_id=1,
            source="web",
            graded_at=datetime.now(UTC),
            relevance_judgment="false_positive",
            action_judgment="fail",
            dimensions=["usefulness"],
            failure_note="Synthetic correction: not relevant to the project",
            schema_version=3,
        )
    )
    exported = state.artifacts.export_bundle()
    assert isinstance(exported, Ok)
    assert verify_snapshot_replay(exported.value) == Ok(1)
    assert (
        select_corpus(population, CorpusSelection(project_key="synthetic")).members[0].is_relevant
    )
    assert (
        not select_corpus(capture(state), CorpusSelection(project_key="synthetic"))
        .members[0]
        .is_relevant
    )


def test_repeated_selection_digest_does_not_depend_on_read_timestamp(state: StateManager) -> None:
    first = build_snapshot_bundle(
        capture(state), CorpusSelection(project_key="synthetic"), b"environment"
    )
    second = build_snapshot_bundle(
        capture(state), CorpusSelection(project_key="synthetic"), b"environment"
    )
    assert first.lineages[0].outputs == second.lineages[0].outputs


def test_snapshot_and_pinned_context_round_trip_without_live_database(state: StateManager) -> None:
    bundle = build_snapshot_bundle(
        capture(state), CorpusSelection(project_key="synthetic"), b"environment"
    )
    parsed = decode_bundle(bundle.model_dump_json().encode())
    assert isinstance(parsed, Ok)
    with StateManager(":memory:") as restored:
        assert restored.artifacts.import_bundle(parsed.value) == Ok(None)
        exported = restored.artifacts.export_bundle()
        assert isinstance(exported, Ok)
        assert verify_snapshot_replay(exported.value) == Ok(1)


def test_missing_context_has_explicit_exclusion(state: StateManager) -> None:
    with state.db.transaction():
        state.conn.execute("UPDATE evaluations SET dossier_revision = NULL WHERE id = 1")
    snapshot = select_corpus(capture(state), CorpusSelection(project_key="synthetic"))
    assert snapshot.exclusions[0].reason == "unavailable_pinned_context"


def test_unrecorded_grade_edit_is_not_treated_as_pinned_revision(state: StateManager) -> None:
    with state.db.transaction():
        state.conn.execute("UPDATE grades SET graded_at = '2026-01-01T00:00:00.000Z'")
    snapshot = select_corpus(capture(state), CorpusSelection(project_key="synthetic"))
    assert snapshot.exclusions[0].reason == "revision_mismatch"


def test_read_requires_stable_transaction(state: StateManager) -> None:
    assert isinstance(read_grade_population(state.conn, Path("/unused")), Err)


def test_grading_preservation_export_carries_analysis_store(
    state: StateManager, tmp_path: Path
) -> None:
    bundle = build_snapshot_bundle(
        capture(state), CorpusSelection(project_key="synthetic"), b"environment"
    )
    assert state.artifacts.import_bundle(bundle) == Ok(None)
    destination = tmp_path / "grading-preservation.db"
    result = export_grading_corpus(state.db_path, str(destination))
    assert {table.name for table in result.tables} >= {"analysis_artifacts", "analysis_lineage"}
    # The preservation export isn't a live Scout database. Read the independent
    # artifact tables without bootstrapping/migrating the partial source export.
    import sqlite3

    from scout.storage.artifacts import read_artifact_bundle

    with sqlite3.connect(destination) as restored:
        restored.execute("BEGIN")
        exported = read_artifact_bundle(restored)
        assert isinstance(exported, Ok)
        assert verify_snapshot_replay(exported.value) == Ok(1)


def legacy_bundle() -> ArtifactBundle:
    result = decode_bundle(
        (Path(__file__).parent / "fixtures/grading_artifacts/legacy-snapshot-v1.json").read_bytes()
    )
    assert isinstance(result, Ok)
    return result.value


def test_retained_v1_fixture_is_byte_compatible_and_replays() -> None:
    bundle = legacy_bundle()
    contents = {artifact.digest: artifact.content for artifact in bundle.artifacts}
    original = contents[bundle.lineages[0].inputs[0]]
    population = FrozenGradePopulation.model_validate_json(original)
    assert encode_wire_v1(population, POPULATION_WIRE_V1) == original
    assert verify_snapshot_replay(bundle) == Ok(1)
    with StateManager(":memory:") as restored:
        assert restored.artifacts.import_bundle(bundle) == Ok(None)
        exported = restored.artifacts.export_bundle()
        assert isinstance(exported, Ok)
        assert verify_snapshot_replay(exported.value) == Ok(1)


def test_operational_dataclass_reordering_does_not_break_retained_v1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hints = get_type_hints(GradePopulationRow)
    reordered = list(fields(GradePopulationRow))
    reordered[0], reordered[1] = reordered[1], reordered[0]
    changed_row = make_dataclass(
        "ReorderedGradePopulationRow",
        [(field.name, hints[field.name]) for field in reordered],
        frozen=True,
        slots=True,
    )
    changed_input = create_model(
        "ReorderedInput", __base__=FrozenGradeInput, grade=(changed_row, ...)
    )
    changed_population = create_model(
        "ReorderedPopulation",
        __base__=FrozenGradePopulation,
        items=(tuple[changed_input, ...], ...),
    )
    monkeypatch.setattr("scout.grading.snapshots.FrozenGradePopulation", changed_population)
    assert verify_snapshot_replay(legacy_bundle()) == Ok(1)


def test_repeated_capture_only_adds_small_observation_metadata(state: StateManager) -> None:
    population = capture(state)
    selection = CorpusSelection(project_key="synthetic")
    first = build_snapshot_bundle(population, selection, b"environment")
    later = build_snapshot_bundle(
        population.model_copy(update={"observed_at": "2026-09-06T00:00:00Z"}),
        selection,
        b"environment",
    )
    assert first.lineages == later.lineages
    first_digests = {artifact.digest for artifact in first.artifacts}
    new_artifacts = [
        artifact for artifact in later.artifacts if artifact.digest not in first_digests
    ]
    assert len(new_artifacts) == 1
    assert len(new_artifacts[0].content) < 256
    assert b"scout.population-capture/v1" in new_artifacts[0].content
    assert state.artifacts.import_bundle(first) == Ok(None)
    assert state.artifacts.import_bundle(later) == Ok(None)
    exported = state.artifacts.export_bundle()
    assert isinstance(exported, Ok)
    assert verify_snapshot_replay(exported.value) == Ok(1)


def test_empty_population_is_previewable_but_cannot_be_snapshotted(state: StateManager) -> None:
    empty = capture(state).model_copy(update={"items": ()})
    selection = CorpusSelection(project_key="synthetic")
    assert preview_corpus(select_corpus(empty, selection)).population_count == 0
    assert isinstance(try_build_snapshot_bundle(empty, selection, b"environment"), Err)


def test_nonempty_all_excluded_population_still_replays(state: StateManager) -> None:
    bundle = build_snapshot_bundle(
        capture(state), CorpusSelection(project_key="different-project"), b"environment"
    )
    assert verify_snapshot_replay(bundle) == Ok(1)


@pytest.mark.parametrize("payload", ["not json", "42", "{}"])
def test_corrupt_revision_payload_aborts_capture(state: StateManager, payload: str) -> None:
    with state.db.transaction():
        state.conn.execute("DROP TRIGGER grade_revisions_no_update")
        state.conn.execute("UPDATE grade_revisions SET payload = ?", (payload,))
    with state.db.read_transaction():
        result = read_grade_population(state.conn, Path("/unused"))
    assert isinstance(result, Err)
    assert result.error.entity_id == "1"


def test_missing_recorded_column_returns_error_value(state: StateManager) -> None:
    with state.db.transaction():
        state.conn.execute("ALTER TABLE evaluations DROP COLUMN relevant_to")
    with state.db.read_transaction():
        result = read_grade_population(state.conn, Path("/unused"))
    assert isinstance(result, Err)
    assert result.error.operation == "freeze_grade_input"


def test_invalid_mutable_grade_remains_an_exclusion(state: StateManager) -> None:
    with state.db.transaction():
        state.conn.execute("UPDATE grades SET dimensions = 'not json'")
    snapshot = select_corpus(capture(state), CorpusSelection(project_key="synthetic"))
    assert snapshot.exclusions[0].reason == "shared_contract_invalid"


@pytest.mark.parametrize("replacement_id", ["id", "id + 100"])
def test_grade_revision_cannot_be_replaced_through_either_unique_key(
    state: StateManager, replacement_id: str
) -> None:
    before = tuple(state.conn.execute("SELECT * FROM grade_revisions").fetchone())
    state.conn.execute("PRAGMA recursive_triggers = OFF")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"), state.db.transaction():
        state.conn.execute(
            "INSERT OR REPLACE INTO grade_revisions "
            "(id,grade_id,evaluation_id,revision,schema_version,source,payload,recorded_at) "
            f"SELECT {replacement_id},grade_id,evaluation_id,revision,schema_version,source,"
            "'42',recorded_at FROM grade_revisions"
        )
    assert tuple(state.conn.execute("SELECT * FROM grade_revisions").fetchone()) == before


def test_v38_upgrade_protects_existing_revision_without_rewriting_it(state: StateManager) -> None:
    before = tuple(state.conn.execute("SELECT * FROM grade_revisions").fetchone())
    with state.db.transaction():
        state.conn.execute("DROP TRIGGER grade_revisions_no_replace")
        state.conn.execute("PRAGMA user_version = 38")
    with StateManager(state.db_path) as upgraded:
        assert upgraded.conn.execute("PRAGMA user_version").fetchone()[0] == 39
        assert tuple(upgraded.conn.execute("SELECT * FROM grade_revisions").fetchone()) == before
        assert (
            upgraded.conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='grade_revisions_no_replace'"
            ).fetchone()[0]
            == 1
        )
