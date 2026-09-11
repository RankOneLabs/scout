"""Tests for canonical batch/sweep replay reports (replay_reporting.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jig import SQLiteFeedbackLoop, SQLiteTracer

import scout.replay.experiments as ee
import scout.replay.reporting as rr
from scout.storage.state import StateManager
from tests.test_evaluation_experiments import (
    _GRADED_CANDIDATE_PAYLOAD,
    CORRECTION_TEXT,
    RELEVANCE_PAYLOAD_B,
    _FakeLLMClient,
    _make_baseline_trace,
    _patch_resolve_dossier,
    _pricing_catalog_for_tests,
    _seed_phase_run,
    _seed_reply_draft_correction,
    _stub_from_model,
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


async def _run_single_variant_batch(
    state, tracer, feedback, monkeypatch, *, case_count: int = 2, repeats: int = 1,
    on_queued=None,
) -> tuple[int, list[int]]:
    """Seed `case_count` reply_draft cases (all sharing one baseline model/
    prompt segment) and execute one batch replay against them with a
    single candidate variant, `repeats` attempts per case. Returns
    (experiment_run_id, phase_run_ids)."""
    _patch_resolve_dossier(monkeypatch)
    phase_run_ids = []
    for _ in range(case_count):
        phase_run_id, _ = await _seed_reply_draft_correction(
            state, tracer, feedback, model="claude-opus-4-20250514",
        )
        phase_run_ids.append(phase_run_id)
    candidate_client = _FakeLLMClient(
        [_submit_response(_GRADED_CANDIDATE_PAYLOAD) for _ in range(case_count * repeats)],
        model="claude-sonnet-4-20250514",
    )
    _stub_from_model(monkeypatch, {"claude-sonnet-4-20250514": candidate_client})
    variants = (ee.BatchVariant(ee.DEFAULT_BATCH_VARIANT_NAME, "claude-sonnet-4-20250514", None),)
    selector = ee.BatchSelector.by_phase_run_ids(phase_run_ids)
    catalog = _pricing_catalog_for_tests()
    plan = await ee.build_batch_plan(
        state=state, tracer=tracer, selector=selector, variants=variants,
        skip_policy=ee.SkipPolicy(), pricing_catalog=catalog, dossier_root=Path("/unused"),
        repeats=repeats,
    )
    outcome = await ee.execute_batch_replay(
        state=state, tracer=tracer, feedback=feedback, name="report-fixture",
        selector=selector, variants=variants, skip_policy=ee.SkipPolicy(),
        authorize_plan_sha256=plan.plan_sha256, pricing_catalog=catalog,
        dossier_root=Path("/unused"), repeats=repeats,
        on_queued=on_queued,
    )
    return outcome.experiment_run_ids[ee.DEFAULT_BATCH_VARIANT_NAME], phase_run_ids


@pytest.mark.parametrize("malformation", ["repeat", "case", "missing"])
async def test_report_rejects_historical_out_of_plan_or_incomplete_terminal_rows(
    state, tracer, feedback, monkeypatch, malformation,
) -> None:
    run_id, _ = await _run_single_variant_batch(state, tracer, feedback, monkeypatch)
    rows = state.list_experiment_attempts(run_id)
    if malformation == "repeat":
        rows[0] = {**rows[0], "repeat_index": 2}
    elif malformation == "case":
        rows[0] = {**rows[0], "phase_run_id": 999999}
    else:
        rows = rows[:1]
    monkeypatch.setattr(state, "list_experiment_attempts", lambda _: rows)
    with pytest.raises(rr.ReportError, match="out-of-plan|incomplete terminal"):
        rr.build_batch_report(state, experiment_run_ids=[run_id])


async def test_report_is_independent_of_run_id_order(state, tracer, feedback, monkeypatch) -> None:
    runs, _ = await _run_two_variant_sweep(state, tracer, feedback, monkeypatch)
    ids = list(runs.values())
    assert rr.render_json(rr.build_batch_report(state, experiment_run_ids=ids)) == rr.render_json(
        rr.build_batch_report(state, experiment_run_ids=list(reversed(ids))),
    )


async def _run_two_variant_sweep(
    state, tracer, feedback, monkeypatch, *, case_count: int = 3,
) -> tuple[dict[str, int], list[int]]:
    """Seed `case_count` shared cases and execute a two-variant model sweep
    where variant "a" always distances 0.0 from the correction and variant
    "b" always distances further away, giving a deterministic ranking.
    Returns (experiment_run_ids_by_variant, phase_run_ids)."""
    _patch_resolve_dossier(monkeypatch)
    phase_run_ids = []
    for _ in range(case_count):
        phase_run_id, _ = await _seed_reply_draft_correction(
            state, tracer, feedback, model="claude-opus-4-20250514",
        )
        phase_run_ids.append(phase_run_id)

    worse_payload = {
        "posture": "answer",
        "segments": [{"type": "declarative", "fact_id": "fact-1", "text": "A worse reply."}],
        "claims": ["A worse reply."],
        "resources_used": [],
    }
    client_a = _FakeLLMClient(
        [_submit_response(_GRADED_CANDIDATE_PAYLOAD) for _ in range(case_count)],
        model="claude-sonnet-4-20250514",
    )
    client_b = _FakeLLMClient(
        [_submit_response(worse_payload) for _ in range(case_count)],
        model="claude-haiku-4-5-20251001",
    )
    _stub_from_model(monkeypatch, {
        "claude-sonnet-4-20250514": client_a, "claude-haiku-4-5-20251001": client_b,
    })

    sweep = ee.SweepDefinition(
        name="ab-tune", axis="model", shared_model=None, shared_prompt_file=None,
        variants=(
            ee.SweepVariant("a", "claude-sonnet-4-20250514", None),
            ee.SweepVariant("b", "claude-haiku-4-5-20251001", None),
        ),
    )
    variants = ee.batch_variants_for_sweep(sweep, base_dir=Path("/unused"))
    selector = ee.BatchSelector.by_phase_run_ids(phase_run_ids)
    catalog = _pricing_catalog_for_tests()
    plan = await ee.build_batch_plan(
        state=state, tracer=tracer, selector=selector, variants=variants,
        skip_policy=ee.SkipPolicy(), pricing_catalog=catalog, dossier_root=Path("/unused"),
        sweep=sweep,
    )
    outcome = await ee.execute_batch_replay(
        state=state, tracer=tracer, feedback=feedback, name="ab-tune",
        selector=selector, variants=variants, skip_policy=ee.SkipPolicy(),
        authorize_plan_sha256=plan.plan_sha256, pricing_catalog=catalog,
        dossier_root=Path("/unused"), sweep=sweep,
    )
    return outcome.experiment_run_ids, phase_run_ids


_ABSTAIN_PAYLOAD = {
    "posture": "abstain",
    "segments": [],
    "claims": [],
    "resources_used": [],
    "abstain_reason": "nothing to add",
}


async def _run_abstaining_sweep(
    state, tracer, feedback, monkeypatch, *, case_count: int = 3,
) -> dict[str, int]:
    """Seed `case_count` shared cases and execute a two-variant model sweep
    where variant "a" assembles the correction exactly and variant "b"
    abstains on every case. Returns experiment_run_ids_by_variant."""
    _patch_resolve_dossier(monkeypatch)
    phase_run_ids = []
    for _ in range(case_count):
        phase_run_id, _ = await _seed_reply_draft_correction(
            state, tracer, feedback, model="claude-opus-4-20250514",
        )
        phase_run_ids.append(phase_run_id)
    client_a = _FakeLLMClient(
        [_submit_response(_GRADED_CANDIDATE_PAYLOAD) for _ in range(case_count)],
        model="claude-sonnet-4-20250514",
    )
    client_b = _FakeLLMClient(
        [_submit_response(_ABSTAIN_PAYLOAD) for _ in range(case_count)],
        model="claude-haiku-4-5-20251001",
    )
    _stub_from_model(monkeypatch, {
        "claude-sonnet-4-20250514": client_a, "claude-haiku-4-5-20251001": client_b,
    })
    sweep = ee.SweepDefinition(
        name="abstain-tune", axis="model", shared_model=None, shared_prompt_file=None,
        variants=(
            ee.SweepVariant("a", "claude-sonnet-4-20250514", None),
            ee.SweepVariant("b", "claude-haiku-4-5-20251001", None),
        ),
    )
    variants = ee.batch_variants_for_sweep(sweep, base_dir=Path("/unused"))
    selector = ee.BatchSelector.by_phase_run_ids(phase_run_ids)
    catalog = _pricing_catalog_for_tests()
    plan = await ee.build_batch_plan(
        state=state, tracer=tracer, selector=selector, variants=variants,
        skip_policy=ee.SkipPolicy(), pricing_catalog=catalog, dossier_root=Path("/unused"),
        sweep=sweep,
    )
    outcome = await ee.execute_batch_replay(
        state=state, tracer=tracer, feedback=feedback, name="abstain-tune",
        selector=selector, variants=variants, skip_policy=ee.SkipPolicy(),
        authorize_plan_sha256=plan.plan_sha256, pricing_catalog=catalog,
        dossier_root=Path("/unused"), sweep=sweep,
    )
    return outcome.experiment_run_ids


async def _run_mixed_segment_batch(
    state, tracer, feedback, monkeypatch,
) -> tuple[int, dict[str, list[int]]]:
    """Seed two cases on baseline model A and one case on baseline model
    B, and execute one batch replay across all three with a single
    candidate variant -- one experiment_run_id spanning two segments.
    Returns (experiment_run_id, {"A": [...], "B": [...]})."""
    _patch_resolve_dossier(monkeypatch)
    a1, _ = await _seed_reply_draft_correction(
        state, tracer, feedback, model="claude-opus-4-20250514",
    )
    a2, _ = await _seed_reply_draft_correction(
        state, tracer, feedback, model="claude-opus-4-20250514",
    )
    b1, _ = await _seed_reply_draft_correction(
        state, tracer, feedback, model="claude-haiku-4-5-20251001",
    )
    all_ids = [a1, a2, b1]
    candidate_client = _FakeLLMClient(
        [_submit_response(_GRADED_CANDIDATE_PAYLOAD) for _ in range(len(all_ids))],
        model="claude-sonnet-4-20250514",
    )
    _stub_from_model(monkeypatch, {"claude-sonnet-4-20250514": candidate_client})
    variants = (ee.BatchVariant(ee.DEFAULT_BATCH_VARIANT_NAME, "claude-sonnet-4-20250514", None),)
    selector = ee.BatchSelector.by_phase_run_ids(all_ids)
    catalog = _pricing_catalog_for_tests()
    plan = await ee.build_batch_plan(
        state=state, tracer=tracer, selector=selector, variants=variants,
        skip_policy=ee.SkipPolicy(), pricing_catalog=catalog, dossier_root=Path("/unused"),
    )
    outcome = await ee.execute_batch_replay(
        state=state, tracer=tracer, feedback=feedback, name="mixed-segments",
        selector=selector, variants=variants, skip_policy=ee.SkipPolicy(),
        authorize_plan_sha256=plan.plan_sha256, pricing_catalog=catalog,
        dossier_root=Path("/unused"),
    )
    run_id = outcome.experiment_run_ids[ee.DEFAULT_BATCH_VARIANT_NAME]
    return run_id, {"A": [a1, a2], "B": [b1]}


class TestBuildBatchReport:
    async def test_single_variant_report_basic_shape(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        run_id, phase_run_ids = await _run_single_variant_batch(
            state, tracer, feedback, monkeypatch,
        )
        report = rr.build_batch_report(state, experiment_run_ids=[run_id])

        assert report["version"] == rr.REPORT_SCHEMA_VERSION
        assert report["experiment_run_ids"] == [run_id]
        assert len(report["plan_sha256"]) == 64
        assert report["correction_coverage"] == {
            "population_size": 2,
            "dropped_duplicate_phase_run_ids": [],
            "attempted": 2, "scored_attempts": 2, "failed_attempts": 0,
            "skipped": {"unscored": 0, "no_op": 0, "unpriceable": 0},
        }
        assert report["exclusions"] == []
        assert report["cost"]["estimated_usd"] > 0
        assert report["cost"]["actual_usd"] == pytest.approx(0.002)  # two calls @ 0.001 each

        assert len(report["segments"]) == 1
        segment = report["segments"][0]
        assert segment["baseline_model"] == "claude-opus-4-20250514"
        # Two paired cases is below the interval floor: the variant has a
        # mean but no interval, so it is indistinguishable, never ranked.
        assert segment["ranking"] == []
        assert segment["indistinguishable_from_baseline"] == [ee.DEFAULT_BATCH_VARIANT_NAME]
        assert segment["interval_family_size"] == 0
        variant = segment["variants"][0]
        assert variant["scored_case_count"] == 2
        assert variant["unscored_count"] == 0
        assert variant["no_op_count"] == 0
        assert variant["unpriceable_count"] == 0
        assert variant["common_case_count"] == 2
        assert variant["mean_delta"] is not None
        assert variant["interval_available"] is False
        assert variant["interval_seed"] is None
        assert variant["ci_lower"] is None and variant["ci_upper"] is None
        assert variant["interval_excludes_zero"] is None
        reported_ids = {case["phase_run_id"] for case in segment["cases"]}
        assert reported_ids == set(phase_run_ids)

    async def test_sweep_ranks_closer_variant_first(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        case_count = rr.BOOTSTRAP_MIN_PAIRED_CASES
        experiment_run_ids, _phase_run_ids = await _run_two_variant_sweep(
            state, tracer, feedback, monkeypatch, case_count=case_count,
        )
        report = rr.build_batch_report(state, experiment_run_ids=list(experiment_run_ids.values()))
        assert len(report["segments"]) == 1
        segment = report["segments"][0]
        # "a" assembles exactly the correction text (distance 0.0); "b" does
        # not. At the floor both intervals exist and exclude zero, so both
        # are ranked and "a" must come first (a more negative mean delta).
        assert segment["ranking"] == ["a", "b"]
        assert segment["indistinguishable_from_baseline"] == []
        assert segment["interval_family_size"] == 2
        by_name = {v["variant_name"]: v for v in segment["variants"]}
        assert by_name["a"]["mean_delta"] < by_name["b"]["mean_delta"]
        assert by_name["a"]["common_case_count"] == case_count
        assert by_name["b"]["common_case_count"] == case_count
        for variant in by_name.values():
            assert variant["interval_available"] is True
            assert variant["interval_seed"] is not None
            assert variant["ci_lower"] <= variant["mean_delta"] <= variant["ci_upper"]
            assert variant["interval_excludes_zero"] is True

    async def test_sweep_below_interval_floor_ranks_nothing(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        experiment_run_ids, _phase_run_ids = await _run_two_variant_sweep(
            state, tracer, feedback, monkeypatch, case_count=3,
        )
        report = rr.build_batch_report(state, experiment_run_ids=list(experiment_run_ids.values()))
        segment = report["segments"][0]
        # Three paired cases: "a" still has the smaller mean, but without an
        # interval neither variant may be ordered against the other.
        assert segment["ranking"] == []
        assert segment["indistinguishable_from_baseline"] == ["a", "b"]
        assert segment["interval_family_size"] == 0
        assert all(v["interval_available"] is False for v in segment["variants"])

    async def test_repeats_pair_one_delta_per_case(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        run_id, phase_run_ids = await _run_single_variant_batch(
            state, tracer, feedback, monkeypatch, case_count=2, repeats=3,
        )
        report = rr.build_batch_report(state, experiment_run_ids=[run_id])
        assert report["correction_coverage"]["attempted"] == 6
        segment = report["segments"][0]
        variant = segment["variants"][0]
        assert variant["repeat_count"] == 3
        assert variant["scored_case_count"] == 2
        assert variant["scored_attempt_count"] == 6
        # The bootstrap resamples cases: two paired deltas, not six.
        assert variant["common_case_count"] == 2
        # Identical scripted replies: every repeat lands on the same distance.
        assert variant["within_case_range_mean"] == 0.0
        rows = {(case["phase_run_id"], case["repeat_index"]) for case in segment["cases"]}
        assert rows == {(pid, r) for pid in phase_run_ids for r in (1, 2, 3)}
        markdown = rr.render_markdown(report)
        assert "| repeat |" in markdown
        assert "within-case range" in markdown

    async def test_abstaining_variant_is_counted_not_distanced(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        experiment_run_ids = await _run_abstaining_sweep(
            state, tracer, feedback, monkeypatch, case_count=3,
        )
        report = rr.build_batch_report(state, experiment_run_ids=list(experiment_run_ids.values()))
        segment = report["segments"][0]
        by_name = {v["variant_name"]: v for v in segment["variants"]}
        # "b" abstained on every case: all three are scored attempts, all
        # three are counted as abstains, and none of them carries a
        # distance delta into the comparison.
        assert by_name["b"]["scored_case_count"] == 3
        assert by_name["b"]["candidate_abstain_count"] == 3
        assert by_name["b"]["baseline_abstain_count"] == 0
        assert by_name["b"]["common_case_count"] == 0
        assert by_name["b"]["mean_delta"] is None
        # One variant's abstain removes the case from every variant's
        # common distance set, so "a" has nothing to pair either.
        assert by_name["a"]["candidate_abstain_count"] == 0
        assert by_name["a"]["common_case_count"] == 0
        assert segment["ranking"] == []
        assert segment["indistinguishable_from_baseline"] == []
        abstained = {
            (case["variant"], case["candidate_abstained"]) for case in segment["cases"]
        }
        assert abstained == {("a", False), ("b", True)}
        assert all(case["baseline_abstained"] is False for case in segment["cases"])

    async def test_evidence_without_abstain_flags_stays_in_population(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        run_id, _ = await _run_single_variant_batch(
            state, tracer, feedback, monkeypatch, case_count=2,
        )
        # Serve the retained evidence without the flags, as rows written
        # before they existed look (the table itself is immutable).
        get_trace_comparison = state.get_trace_comparison

        def without_abstain_flags(experiment_id: int):
            comparison = dict(get_trace_comparison(experiment_id))
            score = json.loads(comparison["score_evidence"])
            del score["baseline_abstained"], score["candidate_abstained"]
            comparison["score_evidence"] = json.dumps(score)
            return comparison

        monkeypatch.setattr(state, "get_trace_comparison", without_abstain_flags)
        report = rr.build_batch_report(state, experiment_run_ids=[run_id])
        variant = report["segments"][0]["variants"][0]
        assert variant["common_case_count"] == 2
        assert variant["candidate_abstain_count"] == 0
        assert all(
            case["candidate_abstained"] is None for case in report["segments"][0]["cases"]
        )

    async def test_exclusions_report_failed_case_reason(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        a, _ = await _seed_reply_draft_correction(
            state, tracer, feedback, model="claude-opus-4-20250514",
        )
        b, _ = await _seed_reply_draft_correction(
            state, tracer, feedback, model="claude-opus-4-20250514",
        )
        lo, hi = sorted([a, b])
        candidate_client = _FakeLLMClient(
            [_submit_response(_GRADED_CANDIDATE_PAYLOAD)], model="claude-sonnet-4-20250514",
        )
        _stub_from_model(monkeypatch, {"claude-sonnet-4-20250514": candidate_client})
        variants = (
            ee.BatchVariant(ee.DEFAULT_BATCH_VARIANT_NAME, "claude-sonnet-4-20250514", None),
        )
        selector = ee.BatchSelector.by_phase_run_ids([lo, hi])
        catalog = _pricing_catalog_for_tests()
        plan = await ee.build_batch_plan(
            state=state, tracer=tracer, selector=selector, variants=variants,
            skip_policy=ee.SkipPolicy(), pricing_catalog=catalog, dossier_root=Path("/unused"),
        )
        outcome = await ee.execute_batch_replay(
            state=state, tracer=tracer, feedback=feedback, name="one-fails",
            selector=selector, variants=variants, skip_policy=ee.SkipPolicy(),
            authorize_plan_sha256=plan.plan_sha256, pricing_catalog=catalog,
            dossier_root=Path("/unused"),
        )
        run_id = outcome.experiment_run_ids[ee.DEFAULT_BATCH_VARIANT_NAME]
        report = rr.build_batch_report(state, experiment_run_ids=[run_id])
        assert report["correction_coverage"]["failed_attempts"] == 1
        assert len(report["exclusions"]) == 1
        assert report["exclusions"][0]["phase_run_id"] == hi
        assert report["exclusions"][0]["repeat_index"] == 1
        assert report["exclusions"][0]["reason"] == ee._STAGE_MESSAGES["candidate_execution"]
        markdown = rr.render_markdown(report)
        assert f"- phase_run_id `{hi}` variant `default` repeat 1 (failed)" in markdown

    async def test_all_skipped_variant_reports_planned_repeats(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        unscored, _ = await _seed_reply_draft_correction(
            state, tracer, feedback, model="claude-opus-4-20250514", link_reply_revision=False,
        )
        candidate_client = _FakeLLMClient([], model="claude-sonnet-4-20250514")
        _stub_from_model(monkeypatch, {"claude-sonnet-4-20250514": candidate_client})
        variants = (
            ee.BatchVariant(ee.DEFAULT_BATCH_VARIANT_NAME, "claude-sonnet-4-20250514", None),
        )
        selector = ee.BatchSelector.by_phase_run_ids([unscored])
        skip_policy = ee.SkipPolicy(skip_unscored=True)
        catalog = _pricing_catalog_for_tests()
        plan = await ee.build_batch_plan(
            state=state, tracer=tracer, selector=selector, variants=variants,
            skip_policy=skip_policy, pricing_catalog=catalog, dossier_root=Path("/unused"),
            repeats=3,
        )
        outcome = await ee.execute_batch_replay(
            state=state, tracer=tracer, feedback=feedback, name="all-skipped",
            selector=selector, variants=variants, skip_policy=skip_policy,
            authorize_plan_sha256=plan.plan_sha256, pricing_catalog=catalog,
            dossier_root=Path("/unused"), repeats=3,
        )
        run_id = outcome.experiment_run_ids[ee.DEFAULT_BATCH_VARIANT_NAME]
        report = rr.build_batch_report(state, experiment_run_ids=[run_id])
        variant = report["segments"][0]["variants"][0]
        assert variant["scored_case_count"] == 0
        assert variant["unscored_count"] == 1
        # No attempt ever ran, so the plan is the only witness to the repeat count.
        assert variant["repeat_count"] == 3
        assert report["exclusions"][0]["repeat_index"] is None

    def test_unknown_experiment_run_id_raises(self, state) -> None:
        with pytest.raises(rr.ReportError, match="no experiment_runs row"):
            rr.build_batch_report(state, experiment_run_ids=[999_999])

    def test_requires_at_least_one_id(self, state) -> None:
        with pytest.raises(rr.ReportError, match="at least one"):
            rr.build_batch_report(state, experiment_run_ids=[])

    async def test_rejects_duplicate_experiment_run_ids(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        run_id, _ = await _run_single_variant_batch(
            state, tracer, feedback, monkeypatch, case_count=1,
        )
        with pytest.raises(rr.ReportError, match="duplicate experiment_run_id"):
            rr.build_batch_report(state, experiment_run_ids=[run_id, run_id])

    async def test_non_batch_parent_rejected(self, state, tracer, feedback, monkeypatch) -> None:
        trace_id = await _make_baseline_trace(tracer, feedback)
        phase_run_id = _seed_phase_run(state, trace_id=trace_id, model="claude-haiku-4-5-20251001")
        candidate_client = _FakeLLMClient(
            [_submit_response(RELEVANCE_PAYLOAD_B)], model="claude-sonnet-4-20250514",
        )
        _stub_from_model(monkeypatch, {"claude-sonnet-4-20250514": candidate_client})
        outcome = await ee.execute_replay(
            state=state, tracer=tracer, feedback=feedback, phase_run_id=phase_run_id,
            name="single", model_override="claude-sonnet-4-20250514", system_prompt_override=None,
        )
        with pytest.raises(rr.ReportError, match="not a batch/sweep parent"):
            rr.build_batch_report(state, experiment_run_ids=[outcome.experiment_run_id])

    async def test_no_reportable_attempts_raises(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id, _ = await _seed_reply_draft_correction(state, tracer, feedback)
        config = ee.build_batch_candidate_config(
            phase="reply_draft", variant_name=ee.DEFAULT_BATCH_VARIANT_NAME,
            model_override="claude-sonnet-4-20250514", system_prompt_override=None,
            grader_attached=True, sweep=None, plan_sha256="0" * 64,
            phase_run_ids=(phase_run_id,), dropped_duplicate_phase_run_ids=(),
            skipped_pairs=(),
        )
        run_id = state.create_experiment_run(name="never-executed", candidate_config=config)
        with pytest.raises(rr.ReportError, match="no reportable"):
            rr.build_batch_report(state, experiment_run_ids=[run_id])

    async def test_retry_only_counts_latest_attempt(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        a, _ = await _seed_reply_draft_correction(
            state, tracer, feedback, model="claude-opus-4-20250514",
        )
        b, _ = await _seed_reply_draft_correction(
            state, tracer, feedback, model="claude-opus-4-20250514",
        )
        lo, hi = sorted([a, b])
        candidate_client = _FakeLLMClient(
            [_submit_response(_GRADED_CANDIDATE_PAYLOAD)], model="claude-sonnet-4-20250514",
        )
        _stub_from_model(monkeypatch, {"claude-sonnet-4-20250514": candidate_client})
        variants = (
            ee.BatchVariant(ee.DEFAULT_BATCH_VARIANT_NAME, "claude-sonnet-4-20250514", None),
        )
        selector = ee.BatchSelector.by_phase_run_ids([lo, hi])
        catalog = _pricing_catalog_for_tests()
        plan = await ee.build_batch_plan(
            state=state, tracer=tracer, selector=selector, variants=variants,
            skip_policy=ee.SkipPolicy(), pricing_catalog=catalog, dossier_root=Path("/unused"),
        )
        outcome = await ee.execute_batch_replay(
            state=state, tracer=tracer, feedback=feedback, name="retry-then-report",
            selector=selector, variants=variants, skip_policy=ee.SkipPolicy(),
            authorize_plan_sha256=plan.plan_sha256, pricing_catalog=catalog,
            dossier_root=Path("/unused"),
        )
        run_id = outcome.experiment_run_ids[ee.DEFAULT_BATCH_VARIANT_NAME]

        retry_client = _FakeLLMClient(
            [_submit_response(_GRADED_CANDIDATE_PAYLOAD)], model="claude-sonnet-4-20250514",
        )
        _stub_from_model(monkeypatch, {"claude-sonnet-4-20250514": retry_client})
        await ee.retry_batch_replay(
            state=state, tracer=tracer, feedback=feedback, experiment_run_id=run_id,
            pricing_catalog=catalog, dossier_root=Path("/unused"),
        )

        report = rr.build_batch_report(state, experiment_run_ids=[run_id])
        # Two cases total, not three -- the superseded failed attempt for
        # `hi` must not double-count alongside its successful retry.
        assert report["correction_coverage"]["attempted"] == 2
        assert report["correction_coverage"]["scored_attempts"] == 2
        assert report["correction_coverage"]["failed_attempts"] == 0
        assert report["exclusions"] == []

    async def test_rejects_parents_from_different_authorized_plans(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        run_id_a, _ = await _run_single_variant_batch(state, tracer, feedback, monkeypatch)
        run_id_b, _ = await _run_single_variant_batch(state, tracer, feedback, monkeypatch)
        with pytest.raises(rr.ReportError, match="do not share one authorized plan"):
            rr.build_batch_report(state, experiment_run_ids=[run_id_a, run_id_b])


class TestGoldenReports:
    """True golden coverage for mixed segments, skip policy, retry cost
    accounting, and publication safety -- the properties an operator
    actually relies on when reading or sharing a report."""

    async def test_mixed_segments_json_and_markdown(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        run_id, groups = await _run_mixed_segment_batch(state, tracer, feedback, monkeypatch)
        report = rr.build_batch_report(state, experiment_run_ids=[run_id])

        assert len(report["segments"]) == 2
        by_model = {segment["baseline_model"]: segment for segment in report["segments"]}
        assert set(by_model) == {"claude-opus-4-20250514", "claude-haiku-4-5-20251001"}

        segment_a = by_model["claude-opus-4-20250514"]
        segment_b = by_model["claude-haiku-4-5-20251001"]
        assert {case["phase_run_id"] for case in segment_a["cases"]} == set(groups["A"])
        assert {case["phase_run_id"] for case in segment_b["cases"]} == set(groups["B"])
        # Segment A has 2 paired cases and segment B only 1 -- each
        # segment's own coverage, never pooled with the other, and both
        # below the interval floor.
        assert segment_a["variants"][0]["common_case_count"] == 2
        assert segment_b["variants"][0]["common_case_count"] == 1
        assert segment_a["variants"][0]["interval_available"] is False
        assert segment_b["variants"][0]["interval_available"] is False
        assert segment_b["variants"][0]["mean_delta"] is not None  # a mean is still reportable

        markdown = rr.render_markdown(report)
        assert "## Segment `claude-opus-4-20250514|" in markdown
        assert "## Segment `claude-haiku-4-5-20251001|" in markdown
        for phase_run_id in groups["A"] + groups["B"]:
            assert f"`{phase_run_id}`" in markdown

    async def test_skip_policy_exclusion_reported_with_classification_and_reason(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        scored, _ = await _seed_reply_draft_correction(
            state, tracer, feedback, model="claude-opus-4-20250514",
        )
        unscored, _ = await _seed_reply_draft_correction(
            state, tracer, feedback, model="claude-opus-4-20250514", link_reply_revision=False,
        )
        candidate_client = _FakeLLMClient(
            [_submit_response(_GRADED_CANDIDATE_PAYLOAD)], model="claude-sonnet-4-20250514",
        )
        _stub_from_model(monkeypatch, {"claude-sonnet-4-20250514": candidate_client})
        variants = (
            ee.BatchVariant(ee.DEFAULT_BATCH_VARIANT_NAME, "claude-sonnet-4-20250514", None),
        )
        selector = ee.BatchSelector.by_phase_run_ids([scored, unscored])
        skip_policy = ee.SkipPolicy(skip_unscored=True)
        catalog = _pricing_catalog_for_tests()
        plan = await ee.build_batch_plan(
            state=state, tracer=tracer, selector=selector, variants=variants,
            skip_policy=skip_policy, pricing_catalog=catalog, dossier_root=Path("/unused"),
        )
        outcome = await ee.execute_batch_replay(
            state=state, tracer=tracer, feedback=feedback, name="golden-skip",
            selector=selector, variants=variants, skip_policy=skip_policy,
            authorize_plan_sha256=plan.plan_sha256, pricing_catalog=catalog,
            dossier_root=Path("/unused"),
        )
        run_id = outcome.experiment_run_ids[ee.DEFAULT_BATCH_VARIANT_NAME]

        report = rr.build_batch_report(state, experiment_run_ids=[run_id])
        assert report["correction_coverage"]["population_size"] == 2
        assert report["correction_coverage"]["attempted"] == 1
        assert report["correction_coverage"]["skipped"] == {
            "unscored": 1, "no_op": 0, "unpriceable": 0,
        }
        skipped_exclusions = [e for e in report["exclusions"] if e["kind"] == "skipped"]
        assert len(skipped_exclusions) == 1
        assert skipped_exclusions[0]["phase_run_id"] == unscored
        assert skipped_exclusions[0]["classification"] == "unscored"
        assert skipped_exclusions[0]["reason"] is not None

        variant = report["segments"][0]["variants"][0]
        assert variant["unscored_count"] == 1
        assert variant["scored_case_count"] == 1

        markdown = rr.render_markdown(report)
        assert "unscored" in markdown
        assert str(unscored) in markdown

    async def test_retry_counts_superseded_cost_but_per_case_shows_latest_only(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id, _ = await _seed_reply_draft_correction(
            state, tracer, feedback, model="claude-opus-4-20250514",
        )
        candidate_client = _FakeLLMClient(
            [
                _submit_response(_GRADED_CANDIDATE_PAYLOAD),
                _submit_response(_GRADED_CANDIDATE_PAYLOAD),
            ],
            model="claude-sonnet-4-20250514",
        )
        _stub_from_model(monkeypatch, {"claude-sonnet-4-20250514": candidate_client})

        # Force the FIRST attempt to fail after record_candidate_trace has
        # already run (so its candidate_cost is durably recorded), then let
        # the retry's own call through unmodified.
        real_verify = ee._verify_correction_hash
        calls = {"n": 0}

        def _flaky_verify(state_arg, oracle):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ee.CorrectionEvidenceIntegrityError("simulated transient failure")
            return real_verify(state_arg, oracle)

        monkeypatch.setattr(ee, "_verify_correction_hash", _flaky_verify)

        variants = (
            ee.BatchVariant(ee.DEFAULT_BATCH_VARIANT_NAME, "claude-sonnet-4-20250514", None),
        )
        selector = ee.BatchSelector.by_phase_run_ids([phase_run_id])
        catalog = _pricing_catalog_for_tests()
        plan = await ee.build_batch_plan(
            state=state, tracer=tracer, selector=selector, variants=variants,
            skip_policy=ee.SkipPolicy(), pricing_catalog=catalog, dossier_root=Path("/unused"),
        )
        outcome = await ee.execute_batch_replay(
            state=state, tracer=tracer, feedback=feedback, name="golden-retry-cost",
            selector=selector, variants=variants, skip_policy=ee.SkipPolicy(),
            authorize_plan_sha256=plan.plan_sha256, pricing_catalog=catalog,
            dossier_root=Path("/unused"),
        )
        assert outcome.attempts[0].status == "failed"
        failed_row = state.get_experiment(outcome.attempts[0].experiment_id)
        assert failed_row is not None
        assert failed_row["candidate_cost"] is not None  # sanity: cost recorded before failure

        run_id = outcome.experiment_run_ids[ee.DEFAULT_BATCH_VARIANT_NAME]
        retry_outcome = await ee.retry_batch_replay(
            state=state, tracer=tracer, feedback=feedback, experiment_run_id=run_id,
            pricing_catalog=catalog, dossier_root=Path("/unused"),
        )
        assert retry_outcome.attempts[0].status == "complete"

        report = rr.build_batch_report(state, experiment_run_ids=[run_id])
        segment = report["segments"][0]
        assert len(segment["cases"]) == 1  # only the latest attempt is a "case"
        case = segment["cases"][0]
        assert case["status"] == "complete"
        assert case["actual_usd"] == pytest.approx(0.001)  # the successful attempt's own cost only

        # Total actual spend includes BOTH the failed and the successful
        # attempt's recorded cost -- real money was spent on both.
        assert report["cost"]["actual_usd"] == pytest.approx(0.002)
        assert report["correction_coverage"]["attempted"] == 1
        assert report["correction_coverage"]["failed_attempts"] == 0

    async def test_report_never_leaks_raw_prompt_correction_or_source_text(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        run_id, _ = await _run_single_variant_batch(state, tracer, feedback, monkeypatch)
        report = rr.build_batch_report(state, experiment_run_ids=[run_id])
        rendered_json = rr.render_json(report)
        rendered_markdown = rr.render_markdown(report)
        for raw in (CORRECTION_TEXT, "Baseline reply text.", "https://example.com/gateway"):
            assert raw not in rendered_json
            assert raw not in rendered_markdown


class TestBootstrapDeterminism:
    def test_same_seed_reproduces_identical_interval(self) -> None:
        deltas = [-0.4, -0.2, -0.35, -0.1, -0.5]
        ci_a = rr._bootstrap_ci(deltas, seed=12345)
        ci_b = rr._bootstrap_ci(deltas, seed=12345)
        assert ci_a == ci_b

    def test_seed_is_derived_from_report_identity(self) -> None:
        seed_a = rr._bootstrap_seed([1, 2], "segA", "v1")
        seed_b = rr._bootstrap_seed([1, 2], "segA", "v2")
        seed_c = rr._bootstrap_seed([1, 3], "segA", "v1")
        assert len({seed_a, seed_b, seed_c}) == 3
        assert rr._bootstrap_seed([2, 1], "segA", "v1") == seed_a  # order-independent


def _summary(
    name: str, mean: float | None, ci: tuple[float, float] | None,
) -> rr.VariantSegmentSummary:
    return rr.VariantSegmentSummary(
        variant_name=name, scored_case_count=0, failed_case_count=0, unscored_count=0,
        no_op_count=0, unpriceable_count=0, baseline_abstain_count=0,
        candidate_abstain_count=0, common_case_count=0, mean_delta=mean, repeat_count=1,
        scored_attempt_count=0, failed_attempt_count=0, within_case_range_mean=None,
        interval_available=ci is not None, interval_seed=None,
        ci_lower=None if ci is None else ci[0], ci_upper=None if ci is None else ci[1],
        interval_excludes_zero=rr._interval_excludes_zero(
            None if ci is None else ci[0], None if ci is None else ci[1],
        ),
    )


class TestIntervalPartition:
    def test_interval_excludes_zero_on_either_side(self) -> None:
        assert rr._interval_excludes_zero(-0.4, -0.1) is True
        assert rr._interval_excludes_zero(0.1, 0.4) is True

    def test_interval_containing_or_touching_zero_does_not_exclude_it(self) -> None:
        assert rr._interval_excludes_zero(-0.2, 0.2) is False
        assert rr._interval_excludes_zero(0.0, 0.3) is False
        assert rr._interval_excludes_zero(-0.3, 0.0) is False

    def test_unavailable_interval_is_none(self) -> None:
        assert rr._interval_excludes_zero(None, None) is None

    def test_only_variants_whose_interval_excludes_zero_are_ordered(self) -> None:
        summaries = [
            _summary("worse", 0.3, (0.1, 0.5)),
            _summary("noise", -0.05, (-0.2, 0.1)),
            _summary("better", -0.4, (-0.5, -0.3)),
            _summary("tiny", -0.6, None),
            _summary("empty", None, None),
        ]
        ranking, indistinguishable = rr._partition_by_interval(summaries)
        assert ranking == ("better", "worse")
        assert indistinguishable == ("noise", "tiny")


class TestRenderJsonAndMarkdown:
    async def test_render_json_is_canonical_and_deterministic(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        run_id, _ = await _run_single_variant_batch(state, tracer, feedback, monkeypatch)
        report = rr.build_batch_report(state, experiment_run_ids=[run_id])
        rendered_a = rr.render_json(report)
        rendered_b = rr.render_json(json.loads(rendered_a))
        assert rendered_a == rendered_b
        assert "\n" not in rendered_a
        assert ", " not in rendered_a

    async def test_render_markdown_never_names_a_winner(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        experiment_run_ids, _ = await _run_two_variant_sweep(state, tracer, feedback, monkeypatch)
        report = rr.build_batch_report(state, experiment_run_ids=list(experiment_run_ids.values()))
        markdown = rr.render_markdown(report)
        lowered = markdown.lower()
        for banned in ("winner", "best variant", "recommended variant", "wins"):
            assert banned not in lowered

    async def test_render_markdown_contains_expected_sections(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        run_id, _ = await _run_single_variant_batch(state, tracer, feedback, monkeypatch)
        report = rr.build_batch_report(state, experiment_run_ids=[run_id])
        markdown = rr.render_markdown(report)
        assert "# Scout batch/sweep replay report" in markdown
        assert "## Correction coverage" in markdown
        assert "## Cost" in markdown
        assert f"## Segment `{report['segments'][0]['segment_key']}`" in markdown
        assert "95% CI" in markdown
        assert "excludes zero" in markdown
        assert "abstains (baseline / candidate)" in markdown
        assert "| abstained |" in markdown
        assert "Indistinguishable from baseline" in markdown
        assert f"unavailable (< {rr.BOOTSTRAP_MIN_PAIRED_CASES} paired cases)" in markdown

    async def test_render_markdown_orders_only_interval_backed_variants(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        experiment_run_ids, _ = await _run_two_variant_sweep(state, tracer, feedback, monkeypatch)
        report = rr.build_batch_report(state, experiment_run_ids=list(experiment_run_ids.values()))
        markdown = rr.render_markdown(report)
        assert "excludes zero only: none" in markdown
        assert "unordered): `a`, `b`" in markdown

    async def test_render_markdown_names_abstaining_side(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        experiment_run_ids = await _run_abstaining_sweep(state, tracer, feedback, monkeypatch)
        report = rr.build_batch_report(state, experiment_run_ids=list(experiment_run_ids.values()))
        markdown = rr.render_markdown(report)
        assert "| 0 / 3 |" in markdown  # variant b: no baseline abstains, three candidate
        assert "| candidate |" in markdown
        assert "| unknown |" not in markdown


class TestFormatAbstained:
    def test_both_missing_is_unknown(self) -> None:
        assert rr._format_abstained(None, None) == "unknown"

    def test_neither_abstained_is_no(self) -> None:
        assert rr._format_abstained(False, False) == "no"

    def test_names_each_abstaining_side(self) -> None:
        assert rr._format_abstained(True, True) == "baseline, candidate"

    def test_one_missing_flag_is_named_not_read_as_no(self) -> None:
        assert rr._format_abstained(None, False) == "baseline unknown"
        assert rr._format_abstained(True, None) == "baseline, candidate unknown"


class TestRelevanceRates:
    def test_precision_is_none_without_predicted_positives(self) -> None:
        confusion = {
            "true_positive": 0, "true_negative": 3, "false_positive": 0, "false_negative": 2,
        }
        assert rr._precision(confusion) is None
        assert rr._recall(confusion) == 0.0

    def test_recall_is_none_without_relevant_cases(self) -> None:
        confusion = {
            "true_positive": 0, "true_negative": 3, "false_positive": 1, "false_negative": 0,
        }
        assert rr._recall(confusion) is None
        assert rr._precision(confusion) == 0.0

    def test_majority_reference_on_empty_common_set(self) -> None:
        reference = rr._majority_class_reference([])
        assert reference["majority_class_accuracy"] is None
        assert reference["majority_label_relevant"] is None
        assert reference["common_case_count"] == 0
