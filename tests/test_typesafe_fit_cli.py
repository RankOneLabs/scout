from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from scout.cli.typesafe import run_typesafe
from scout.config import GradeRecord
from scout.dossiers.resolver import DossierResolution, DossierSummary, ResolutionMetadata
from scout.grading.artifacts import digest_artifact
from scout.grading.assistance_types import FrozenPartition, PartitionMember
from scout.grading.snapshots import (
    CorpusSelection,
    CorpusSnapshot,
    build_snapshot_bundle,
    read_grade_population,
)
from scout.result import Ok
from scout.storage.shadow_relevance import ShadowRunWrite
from scout.storage.state import StateManager
from scout.typesafe.compose import DECIDE_REGISTRY, register_fitted_gate
from scout.typesafe.models import Answers
from scout.typesafe.weights import WeightSet


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _args(db_path: Path, task: dict[str, object], out: Path) -> argparse.Namespace:
    return argparse.Namespace(
        typesafe_command="fit",
        db_path=str(db_path),
        task=json.dumps(task),
        catalogue_version="c" * 64,
        out=out,
        c_fp=2.0,
        c_fn=1.0,
    )


@pytest.mark.parametrize("partition", ["all", "heldout"])
def test_non_train_task_refuses_before_database_open(monkeypatch, tmp_path, partition: str) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("database must not be opened")

    monkeypatch.setattr("scout.cli.typesafe.read_only_connection", forbidden)
    task: dict[str, object] = {
        "kind": "relevance",
        "snapshot_digest": "a" * 64,
        "partition": partition,
    }
    if partition == "heldout":
        task["partition_digest"] = "b" * 64
    assert run_typesafe(_args(tmp_path / "missing.db", task, tmp_path / "out")) == 2


def _seed_fit_db(
    path: Path, monkeypatch, tmp_path: Path
) -> tuple[dict[str, object], dict[int, dict[str, object]]]:
    def resolve(_repository, revision, project_key, summary_id):
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
    answers = {
        1: {
            "answers": {"relevance": {"kind": "probability", "probability": 0.1}},
            "request_id": "negative",
            "model": "placeholder/v1",
        },
        2: {
            "answers": {"relevance": {"kind": "probability", "probability": 0.9}},
            "request_id": "positive",
            "model": "placeholder/v1",
        },
    }
    with StateManager(str(path)) as state:
        scan_id = state.start_scan()
        with state.db.transaction():
            for evaluation_id, relevant in ((1, 0), (2, 1), (999, 1)):
                state.conn.execute(
                    "INSERT INTO posts(id, platform, platform_msg_id, content, scan_id) "
                    "VALUES (?, 'bluesky', ?, ?, ?)",
                    (
                        evaluation_id,
                        f"fit-{evaluation_id}",
                        f"independent {evaluation_id}",
                        scan_id,
                    ),
                )
                state.conn.execute(
                    "INSERT INTO evaluations(id, post_id, relevant, score, surface_status, "
                    "project_key, posture, dossier_revision, dossier_summary_id, scan_id) "
                    "VALUES (?, ?, ?, .9, 'surfaced', 'agent-ops', 'answer', ?, 'summary', ?)",
                    (evaluation_id, evaluation_id, relevant, "b" * 40, scan_id),
                )
        for evaluation_id in (1, 2, 999):
            state.save_grade(
                GradeRecord(
                    post_id=evaluation_id,
                    evaluation_id=evaluation_id,
                    source="web",
                    graded_at=datetime(2026, 1, 1, tzinfo=UTC),
                    relevance_judgment="correct",
                    action_judgment="accept",
                    schema_version=3,
                )
            )
        with state.db.read_transaction():
            population = read_grade_population(state.conn, tmp_path)
        assert isinstance(population, Ok)
        built = build_snapshot_bundle(
            population.value, CorpusSelection(project_key="agent-ops"), b"synthetic"
        )
        assert isinstance(built, Ok)
        assert state.artifacts.import_bundle(built.value) == Ok(None)
        snapshot_digest = built.value.lineages[0].outputs[0]
        snapshot = next(
            artifact for artifact in built.value.artifacts if artifact.digest == snapshot_digest
        )
        snapshot_model = CorpusSnapshot.model_validate_json(snapshot.content)
        partition = FrozenPartition(
            snapshot_digest=snapshot_digest,
            members=tuple(
                PartitionMember(
                    evaluation_id=member.evaluation_id,
                    input_digest=member.input_digest,
                    group_id=f"group-{member.evaluation_id}",
                    partition="heldout" if member.evaluation_id == 999 else "train",
                    exposed=False,
                    provenance=(),
                )
                for member in snapshot_model.members
            ),
        )
        partition_bytes = partition.model_dump_json().encode()
        partition_digest = digest_artifact(partition_bytes)
        with state.db.transaction():
            state.conn.execute(
                "INSERT INTO analysis_artifacts(digest, content) VALUES (?, ?)",
                (partition_digest, partition_bytes),
            )
        for evaluation_id in (1, 2):
            state.shadow_relevance.record_shadow_run(
                ShadowRunWrite(
                    scan_id=scan_id,
                    post_id=evaluation_id,
                    evaluation_id=evaluation_id,
                    backend="placeholder",
                    model="fixture",
                    catalogue_id="catalogue",
                    catalogue_version="c" * 64,
                    request_id=f"fit-{evaluation_id}",
                    state={"evaluation_id": evaluation_id},
                    status="ok",
                    answers=answers[evaluation_id],
                    created_at=f"2026-09-17T00:00:0{evaluation_id}Z",
                )
            )
    return (
        {
            "kind": "relevance",
            "snapshot_digest": snapshot_digest,
            "partition_digest": partition_digest,
            "partition": "train",
        },
        answers,
    )


