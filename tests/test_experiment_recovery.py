from __future__ import annotations

import json
from pathlib import Path

import pytest
from jig import SQLiteFeedbackLoop, SQLiteTracer

import scout.replay.experiments as ee
import scout.replay.reporting as rr
from scout.cli.replay import _write_batch_outcome_file
from scout.storage.state import StateManager
from tests.test_evaluation_experiments import _pricing_catalog_for_tests
from tests.test_replay_reporting import _run_single_variant_batch


async def test_recovery_reports_malformed_stored_plan_as_domain_error(tmp_path) -> None:
    with StateManager(db_path=":memory:") as state:
        run = state.create_experiment_run(name="malformed", candidate_config=json.dumps({
            "version": ee.BATCH_CANDIDATE_CONFIG_VERSION, "phase_run_ids": [1], "repeats": 0,
        }))
        with pytest.raises(ee.RetryResolutionError, match="invalid stored plan"):
            await ee.retry_batch_replay(
                state=state, tracer=None, feedback=None, experiment_run_id=run,
                recover_interrupted=True,
            )


@pytest.mark.parametrize("interrupted_status", ["queued", "running"])
async def test_interruption_preserves_plan_and_recovery_completes_only_unfinished_work(
    tmp_path, monkeypatch, interrupted_status,
) -> None:
    class Interrupted(Exception):
        pass

    original = ee._execute_one_batch_attempt
    calls = 0

    async def interrupt_second(**kwargs):
        nonlocal calls
        assert outcome_path.exists(), "run IDs must be checkpointed before any model call"
        calls += 1
        if calls == 2:
            if interrupted_status == "running":
                kwargs["state"].cas_experiment_to_running(kwargs["queued_experiment_id"])
            raise Interrupted()
        return await original(**kwargs)

    monkeypatch.setattr(ee, "_execute_one_batch_attempt", interrupt_second)
    outcome_path = tmp_path / "sweep.outcome.json"
    with StateManager(db_path=":memory:") as state:
        tracer = SQLiteTracer(db_path=str(tmp_path / "traces.db"))
        feedback = SQLiteFeedbackLoop(db_path=str(tmp_path / "feedback.db"))
        with pytest.raises(Interrupted):
            await _run_single_variant_batch(
                state, tracer, feedback, monkeypatch, case_count=2,
                on_queued=lambda ids: _write_batch_outcome_file(outcome_path, ids),
            )
        run = state.conn.execute("select id from experiment_runs").fetchone()[0]
        checkpoint = json.loads(outcome_path.read_text())
        assert checkpoint["experiment_run_ids"] == {"default": run}
        assert checkpoint["complete"] is None
        before = state.list_experiment_attempts(run)
        assert [row["status"] for row in before] == ["complete", interrupted_status]
        report = rr.build_batch_report(state, experiment_run_ids=[run])
        assert report["provisional"] is True
        assert report["runs"][0]["missing_chain_count"] == 0
        assert "PROVISIONAL" in rr.render_markdown(report)
        with pytest.raises(ee.RetryResolutionError, match="no failed cases"):
            await ee.retry_batch_replay(
                state=state, tracer=tracer, feedback=feedback, experiment_run_id=run,
            )
        outcome = await ee.retry_batch_replay(
            state=state, tracer=tracer, feedback=feedback, experiment_run_id=run,
            recover_interrupted=True, pricing_catalog=_pricing_catalog_for_tests(),
            dossier_root=Path("/unused"),
        )
        assert len(outcome.attempts) == 1
        assert outcome.attempts[0].status == "complete"
        assert state.get_experiment(before[0]["id"]) == before[0]
        if interrupted_status == "running":
            old = state.get_experiment(before[1]["id"])
            assert old["status"] == "failed"
            assert "Operator recovered" in old["error_detail"]
            new = state.get_experiment(outcome.attempts[0].experiment_id)
            assert new["supersedes_experiment_id"] == old["id"]
        else:
            assert outcome.attempts[0].experiment_id == before[1]["id"]
        final = rr.build_batch_report(state, experiment_run_ids=[run])
        assert (final["status"], final["provisional"]) == ("complete", False)


def test_failed_final_write_preserves_run_id_checkpoint(tmp_path, monkeypatch) -> None:
    import scout.cli.replay as replay_cli

    path = tmp_path / "outcome.json"
    run_ids = {"candidate": 1}
    _write_batch_outcome_file(path, run_ids)
    before = path.read_bytes()

    def fail_replace(*args):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(replay_cli.os, "replace", fail_replace)
    with pytest.raises(ee.ReplayError, match="Cannot checkpoint"):
        _write_batch_outcome_file(
            path, run_ids,
            outcome=ee.BatchExecutionOutcome(experiment_run_ids=run_ids, attempts=()),
        )
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]
