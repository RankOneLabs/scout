from __future__ import annotations

import json

import pytest
from jig import SQLiteFeedbackLoop, SQLiteTracer

from scout.replay.reporting import ReportError
from scout.replay.study_reporting import StudyCell, export_study_reports
from scout.storage.state import StateManager
from tests.test_replay_reporting import _run_two_variant_sweep


def test_study_cell_rejects_undeclared_provenance_fields() -> None:
    with pytest.raises(ValueError, match="Extra inputs"):
        StudyCell.model_validate({
            "sweep_name": "study", "variant": "a", "model": "model", "verified_prompt": "fake",
        })


@pytest.mark.parametrize("outcome_kind", ["complete", "missing", "wrong_model", "missing_variant"])
async def test_manifest_export_uses_explicit_runs_and_reports_every_declared_sweep(
    tmp_path, monkeypatch, outcome_kind,
) -> None:
    with StateManager(db_path=":memory:") as state:
        tracer = SQLiteTracer(db_path=str(tmp_path / "traces.db"))
        feedback = SQLiteFeedbackLoop(db_path=str(tmp_path / "feedback.db"))
        runs, _ = await _run_two_variant_sweep(state, tracer, feedback, monkeypatch)
        cells = [{
            "sweep_name": "ab-tune", "variant": variant,
            "model": model, "project": "test-project", "prompt": "test-prompt",
        } for variant, model in (
            ("a", "claude-sonnet-4-20250514"), ("b", "claude-haiku-4-5-20251001"),
        )]
        if outcome_kind == "wrong_model":
            cells[0]["model"] = "other-model"
        if outcome_kind == "missing_variant":
            runs.pop("b")
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({"manifest_version": 1, "grid": "test", "cells": cells}))
        if outcome_kind != "missing":
            (tmp_path / "ab-tune.outcome.json").write_text(json.dumps({
                "version": 1, "experiment_run_ids": runs,
            }))
        if outcome_kind in ("wrong_model", "missing_variant"):
            with pytest.raises(ReportError, match="differs|does not match"):
                export_study_reports(state, manifest, tmp_path / "reports")
            return
        complete = export_study_reports(state, manifest, tmp_path / "reports")
        assert complete is (outcome_kind == "complete")
        index = (tmp_path / "reports/index.md").read_text()
        if complete:
            report = json.loads((tmp_path / "reports/ab-tune.json").read_text())
            assert report["experiment_run_ids"] == sorted(runs.values())
            assert all("project" not in cell and "prompt" not in cell
                       for cell in report["study_cells"])
            assert "ab-tune.md" in index
        else:
            assert "missing outcome" in index
