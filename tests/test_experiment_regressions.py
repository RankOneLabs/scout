from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import scout.cli.replay as cli
import scout.replay.experiments as ee
import scout.replay.reporting as rr
from tests.test_replay_cli import _batch_args


def test_all_failed_cli_exits_nonzero(monkeypatch, capsys):
    @asynccontextmanager
    async def runtime(**kwargs):
        yield SimpleNamespace(state=None, tracer=None, feedback=None)

    async def execute(**kwargs):
        return ee.BatchExecutionOutcome(experiment_run_ids={"default": 1}, attempts=(
            ee.BatchAttemptOutcome(
                phase_run_id=1, variant_name="default", status="failed", experiment_id=1,
                candidate_trace_id=None, candidate_llm_call_count=0,
                candidate_cost=None, error_detail="provider failed",
            ),
        ))

    monkeypatch.setattr(cli, "replay_runtime", runtime)
    monkeypatch.setattr(ee, "execute_batch_replay", execute)
    monkeypatch.setattr(cli, "_resolve_batch_variants", lambda args: ((), None))
    with pytest.raises(SystemExit) as exc:
        cli.batch_replay_feedback(_batch_args(
            phase_run_id=[1], execute_paid_replay=True, authorize_plan_sha256="a" * 64,
        ))
    assert exc.value.code == 1
    assert "attempts failed: 1" in capsys.readouterr().out


def test_failed_repeats_keep_equal_case_weight_for_every_variant():
    from scout.replay.tasks import RelevanceScore, RelevanceTarget, RelevanceTask

    task = RelevanceTask(snapshot_digest="a" * 64)
    rows = {1: [], 2: []}
    scores = {}
    # Same two cases and predictions; only which repeat failed differs.
    for run in (1, 2):
        for case in (1, 2):
            target = RelevanceTarget(
                task=task, evaluation_id=case, grade_revision_id=case,
                input_digest="b" * 64, project_key="project", is_relevant=True,
                provenance=(),
            )
            score = RelevanceScore(
                target=target, baseline_relevant=case == 1, candidate_relevant=case == 1,
                baseline_correct=case == 1, candidate_correct=case == 1, accuracy_delta=0,
            )
            for repeat in (1, 2):
                ident = run * 100 + case * 10 + repeat
                status = "failed" if repeat == 2 and case == run else "complete"
                scores[ident] = {"score_evidence": score.model_dump_json()}
                rows[run].append(dict(
                    id=ident, experiment_run_id=run, phase_run_id=case,
                    attempt_number=repeat, repeat_index=repeat, supersedes_experiment_id=None,
                    status=status, baseline_evidence=json.dumps({
                        "target": target.model_dump(mode="json"),
                        "baseline_model":"baseline", "baseline_prompt_sha256":"c" * 64,
                    }), candidate_trace_id=None, candidate_llm_call_count=0,
                    candidate_cost=0, error_detail=None, created_at="now", completed_at="now",
                ))
    state = SimpleNamespace(
        list_experiment_attempts=lambda run: rows[run],
        get_trace_comparison=lambda ident: scores[ident],
    )
    parents = [dict(
        experiment_run_id=run, phase="relevance", task=task.model_dump(mode="json"),
        variant_name=str(run), repeats=2, skipped_pairs=[], phase_run_ids=[1, 2],
        plan_sha256="d" * 64, source_exclusions=[],
    ) for run in (1, 2)]
    report = rr._build_relevance_report(state, parents)
    summaries = report["segments"][0]["variants"]
    assert [v["common_case_count"] for v in summaries] == [2, 2]
    assert [v["baseline_accuracy"] for v in summaries] == [0.5, 0.5]
