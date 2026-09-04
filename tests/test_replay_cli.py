"""Tests for the `scout feedback` CLI handlers (replay_cli.py)."""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from jig import SQLiteFeedbackLoop, SQLiteTracer

import scout.cli.replay as replay_cli
import scout.replay.experiments as ee
from scout.replay.runtime import ReplayRuntime
from scout.storage.state import StateManager
from tests.test_evaluation_experiments import (
    _GRADED_CANDIDATE_PAYLOAD,
    RELEVANCE_PAYLOAD_B,
    _FakeLLMClient,
    _make_baseline_trace,
    _patch_resolve_dossier,
    _seed_phase_run,
    _seed_reply_draft_correction,
    _submit_response,
)


@pytest.fixture
def tracer(tmp_path) -> SQLiteTracer:
    return SQLiteTracer(db_path=str(tmp_path / "traces.db"))


@pytest.fixture
def feedback(tmp_path) -> SQLiteFeedbackLoop:
    return SQLiteFeedbackLoop(db_path=str(tmp_path / "feedback.db"))


@pytest.fixture
def state():
    with StateManager(db_path=":memory:") as s:
        yield s


@pytest.fixture(autouse=True)
def _patch_replay_runtime(monkeypatch, state, tracer, feedback):
    """Route replay_cli's replay_runtime() call at its real DB_PATH-bound
    resources so tests never depend on (or pollute) real relative-path
    scout.db / scout_traces.db / scout_feedback.db files."""

    @asynccontextmanager
    async def _fake_replay_runtime(*, db_path=None):
        yield ReplayRuntime(state=state, tracer=tracer, feedback=feedback)

    monkeypatch.setattr(replay_cli, "replay_runtime", _fake_replay_runtime)


