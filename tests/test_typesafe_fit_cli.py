from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path

import pytest

from scout.cli.typesafe import run_typesafe
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
def test_non_train_task_refuses_before_database_open(
    monkeypatch, tmp_path, partition: str
) -> None:
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


def _seed_fit_db(path: Path) -> tuple[dict[str, object], dict[int, dict[str, object]]]:
    input_bytes = b"opaque frozen input"
    input_digest = _digest(input_bytes)
    snapshot = {
        "format": "scout.relevance-corpus/v1",
        "selection": {"project_key": "agent-ops"},
        "members": [
            {
                "grade_id": 101,
                "evaluation_id": 1,
                "grade_revision_id": 11,
                "is_relevant": False,
                "input_digest": input_digest,
            },
            {
                "grade_id": 102,
                "evaluation_id": 2,
                "grade_revision_id": 12,
                "is_relevant": True,
                "input_digest": input_digest,
            },
            {
                "grade_id": 103,
                "evaluation_id": 999,
                "grade_revision_id": 13,
                "is_relevant": True,
                "input_digest": input_digest,
            },
        ],
        "exclusions": [],
    }
    snapshot_bytes = json.dumps(snapshot, separators=(",", ":")).encode()
    snapshot_digest = _digest(snapshot_bytes)
    partition = {
        "format": "scout.assistance-partition/v1",
        "snapshot_digest": snapshot_digest,
        "members": [
            {
                "evaluation_id": 1,
                "input_digest": input_digest,
                "group_id": "one",
                "partition": "train",
                "exposed": False,
                "provenance": [],
            },
            {
                "evaluation_id": 2,
                "input_digest": input_digest,
                "group_id": "two",
                "partition": "train",
                "exposed": False,
                "provenance": [],
            },
            {
                "evaluation_id": 999,
                "input_digest": input_digest,
                "group_id": "heldout",
                "partition": "heldout",
                "exposed": False,
                "provenance": [],
            },
        ],
    }
    partition_bytes = json.dumps(partition, separators=(",", ":")).encode()
    partition_digest = _digest(partition_bytes)
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
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE analysis_artifacts (digest TEXT PRIMARY KEY, content BLOB);
        CREATE TABLE analysis_lineage (digest TEXT PRIMARY KEY);
        CREATE TABLE evaluations (id INTEGER PRIMARY KEY, relevant INTEGER);
        CREATE TABLE grade_revisions (
          id INTEGER PRIMARY KEY, evaluation_id INTEGER, payload TEXT);
        CREATE TABLE shadow_relevance_runs (
          id INTEGER PRIMARY KEY, evaluation_id INTEGER, catalogue_version TEXT,
          status TEXT, answers_json TEXT, created_at TEXT);
        """
    )
    conn.executemany(
        "INSERT INTO analysis_artifacts VALUES (?, ?)",
        [
            (input_digest, input_bytes),
            (snapshot_digest, snapshot_bytes),
            (partition_digest, partition_bytes),
        ],
    )
    conn.executemany("INSERT INTO evaluations VALUES (?, ?)", [(1, 0), (2, 1)])
    conn.executemany(
        "INSERT INTO grade_revisions VALUES (?, ?, ?)",
        [(11, 1, '{"relevance_judgment":"correct"}'), (12, 2, '{"relevance_judgment":"correct"}')],
    )
    conn.executemany(
        "INSERT INTO shadow_relevance_runs VALUES (?, ?, ?, 'ok', ?, ?)",
        [
            (1, 1, "c" * 64, json.dumps(answers[1]), "2026-09-17T00:00:00Z"),
            (2, 2, "c" * 64, json.dumps(answers[2]), "2026-09-17T00:00:01Z"),
        ],
    )
    conn.commit()
    conn.close()
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
) -> None:
    db_path = tmp_path / "fit.db"
    task, answers = _seed_fit_db(db_path)
    out = tmp_path / "evidence" / "shadow-relevance-2026-09-17"
    assert run_typesafe(_args(db_path, task, out)) == 0
    assert {path.name for path in out.iterdir()} == {
        "PLAN.md", "RESULTS.md", "checksums.json", "inventory.json", "weight-set.json"
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
    expected = {
        item["evaluation_id"]: item["decision"]
        for item in inventory["training_decisions"]
    }
    assert actual == expected
    DECIDE_REGISTRY.pop(name)


def test_fit_refuses_incomplete_training_rows(tmp_path, capsys) -> None:
    db_path = tmp_path / "fit.db"
    task, _answers = _seed_fit_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM shadow_relevance_runs WHERE evaluation_id = 2")
    out = tmp_path / "evidence"
    assert run_typesafe(_args(db_path, task, out)) == 2
    assert "missing=[2]" in capsys.readouterr().err
    assert not out.exists()


def test_fit_reports_duplicate_score_levels_as_invalid_answers(tmp_path, capsys) -> None:
    db_path = tmp_path / "fit.db"
    task, _answers = _seed_fit_db(db_path)
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
        line.split("=", 1)[0]
        for line in example.splitlines()
        if line.startswith("TYPESAFE_")
    }
    assert names <= listed