def test_fit_writes_september_report_and_registered_gate_reproduces_decisions(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "fit.db"
    task, answers = _seed_fit_db(db_path, monkeypatch, tmp_path)
    out = tmp_path / "evidence" / "shadow-relevance-2026-09-17"
    assert run_typesafe(_args(db_path, task, out)) == 0
    assert {path.name for path in out.iterdir()} == {
        "PLAN.md",
        "RESULTS.md",
        "checksums.json",
        "inventory.json",
        "weight-set.json",
    }
    checksums = json.loads((out / "checksums.json").read_text())
    for name, digest in checksums.items():
        assert _digest((out / name).read_bytes()) == digest
    inventory = json.loads((out / "inventory.json").read_text())
    assert inventory["train_evaluation_ids"] == [1, 2]
    assert 999 not in inventory["fitted_evaluation_ids"]
    weight_set = WeightSet.model_validate_json((out / "weight-set.json").read_bytes())
    name = register_fitted_gate(weight_set)
    actual = {
        evaluation_id: DECIDE_REGISTRY[name](Answers.model_validate(document)).eligible
        for evaluation_id, document in answers.items()
    }
    expected = {item["evaluation_id"]: item["decision"] for item in inventory["training_decisions"]}
    assert actual == expected
    DECIDE_REGISTRY.pop(name)


def test_fit_refuses_incomplete_training_rows(tmp_path, capsys, monkeypatch) -> None:
    db_path = tmp_path / "fit.db"
    task, _answers = _seed_fit_db(db_path, monkeypatch, tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM shadow_relevance_runs WHERE evaluation_id = 2")
    out = tmp_path / "evidence"
    assert run_typesafe(_args(db_path, task, out)) == 2
    assert "missing=[2]" in capsys.readouterr().err
    assert not out.exists()


def test_fit_refuses_retained_group_crossing_partitions_before_query(
    tmp_path, capsys, monkeypatch
) -> None:
    db_path = tmp_path / "fit.db"
    task, _answers = _seed_fit_db(db_path, monkeypatch, tmp_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT content FROM analysis_artifacts WHERE digest = ?",
            (task["partition_digest"],),
        ).fetchone()
        assert row is not None
        partition = FrozenPartition.model_validate_json(row[0])
        members = tuple(
            member.model_copy(update={"group_id": "shared"})
            if member.evaluation_id in (1, 999)
            else member
            for member in partition.members
        )
        changed = partition.model_copy(update={"members": members}).model_dump_json().encode()
        task["partition_digest"] = _digest(changed)
        conn.execute(
            "INSERT INTO analysis_artifacts(digest, content) VALUES (?, ?)",
            (task["partition_digest"], changed),
        )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("shadow rows must not be queried")

    monkeypatch.setattr(
        "scout.storage.shadow_relevance.ShadowRelevanceStore.fitting_rows", forbidden
    )
    out = tmp_path / "evidence"
    assert run_typesafe(_args(db_path, task, out)) == 2
    assert "Recorded related group crosses partitions" in capsys.readouterr().err
    assert not out.exists()


def test_fit_reports_duplicate_score_levels_as_invalid_answers(
    tmp_path, capsys, monkeypatch
) -> None:
    db_path = tmp_path / "fit.db"
    task, _answers = _seed_fit_db(db_path, monkeypatch, tmp_path)
    duplicate = {
        "answers": {
            "quality": {
                "kind": "score",
                "levels": [
                    {"level": "high", "probability": 0.4},
                    {"level": "high", "probability": 0.6},
                ],
                "confidence": 0.7,
            }
        },
        "request_id": "duplicate",
        "model": "placeholder/v1",
    }
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE shadow_relevance_runs SET answers_json = ? WHERE evaluation_id = 1",
            (json.dumps(duplicate),),
        )
    assert run_typesafe(_args(db_path, task, tmp_path / "evidence")) == 2
    assert "invalid_answers" in capsys.readouterr().err


def test_env_example_lists_every_typesafe_variable_read_by_config() -> None:
    config = Path("src/scout/config.py").read_text()
    names = set(re.findall(r'["\'](TYPESAFE_[A-Z0-9_]+)["\']', config))
    example = Path(".env.example").read_text()
    listed = {
        line.split("=", 1)[0] for line in example.splitlines() if line.startswith("TYPESAFE_")
    }
    assert names <= listed
