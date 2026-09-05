from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from scout.config import GradeRecord
from scout.dossiers.resolver import DossierResolution, DossierSummary, ResolutionMetadata
from scout.grading.artifacts import decode_bundle
from scout.grading.corpus_export import export_grading_corpus
from scout.grading.snapshots import (
    CorpusSelection,
    FrozenGradePopulation,
    build_snapshot_bundle,
    read_grade_population,
    select_corpus,
    verify_snapshot_replay,
)
from scout.result import Err, Ok
from scout.storage.state import StateManager


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
