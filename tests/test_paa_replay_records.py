"""Contract projections over actual hermetic Jig/Scout replay records."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import paa_contracts
import pytest
from jig import Span, SpanKind, SQLiteFeedbackLoop, SQLiteTracer, Usage
from jsonschema import Draft7Validator
from paa_runtime import PaaTransitionError, SqliteOperatingRecordStore, propose

import scout.replay.experiments as ee
from scout.paa.config import build_paa_config
from scout.paa.event_store import ScoutEventStore
from scout.paa.replay_records import (
    ReplayParent,
    build_replay_paa_export,
    project_call_usage,
    project_variant_coverage,
    render_replay_paa_export,
    trace_has_single_rooted_tree,
)
from scout.replay.pricing import ModelRate, PricingCatalog
from scout.result import Err, Ok
from scout.storage.state import StateManager
from tests.test_evaluation_experiments import (
    _GRADED_CANDIDATE_PAYLOAD,
    CORRECTION_TEXT,
    _FakeLLMClient,
    _pricing_catalog_for_tests,
    _stub_from_model,
    _submit_response,
)
from tests.test_replay_reporting import _run_single_variant_batch, _run_two_variant_sweep


@pytest.fixture
async def tracer(tmp_path: Path) -> AsyncIterator[SQLiteTracer]:
    tracer = SQLiteTracer(db_path=str(tmp_path / "traces.db"))
    yield tracer
    await tracer.close()


@pytest.fixture
async def feedback(tmp_path: Path) -> AsyncIterator[SQLiteFeedbackLoop]:
    feedback = SQLiteFeedbackLoop(db_path=str(tmp_path / "feedback.db"))
    yield feedback
    await feedback.close()


@pytest.fixture
def state() -> Iterator[StateManager]:
    with StateManager(db_path=":memory:") as state:
        yield state


async def test_replay_export_conforms_to_released_records(
    state,
    tracer,
    feedback,
    monkeypatch,
    tmp_path: Path,
) -> None:
    run_id, _ = await _run_single_variant_batch(state, tracer, feedback, monkeypatch)
    result = await build_replay_paa_export(
        state,
        tracer,
        experiment_run_ids=[run_id],
        catalog=_pricing_catalog_for_tests(),
    )
    assert isinstance(result, Ok), result
    document = json.loads(render_replay_paa_export(result.value))
    payload_schema = json.loads(
        Path("contracts/reply-draft-measurement.v1.schema.json").read_text()
    )
    for record in document["evidence_records"]:
        Draft7Validator(paa_contracts.load_schema("paa-evidence-record")).validate(record)
        Draft7Validator(payload_schema).validate(record["payload"])
    store = SqliteOperatingRecordStore(tmp_path / "operating.db")
    try:
        for record in result.value.operating_records:
            Draft7Validator(paa_contracts.load_schema("paa-operating-record")).validate(record)
            store.append(record)
            assert store.get_by_subject(record["subject"]) == (record,)
    finally:
        store.close()


async def test_export_is_read_only_and_deterministic(state, tracer, feedback, monkeypatch) -> None:
    run_id, _ = await _run_single_variant_batch(state, tracer, feedback, monkeypatch)
    before = state.conn.total_changes
    first = await build_replay_paa_export(
        state,
        tracer,
        experiment_run_ids=[run_id],
        catalog=_pricing_catalog_for_tests(),
    )
    second = await build_replay_paa_export(
        state,
        tracer,
        experiment_run_ids=[run_id],
        catalog=_pricing_catalog_for_tests(),
    )
    assert first == second
    assert state.conn.total_changes == before


async def test_sweep_costs_and_worker_identities_stay_separate(
    state,
    tracer,
    feedback,
    monkeypatch,
) -> None:
    runs, _ = await _run_two_variant_sweep(state, tracer, feedback, monkeypatch)
    result = await build_replay_paa_export(
        state,
        tracer,
        experiment_run_ids=list(runs.values()),
        catalog=_pricing_catalog_for_tests(),
    )
    assert isinstance(result, Ok), result
    configurations = {
        record["worker"]["configuration_ref"] for record in result.value.operating_records
    }
    assert len(configurations) == 2
    assert len(result.value.operating_records) == 6


async def test_export_does_not_invent_acceptance_or_authority(
    state,
    tracer,
    feedback,
    monkeypatch,
) -> None:
    run_id, _ = await _run_single_variant_batch(state, tracer, feedback, monkeypatch)
    result = await build_replay_paa_export(
        state,
        tracer,
        experiment_run_ids=[run_id],
        catalog=_pricing_catalog_for_tests(),
    )
    assert isinstance(result, Ok), result
    assert (
        result.value.acceptance_rule,
        result.value.effective_cost,
        result.value.operating_decision,
    ) == (
        None,
        None,
        None,
    )
    assert {record.verdict.value for record in result.value.evidence_records} == {"scored"}


async def test_source_references_resolve_to_content_addressed_constituents(
    state,
    tracer,
    feedback,
    monkeypatch,
) -> None:
    run_id, _ = await _run_single_variant_batch(state, tracer, feedback, monkeypatch)
    result = await build_replay_paa_export(
        state,
        tracer,
        experiment_run_ids=[run_id],
        catalog=_pricing_catalog_for_tests(),
    )
    assert isinstance(result, Ok), result
    for source in result.value.sources:
        canonical = json.dumps(
            dataclasses.asdict(source.content),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        assert source.reference.endswith(hashlib.sha256(canonical.encode()).hexdigest())
        assert source.content.calls


@pytest.mark.parametrize(
    "model, expected", [("model-a", 0.0005), ("model-b", 0.0008), ("unknown", None)]
)
def test_each_call_uses_its_own_model_rate(model: str, expected: float | None) -> None:
    catalog = PricingCatalog(
        1,
        "2026-01-01",
        "example://rates",
        "a" * 64,
        {
            "model-a": ModelRate(1, 2),
            "model-b": ModelRate(2, 3),
        },
    )
    span = Span(
        id="call",
        trace_id="trace",
        kind=SpanKind.LLM_CALL,
        name="completion",
        started_at=datetime.now(UTC),
        metadata={"model": model},
        usage=Usage(100, 200, 10),
    )
    source = project_call_usage(span, "model-a", catalog)
    assert source.catalog_estimate_usd == expected
    assert source.recorded_cost_usd == 10


def test_missing_usage_is_not_zero() -> None:
    span = Span(
        id="call",
        trace_id="trace",
        kind=SpanKind.LLM_CALL,
        name="completion",
        started_at=datetime.now(UTC),
        metadata={"model": "model-a"},
        usage=None,
    )
    source = project_call_usage(span, "model-a", _pricing_catalog_for_tests())
    assert (source.input_tokens, source.output_tokens, source.catalog_estimate_usd) == (
        None,
        None,
        None,
    )


def test_coverage_keeps_skipped_and_missing_cases_distinct() -> None:
    parent = ReplayParent.model_validate(
        {
            "version": 4,
            "phase": "reply_draft",
            "variant_name": "candidate",
            "plan_sha256": "a" * 64,
            "phase_run_ids": [1, 2],
            "dropped_duplicate_phase_run_ids": [],
            "skipped_pairs": [
                {
                    "phase_run_id": 1,
                    "classification": "unscored",
                    "reason": "no correction",
                    "baseline_model": "model-a",
                    "baseline_prompt_sha256": "b" * 64,
                }
            ],
        }
    )
    coverage = project_variant_coverage(1, parent, [], [])
    assert coverage.missing_phase_run_ids == (2,)
    assert coverage.skipped_pairs == parent.skipped_pairs
    assert coverage.attempt_count == 0


def test_orphan_usage_cannot_be_attributed_to_the_agent() -> None:
    span = Span(
        id="orphan",
        trace_id="trace",
        parent_id="missing",
        kind=SpanKind.LLM_CALL,
        name="completion",
        started_at=datetime.now(UTC),
    )
    assert not trace_has_single_rooted_tree([span])


async def test_duplicate_runs_are_rejected(state, tracer) -> None:
    result = await build_replay_paa_export(
        state,
        tracer,
        experiment_run_ids=[1, 1],
        catalog=_pricing_catalog_for_tests(),
    )
    assert isinstance(result, Err)


async def test_failed_attempt_and_successful_retry_are_both_accounted(
    state,
    tracer,
    feedback,
    monkeypatch,
) -> None:
    verify = ee._verify_correction_hash

    def fail_verification(*args):
        raise ee.CorrectionEvidenceIntegrityError("simulated failure after paid calls")

    monkeypatch.setattr(ee, "_verify_correction_hash", fail_verification)
    run_id, _ = await _run_single_variant_batch(
        state,
        tracer,
        feedback,
        monkeypatch,
        case_count=1,
    )
    monkeypatch.setattr(ee, "_verify_correction_hash", verify)
    client = _FakeLLMClient(
        [_submit_response(_GRADED_CANDIDATE_PAYLOAD)],
        model="claude-sonnet-4-20250514",
    )
    _stub_from_model(monkeypatch, {"claude-sonnet-4-20250514": client})
    await ee.retry_batch_replay(
        state=state,
        tracer=tracer,
        feedback=feedback,
        experiment_run_id=run_id,
        pricing_catalog=_pricing_catalog_for_tests(),
        dossier_root=Path("/unused"),
    )
    result = await build_replay_paa_export(
        state,
        tracer,
        experiment_run_ids=[run_id],
        catalog=_pricing_catalog_for_tests(),
    )
    assert isinstance(result, Ok), result
    assert len(result.value.operating_records) == 2
    assert all(record["price"] is not None for record in result.value.operating_records)
    assert len(result.value.evidence_records) == 1
    assert result.value.variants[0].failed_attempt_count == 1
    assert result.value.sources[1].content.supersedes_experiment_id == (
        result.value.sources[0].content.experiment_id
    )


async def test_retry_refuses_changed_worker_before_spending(
    state,
    tracer,
    feedback,
    monkeypatch,
) -> None:
    def fail_verification(*args):
        raise ee.CorrectionEvidenceIntegrityError("simulated failure")

    monkeypatch.setattr(ee, "_verify_correction_hash", fail_verification)
    run_id, _ = await _run_single_variant_batch(
        state,
        tracer,
        feedback,
        monkeypatch,
        case_count=1,
    )
    before = state.conn.total_changes
    monkeypatch.setattr(ee, "JIG_REVISION", "changed-runtime-revision")
    with pytest.raises(ee.RetryResolutionError, match="changed fields: .*worker_configuration"):
        await ee.retry_batch_replay(
            state=state,
            tracer=tracer,
            feedback=feedback,
            experiment_run_id=run_id,
            pricing_catalog=_pricing_catalog_for_tests(),
            dossier_root=Path("/unused"),
        )
    assert state.conn.total_changes == before


async def test_missing_trace_keeps_attempt_but_marks_price_unavailable(
    state,
    tracer,
    feedback,
    monkeypatch,
) -> None:
    run_id, _ = await _run_single_variant_batch(state, tracer, feedback, monkeypatch)

    async def missing_trace(trace_id):
        return []

    monkeypatch.setattr(tracer, "get_trace", missing_trace)
    result = await build_replay_paa_export(
        state,
        tracer,
        experiment_run_ids=[run_id],
        catalog=_pricing_catalog_for_tests(),
    )
    assert isinstance(result, Ok), result
    assert all(record["price"] is None for record in result.value.operating_records)
    assert all(record["usage"] is None for record in result.value.operating_records)
    assert result.value.variants[0].unpriced_attempt_count == 2


async def test_historical_configuration_is_not_backfilled(
    state,
    tracer,
    feedback,
    monkeypatch,
) -> None:
    run_id, _ = await _run_single_variant_batch(state, tracer, feedback, monkeypatch)
    rows = state.list_experiment_attempts(run_id)
    historical_rows = []
    for row in rows:
        evidence = json.loads(row["baseline_evidence"])
        del evidence["worker_configuration"]
        historical_rows.append({**row, "baseline_evidence": json.dumps(evidence)})
    monkeypatch.setattr(state, "list_experiment_attempts", lambda _: historical_rows)
    result = await build_replay_paa_export(
        state,
        tracer,
        experiment_run_ids=[run_id],
        catalog=_pricing_catalog_for_tests(),
    )
    assert isinstance(result, Err)
    assert "no safe backfill" in result.error.detail


async def test_export_excludes_raw_prompt_correction_and_source_text(
    state,
    tracer,
    feedback,
    monkeypatch,
) -> None:
    run_id, _ = await _run_single_variant_batch(state, tracer, feedback, monkeypatch)
    result = await build_replay_paa_export(
        state,
        tracer,
        experiment_run_ids=[run_id],
        catalog=_pricing_catalog_for_tests(),
    )
    assert isinstance(result, Ok), result
    rendered = render_replay_paa_export(result.value)
    for raw in (CORRECTION_TEXT, "Baseline reply text.", "https://example.com/gateway"):
        assert raw not in rendered


def test_shadow_reply_draft_has_no_reachable_promotion(state, tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text('{"measurement": "only"}')
    config = build_paa_config(evidence_root=tmp_path)
    with pytest.raises(PaaTransitionError):
        propose(
            ScoutEventStore(state),
            config,
            task="reply_draft",
            scope=None,
            to_position="hotl",
            evidence_path=report,
            actor="test:operator",
        )
