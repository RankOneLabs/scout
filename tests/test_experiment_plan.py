from __future__ import annotations

import json

import pytest

from scout.storage.evaluations import ExperimentCASError
from scout.storage.experiment_plan import expected_experiment_pairs
from scout.storage.state import StateManager
from tests.test_evaluation_experiments import _seed_phase_run


@pytest.mark.parametrize("pair", [(0, 2), (1, 1), (2, 1)])
def test_insertion_rejects_unplanned_repeat_skipped_or_unknown_case(pair: tuple[int, int]) -> None:
    with StateManager(db_path=":memory:") as state:
        ids = [_seed_phase_run(state, trace_id=f"case-{i}", model="baseline") for i in range(3)]
        run = state.create_experiment_run(name="plan", candidate_config=json.dumps({
            "phase_run_ids": ids[:2], "skipped_pairs": [{"phase_run_id": ids[1]}],
            "repeats": 1,
        }))
        with pytest.raises(ExperimentCASError, match="outside"):
            state.insert_experiment_attempt(
                experiment_run_id=run, phase_run_id=ids[pair[0]], repeat_index=pair[1],
                baseline_evidence="{}",
            )
        assert state.list_experiment_attempts(run) == []


@pytest.mark.parametrize("config", [
    {"phase_run_ids": [1, 1]}, {"phase_run_ids": [True]}, {"phase_run_ids": "1"},
    {"phase_run_ids": [1], "repeats": True}, {"phase_run_ids": [1], "repeats": 0},
    {"phase_run_ids": [1], "skipped_pairs": [{"phase_run_id": 2}]},
    {"phase_run_ids": [1], "skipped_pairs": [{"phase_run_id": 1}, {"phase_run_id": 1}]},
])
def test_malformed_plan_cannot_fall_back_to_legacy(config: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        expected_experiment_pairs(config)


def test_exact_plan_excludes_skipped_cases_for_every_repeat() -> None:
    assert expected_experiment_pairs({
        "phase_run_ids": [1, 2, 3], "repeats": 2, "skipped_pairs": [{"phase_run_id": 2}],
    }) == {(1, 1), (1, 2), (3, 1), (3, 2)}


def test_equal_count_with_wrong_historical_pairs_cannot_terminalize() -> None:
    with StateManager(db_path=":memory:") as state:
        ids = [_seed_phase_run(state, trace_id=f"case-{i}", model="baseline") for i in range(2)]
        run = state.create_experiment_run(name="plan", candidate_config=json.dumps({
            "phase_run_ids": ids, "repeats": 1,
        }))
        with pytest.raises(ExperimentCASError, match="empty authorized plan"):
            state.complete_experiment_run_without_attempts(run)
        for repeat in (1, 2):
            attempt = state.conn.execute(
                "INSERT INTO evaluation_experiments (experiment_run_id, phase_run_id, "
                "attempt_number, repeat_index, status, baseline_evidence, created_at) "
                "VALUES (?, ?, ?, ?, 'queued', '{}', '2026-09-11T00:00:00Z')",
                (run, ids[0], repeat, repeat),
            ).lastrowid
            state.cas_experiment_to_running(attempt)
            state.fail_experiment(attempt, error_detail="historical malformed row")
        assert state.get_experiment_run(run)["status"] == "running"