def _base_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        phase_run_id=1, name="test-experiment", model=None, prompt_file=None,
        execute_paid_replay=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestPositiveInt:
    def test_accepts_positive_integer(self) -> None:
        assert replay_cli.positive_int("7") == 7

    def test_rejects_zero(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            replay_cli.positive_int("0")

    def test_rejects_negative(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            replay_cli.positive_int("-3")

    def test_rejects_non_integer(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            replay_cli.positive_int("not-a-number")


class TestReplayFeedbackPreview:
    def test_preview_prints_expected_fields_and_writes_nothing(
        self, state, tracer, feedback, capsys
    ) -> None:
        # replay_feedback manages its own event loop internally (asyncio.run),
        # so this test — and every other test invoking it directly — must
        # stay a plain sync function: calling it from inside a pytest-asyncio
        # coroutine would nest event loops.
        trace_id = asyncio.run(_make_baseline_trace(tracer, feedback))
        phase_run_id = _seed_phase_run(state, trace_id=trace_id, model="claude-haiku-4-5-20251001")

        replay_cli.replay_feedback(_base_args(phase_run_id=phase_run_id))

        out = capsys.readouterr().out
        assert "phase: relevance" in out
        assert "baseline model: claude-haiku-4-5-20251001" in out
        assert "no-op: True" in out
        assert "trusted max_llm_calls: 4" in out
        assert "warning: executing with --execute-paid-replay" in out
        count = state.conn.execute("SELECT COUNT(*) FROM evaluation_experiments").fetchone()[0]
        assert count == 0

    def test_blank_name_rejected_before_any_async_work(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            replay_cli.replay_feedback(_base_args(name="   "))
        assert exc_info.value.code == 2

    def test_unreadable_prompt_file_rejected(self, tmp_path) -> None:
        missing = tmp_path / "does-not-exist.txt"
        with pytest.raises(SystemExit) as exc_info:
            replay_cli.replay_feedback(_base_args(prompt_file=str(missing)))
        assert exc_info.value.code == 2

    def test_invalid_utf8_prompt_file_rejected(self, tmp_path) -> None:
        bad = tmp_path / "bad.txt"
        bad.write_bytes(b"\xff\xfe\x00invalid")
        with pytest.raises(SystemExit) as exc_info:
            replay_cli.replay_feedback(_base_args(prompt_file=str(bad)))
        assert exc_info.value.code == 2

    def test_domain_error_prints_to_stderr_and_exits_1(
        self, state, tracer, feedback, capsys
    ) -> None:
        with pytest.raises(SystemExit) as exc_info:
            replay_cli.replay_feedback(_base_args(phase_run_id=999_999))
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "error:" in err


class TestReplayFeedbackExecute:
    def test_execute_persists_experiment_and_prints_outcome(
        self, state, tracer, feedback, capsys, monkeypatch
    ) -> None:
        trace_id = asyncio.run(_make_baseline_trace(tracer, feedback))
        phase_run_id = _seed_phase_run(state, trace_id=trace_id, model="claude-haiku-4-5-20251001")

        candidate_client = _FakeLLMClient(
            [_submit_response(RELEVANCE_PAYLOAD_B)], model="claude-sonnet-4-20250514",
        )

        def _fake_from_model(model: str, **_kwargs):
            assert model == "claude-sonnet-4-20250514"
            return candidate_client

        monkeypatch.setattr(ee, "from_model", _fake_from_model)

        replay_cli.replay_feedback(
            _base_args(
                phase_run_id=phase_run_id, model="claude-sonnet-4-20250514",
                execute_paid_replay=True,
            )
        )

        out = capsys.readouterr().out
        assert "complete" in out
        assert "candidate llm_call_count: 1" in out

        row = state.conn.execute(
            "SELECT status FROM evaluation_experiments ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["status"] == "complete"


def _batch_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        phase_run_id=None, scan_id=None, from_utc=None, to_utc=None,
        graded_with_corrections=False, name="cli-batch", model=None, prompt_file=None,
        sweep_file=None, skip_unscored=False, skip_no_op=False, skip_unpriceable=False,
        pricing_catalog=None, dossier_root=None, authorize_plan_sha256=None,
        execute_paid_replay=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestBatchReplayFeedback:
    def test_blank_name_rejected_before_any_async_work(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            replay_cli.batch_replay_feedback(_batch_args(name="  ", phase_run_id=[1]))
        assert exc_info.value.code == 2

    def test_missing_selector_rejected(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            replay_cli.batch_replay_feedback(_batch_args())
        assert exc_info.value.code == 2

    def test_two_selectors_rejected(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            replay_cli.batch_replay_feedback(_batch_args(phase_run_id=[1], scan_id=2))
        assert exc_info.value.code == 2

    def test_from_without_to_rejected(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            replay_cli.batch_replay_feedback(
                _batch_args(from_utc="2026-01-01T00:00:00+00:00")
            )
        assert exc_info.value.code == 2

    def test_execute_without_authorize_hash_rejected(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            replay_cli.batch_replay_feedback(
                _batch_args(phase_run_id=[1], execute_paid_replay=True)
            )
        assert exc_info.value.code == 2

    def test_sweep_file_combined_with_model_rejected(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            replay_cli.batch_replay_feedback(
                _batch_args(
                    phase_run_id=[1], sweep_file="x.yaml", model="claude-sonnet-4-20250514",
                )
            )
        assert exc_info.value.code == 2

    def test_sweep_file_with_duplicate_model_values_rejected(
        self, tmp_path, capsys
    ) -> None:
        sweep_path = tmp_path / "dup-model-sweep.yaml"
        sweep_path.write_text(
            "version: 1\n"
            "name: dup-model\n"
            "axis: model\n"
            "variants:\n"
            "  - name: a\n"
            "    model: claude-sonnet-4-20250514\n"
            "  - name: b\n"
            "    model: claude-sonnet-4-20250514\n"
        )
        with pytest.raises(SystemExit) as exc_info:
            replay_cli.batch_replay_feedback(
                _batch_args(phase_run_id=[1], sweep_file=str(sweep_path))
            )
        assert exc_info.value.code == 2
        assert "semantically distinct" in capsys.readouterr().err

    def test_sweep_file_with_duplicate_prompt_content_rejected(
        self, tmp_path, capsys
    ) -> None:
        (tmp_path / "a.txt").write_text("Identical prompt text.")
        (tmp_path / "b.txt").write_text("Identical prompt text.")
        sweep_path = tmp_path / "dup-prompt-sweep.yaml"
        sweep_path.write_text(
            "version: 1\n"
            "name: dup-prompt\n"
            "axis: prompt\n"
            "model: claude-haiku-4-5-20251001\n"
            "variants:\n"
            "  - name: a\n"
            "    prompt_file: a.txt\n"
            "  - name: b\n"
            "    prompt_file: b.txt\n"
        )
        with pytest.raises(SystemExit) as exc_info:
            replay_cli.batch_replay_feedback(
                _batch_args(phase_run_id=[1], sweep_file=str(sweep_path))
            )
        assert exc_info.value.code == 2
        assert "semantically distinct" in capsys.readouterr().err

    def test_preview_prints_expected_fields_and_writes_nothing(
        self, state, tracer, feedback, monkeypatch, capsys
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id, _ = asyncio.run(_seed_reply_draft_correction(state, tracer, feedback))

        replay_cli.batch_replay_feedback(
            _batch_args(
                phase_run_id=[phase_run_id], name="preview-batch",
                model="claude-sonnet-4-20250514",
            )
        )
        out = capsys.readouterr().out
        assert "population: 1 case(s)" in out
        assert "scored: 1" in out
        assert "estimated USD (claude-sonnet-4-20250514):" in out
        assert "canonical plan sha256:" in out
        assert state.conn.execute("SELECT COUNT(*) FROM experiment_runs").fetchone()[0] == 0

    def test_execute_persists_and_prints_outcome(
        self, state, tracer, feedback, monkeypatch, capsys
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id, _ = asyncio.run(_seed_reply_draft_correction(state, tracer, feedback))
        candidate_client = _FakeLLMClient(
            [_submit_response(_GRADED_CANDIDATE_PAYLOAD)], model="claude-sonnet-4-20250514",
        )

        def _fake_from_model(model: str, **_kwargs):
            assert model == "claude-sonnet-4-20250514"
            return candidate_client

        monkeypatch.setattr(ee, "from_model", _fake_from_model)

        replay_cli.batch_replay_feedback(
            _batch_args(
                phase_run_id=[phase_run_id], name="exec-batch",
                model="claude-sonnet-4-20250514",
            )
        )
        printed = capsys.readouterr().out
        plan_hash = next(
            line.split("canonical plan sha256: ")[1]
            for line in printed.splitlines()
            if line.startswith("canonical plan sha256")
        )

        replay_cli.batch_replay_feedback(
            _batch_args(
                phase_run_id=[phase_run_id], name="exec-batch",
                model="claude-sonnet-4-20250514",
                execute_paid_replay=True, authorize_plan_sha256=plan_hash,
            )
        )
        out = capsys.readouterr().out
        assert "attempts complete: 1" in out
        row = state.conn.execute("SELECT status FROM evaluation_experiments").fetchone()
        assert row["status"] == "complete"

    def test_stale_plan_hash_rejected_at_execute(
        self, state, tracer, feedback, monkeypatch, capsys
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id, _ = asyncio.run(_seed_reply_draft_correction(state, tracer, feedback))

        with pytest.raises(SystemExit) as exc_info:
            replay_cli.batch_replay_feedback(
                _batch_args(
                    phase_run_id=[phase_run_id], name="exec-batch",
                    model="claude-sonnet-4-20250514", execute_paid_replay=True,
                    authorize_plan_sha256="0" * 64,
                )
            )
        assert exc_info.value.code == 1
        assert state.conn.execute("SELECT COUNT(*) FROM experiment_runs").fetchone()[0] == 0


class TestBatchRetryFeedback:
    def test_retry_rejects_when_no_failed_cases(
        self, state, tracer, feedback, monkeypatch, capsys
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id, _ = asyncio.run(_seed_reply_draft_correction(state, tracer, feedback))
        candidate_client = _FakeLLMClient(
            [_submit_response(_GRADED_CANDIDATE_PAYLOAD)], model="claude-sonnet-4-20250514",
        )
        monkeypatch.setattr(ee, "from_model", lambda model, **_kwargs: candidate_client)

        replay_cli.batch_replay_feedback(
            _batch_args(
                phase_run_id=[phase_run_id], name="all-good", model="claude-sonnet-4-20250514",
            )
        )
        plan_hash = next(
            line.split("canonical plan sha256: ")[1]
            for line in capsys.readouterr().out.splitlines()
            if line.startswith("canonical plan sha256")
        )
        replay_cli.batch_replay_feedback(
            _batch_args(
                phase_run_id=[phase_run_id], name="all-good", model="claude-sonnet-4-20250514",
                execute_paid_replay=True, authorize_plan_sha256=plan_hash,
            )
        )
        run_id = state.conn.execute("SELECT id FROM experiment_runs").fetchone()["id"]

        with pytest.raises(SystemExit) as exc_info:
            replay_cli.batch_retry_feedback(
                argparse.Namespace(
                    experiment_run_id=run_id, phase_run_id=None,
                    pricing_catalog=None, dossier_root=None,
                )
            )
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "no failed cases" in err


class TestReportFeedback:
    def test_prints_paa_records(self, state, tracer, feedback, monkeypatch, capsys) -> None:
        run_id = self._seed_completed_batch(state, tracer, feedback, monkeypatch)
        replay_cli.report_feedback(
            argparse.Namespace(
                experiment_run_id=[run_id], format="paa-json", out=None, pricing_catalog=None,
            )
        )
        document = json.loads(capsys.readouterr().out)
        assert document["schema"] == "scout-paa-replay/1"
        assert document["operating_records"][0]["task"] == "reply_draft"

    def _seed_completed_batch(self, state, tracer, feedback, monkeypatch) -> int:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id, _ = asyncio.run(_seed_reply_draft_correction(state, tracer, feedback))
        candidate_client = _FakeLLMClient(
            [_submit_response(_GRADED_CANDIDATE_PAYLOAD)], model="claude-sonnet-4-20250514",
        )
        monkeypatch.setattr(ee, "from_model", lambda model, **_kwargs: candidate_client)
        selector = ee.BatchSelector.by_phase_run_ids([phase_run_id])
        variants = (
            ee.BatchVariant(ee.DEFAULT_BATCH_VARIANT_NAME, "claude-sonnet-4-20250514", None),
        )
        catalog = ee.load_pricing_catalog()
        plan = asyncio.run(ee.build_batch_plan(
            state=state, tracer=tracer, selector=selector, variants=variants,
            skip_policy=ee.SkipPolicy(), pricing_catalog=catalog, dossier_root=Path("/unused"),
        ))
        outcome = asyncio.run(ee.execute_batch_replay(
            state=state, tracer=tracer, feedback=feedback, name="report-source",
            selector=selector, variants=variants, skip_policy=ee.SkipPolicy(),
            authorize_plan_sha256=plan.plan_sha256, pricing_catalog=catalog,
            dossier_root=Path("/unused"),
        ))
        return outcome.experiment_run_ids[ee.DEFAULT_BATCH_VARIANT_NAME]

    def test_prints_markdown_report_by_default(
        self, state, tracer, feedback, monkeypatch, capsys
    ) -> None:
        run_id = self._seed_completed_batch(state, tracer, feedback, monkeypatch)
        replay_cli.report_feedback(
            argparse.Namespace(experiment_run_id=[run_id], format="markdown", out=None)
        )
        out = capsys.readouterr().out
        assert "# Scout batch/sweep replay report" in out

    def test_writes_json_report_to_out_file(
        self, state, tracer, feedback, monkeypatch, capsys, tmp_path
    ) -> None:
        run_id = self._seed_completed_batch(state, tracer, feedback, monkeypatch)
        out_path = tmp_path / "report.json"
        replay_cli.report_feedback(
            argparse.Namespace(experiment_run_id=[run_id], format="json", out=str(out_path))
        )
        doc = json.loads(out_path.read_text())
        assert doc["version"] == 2
        assert doc["experiment_run_ids"] == [run_id]
        printed = capsys.readouterr().out
        assert "wrote json report" in printed

    def test_unknown_experiment_run_id_exits_1(self, capsys) -> None:
        with pytest.raises(SystemExit) as exc_info:
            replay_cli.report_feedback(
                argparse.Namespace(experiment_run_id=[999_999], format="markdown", out=None)
            )
        assert exc_info.value.code == 1
        assert "error:" in capsys.readouterr().err
