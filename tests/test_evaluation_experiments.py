"""Tests for the CLI-only offline replay domain (evaluation_experiments.py)."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from jig import (
    AgentConfig,
    CompletionParams,
    LLMClient,
    LLMResponse,
    ScoreSource,
    Span,
    SpanKind,
    SQLiteFeedbackLoop,
    SQLiteTracer,
    ToolCall,
    ToolRegistry,
    TraceDiff,
    Usage,
    run_agent,
)

import scout.replay.experiments as ee
import scout.replay.pricing as rp
from scout.config import GradeRecord, Message, RelevanceResult
from scout.dossiers.resolver import (
    DossierFact,
    DossierResolution,
    DossierResolutionError,
    DossierResource,
    DossierSummary,
    ResolutionMetadata,
)
from scout.grading.correction import ReplyCorrectionGrader, normalized_edit_distance
from scout.scanning.schemas import RelevancePhaseOutput, StructuredDraftOutput
from scout.storage.state import StateManager
from scout.verifier import DRAFT_TEXT_ASSEMBLER_VERSION, assemble_draft_text


class _FakeLLMClient(LLMClient):
    """Replays scripted LLMResponses in order and stamps `_model` so Jig's
    config-snapshot model_id resolution (and this test's baseline
    agreement check) sees a deterministic value. Raises RuntimeError on
    exhaustion, or immediately if `error` is set (simulating a permanent
    provider failure)."""

    def __init__(
        self, responses: list[LLMResponse], *, model: str, error: Exception | None = None
    ) -> None:
        self._responses = list(responses)
        self._model = model
        self._error = error

    async def complete(self, params: CompletionParams) -> LLMResponse:
        if self._error is not None:
            raise self._error
        if not self._responses:
            raise RuntimeError("_FakeLLMClient exhausted — test expected fewer turns")
        return self._responses.pop(0)


def _submit_response(args: dict) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCall(id="call-submit", name="submit_output", arguments=args)],
        usage=Usage(input_tokens=100, output_tokens=50, cost=0.001),
        latency_ms=10.0,
        model="scripted",
    )


def _invalid_submit_response() -> LLMResponse:
    """Missing every required field — triggers one schema-validation retry."""
    return _submit_response({})


RELEVANCE_PAYLOAD = {
    "relevant": True, "score": 0.9, "reason": "fit", "relevant_to": ["gateway"],
}
RELEVANCE_PAYLOAD_B = {
    "relevant": True, "score": 0.4, "reason": "different fit", "relevant_to": ["gateway", "other"],
}


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


async def _make_baseline_trace(
    tracer: SQLiteTracer,
    feedback: SQLiteFeedbackLoop,
    *,
    system_prompt: str = "You are Scout's relevance evaluator.",
    input_text: str = "Evaluate this message.",
    model: str = "claude-haiku-4-5-20251001",
    payload: dict = RELEVANCE_PAYLOAD,
) -> str:
    config = AgentConfig(
        name="scout_relevance",
        description="test baseline",
        system_prompt=system_prompt,
        llm=_FakeLLMClient([_submit_response(payload)], model=model),
        feedback=feedback,
        tracer=tracer,
        tools=ToolRegistry([]),
        output_schema=RelevancePhaseOutput,
        max_tool_calls=1,
        max_llm_calls=4,
        max_parse_retries=2,
        include_memory_in_prompt=False,
        include_feedback_in_prompt=False,
    )
    result = await run_agent(config, input_text)
    assert result.parsed is not None, f"baseline run failed to parse: {result.error}"
    return result.trace_id


_BASELINE_DRAFT_PAYLOAD = {
    "posture": "answer",
    "segments": [{"type": "declarative", "fact_id": "fact-1", "text": "Baseline reply text."}],
    "claims": ["Baseline reply text."],
    "resources_used": [],
}

CORRECTION_TEXT = "Please check our docs at https://example.com/gateway for details."

_TEST_DOSSIER = DossierSummary(
    project_key="gateway",
    last_reviewed=date.today(),
    reviewer="reviewer",
    facts=[
        DossierFact(
            id="fact-1", text="Baseline reply text.",
            safe_phrasings=["Baseline reply text."], immutable_evidence=["ev-1"],
        ),
    ],
    resources=[
        DossierResource(
            id="res-gateway", label="Gateway", canonical_url="https://example.com/gateway",
            immutable_evidence=["ev-1"],
        ),
    ],
    prohibitions=[],
    references=[],
)


async def _make_reply_draft_trace(
    tracer: SQLiteTracer,
    feedback: SQLiteFeedbackLoop,
    *,
    system_prompt: str = "You are Scout's reply drafter.",
    input_text: str = "Draft a reply.",
    model: str = "claude-haiku-4-5-20251001",
    payload: dict = _BASELINE_DRAFT_PAYLOAD,
    output_schema: type = StructuredDraftOutput,
) -> str:
    config = AgentConfig(
        name="scout_reply_draft",
        description="test baseline",
        system_prompt=system_prompt,
        llm=_FakeLLMClient([_submit_response(payload)], model=model),
        feedback=feedback,
        tracer=tracer,
        tools=ToolRegistry([]),
        output_schema=output_schema,
        max_tool_calls=1,
        max_llm_calls=4,
        max_parse_retries=2,
        include_memory_in_prompt=False,
        include_feedback_in_prompt=False,
    )
    result = await run_agent(config, input_text)
    assert result.parsed is not None, f"baseline run failed to parse: {result.error}"
    return result.trace_id


async def _seed_reply_draft_correction(
    state: StateManager,
    tracer: SQLiteTracer,
    feedback: SQLiteFeedbackLoop,
    *,
    project_key: str = "gateway",
    dossier_summary_id: str = "gateway-dossier",
    dossier_revision: str = "a" * 40,
    correction_text: str | None = CORRECTION_TEXT,
    baseline_payload: dict = _BASELINE_DRAFT_PAYLOAD,
    baseline_output_schema: type = StructuredDraftOutput,
    link_reply_revision: bool = True,
    model: str = "claude-haiku-4-5-20251001",
    scan_id: int | None = None,
    snapshot_phase_id: int | None = None,
) -> tuple[int, int]:
    """Build a complete reply_draft baseline linked to a scored, drafted
    evaluation, and (unless `link_reply_revision` is False) a grade whose
    correction points at that draft's one reply_draft_revisions row.
    Returns (phase_run_id, evaluation_id).

    `baseline_output_schema` defaults to the real StructuredDraftOutput
    contract; passing RelevancePhaseOutput (with a matching `baseline_payload`)
    seeds a baseline whose stored complete output is well-formed for *some*
    phase but does not validate as StructuredDraftOutput — the malformed-
    baseline-output eligibility gate.

    `scan_id`/`snapshot_phase_id` let a batch test share one scan (and its
    one feedback_snapshot_phases row) across several cases — pass the
    first case's own `scan_id`/`snapshot_phase_id` (from its phase_run row)
    to seed a second case under the same scan without re-recording a
    feedback snapshot for it (feedback_snapshots.scan_id is unique)."""
    trace_id = await _make_reply_draft_trace(
        tracer, feedback, payload=baseline_payload,
        output_schema=baseline_output_schema, model=model,
    )
    phase_run_id = _seed_phase_run(
        state, trace_id=trace_id, model=model, phase="reply_draft",
        scan_id=scan_id, snapshot_phase_id=snapshot_phase_id,
    )
    phase_run = state.get_phase_run(phase_run_id)
    assert phase_run is not None
    # A distinct author_id per call — several tests seed more than one
    # correction chain against the same in-memory state, and a shared
    # author_id would collide with SCOUT_AUTHOR_WEEKLY_CAP.
    author_id = f"u-{trace_id}"
    msg = Message(
        platform="discord", platform_id=f"corr-{trace_id}", channel_name="general",
        channel_id="ch-1", author_name="alice", author_id=author_id,
        content="post", created_at=datetime.now(UTC),
    )
    relevance_result = RelevanceResult(
        message=msg, relevant=True, score=0.9, reason="fit", relevant_to=("gateway",),
    )
    evaluation_id, _draft_id, _event_id = state.persist_surfaced_outcome(
        relevance_result, phase_run["post_id"], phase_run["scan_id"],
        project_key=project_key, author_id=author_id, platform="discord",
        comment_text="Baseline reply text.", structured_output=json.dumps(baseline_payload),
        contributor_phase_run_ids=[phase_run_id],
        dossier_revision=dossier_revision, dossier_summary_id=dossier_summary_id,
        allow_response_only_phase_runs=True,
    )
    if link_reply_revision:
        assert correction_text is not None
        state.save_grade_for_migration(
            GradeRecord(
                post_id=phase_run["post_id"], source="migration", graded_at=datetime.now(UTC),
                relevance_judgment="correct", evaluation_id=evaluation_id,
                edited_text=correction_text,
            ),
            migration_reason="test fixture",
        )
    return phase_run_id, evaluation_id


def _patch_resolve_dossier(
    monkeypatch, *, dossier: DossierSummary | None = _TEST_DOSSIER, error: Exception | None = None,
) -> None:
    """Replace ee.resolve_dossier with a hermetic fake — the real function
    needs a schema-bearing git checkout (see tests/test_dossier.py's
    module docstring), which these tests must not depend on."""

    def _fake_resolve_dossier(repository, revision, project_key, dossier_summary_id, **kwargs):
        if error is not None:
            raise error
        assert dossier is not None
        return DossierResolution(
            summary=dossier,
            metadata=ResolutionMetadata(
                project_key=project_key, summary_id=dossier_summary_id,
                revision=revision, path="summaries/gateway.yaml",
            ),
            known_gaps=(),
        )

    monkeypatch.setattr(ee, "resolve_dossier", _fake_resolve_dossier)


def _stub_from_model(monkeypatch, mapping: dict) -> None:
    def _fake_from_model(model: str, **_kwargs):
        if model not in mapping:
            raise ValueError(f"No provider matches model {model!r}.")
        return mapping[model]

    monkeypatch.setattr(ee, "from_model", _fake_from_model)


def _seed_phase_run(
    state: StateManager, *, trace_id: str, model: str, phase: str = "relevance",
    scan_id: int | None = None, snapshot_phase_id: int | None = None, status: str = "complete",
) -> int:
    """`scan_id`/`snapshot_phase_id` let a caller share one scan (and its
    one feedback_snapshot_phases row) across several phase runs — pass an
    existing scan's own values to add a second phase run to it without
    re-recording a feedback snapshot (feedback_snapshots.scan_id is
    unique). Both default to None, creating a fresh scan/snapshot, exactly
    as before this parameter existed."""
    if scan_id is None:
        scan_id = state.start_scan()
    if snapshot_phase_id is None:
        snapshot = state.record_feedback_snapshot(scan_id, mode="shadow")
        phase_by_name = {p.phase: p.snapshot_phase_id for p in snapshot.phases}
        snapshot_phase_id = phase_by_name[phase]
    msg = Message(
        platform="discord", platform_id=f"exp-{trace_id}", channel_name="general",
        channel_id="ch-1", author_name="alice", author_id="u1",
        content="post", created_at=datetime.now(UTC),
    )
    post_id = state.save_post(msg, scan_id)
    return state.insert_phase_run(
        scan_id=scan_id, post_id=post_id,
        snapshot_phase_id=snapshot_phase_id,
        phase=phase, trace_id=trace_id, model=model, status=status,
    )


class TestResolveBaseline:
    async def test_resolves_trusted_baseline(self, state, tracer, feedback) -> None:
        trace_id = await _make_baseline_trace(tracer, feedback)
        phase_run_id = _seed_phase_run(state, trace_id=trace_id, model="claude-haiku-4-5-20251001")

        baseline = await ee.resolve_baseline(state, tracer, phase_run_id)

        assert baseline.phase == "relevance"
        assert baseline.baseline_trace_id == trace_id
        assert baseline.baseline_model == "claude-haiku-4-5-20251001"
        assert baseline.baseline_system_prompt == "You are Scout's relevance evaluator."
        assert baseline.recorded_input == "Evaluate this message."
        assert baseline.root_span.kind == SpanKind.AGENT_RUN

    async def test_rejects_missing_phase_run(self, state, tracer) -> None:
        with pytest.raises(ee.BaselineResolutionError, match="no evaluation_phase_runs"):
            await ee.resolve_baseline(state, tracer, 999_999)

    async def test_rejects_non_positive_phase_run_id(self, state, tracer) -> None:
        with pytest.raises(ee.BaselineResolutionError, match="positive"):
            await ee.resolve_baseline(state, tracer, 0)

    async def test_rejects_trace_that_does_not_resolve(self, state, tracer) -> None:
        phase_run_id = _seed_phase_run(
            state, trace_id="unknown-trace", model="claude-haiku-4-5-20251001"
        )
        with pytest.raises(ee.BaselineResolutionError, match="root span"):
            await ee.resolve_baseline(state, tracer, phase_run_id)

    async def test_rejects_model_disagreement_with_trace(self, state, tracer, feedback) -> None:
        """Trust-boundary test: the stored evaluation_phase_runs.model must
        agree with the trace's own recorded model_id."""
        trace_id = await _make_baseline_trace(tracer, feedback, model="claude-haiku-4-5-20251001")
        phase_run_id = _seed_phase_run(state, trace_id=trace_id, model="claude-sonnet-4-99999")
        with pytest.raises(ee.BaselineResolutionError, match="disagrees"):
            await ee.resolve_baseline(state, tracer, phase_run_id)

    async def test_accepts_openrouter_configured_prefix_absent_from_trace(
        self, state, tracer, feedback
    ) -> None:
        """Jig records OpenRouter's provider-native model id after routing."""
        trace_id = await _make_baseline_trace(
            tracer, feedback, model="openai/gpt-5-mini"
        )
        phase_run_id = _seed_phase_run(
            state, trace_id=trace_id, model="openrouter/openai/gpt-5-mini"
        )

        baseline = await ee.resolve_baseline(state, tracer, phase_run_id)

        assert baseline.baseline_model == "openrouter/openai/gpt-5-mini"

    async def test_rejects_openrouter_model_disagreement_after_prefix_translation(
        self, state, tracer, feedback
    ) -> None:
        trace_id = await _make_baseline_trace(
            tracer, feedback, model="openai/gpt-5-mini"
        )
        phase_run_id = _seed_phase_run(
            state, trace_id=trace_id, model="openrouter/openai/gpt-5-nano"
        )

        with pytest.raises(ee.BaselineResolutionError, match="disagrees"):
            await ee.resolve_baseline(state, tracer, phase_run_id)


class TestBuildCandidatePlan:
    async def _baseline(self, state, tracer, feedback) -> ee.BaselineRecord:
        trace_id = await _make_baseline_trace(tracer, feedback)
        phase_run_id = _seed_phase_run(state, trace_id=trace_id, model="claude-haiku-4-5-20251001")
        return await ee.resolve_baseline(state, tracer, phase_run_id)

    async def test_no_overrides_is_a_no_op(self, state, tracer, feedback) -> None:
        baseline = await self._baseline(state, tracer, feedback)
        plan = ee.build_candidate_plan(baseline, model_override=None, system_prompt_override=None)
        assert plan.is_no_op is True
        assert plan.candidate_model == baseline.baseline_model
        assert plan.baseline_prompt_reused is True

    async def test_model_override_is_not_a_no_op(self, state, tracer, feedback) -> None:
        baseline = await self._baseline(state, tracer, feedback)
        plan = ee.build_candidate_plan(
            baseline, model_override="claude-sonnet-4-20250514", system_prompt_override=None,
        )
        assert plan.is_no_op is False
        assert plan.candidate_model == "claude-sonnet-4-20250514"
        assert plan.baseline_prompt_reused is True

    async def test_prompt_override_is_not_a_no_op(self, state, tracer, feedback) -> None:
        baseline = await self._baseline(state, tracer, feedback)
        plan = ee.build_candidate_plan(
            baseline, model_override=None, system_prompt_override="A different prompt entirely.",
        )
        assert plan.is_no_op is False
        assert plan.baseline_prompt_reused is False
        assert plan.candidate_prompt_sha256 != plan.baseline_prompt_sha256

    async def test_unroutable_model_rejected(self, state, tracer, feedback) -> None:
        baseline = await self._baseline(state, tracer, feedback)
        with pytest.raises(ee.ModelResolutionError):
            ee.build_candidate_plan(
                baseline, model_override="totally-unknown-model", system_prompt_override=None,
            )

    async def test_candidate_config_is_canonical_v2_json(self, state, tracer, feedback) -> None:
        baseline = await self._baseline(state, tracer, feedback)
        plan = ee.build_candidate_plan(
            baseline, model_override="claude-sonnet-4-20250514", system_prompt_override=None,
        )
        assert "\n" not in plan.candidate_config_json
        assert ", " not in plan.candidate_config_json
        parsed = json.loads(plan.candidate_config_json)
        assert parsed == {
            "version": 2,
            "phase": "relevance",
            "model": "claude-sonnet-4-20250514",
            "system_prompt": baseline.baseline_system_prompt,
            "system_prompt_sha256": plan.candidate_prompt_sha256,
            "grader_attached": False,
        }

    async def test_reply_draft_candidate_config_has_grader_attached(
        self, state, tracer, feedback
    ) -> None:
        trace_id = await _make_reply_draft_trace(tracer, feedback, payload=_BASELINE_DRAFT_PAYLOAD)
        phase_run_id = _seed_phase_run(
            state, trace_id=trace_id, model="claude-haiku-4-5-20251001", phase="reply_draft",
        )
        baseline = await ee.resolve_baseline(state, tracer, phase_run_id)
        plan = ee.build_candidate_plan(baseline, model_override=None, system_prompt_override=None)
        assert plan.grader_attached is True
        assert json.loads(plan.candidate_config_json)["grader_attached"] is True


class TestPreviewReplay:
    async def test_preview_makes_no_writes_and_reports_no_op(self, state, tracer, feedback) -> None:
        trace_id = await _make_baseline_trace(tracer, feedback)
        phase_run_id = _seed_phase_run(state, trace_id=trace_id, model="claude-haiku-4-5-20251001")

        preview = await ee.preview_replay(
            state=state, tracer=tracer, phase_run_id=phase_run_id,
            model_override=None, system_prompt_override=None,
        )

        assert preview.is_no_op is True
        assert preview.max_llm_calls == 4
        assert preview.recorded_input_reused is True
        count = state.conn.execute("SELECT COUNT(*) FROM evaluation_experiments").fetchone()[0]
        assert count == 0

    async def test_preview_with_override_reports_not_no_op(self, state, tracer, feedback) -> None:
        trace_id = await _make_baseline_trace(tracer, feedback)
        phase_run_id = _seed_phase_run(state, trace_id=trace_id, model="claude-haiku-4-5-20251001")

        preview = await ee.preview_replay(
            state=state, tracer=tracer, phase_run_id=phase_run_id,
            model_override="claude-sonnet-4-20250514", system_prompt_override=None,
        )
        assert preview.is_no_op is False
        assert preview.candidate_model == "claude-sonnet-4-20250514"


class TestExecuteReplay:
    async def _baseline_and_phase_run(self, state, tracer, feedback) -> tuple[str, int]:
        trace_id = await _make_baseline_trace(tracer, feedback)
        phase_run_id = _seed_phase_run(state, trace_id=trace_id, model="claude-haiku-4-5-20251001")
        return trace_id, phase_run_id

    def _patch_from_model(self, monkeypatch, mapping: dict) -> None:
        def _fake_from_model(model: str, **_kwargs):
            if model not in mapping:
                raise ValueError(f"No provider matches model {model!r}.")
            return mapping[model]
        monkeypatch.setattr(ee, "from_model", _fake_from_model)

    async def test_no_op_execution_rejected_before_any_write(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _baseline_trace_id, phase_run_id = await self._baseline_and_phase_run(
            state, tracer, feedback
        )
        self._patch_from_model(
            monkeypatch,
            {"claude-haiku-4-5-20251001": _FakeLLMClient([], model="claude-haiku-4-5-20251001")},
        )
        with pytest.raises(ee.NoOpReplayError):
            await ee.execute_replay(
                state=state, tracer=tracer, feedback=feedback, phase_run_id=phase_run_id,
                name="no-op-attempt", model_override=None, system_prompt_override=None,
            )
        count = state.conn.execute("SELECT COUNT(*) FROM evaluation_experiments").fetchone()[0]
        assert count == 0

    async def test_successful_execution_persists_candidate_and_comparison(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _baseline_trace_id, phase_run_id = await self._baseline_and_phase_run(
            state, tracer, feedback
        )
        candidate_client = _FakeLLMClient(
            [_submit_response(RELEVANCE_PAYLOAD_B)], model="claude-sonnet-4-20250514",
        )
        self._patch_from_model(monkeypatch, {"claude-sonnet-4-20250514": candidate_client})

        outcome = await ee.execute_replay(
            state=state, tracer=tracer, feedback=feedback, phase_run_id=phase_run_id,
            name="try-sonnet",
            model_override="claude-sonnet-4-20250514",
            system_prompt_override=None,
        )

        assert outcome.candidate_llm_call_count == 1
        assert outcome.candidate_cost == pytest.approx(0.001)

        row = state.get_experiment(outcome.experiment_id)
        assert row["status"] == "complete"
        assert row["candidate_trace_id"] == outcome.candidate_trace_id
        assert row["completed_at"] is not None

        comparison = state.get_trace_comparison(outcome.experiment_id)
        assert comparison is not None
        assert comparison["jig_revision"] == ee.JIG_REVISION
        trace_diff_doc = json.loads(comparison["trace_diff"])
        assert trace_diff_doc["trace_a_id"] == _baseline_trace_id
        assert trace_diff_doc["trace_b_id"] == outcome.candidate_trace_id
        assert trace_diff_doc["comparison_complete"] is True
        domain_diff_doc = json.loads(comparison["domain_diff"])
        assert domain_diff_doc["baseline"]["complete"] is True
        assert domain_diff_doc["candidate"]["complete"] is True
        assert domain_diff_doc["grader_not_attached"] is True
        # RELEVANCE_PAYLOAD_B differs on score, reason, and relevant_to.
        assert "/score" in domain_diff_doc["changes"]
        assert "/reason" in domain_diff_doc["changes"]

    async def test_retryable_schema_validation_counts_as_two_llm_calls(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        candidate_client = _FakeLLMClient(
            [_invalid_submit_response(), _submit_response(RELEVANCE_PAYLOAD_B)],
            model="claude-sonnet-4-20250514",
        )
        _baseline_trace_id, phase_run_id = await self._baseline_and_phase_run(
            state, tracer, feedback
        )
        self._patch_from_model(monkeypatch, {"claude-sonnet-4-20250514": candidate_client})

        outcome = await ee.execute_replay(
            state=state, tracer=tracer, feedback=feedback, phase_run_id=phase_run_id,
            name="retry-once",
            model_override="claude-sonnet-4-20250514",
            system_prompt_override=None,
        )
        assert outcome.candidate_llm_call_count == 2
        assert state.get_experiment(outcome.experiment_id)["status"] == "complete"

    async def test_candidate_execution_failure_marks_experiment_failed(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _baseline_trace_id, phase_run_id = await self._baseline_and_phase_run(
            state, tracer, feedback
        )
        failing_client = _FakeLLMClient(
            [],
            model="claude-sonnet-4-20250514",
            error=RuntimeError("boom: sensitive provider payload"),
        )
        self._patch_from_model(monkeypatch, {"claude-sonnet-4-20250514": failing_client})

        with pytest.raises(ee.CandidateExecutionError):
            await ee.execute_replay(
                state=state, tracer=tracer, feedback=feedback, phase_run_id=phase_run_id,
                name="will-fail",
                model_override="claude-sonnet-4-20250514",
                system_prompt_override=None,
            )

        rows = state.conn.execute(
            "SELECT id, status, error_detail FROM evaluation_experiments"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        # Sanitized, fixed, stage-specific message — never the raw exception text.
        assert rows[0]["error_detail"] == ee._STAGE_MESSAGES["candidate_execution"]
        assert "sensitive provider payload" not in rows[0]["error_detail"]
        assert len(rows[0]["error_detail"]) <= 2000

    async def test_returned_schema_failure_marks_experiment_failed(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        """Jig returns exhausted parse retries as AgentResult.error rather
        than raising; that terminal failure must never become a completed
        experiment merely because its failed AGENT_RUN trace was flushed."""
        _baseline_trace_id, phase_run_id = await self._baseline_and_phase_run(
            state, tracer, feedback
        )
        invalid_client = _FakeLLMClient(
            [_invalid_submit_response(), _invalid_submit_response(), _invalid_submit_response()],
            model="claude-sonnet-4-20250514",
        )
        self._patch_from_model(monkeypatch, {"claude-sonnet-4-20250514": invalid_client})

        with pytest.raises(ee.CandidateExecutionError):
            await ee.execute_replay(
                state=state,
                tracer=tracer,
                feedback=feedback,
                phase_run_id=phase_run_id,
                name="invalid-structured-output",
                model_override="claude-sonnet-4-20250514",
                system_prompt_override=None,
            )

        row = state.conn.execute(
            "SELECT status, candidate_trace_id, candidate_cost, candidate_llm_call_count, "
            "error_detail FROM evaluation_experiments"
        ).fetchone()
        assert row["status"] == "failed"
        assert row["candidate_trace_id"] is not None
        assert row["candidate_cost"] == pytest.approx(0.003)
        assert row["candidate_llm_call_count"] == 3
        assert row["error_detail"] == ee._STAGE_MESSAGES["candidate_execution"]
        assert state.conn.execute("SELECT COUNT(*) FROM trace_comparisons").fetchone()[0] == 0

    async def test_diff_failure_after_candidate_trace_retains_candidate_evidence(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _baseline_trace_id, phase_run_id = await self._baseline_and_phase_run(
            state, tracer, feedback
        )
        candidate_client = _FakeLLMClient(
            [_submit_response(RELEVANCE_PAYLOAD_B)], model="claude-sonnet-4-20250514",
        )
        self._patch_from_model(monkeypatch, {"claude-sonnet-4-20250514": candidate_client})

        async def _boom(*_args, **_kwargs):
            raise RuntimeError("diff blew up")

        monkeypatch.setattr(ee, "jig_trace_diff", _boom)

        with pytest.raises(ee.ComparisonConstructionError):
            await ee.execute_replay(
                state=state, tracer=tracer, feedback=feedback, phase_run_id=phase_run_id,
                name="diff-fails",
                model_override="claude-sonnet-4-20250514",
                system_prompt_override=None,
            )

        rows = state.conn.execute(
            "SELECT id, status, candidate_trace_id, candidate_llm_call_count, error_detail "
            "FROM evaluation_experiments"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        assert rows[0]["candidate_trace_id"] is not None
        assert rows[0]["candidate_llm_call_count"] == 1
        assert rows[0]["error_detail"] == ee._STAGE_MESSAGES["diff_construction"]
        assert (
            state.conn.execute("SELECT COUNT(*) FROM trace_comparisons").fetchone()[0] == 0
        )

    async def test_missing_usage_cost_is_null_not_zero(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        no_usage_response = LLMResponse(
            content="",
            tool_calls=[ToolCall(id="c1", name="submit_output", arguments=RELEVANCE_PAYLOAD_B)],
            usage=Usage(input_tokens=10, output_tokens=5, cost=None),
            latency_ms=1.0,
            model="scripted",
        )
        candidate_client = _FakeLLMClient([no_usage_response], model="claude-sonnet-4-20250514")
        _baseline_trace_id, phase_run_id = await self._baseline_and_phase_run(
            state, tracer, feedback
        )
        self._patch_from_model(monkeypatch, {"claude-sonnet-4-20250514": candidate_client})

        outcome = await ee.execute_replay(
            state=state, tracer=tracer, feedback=feedback, phase_run_id=phase_run_id,
            name="no-cost", model_override="claude-sonnet-4-20250514", system_prompt_override=None,
        )
        assert outcome.candidate_cost is None
        assert state.get_experiment(outcome.experiment_id)["candidate_cost"] is None


class TestNormalizedEditDistance:
    """normalized_edit_distance/v1: Unicode-code-point Levenshtein distance
    divided by max(len(output), len(correction), 1)."""

    def test_exact_match_is_zero(self) -> None:
        assert normalized_edit_distance("hello world", "hello world") == 0.0

    def test_both_empty_is_zero(self) -> None:
        assert normalized_edit_distance("", "") == 0.0

    def test_substitution(self) -> None:
        assert normalized_edit_distance("cat", "bat") == pytest.approx(1 / 3)

    def test_insertion(self) -> None:
        assert normalized_edit_distance("cat", "cats") == pytest.approx(1 / 4)

    def test_deletion(self) -> None:
        assert normalized_edit_distance("cats", "cat") == pytest.approx(1 / 4)

    def test_none_output_is_treated_as_empty_string(self) -> None:
        # An abstain/empty-assembling draft assembles to None; a valid
        # nonblank correction against no text scores the maximum distance.
        assert normalized_edit_distance(None, "abc") == 1.0

    def test_unicode_code_point_counting_not_utf8_bytes(self) -> None:
        # "🎉" is a single Unicode code point but 4 UTF-8 bytes; a
        # byte-based distance would compute a very different ratio than
        # this code-point-based one. One code point (é) is deleted.
        assert len("🎉café") == 5
        assert normalized_edit_distance("🎉caf", "🎉café") == pytest.approx(1 / 5)

    def test_distance_is_symmetric_property_bounded_by_max_length(self) -> None:
        distance = normalized_edit_distance("abcdef", "xyz")
        assert 0.0 <= distance <= 1.0


class TestBuildBaselineEvidence:
    async def test_base_shape_has_no_correction_fields(self, state, tracer, feedback) -> None:
        trace_id = await _make_baseline_trace(tracer, feedback)
        phase_run_id = _seed_phase_run(state, trace_id=trace_id, model="claude-haiku-4-5-20251001")
        baseline = await ee.resolve_baseline(state, tracer, phase_run_id)
        plan = ee.build_candidate_plan(baseline, model_override=None, system_prompt_override=None)

        evidence = json.loads(ee.build_baseline_evidence(baseline, plan, None))

        assert evidence == {
            "version": 2,
            "recorded_input_sha256": plan.recorded_input_sha256,
            "baseline_prompt_reused": plan.baseline_prompt_reused,
        }

    async def test_extended_shape_pins_correction_oracle(self, state, tracer, feedback) -> None:
        trace_id = await _make_reply_draft_trace(tracer, feedback)
        phase_run_id = _seed_phase_run(
            state, trace_id=trace_id, model="claude-haiku-4-5-20251001", phase="reply_draft",
        )
        baseline = await ee.resolve_baseline(state, tracer, phase_run_id)
        plan = ee.build_candidate_plan(baseline, model_override=None, system_prompt_override=None)
        oracle = ee.ReplyCorrectionOracle(
            grade_id=1, reply_revision_id=7, correction_text=CORRECTION_TEXT,
            correction_sha256=ee._sha256_utf8(CORRECTION_TEXT), project_key="gateway",
            dossier_summary_id="gateway-dossier", dossier_revision="a" * 40, dossier=_TEST_DOSSIER,
        )

        evidence = json.loads(ee.build_baseline_evidence(baseline, plan, oracle))

        assert evidence["version"] == 2
        assert evidence["reply_revision_id"] == 7
        assert evidence["correction_sha256"] == oracle.correction_sha256
        assert evidence["project_key"] == "gateway"
        assert evidence["dossier_summary_id"] == "gateway-dossier"
        assert evidence["dossier_revision"] == "a" * 40
        assert evidence["grader_version"] == "normalized_edit_distance/v1"
        assert evidence["assembler_version"] == DRAFT_TEXT_ASSEMBLER_VERSION
        assert evidence["baseline_model"] == baseline.baseline_model
        assert evidence["baseline_prompt_sha256"] == plan.baseline_prompt_sha256


class TestReplyCorrectionGrader:
    async def test_exact_match_scores_zero_as_ground_truth(self) -> None:
        grader = ReplyCorrectionGrader(dossier=_TEST_DOSSIER, correction_text=CORRECTION_TEXT)
        draft = StructuredDraftOutput.model_validate({
            "posture": "answer",
            "segments": [{"type": "declarative", "fact_id": "fact-1", "text": CORRECTION_TEXT}],
            "claims": [CORRECTION_TEXT],
            "resources_used": [],
        })

        scores = await grader.grade(input="ignored", output=draft)

        assert len(scores) == 1
        assert scores[0].dimension == "normalized_edit_distance/v1"
        assert scores[0].value == 0.0
        assert scores[0].source == ScoreSource.GROUND_TRUTH
        assert scores[0].metadata == {"assembler_version": DRAFT_TEXT_ASSEMBLER_VERSION}

    async def test_resource_segment_assembly(self) -> None:
        correction = "Resource: Gateway — https://example.com/gateway"
        grader = ReplyCorrectionGrader(dossier=_TEST_DOSSIER, correction_text=correction)
        draft = StructuredDraftOutput.model_validate({
            "posture": "answer",
            "segments": [{"type": "resource", "resource_id": "res-gateway"}],
            "claims": [],
            "resources_used": ["res-gateway"],
        })

        scores = await grader.grade(input="ignored", output=draft)

        assert scores[0].value == 0.0

    async def test_dangling_resource_segment_contributes_nothing(self) -> None:
        grader = ReplyCorrectionGrader(dossier=_TEST_DOSSIER, correction_text=CORRECTION_TEXT)
        draft = StructuredDraftOutput.model_validate({
            "posture": "answer",
            "segments": [{"type": "resource", "resource_id": "unknown-resource"}],
            "claims": [],
            "resources_used": ["unknown-resource"],
        })

        scores = await grader.grade(input="ignored", output=draft)

        # assemble_draft_text skips the unresolved resource silently and
        # produces no other part, so the assembled text is None -> "".
        assert scores[0].value == normalized_edit_distance(None, CORRECTION_TEXT)

    async def test_abstain_empty_output_scores_against_empty_string(self) -> None:
        grader = ReplyCorrectionGrader(dossier=_TEST_DOSSIER, correction_text=CORRECTION_TEXT)
        draft = StructuredDraftOutput.model_validate({
            "posture": "abstain",
            "segments": [], "claims": [], "resources_used": [],
            "abstain_reason": "not applicable",
        })

        scores = await grader.grade(input="ignored", output=draft)

        assert scores[0].value == 1.0


class TestResolveBaselineStructuredDraft:
    def _root(self, output: dict) -> Span:
        return Span(
            id="root", trace_id="t1", kind=SpanKind.AGENT_RUN, name="agent",
            started_at=datetime.now(UTC), ended_at=datetime.now(UTC), duration_ms=1.0,
            input=None, output=output,
        )

    def test_preview_only_output_is_rejected(self) -> None:
        root = self._root({"output": "preview", "scores": None})
        with pytest.raises(ee.CorrectionOracleResolutionError, match="no complete structured"):
            ee._resolve_baseline_structured_draft(root)

    def test_incomplete_structured_output_is_rejected(self) -> None:
        root = self._root({"output": "preview", "scores": None, "output_kind": "structured"})
        with pytest.raises(ee.CorrectionOracleResolutionError, match="no complete structured"):
            ee._resolve_baseline_structured_draft(root)

    def test_malformed_output_fails_pydantic_validation(self) -> None:
        root = self._root({
            "output": "preview", "scores": None, "output_kind": "structured",
            "output_complete": {"not": "a valid draft"},
            "output_sha256": "deadbeef", "output_byte_length": 10,
        })
        with pytest.raises(ee.CorrectionOracleResolutionError, match="does not validate"):
            ee._resolve_baseline_structured_draft(root)

    def test_complete_output_validates(self) -> None:
        root = self._root({
            "output": "preview", "scores": None, "output_kind": "structured",
            "output_complete": _BASELINE_DRAFT_PAYLOAD,
            "output_sha256": "deadbeef", "output_byte_length": 10,
        })
        draft = ee._resolve_baseline_structured_draft(root)
        assert draft.posture == "answer"


class TestVerifyCorrectionHash:
    async def test_matching_hash_passes(self, state, tracer, feedback) -> None:
        _phase_run_id, evaluation_id = await _seed_reply_draft_correction(state, tracer, feedback)
        grade_id = state.get_grade_id_for_evaluation(evaluation_id)
        assert grade_id is not None
        row = state.get_grade_row_by_id(grade_id)
        assert row is not None
        oracle = ee.ReplyCorrectionOracle(
            grade_id=grade_id, reply_revision_id=row["reply_revision_id"],
            correction_text=CORRECTION_TEXT, correction_sha256=ee._sha256_utf8(CORRECTION_TEXT),
            project_key="gateway", dossier_summary_id="gateway-dossier", dossier_revision="a" * 40,
            dossier=_TEST_DOSSIER,
        )
        ee._verify_correction_hash(state, oracle)  # does not raise

    async def test_mismatched_hash_raises(self, state, tracer, feedback) -> None:
        _phase_run_id, evaluation_id = await _seed_reply_draft_correction(state, tracer, feedback)
        grade_id = state.get_grade_id_for_evaluation(evaluation_id)
        assert grade_id is not None
        row = state.get_grade_row_by_id(grade_id)
        assert row is not None
        oracle = ee.ReplyCorrectionOracle(
            grade_id=grade_id, reply_revision_id=row["reply_revision_id"],
            correction_text="tampered", correction_sha256=ee._sha256_utf8("tampered"),
            project_key="gateway", dossier_summary_id="gateway-dossier", dossier_revision="a" * 40,
            dossier=_TEST_DOSSIER,
        )
        with pytest.raises(ee.CorrectionEvidenceIntegrityError):
            ee._verify_correction_hash(state, oracle)


class TestResolveReplyCorrectionOracle:
    async def test_happy_path_resolves_oracle(self, state, tracer, feedback, monkeypatch) -> None:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id, _evaluation_id = await _seed_reply_draft_correction(state, tracer, feedback)
        phase_run = state.get_phase_run(phase_run_id)
        assert phase_run is not None

        oracle = ee.resolve_reply_correction_oracle(state, phase_run, dossier_root=Path("/unused"))

        assert oracle.correction_text == CORRECTION_TEXT
        assert oracle.correction_sha256 == ee._sha256_utf8(CORRECTION_TEXT)
        assert oracle.project_key == "gateway"
        assert oracle.dossier_summary_id == "gateway-dossier"
        assert oracle.dossier_revision == "a" * 40
        assert oracle.dossier is _TEST_DOSSIER

    async def test_unlinked_phase_run_is_ineligible(self, state, tracer, feedback) -> None:
        trace_id = await _make_reply_draft_trace(tracer, feedback)
        phase_run_id = _seed_phase_run(
            state, trace_id=trace_id, model="claude-haiku-4-5-20251001", phase="reply_draft",
        )
        phase_run = state.get_phase_run(phase_run_id)
        assert phase_run is not None
        with pytest.raises(ee.CorrectionOracleResolutionError, match="not linked to an evaluation"):
            ee.resolve_reply_correction_oracle(state, phase_run, dossier_root=Path("/unused"))

    async def test_missing_grade_is_ineligible(self, state, tracer, feedback) -> None:
        phase_run_id, _evaluation_id = await _seed_reply_draft_correction(
            state, tracer, feedback, link_reply_revision=False,
        )
        phase_run = state.get_phase_run(phase_run_id)
        assert phase_run is not None
        with pytest.raises(ee.CorrectionOracleResolutionError, match="no grade"):
            ee.resolve_reply_correction_oracle(state, phase_run, dossier_root=Path("/unused"))

    async def test_grade_with_no_reply_revision_is_ineligible(
        self, state, tracer, feedback
    ) -> None:
        phase_run_id, evaluation_id = await _seed_reply_draft_correction(
            state, tracer, feedback, link_reply_revision=False,
        )
        phase_run = state.get_phase_run(phase_run_id)
        assert phase_run is not None
        state.save_grade_for_migration(
            GradeRecord(
                post_id=phase_run["post_id"], source="migration", graded_at=datetime.now(UTC),
                relevance_judgment="correct", evaluation_id=evaluation_id,
            ),
            migration_reason="test fixture",
        )
        with pytest.raises(ee.CorrectionOracleResolutionError, match="no reply_revision_id"):
            ee.resolve_reply_correction_oracle(state, phase_run, dossier_root=Path("/unused"))

    async def test_revision_with_no_owning_draft_is_ineligible(
        self, state, tracer, feedback
    ) -> None:
        """A reply_draft_revisions row that does not resolve to any
        draft_comments row (owner_row is None) is rejected outright.

        reply_draft_revisions.draft_comment_id -> draft_comments(id) is
        itself FK-enforced, so a dangling revision can never arise from any
        real write path — this constructs one directly (with FK checks
        off, restored immediately after) purely to exercise the read-side
        defensive check for it.
        """
        phase_run_id, evaluation_id = await _seed_reply_draft_correction(
            state, tracer, feedback, link_reply_revision=False,
        )
        phase_run = state.get_phase_run(phase_run_id)
        assert phase_run is not None

        state.db.set_foreign_keys(False)
        try:
            cursor = state.conn.execute(
                "INSERT INTO reply_draft_revisions "
                "(draft_comment_id, version, parent_revision_id, reply_text, source, created_at) "
                "VALUES (?, ?, NULL, ?, ?, ?)",
                (
                    999_999, 1, "orphaned correction text", "migration",
                    datetime.now(UTC).isoformat(),
                ),
            )
            dangling_revision_id = cursor.lastrowid
            assert dangling_revision_id is not None
            state.commit()

            state.save_grade_for_migration(
                GradeRecord(
                    post_id=phase_run["post_id"], source="migration", graded_at=datetime.now(UTC),
                    relevance_judgment="correct", evaluation_id=evaluation_id,
                ),
                migration_reason="test fixture",
            )
            grade_id = state.get_grade_id_for_evaluation(evaluation_id)
            state.conn.execute(
                "UPDATE grades SET reply_revision_id = ? WHERE id = ?",
                (dangling_revision_id, grade_id),
            )
            state.commit()
        finally:
            state.db.set_foreign_keys(True)

        with pytest.raises(ee.CorrectionOracleResolutionError, match="does not resolve to a draft"):
            ee.resolve_reply_correction_oracle(state, phase_run, dossier_root=Path("/unused"))

    async def test_blank_correction_is_ineligible(self, state, tracer, feedback) -> None:
        phase_run_id, _evaluation_id = await _seed_reply_draft_correction(
            state, tracer, feedback, correction_text="   ",
        )
        phase_run = state.get_phase_run(phase_run_id)
        assert phase_run is not None
        with pytest.raises(ee.CorrectionOracleResolutionError, match="blank"):
            ee.resolve_reply_correction_oracle(state, phase_run, dossier_root=Path("/unused"))

    async def test_missing_dossier_pin_is_ineligible(self, state, tracer, feedback) -> None:
        phase_run_id, _evaluation_id = await _seed_reply_draft_correction(
            state, tracer, feedback, dossier_summary_id="", dossier_revision="",
        )
        phase_run = state.get_phase_run(phase_run_id)
        assert phase_run is not None
        with pytest.raises(ee.CorrectionOracleResolutionError, match="pinned"):
            ee.resolve_reply_correction_oracle(state, phase_run, dossier_root=Path("/unused"))

    async def test_unresolvable_dossier_is_ineligible(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _patch_resolve_dossier(
            monkeypatch, dossier=None,
            error=DossierResolutionError(
                "boom", project_key="gateway", summary_id="gateway-dossier",
            ),
        )
        phase_run_id, _evaluation_id = await _seed_reply_draft_correction(state, tracer, feedback)
        phase_run = state.get_phase_run(phase_run_id)
        assert phase_run is not None
        with pytest.raises(ee.CorrectionOracleResolutionError, match="could not be resolved"):
            ee.resolve_reply_correction_oracle(state, phase_run, dossier_root=Path("/unused"))

    async def test_mismatched_revision_ownership_is_ineligible(
        self, state, tracer, feedback
    ) -> None:
        """A grade whose reply_revision_id points at a *different*
        evaluation's draft must be rejected outright, never trusted."""
        phase_run_id_a, evaluation_id_a = await _seed_reply_draft_correction(
            state, tracer, feedback, link_reply_revision=False,
        )
        _phase_run_id_b, evaluation_id_b = await _seed_reply_draft_correction(
            state, tracer, feedback,
        )
        other_grade_id = state.get_grade_id_for_evaluation(evaluation_id_b)
        assert other_grade_id is not None
        other_row = state.get_grade_row_by_id(other_grade_id)
        assert other_row is not None

        phase_run_a = state.get_phase_run(phase_run_id_a)
        assert phase_run_a is not None
        state.save_grade_for_migration(
            GradeRecord(
                post_id=phase_run_a["post_id"], source="migration", graded_at=datetime.now(UTC),
                relevance_judgment="correct", evaluation_id=evaluation_id_a,
            ),
            migration_reason="test fixture",
        )
        grade_id_a = state.get_grade_id_for_evaluation(evaluation_id_a)
        state.conn.execute(
            "UPDATE grades SET reply_revision_id = ? WHERE id = ?",
            (other_row["reply_revision_id"], grade_id_a),
        )
        state.commit()

        with pytest.raises(ee.CorrectionOracleResolutionError, match="mismatched"):
            ee.resolve_reply_correction_oracle(state, phase_run_a, dossier_root=Path("/unused"))

    async def test_later_regrade_pointer_movement_resolves_to_latest_revision(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id, evaluation_id = await _seed_reply_draft_correction(
            state, tracer, feedback, correction_text="First correction.",
        )
        phase_run = state.get_phase_run(phase_run_id)
        assert phase_run is not None

        first = ee.resolve_reply_correction_oracle(state, phase_run, dossier_root=Path("/unused"))
        assert first.correction_text == "First correction."

        state.save_grade_for_migration(
            GradeRecord(
                post_id=phase_run["post_id"], source="migration", graded_at=datetime.now(UTC),
                relevance_judgment="correct", evaluation_id=evaluation_id,
                edited_text="Second, regraded correction.",
            ),
            migration_reason="test fixture",
        )

        second = ee.resolve_reply_correction_oracle(state, phase_run, dossier_root=Path("/unused"))
        assert second.correction_text == "Second, regraded correction."
        assert second.reply_revision_id != first.reply_revision_id


class TestExecuteReplayReplyDraft:
    async def test_ineligible_case_never_reaches_from_model(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        phase_run_id, _evaluation_id = await _seed_reply_draft_correction(
            state, tracer, feedback, link_reply_revision=False,
        )

        def _spy_from_model(*_args, **_kwargs):
            raise AssertionError("from_model must not be called for an ineligible case")

        monkeypatch.setattr(ee, "from_model", _spy_from_model)

        with pytest.raises(ee.CorrectionOracleResolutionError):
            await ee.execute_replay(
                state=state, tracer=tracer, feedback=feedback, phase_run_id=phase_run_id,
                name="ineligible", model_override=None, system_prompt_override=None,
                dossier_root=Path("/unused"),
            )

        assert state.conn.execute(
            "SELECT COUNT(*) FROM evaluation_experiments"
        ).fetchone()[0] == 0
        assert state.conn.execute("SELECT COUNT(*) FROM experiment_runs").fetchone()[0] == 0

    async def test_graded_replay_persists_matching_score_evidence(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id, _evaluation_id = await _seed_reply_draft_correction(
            state, tracer, feedback, correction_text=CORRECTION_TEXT,
            baseline_payload=_BASELINE_DRAFT_PAYLOAD,
        )
        # Candidate assembles to exactly the correction text (a single
        # declarative segment), so the candidate distance is a perfect 0.0
        # — an unambiguous improvement over the (different) baseline.
        candidate_payload = {
            "posture": "answer",
            "segments": [{"type": "declarative", "fact_id": "fact-1", "text": CORRECTION_TEXT}],
            "claims": [CORRECTION_TEXT],
            "resources_used": [],
        }
        candidate_client = _FakeLLMClient(
            [_submit_response(candidate_payload)], model="claude-sonnet-4-20250514",
        )
        _stub_from_model(monkeypatch, {"claude-sonnet-4-20250514": candidate_client})

        outcome = await ee.execute_replay(
            state=state, tracer=tracer, feedback=feedback, phase_run_id=phase_run_id,
            name="graded-replay", model_override="claude-sonnet-4-20250514",
            system_prompt_override=None, dossier_root=Path("/unused"),
        )

        comparison = state.get_trace_comparison(outcome.experiment_id)
        assert comparison is not None
        score_evidence = json.loads(comparison["score_evidence"])
        assert score_evidence["grader_attached"] is True
        assert score_evidence["grader_version"] == "normalized_edit_distance/v1"
        assert score_evidence["assembler_version"] == DRAFT_TEXT_ASSEMBLER_VERSION
        assert score_evidence["candidate_distance"] == 0.0
        assert score_evidence["correction_sha256"] == ee._sha256_utf8(CORRECTION_TEXT)

        baseline_draft = StructuredDraftOutput.model_validate(_BASELINE_DRAFT_PAYLOAD)
        baseline_text = assemble_draft_text(baseline_draft, _TEST_DOSSIER)
        expected_baseline_distance = normalized_edit_distance(baseline_text, CORRECTION_TEXT)
        assert score_evidence["baseline_distance"] == pytest.approx(expected_baseline_distance)
        assert score_evidence["delta"] == pytest.approx(0.0 - expected_baseline_distance)
        assert score_evidence["delta"] < 0  # candidate is an improvement

        run = state.get_experiment_run(outcome.experiment_run_id)
        assert run is not None
        assert run["status"] == "complete"
        assert json.loads(run["candidate_config"])["grader_attached"] is True

        attempt = state.get_experiment(outcome.experiment_id)
        assert attempt is not None
        baseline_evidence = json.loads(attempt["baseline_evidence"])
        assert baseline_evidence["reply_revision_id"] == score_evidence["reply_revision_id"]
        assert baseline_evidence["grader_version"] == "normalized_edit_distance/v1"
        assert baseline_evidence["assembler_version"] == DRAFT_TEXT_ASSEMBLER_VERSION

    async def test_malformed_candidate_output_still_fails_cleanly_when_graded(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id, _evaluation_id = await _seed_reply_draft_correction(state, tracer, feedback)
        invalid_client = _FakeLLMClient(
            [_invalid_submit_response(), _invalid_submit_response(), _invalid_submit_response()],
            model="claude-sonnet-4-20250514",
        )
        _stub_from_model(monkeypatch, {"claude-sonnet-4-20250514": invalid_client})

        with pytest.raises(ee.CandidateExecutionError):
            await ee.execute_replay(
                state=state, tracer=tracer, feedback=feedback, phase_run_id=phase_run_id,
                name="malformed", model_override="claude-sonnet-4-20250514",
                system_prompt_override=None, dossier_root=Path("/unused"),
            )

        row = state.conn.execute(
            "SELECT status, error_detail FROM evaluation_experiments"
        ).fetchone()
        assert row["status"] == "failed"
        assert row["error_detail"] == ee._STAGE_MESSAGES["candidate_execution"]
        assert state.conn.execute("SELECT COUNT(*) FROM trace_comparisons").fetchone()[0] == 0


class TestBuildDomainDiff:
    def _root(
        self,
        trace_id: str,
        *,
        complete_payload=None,
        preview_only=False,
        structured_missing=False,
    ) -> Span:
        out: dict = {"output": "preview", "scores": None}
        if not preview_only:
            out["output_kind"] = "structured"
            if not structured_missing:
                out["output_complete"] = complete_payload
                out["output_sha256"] = "deadbeef"
                out["output_byte_length"] = 42
        return Span(
            id=f"{trace_id}-root", trace_id=trace_id, kind=SpanKind.AGENT_RUN, name="agent",
            started_at=datetime.now(UTC), ended_at=datetime.now(UTC), duration_ms=1.0,
            input=None, output=out,
        )

    def test_scalar_change_reported_at_pointer(self) -> None:
        a = self._root("a", complete_payload={"score": 0.1, "reason": "x"})
        b = self._root("b", complete_payload={"score": 0.9, "reason": "x"})
        diff = ee.build_domain_diff(a, b)
        assert diff["changes"] == ["/score"]
        assert diff["additions"] == []
        assert diff["removals"] == []

    def test_added_and_removed_keys(self) -> None:
        a = self._root("a", complete_payload={"kept": 1, "removed_key": 2})
        b = self._root("b", complete_payload={"kept": 1, "added_key": 3})
        diff = ee.build_domain_diff(a, b)
        assert diff["additions"] == ["/added_key"]
        assert diff["removals"] == ["/removed_key"]

    def test_array_order_matters(self) -> None:
        a = self._root("a", complete_payload={"tags": ["x", "y"]})
        b = self._root("b", complete_payload={"tags": ["y", "x"]})
        diff = ee.build_domain_diff(a, b)
        assert diff["changes"] == ["/tags/0", "/tags/1"]

    def test_array_length_change_reports_addition(self) -> None:
        a = self._root("a", complete_payload={"tags": ["x"]})
        b = self._root("b", complete_payload={"tags": ["x", "y"]})
        diff = ee.build_domain_diff(a, b)
        assert diff["additions"] == ["/tags/1"]

    def test_pointer_escaping(self) -> None:
        a = self._root("a", complete_payload={"a/b": 1, "c~d": 2})
        b = self._root("b", complete_payload={"a/b": 9, "c~d": 2})
        diff = ee.build_domain_diff(a, b)
        assert diff["changes"] == ["/a~1b"]

    def test_root_scalar_mismatch_uses_empty_pointer(self) -> None:
        a = self._root("a", complete_payload={"x": {"nested": 1}})
        b = self._root("b", complete_payload={"x": [1]})
        diff = ee.build_domain_diff(a, b)
        assert diff["changes"] == ["/x"]

    def test_preview_only_side_reports_incomplete_and_omits_field_diff(self) -> None:
        a = self._root("a", preview_only=True)
        b = self._root("b", complete_payload={"x": 1})
        diff = ee.build_domain_diff(a, b)
        assert diff["baseline"]["complete"] is False
        assert diff["baseline"]["incomplete_reason"] == "preview_only_output"
        assert diff["baseline"]["sha256"] is None
        assert "value" not in diff["baseline"]
        assert "additions" not in diff
        assert "removals" not in diff
        assert "changes" not in diff

    def test_structured_output_unavailable_side(self) -> None:
        a = self._root("a", structured_missing=True)
        b = self._root("b", complete_payload={"x": 1})
        diff = ee.build_domain_diff(a, b)
        assert diff["baseline"]["complete"] is False
        assert diff["baseline"]["incomplete_reason"] == "structured_output_unavailable"

    def test_grader_not_attached_flag_always_present(self) -> None:
        a = self._root("a", complete_payload={"x": 1})
        b = self._root("b", complete_payload={"x": 1})
        diff = ee.build_domain_diff(a, b)
        assert diff["grader_not_attached"] is True
        assert diff["changes"] == []


class TestSerializeTraceDiff:
    """The pinned Jig TraceDiff must be persisted one-to-one — every field
    the dataclass carries, none dropped, no `default=str` escape hatch,
    and non-finite numbers rejected before they ever reach the database."""

    def _diff(self, **overrides) -> TraceDiff:
        defaults = dict(
            trace_a_id="a", trace_b_id="b", tool_divergence=[], output_diff=None,
            error_category_change=None, score_deltas={"outcome_semantics": 0.5},
            score_details={"outcome_semantics": (0.4, 0.9)}, cost_delta=0.002,
            latency_ms_delta=15.0, comparison_complete=True, comparison_incomplete_reason=None,
            a_output_preview="preview-a", b_output_preview="preview-b",
            a_output_hash="hash-a", b_output_hash="hash-b",
            a_output_byte_length=10, b_output_byte_length=12,
            a_output_complete={"x": 1}, b_output_complete={"x": 2},
        )
        defaults.update(overrides)
        return TraceDiff(**defaults)

    def test_serialization_is_exactly_dataclasses_asdict(self) -> None:
        diff = self._diff()
        serialized = ee._serialize_trace_diff(diff)
        # JSON has no tuple type, so score_details' tuple values normalize to
        # lists on any round trip — re-normalize dataclasses.asdict(diff)
        # through plain json.dumps/loads (no canonicalization) for an
        # apples-to-apples comparison against the one-to-one serialization.
        expected = json.loads(json.dumps(dataclasses.asdict(diff)))
        assert json.loads(serialized) == expected

    def test_serialization_loads_without_default_str(self) -> None:
        diff = self._diff()
        serialized = ee._serialize_trace_diff(diff)
        # json.loads with no custom decoder proves every value round-tripped
        # as a native JSON type — a `default=str` escape hatch would have
        # been needed at dump time if anything were non-native, and none
        # was used here.
        loaded = json.loads(serialized)
        assert loaded["trace_a_id"] == "a"
        assert loaded["score_deltas"]["outcome_semantics"] == 0.5
        assert loaded["score_details"]["outcome_semantics"] == [0.4, 0.9]

    def test_non_finite_cost_delta_rejected(self) -> None:
        diff = self._diff(cost_delta=float("nan"))
        with pytest.raises(ee.ComparisonConstructionError):
            ee._serialize_trace_diff(diff)

    def test_non_finite_score_rejected(self) -> None:
        diff = self._diff(score_deltas={"outcome_semantics": float("inf")})
        with pytest.raises(ee.ComparisonConstructionError):
            ee._serialize_trace_diff(diff)


class TestSerializeDomainDiff:
    def test_canonical_formatting(self) -> None:
        domain_diff = {"b": 1, "a": 2}
        serialized = ee._serialize_domain_diff(domain_diff)
        assert serialized == '{"a":2,"b":1}'

    def test_non_finite_value_rejected(self) -> None:
        with pytest.raises(ee.ComparisonConstructionError):
            ee._serialize_domain_diff({"score": float("nan")})


class TestExampleFixtures:
    """docs/operations/offline-replay.md points operators at these example
    documents — pin them against the real code so they can never silently
    drift into showing a shape or a hash the implementation no longer
    produces."""

    _FIXTURES_DIR = "tests/fixtures/evaluation_experiments"

    def _load(self, name: str) -> dict:
        with open(f"{self._FIXTURES_DIR}/{name}", encoding="utf-8") as f:
            return json.load(f)

    def test_candidate_config_v1_fixture_is_canonical(self) -> None:
        """candidate_config_v1.json is retained as historical input evidence
        for the v36 migration fixtures (see TestMigration36ExperimentEvidence
        in test_state_manager.py) — it is deliberately never asserted
        against the current CANDIDATE_CONFIG_VERSION."""
        with open(f"{self._FIXTURES_DIR}/candidate_config_v1.json", encoding="utf-8") as f:
            raw = f.read().rstrip("\n")
        doc = json.loads(raw)
        assert raw == json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        assert doc["system_prompt_sha256"] == ee._sha256_utf8(doc["system_prompt"])
        assert doc["version"] == 1

    def test_candidate_config_v2_fixture_is_canonical_and_hash_matches(self) -> None:
        with open(f"{self._FIXTURES_DIR}/candidate_config_v2.json", encoding="utf-8") as f:
            raw = f.read().rstrip("\n")
        doc = json.loads(raw)
        assert raw == json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        assert doc["system_prompt_sha256"] == ee._sha256_utf8(doc["system_prompt"])
        assert doc["version"] == ee.CANDIDATE_CONFIG_VERSION
        assert "recorded_input_sha256" not in doc
        assert "baseline_prompt_reused" not in doc

    def test_baseline_evidence_v2_fixture_is_canonical_and_hash_matches(self) -> None:
        with open(f"{self._FIXTURES_DIR}/baseline_evidence_v2.json", encoding="utf-8") as f:
            raw = f.read().rstrip("\n")
        doc = json.loads(raw)
        assert raw == json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        assert doc["version"] == ee.BASELINE_EVIDENCE_VERSION
        assert doc["grader_version"] == "normalized_edit_distance/v1"
        assert doc["assembler_version"] == DRAFT_TEXT_ASSEMBLER_VERSION

    def test_domain_diff_fixture_matches_build_domain_diff(self) -> None:
        doc = self._load("domain_diff_example.json")
        baseline_value = doc["baseline"]["value"]
        candidate_value = doc["candidate"]["value"]

        def _root(trace_id: str, value: dict) -> Span:
            canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            sha256 = ee._sha256_utf8(canonical)
            byte_length = len(canonical.encode("utf-8"))
            return Span(
                id=f"{trace_id}-root", trace_id=trace_id, kind=SpanKind.AGENT_RUN, name="agent",
                started_at=datetime.now(UTC), ended_at=datetime.now(UTC), duration_ms=1.0,
                input=None,
                output={
                    "output": "preview",
                    "scores": None,
                    "output_kind": "structured",
                    "output_complete": value,
                    "output_sha256": sha256,
                    "output_byte_length": byte_length,
                },
            )

        rebuilt = ee.build_domain_diff(_root("a", baseline_value), _root("b", candidate_value))
        assert rebuilt["additions"] == doc["additions"]
        assert rebuilt["removals"] == doc["removals"]
        assert rebuilt["changes"] == doc["changes"]
        assert rebuilt["baseline"]["sha256"] == doc["baseline"]["sha256"]
        assert rebuilt["baseline"]["utf8_byte_length"] == doc["baseline"]["utf8_byte_length"]
        assert rebuilt["candidate"]["sha256"] == doc["candidate"]["sha256"]
        assert rebuilt["candidate"]["utf8_byte_length"] == doc["candidate"]["utf8_byte_length"]


# ---------------------------------------------------------------------------
# Batch/sweep replay: selectors, classification, plan hashing, sweeps,
# batch/sweep execution and retry.
# ---------------------------------------------------------------------------


def _pricing_catalog_for_tests() -> rp.PricingCatalog:
    return rp.PricingCatalog(
        version=1, as_of="2026-01-01", source_url="https://example.com", catalog_hash="deadbeef",
        models={
            "claude-haiku-4-5-20251001": rp.ModelRate(
                input_usd_per_million=1.0, output_usd_per_million=5.0,
            ),
            "claude-sonnet-4-20250514": rp.ModelRate(
                input_usd_per_million=3.0, output_usd_per_million=15.0,
            ),
        },
    )


_GRADED_CANDIDATE_PAYLOAD = {
    "posture": "answer",
    "segments": [{"type": "declarative", "fact_id": "fact-1", "text": CORRECTION_TEXT}],
    "claims": [CORRECTION_TEXT],
    "resources_used": [],
}


class TestBatchSelectors:
    async def test_phase_run_ids_stable_order_and_dedup_of_input(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        a, _ = await _seed_reply_draft_correction(state, tracer, feedback)
        b, _ = await _seed_reply_draft_correction(state, tracer, feedback)
        population = ee.resolve_batch_population(
            state, ee.BatchSelector.by_phase_run_ids([b, a, a]),
        )
        assert population.phase_run_ids == tuple(sorted([a, b]))
        assert population.dropped_duplicate_phase_run_ids == ()

    def test_phase_run_ids_requires_at_least_one(self, state) -> None:
        with pytest.raises(ee.SelectorResolutionError, match="at least one"):
            ee.resolve_batch_population(state, ee.BatchSelector.by_phase_run_ids([]))

    def test_phase_run_ids_rejects_missing(self, state) -> None:
        with pytest.raises(ee.SelectorResolutionError, match="no evaluation_phase_runs"):
            ee.resolve_batch_population(state, ee.BatchSelector.by_phase_run_ids([999_999]))

    async def test_phase_run_ids_rejects_non_reply_draft_phase(
        self, state, tracer, feedback
    ) -> None:
        trace_id = await _make_baseline_trace(tracer, feedback)
        phase_run_id = _seed_phase_run(state, trace_id=trace_id, model="claude-haiku-4-5-20251001")
        with pytest.raises(ee.SelectorResolutionError, match="not 'reply_draft'"):
            ee.resolve_batch_population(state, ee.BatchSelector.by_phase_run_ids([phase_run_id]))

    async def test_phase_run_ids_rejects_incomplete_status(self, state, tracer, feedback) -> None:
        trace_id = await _make_reply_draft_trace(tracer, feedback)
        phase_run_id = _seed_phase_run(
            state, trace_id=trace_id, model="claude-haiku-4-5-20251001", phase="reply_draft",
            status="error",
        )
        with pytest.raises(ee.SelectorResolutionError, match="not 'complete'"):
            ee.resolve_batch_population(state, ee.BatchSelector.by_phase_run_ids([phase_run_id]))

    async def test_scan_id_selects_only_that_scan(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id_a, _ = await _seed_reply_draft_correction(state, tracer, feedback)
        phase_run_a = state.get_phase_run(phase_run_id_a)
        phase_run_id_b, _ = await _seed_reply_draft_correction(
            state, tracer, feedback,
            scan_id=phase_run_a["scan_id"], snapshot_phase_id=phase_run_a["snapshot_phase_id"],
        )
        await _seed_reply_draft_correction(state, tracer, feedback)  # a different scan

        population = ee.resolve_batch_population(
            state, ee.BatchSelector.by_scan_id(phase_run_a["scan_id"]),
        )
        assert population.phase_run_ids == tuple(sorted([phase_run_id_a, phase_run_id_b]))

    async def test_window_is_half_open_utc(self, state, tracer, feedback, monkeypatch) -> None:
        """evaluation_phase_runs is append-only (no controllable created_at
        override), so the exact [from, to) boundary is tested with a case's
        own real, stored created_at used verbatim as a bound: used as `to`,
        that same case must be excluded (created_at < to is false at
        equality); used as `from`, it must be included (created_at >= from
        is true at equality)."""
        _patch_resolve_dossier(monkeypatch)
        case_id, _ = await _seed_reply_draft_correction(state, tracer, feedback)
        created_at = state.get_phase_run(case_id)["created_at"]

        with pytest.raises(ee.SelectorResolutionError, match="empty population"):
            ee.resolve_batch_population(
                state, ee.BatchSelector.by_window("2000-01-01T00:00:00+00:00", created_at),
            )

        included_at_from = ee.resolve_batch_population(
            state, ee.BatchSelector.by_window(created_at, "2999-01-01T00:00:00+00:00"),
        )
        assert included_at_from.phase_run_ids == (case_id,)

    def test_window_rejects_from_not_before_to(self, state) -> None:
        with pytest.raises(ee.SelectorResolutionError, match="strictly before"):
            ee.resolve_batch_population(
                state,
                ee.BatchSelector.by_window(
                    "2026-01-02T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
                ),
            )

    async def test_graded_with_corrections_filters_to_eligible(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        eligible, _ = await _seed_reply_draft_correction(state, tracer, feedback)
        await _seed_reply_draft_correction(state, tracer, feedback, link_reply_revision=False)

        population = ee.resolve_batch_population(state, ee.BatchSelector.graded_with_corrections())
        assert population.phase_run_ids == (eligible,)

    def test_empty_population_raises(self, state) -> None:
        with pytest.raises(ee.SelectorResolutionError, match="empty population"):
            ee.resolve_batch_population(state, ee.BatchSelector.graded_with_corrections())

    async def test_duplicate_baselines_removed_keeping_highest_phase_run_id(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        """evaluation_phase_runs.evaluation_id may only be set once per row
        (never reassigned), so a duplicate baseline — two phase_run rows
        for the same evaluation, e.g. from reprocessing — is constructed
        here with a direct second INSERT already carrying the first row's
        evaluation_id, rather than an UPDATE."""
        _patch_resolve_dossier(monkeypatch)
        first, evaluation_id = await _seed_reply_draft_correction(state, tracer, feedback)
        first_row = state.get_phase_run(first)
        trace_id = await _make_reply_draft_trace(tracer, feedback)
        with state.db.begin_immediate():
            cursor = state.conn.execute(
                "INSERT INTO evaluation_phase_runs (scan_id, post_id, evaluation_id, "
                "snapshot_phase_id, phase, trace_id, model, status, created_at) "
                "VALUES (?, ?, ?, ?, 'reply_draft', ?, ?, 'complete', ?)",
                (
                    first_row["scan_id"], first_row["post_id"], evaluation_id,
                    first_row["snapshot_phase_id"], trace_id, "claude-haiku-4-5-20251001",
                    datetime.now(UTC).isoformat(),
                ),
            )
            second = cursor.lastrowid
        assert second is not None

        population = ee.resolve_batch_population(
            state, ee.BatchSelector.by_phase_run_ids([first, second]),
        )
        assert population.phase_run_ids == (max(first, second),)
        assert population.dropped_duplicate_phase_run_ids == (min(first, second),)


class TestBuildBatchPlanClassification:
    async def test_no_override_is_a_no_op_pair(self, state, tracer, feedback, monkeypatch) -> None:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id, _ = await _seed_reply_draft_correction(state, tracer, feedback)
        plan = await ee.build_batch_plan(
            state=state, tracer=tracer, selector=ee.BatchSelector.by_phase_run_ids([phase_run_id]),
            variants=(ee.BatchVariant(ee.DEFAULT_BATCH_VARIANT_NAME, None, None),),
            skip_policy=ee.SkipPolicy(), pricing_catalog=_pricing_catalog_for_tests(),
            dossier_root=Path("/unused"),
        )
        assert len(plan.pairs) == 1
        assert plan.pairs[0].classification == "no_op"

    async def test_no_grade_is_unscored(self, state, tracer, feedback, monkeypatch) -> None:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id, _ = await _seed_reply_draft_correction(
            state, tracer, feedback, link_reply_revision=False,
        )
        plan = await ee.build_batch_plan(
            state=state, tracer=tracer, selector=ee.BatchSelector.by_phase_run_ids([phase_run_id]),
            variants=(
                ee.BatchVariant(ee.DEFAULT_BATCH_VARIANT_NAME, "claude-sonnet-4-20250514", None),
            ),
            skip_policy=ee.SkipPolicy(), pricing_catalog=_pricing_catalog_for_tests(),
            dossier_root=Path("/unused"),
        )
        assert plan.pairs[0].classification == "unscored"
        assert plan.pairs[0].reason is not None

    async def test_uncataloged_candidate_model_is_unpriceable(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id, _ = await _seed_reply_draft_correction(state, tracer, feedback)
        plan = await ee.build_batch_plan(
            state=state, tracer=tracer, selector=ee.BatchSelector.by_phase_run_ids([phase_run_id]),
            variants=(
                ee.BatchVariant(ee.DEFAULT_BATCH_VARIANT_NAME, "claude-sonnet-4-99999", None),
            ),
            skip_policy=ee.SkipPolicy(), pricing_catalog=_pricing_catalog_for_tests(),
            dossier_root=Path("/unused"),
        )
        assert plan.pairs[0].classification == "unpriceable"
        assert plan.pairs[0].price_estimate is None

    async def test_fully_eligible_pair_is_scored(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id, _ = await _seed_reply_draft_correction(state, tracer, feedback)
        plan = await ee.build_batch_plan(
            state=state, tracer=tracer, selector=ee.BatchSelector.by_phase_run_ids([phase_run_id]),
            variants=(
                ee.BatchVariant(ee.DEFAULT_BATCH_VARIANT_NAME, "claude-sonnet-4-20250514", None),
            ),
            skip_policy=ee.SkipPolicy(), pricing_catalog=_pricing_catalog_for_tests(),
            dossier_root=Path("/unused"),
        )
        assert plan.pairs[0].classification == "scored"
        assert plan.pairs[0].price_estimate is not None
        assert plan.pairs[0].price_estimate.estimated_usd > 0


class TestBatchPlanHash:
    async def test_runtime_configuration_change_requires_new_plan(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id, _ = await _seed_reply_draft_correction(state, tracer, feedback)
        kwargs = self._kwargs(state, tracer, phase_run_id)
        before = await ee.build_batch_plan(**kwargs)
        monkeypatch.setattr(ee, "JIG_REVISION", "changed-runtime-revision")
        after = await ee.build_batch_plan(**kwargs)
        assert before.plan_sha256 != after.plan_sha256

    def _kwargs(self, state, tracer, phase_run_id: int, **overrides: Any) -> dict[str, Any]:
        base = dict(
            state=state, tracer=tracer, selector=ee.BatchSelector.by_phase_run_ids([phase_run_id]),
            variants=(
                ee.BatchVariant(ee.DEFAULT_BATCH_VARIANT_NAME, "claude-sonnet-4-20250514", None),
            ),
            skip_policy=ee.SkipPolicy(), pricing_catalog=_pricing_catalog_for_tests(),
            dossier_root=Path("/unused"),
        )
        base.update(overrides)
        return base

    async def test_identical_inputs_produce_identical_hash(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id, _ = await _seed_reply_draft_correction(state, tracer, feedback)
        kwargs = self._kwargs(state, tracer, phase_run_id)
        plan_a = await ee.build_batch_plan(**kwargs)
        plan_b = await ee.build_batch_plan(**kwargs)
        assert plan_a.plan_sha256 == plan_b.plan_sha256
        assert len(plan_a.plan_sha256) == 64

    async def test_changing_correction_changes_hash(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id, evaluation_id = await _seed_reply_draft_correction(state, tracer, feedback)
        kwargs = self._kwargs(state, tracer, phase_run_id)
        plan_before = await ee.build_batch_plan(**kwargs)

        state.save_grade_for_migration(
            GradeRecord(
                post_id=state.get_phase_run(phase_run_id)["post_id"], source="migration",
                graded_at=datetime.now(UTC), relevance_judgment="correct",
                evaluation_id=evaluation_id, edited_text="A completely different correction.",
            ),
            migration_reason="test: change correction",
        )
        plan_after = await ee.build_batch_plan(**kwargs)
        assert plan_before.plan_sha256 != plan_after.plan_sha256

    async def test_changing_skip_policy_changes_hash(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id, _ = await _seed_reply_draft_correction(
            state, tracer, feedback, link_reply_revision=False,
        )
        plan_default = await ee.build_batch_plan(**self._kwargs(state, tracer, phase_run_id))
        plan_skipped = await ee.build_batch_plan(
            **self._kwargs(
                state, tracer, phase_run_id, skip_policy=ee.SkipPolicy(skip_unscored=True),
            ),
        )
        assert plan_default.plan_sha256 != plan_skipped.plan_sha256

    async def test_changing_pricing_catalog_changes_hash(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id, _ = await _seed_reply_draft_correction(state, tracer, feedback)
        plan_a = await ee.build_batch_plan(**self._kwargs(state, tracer, phase_run_id))
        other_catalog = rp.PricingCatalog(
            version=1, as_of="2026-06-01", source_url="https://example.com", catalog_hash="other",
            models={"claude-sonnet-4-20250514": rp.ModelRate(9.0, 45.0)},
        )
        plan_b = await ee.build_batch_plan(
            **self._kwargs(state, tracer, phase_run_id, pricing_catalog=other_catalog),
        )
        assert plan_a.plan_sha256 != plan_b.plan_sha256


class TestPreviewBatchReplay:
    async def test_preview_makes_no_writes(self, state, tracer, feedback, monkeypatch) -> None:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id, _ = await _seed_reply_draft_correction(state, tracer, feedback)
        preview = await ee.preview_batch_replay(
            state=state, tracer=tracer, selector=ee.BatchSelector.by_phase_run_ids([phase_run_id]),
            variants=(
                ee.BatchVariant(ee.DEFAULT_BATCH_VARIANT_NAME, "claude-sonnet-4-20250514", None),
            ),
            skip_policy=ee.SkipPolicy(), pricing_catalog=_pricing_catalog_for_tests(),
            dossier_root=Path("/unused"),
        )
        assert preview.scored_count == 1
        assert preview.total_estimated_usd > 0
        assert "claude-sonnet-4-20250514" in preview.total_estimated_usd_by_model
        assert preview.aggregate_max_llm_calls == preview.max_llm_calls_per_case * 1
        assert state.conn.execute("SELECT COUNT(*) FROM evaluation_experiments").fetchone()[0] == 0
        assert state.conn.execute("SELECT COUNT(*) FROM experiment_runs").fetchone()[0] == 0

    async def test_preview_reports_mixed_classifications(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        scored, _ = await _seed_reply_draft_correction(state, tracer, feedback)
        unscored, _ = await _seed_reply_draft_correction(
            state, tracer, feedback, link_reply_revision=False,
        )
        preview = await ee.preview_batch_replay(
            state=state, tracer=tracer,
            selector=ee.BatchSelector.by_phase_run_ids([scored, unscored]),
            variants=(
                ee.BatchVariant(ee.DEFAULT_BATCH_VARIANT_NAME, "claude-sonnet-4-20250514", None),
            ),
            skip_policy=ee.SkipPolicy(), pricing_catalog=_pricing_catalog_for_tests(),
            dossier_root=Path("/unused"),
        )
        assert preview.scored_count == 1
        assert preview.unscored_count == 1
        assert preview.selected_count == 1
        assert preview.skipped_count == 1


class TestExecuteBatchReplay:
    async def _two_case_plan_and_client(
        self, state, tracer, feedback, monkeypatch, *, response_count: int = 2,
    ) -> tuple[int, int, ee.BatchPlan, tuple[ee.BatchVariant, ...]]:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id_a, _ = await _seed_reply_draft_correction(
            state, tracer, feedback, model="claude-opus-4-20250514",
        )
        phase_run_id_b, _ = await _seed_reply_draft_correction(
            state, tracer, feedback, model="claude-opus-4-20250514",
        )
        lo, hi = sorted([phase_run_id_a, phase_run_id_b])
        candidate_client = _FakeLLMClient(
            [_submit_response(_GRADED_CANDIDATE_PAYLOAD) for _ in range(response_count)],
            model="claude-sonnet-4-20250514",
        )
        _stub_from_model(monkeypatch, {"claude-sonnet-4-20250514": candidate_client})
        variants = (
            ee.BatchVariant(ee.DEFAULT_BATCH_VARIANT_NAME, "claude-sonnet-4-20250514", None),
        )
        selector = ee.BatchSelector.by_phase_run_ids([lo, hi])
        plan = await ee.build_batch_plan(
            state=state, tracer=tracer, selector=selector, variants=variants,
            skip_policy=ee.SkipPolicy(), pricing_catalog=_pricing_catalog_for_tests(),
            dossier_root=Path("/unused"),
        )
        return lo, hi, plan, variants

    async def test_hash_mismatch_rejected_before_any_write(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        lo, _hi, plan, variants = await self._two_case_plan_and_client(
            state, tracer, feedback, monkeypatch,
        )
        with pytest.raises(ee.PlanAuthorizationError):
            await ee.execute_batch_replay(
                state=state, tracer=tracer, feedback=feedback, name="mismatch",
                selector=ee.BatchSelector.by_phase_run_ids([lo]), variants=variants,
                skip_policy=ee.SkipPolicy(), authorize_plan_sha256="0" * 64,
                pricing_catalog=_pricing_catalog_for_tests(), dossier_root=Path("/unused"),
            )
        assert state.conn.execute("SELECT COUNT(*) FROM experiment_runs").fetchone()[0] == 0

    async def test_stale_authorization_hash_rejected_after_correction_changes(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id, evaluation_id = await _seed_reply_draft_correction(
            state, tracer, feedback, model="claude-opus-4-20250514",
        )
        candidate_client = _FakeLLMClient(
            [_submit_response(_GRADED_CANDIDATE_PAYLOAD)], model="claude-sonnet-4-20250514",
        )
        _stub_from_model(monkeypatch, {"claude-sonnet-4-20250514": candidate_client})
        variants = (
            ee.BatchVariant(ee.DEFAULT_BATCH_VARIANT_NAME, "claude-sonnet-4-20250514", None),
        )
        selector = ee.BatchSelector.by_phase_run_ids([phase_run_id])
        plan = await ee.build_batch_plan(
            state=state, tracer=tracer, selector=selector, variants=variants,
            skip_policy=ee.SkipPolicy(), pricing_catalog=_pricing_catalog_for_tests(),
            dossier_root=Path("/unused"),
        )
        state.save_grade_for_migration(
            GradeRecord(
                post_id=state.get_phase_run(phase_run_id)["post_id"], source="migration",
                graded_at=datetime.now(UTC), relevance_judgment="correct",
                evaluation_id=evaluation_id, edited_text="Changed after preview.",
            ),
            migration_reason="test: stale plan hash",
        )
        with pytest.raises(ee.PlanAuthorizationError):
            await ee.execute_batch_replay(
                state=state, tracer=tracer, feedback=feedback, name="stale",
                selector=selector, variants=variants, skip_policy=ee.SkipPolicy(),
                authorize_plan_sha256=plan.plan_sha256,
                pricing_catalog=_pricing_catalog_for_tests(),
                dossier_root=Path("/unused"),
            )

    async def test_non_executable_pairs_rejected_without_skip_policy(
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
        plan = await ee.build_batch_plan(
            state=state, tracer=tracer, selector=selector, variants=variants,
            skip_policy=ee.SkipPolicy(), pricing_catalog=_pricing_catalog_for_tests(),
            dossier_root=Path("/unused"),
        )
        with pytest.raises(ee.NonExecutablePopulationError):
            await ee.execute_batch_replay(
                state=state, tracer=tracer, feedback=feedback, name="blocked",
                selector=selector, variants=variants, skip_policy=ee.SkipPolicy(),
                authorize_plan_sha256=plan.plan_sha256,
                pricing_catalog=_pricing_catalog_for_tests(),
                dossier_root=Path("/unused"),
            )
        assert state.conn.execute("SELECT COUNT(*) FROM experiment_runs").fetchone()[0] == 0

    async def test_skip_policy_excludes_and_execution_succeeds(
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
        plan = await ee.build_batch_plan(
            state=state, tracer=tracer, selector=selector, variants=variants,
            skip_policy=skip_policy, pricing_catalog=_pricing_catalog_for_tests(),
            dossier_root=Path("/unused"),
        )
        outcome = await ee.execute_batch_replay(
            state=state, tracer=tracer, feedback=feedback, name="skip-unscored",
            selector=selector, variants=variants, skip_policy=skip_policy,
            authorize_plan_sha256=plan.plan_sha256, pricing_catalog=_pricing_catalog_for_tests(),
            dossier_root=Path("/unused"),
        )
        assert len(outcome.attempts) == 1
        assert outcome.attempts[0].phase_run_id == scored
        assert outcome.attempts[0].status == "complete"

    async def test_fully_skipped_variant_is_completed_without_attempts(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id, _ = await _seed_reply_draft_correction(
            state, tracer, feedback, model="claude-haiku-4-5-20251001",
        )
        variants = (ee.BatchVariant(ee.DEFAULT_BATCH_VARIANT_NAME, None, None),)
        selector = ee.BatchSelector.by_phase_run_ids([phase_run_id])
        skip_policy = ee.SkipPolicy(skip_no_op=True)
        plan = await ee.build_batch_plan(
            state=state, tracer=tracer, selector=selector, variants=variants,
            skip_policy=skip_policy, pricing_catalog=_pricing_catalog_for_tests(),
            dossier_root=Path("/unused"),
        )

        outcome = await ee.execute_batch_replay(
            state=state, tracer=tracer, feedback=feedback, name="all-skipped",
            selector=selector, variants=variants, skip_policy=skip_policy,
            authorize_plan_sha256=plan.plan_sha256,
            pricing_catalog=_pricing_catalog_for_tests(), dossier_root=Path("/unused"),
        )

        assert outcome.attempts == ()
        run_id = outcome.experiment_run_ids[ee.DEFAULT_BATCH_VARIANT_NAME]
        run = state.get_experiment_run(run_id)
        assert run is not None
        assert run["status"] == "complete"
        assert run["completed_at"] is not None
        assert state.list_experiment_attempts(run_id) == []

    async def test_batch_case_failure_does_not_abort_other_cases(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        lo, hi, plan, variants = await self._two_case_plan_and_client(
            state, tracer, feedback, monkeypatch, response_count=1,
        )
        outcome = await ee.execute_batch_replay(
            state=state, tracer=tracer, feedback=feedback, name="partial-batch",
            selector=ee.BatchSelector.by_phase_run_ids([lo, hi]), variants=variants,
            skip_policy=ee.SkipPolicy(), authorize_plan_sha256=plan.plan_sha256,
            pricing_catalog=_pricing_catalog_for_tests(), dossier_root=Path("/unused"),
        )
        statuses = {a.phase_run_id: a.status for a in outcome.attempts}
        assert statuses[lo] == "complete"
        assert statuses[hi] == "failed"

        run_id = outcome.experiment_run_ids[ee.DEFAULT_BATCH_VARIANT_NAME]
        run = state.get_experiment_run(run_id)
        assert run is not None
        assert run["status"] == "partial"
        assert state.conn.execute(
            "SELECT COUNT(*) FROM evaluation_experiments WHERE status = 'complete'"
        ).fetchone()[0] == 1
        assert state.conn.execute(
            "SELECT COUNT(*) FROM evaluation_experiments WHERE status = 'failed'"
        ).fetchone()[0] == 1


class TestRetryBatchReplay:
    async def _partial_batch(
        self, state, tracer, feedback, monkeypatch,
    ) -> tuple[int, int, int]:
        """Returns (experiment_run_id, succeeded_phase_run_id, failed_phase_run_id)."""
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
        plan = await ee.build_batch_plan(
            state=state, tracer=tracer, selector=selector, variants=variants,
            skip_policy=ee.SkipPolicy(), pricing_catalog=_pricing_catalog_for_tests(),
            dossier_root=Path("/unused"),
        )
        outcome = await ee.execute_batch_replay(
            state=state, tracer=tracer, feedback=feedback, name="to-retry",
            selector=selector, variants=variants, skip_policy=ee.SkipPolicy(),
            authorize_plan_sha256=plan.plan_sha256, pricing_catalog=_pricing_catalog_for_tests(),
            dossier_root=Path("/unused"),
        )
        run_id = outcome.experiment_run_ids[ee.DEFAULT_BATCH_VARIANT_NAME]
        return run_id, lo, hi

    async def test_retry_creates_new_attempt_for_failed_case_only(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        run_id, succeeded, failed = await self._partial_batch(state, tracer, feedback, monkeypatch)

        retry_client = _FakeLLMClient(
            [_submit_response(_GRADED_CANDIDATE_PAYLOAD)], model="claude-sonnet-4-20250514",
        )
        _stub_from_model(monkeypatch, {"claude-sonnet-4-20250514": retry_client})

        retry_outcome = await ee.retry_batch_replay(
            state=state, tracer=tracer, feedback=feedback, experiment_run_id=run_id,
            pricing_catalog=_pricing_catalog_for_tests(), dossier_root=Path("/unused"),
        )
        assert len(retry_outcome.attempts) == 1
        assert retry_outcome.attempts[0].phase_run_id == failed
        assert retry_outcome.attempts[0].status == "complete"

        attempts = state.list_experiment_attempts(run_id)
        succeeded_attempts = [a for a in attempts if a["phase_run_id"] == succeeded]
        failed_case_attempts = [a for a in attempts if a["phase_run_id"] == failed]
        assert len(succeeded_attempts) == 1
        assert len(failed_case_attempts) == 2
        assert failed_case_attempts[0]["attempt_number"] == 1
        assert failed_case_attempts[1]["attempt_number"] == 2
        assert failed_case_attempts[1]["supersedes_experiment_id"] == failed_case_attempts[0]["id"]

        run = state.get_experiment_run(run_id)
        assert run["status"] == "complete"

    async def test_retry_restricted_to_explicit_phase_run_ids(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        run_id, succeeded, failed = await self._partial_batch(state, tracer, feedback, monkeypatch)
        with pytest.raises(ee.RetryResolutionError, match="latest failed attempt"):
            await ee.retry_batch_replay(
                state=state, tracer=tracer, feedback=feedback, experiment_run_id=run_id,
                phase_run_ids=(succeeded,),
                pricing_catalog=_pricing_catalog_for_tests(), dossier_root=Path("/unused"),
            )

    async def test_retry_rejects_changed_pinned_correction_before_spending(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        run_id, _succeeded, failed = await self._partial_batch(
            state, tracer, feedback, monkeypatch,
        )
        phase_run = state.get_phase_run(failed)
        assert phase_run is not None
        before = state.list_experiment_attempts(run_id)

        state.save_grade_for_migration(
            GradeRecord(
                post_id=phase_run["post_id"], source="migration",
                graded_at=datetime.now(UTC), relevance_judgment="correct",
                evaluation_id=phase_run["evaluation_id"],
                edited_text="A correction recorded after the original authorization.",
            ),
            migration_reason="test: retry must preserve pinned evidence",
        )

        with pytest.raises(ee.RetryResolutionError, match="pinned failed-attempt evidence"):
            await ee.retry_batch_replay(
                state=state, tracer=tracer, feedback=feedback, experiment_run_id=run_id,
                pricing_catalog=_pricing_catalog_for_tests(), dossier_root=Path("/unused"),
            )
        assert state.list_experiment_attempts(run_id) == before

    async def test_retry_rejects_non_batch_parent(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
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
        with pytest.raises(ee.RetryResolutionError, match="batch/sweep"):
            await ee.retry_batch_replay(
                state=state, tracer=tracer, feedback=feedback,
                experiment_run_id=outcome.experiment_run_id,
            )

    async def test_retry_rejects_when_no_failed_cases(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id, _ = await _seed_reply_draft_correction(
            state, tracer, feedback, model="claude-opus-4-20250514",
        )
        candidate_client = _FakeLLMClient(
            [_submit_response(_GRADED_CANDIDATE_PAYLOAD)], model="claude-sonnet-4-20250514",
        )
        _stub_from_model(monkeypatch, {"claude-sonnet-4-20250514": candidate_client})
        variants = (
            ee.BatchVariant(ee.DEFAULT_BATCH_VARIANT_NAME, "claude-sonnet-4-20250514", None),
        )
        selector = ee.BatchSelector.by_phase_run_ids([phase_run_id])
        plan = await ee.build_batch_plan(
            state=state, tracer=tracer, selector=selector, variants=variants,
            skip_policy=ee.SkipPolicy(), pricing_catalog=_pricing_catalog_for_tests(),
            dossier_root=Path("/unused"),
        )
        outcome = await ee.execute_batch_replay(
            state=state, tracer=tracer, feedback=feedback, name="all-good",
            selector=selector, variants=variants, skip_policy=ee.SkipPolicy(),
            authorize_plan_sha256=plan.plan_sha256, pricing_catalog=_pricing_catalog_for_tests(),
            dossier_root=Path("/unused"),
        )
        run_id = outcome.experiment_run_ids[ee.DEFAULT_BATCH_VARIANT_NAME]
        with pytest.raises(ee.RetryResolutionError, match="no failed cases"):
            await ee.retry_batch_replay(
                state=state, tracer=tracer, feedback=feedback, experiment_run_id=run_id,
            )


class TestSweepValidation:
    def test_loads_checked_in_prompt_sweep_fixture(self) -> None:
        path = Path("tests/fixtures/evaluation_experiments/prompt-sweep-v1.yaml")
        sweep = ee.load_and_validate_sweep(path)
        assert sweep.axis == "prompt"
        assert sweep.shared_model == "claude-haiku-4-5-20251001"
        assert [v.name for v in sweep.variants] == ["control", "treatment-a"]

    def test_loads_checked_in_model_sweep_fixture(self) -> None:
        path = Path("tests/fixtures/evaluation_experiments/model-sweep-v1.yaml")
        sweep = ee.load_and_validate_sweep(path)
        assert sweep.axis == "model"
        assert [v.model for v in sweep.variants] == [
            "claude-haiku-4-5-20251001", "claude-sonnet-4-20250514",
        ]

    def test_batch_variants_resolve_prompt_text_per_variant(self) -> None:
        path = Path("tests/fixtures/evaluation_experiments/prompt-sweep-v1.yaml")
        sweep = ee.load_and_validate_sweep(path)
        variants = ee.batch_variants_for_sweep(sweep, base_dir=path.parent)
        assert variants[0].name == "control"
        assert variants[0].model_override == "claude-haiku-4-5-20251001"
        assert "grounded, concise reply" in variants[0].system_prompt_override
        assert variants[1].name == "treatment-a"
        assert "warm, concise reply" in variants[1].system_prompt_override

    def test_prompt_sweep_rejects_variant_with_model(self, tmp_path) -> None:
        doc = {
            "version": 1, "name": "n", "axis": "prompt", "model": "claude-haiku-4-5-20251001",
            "variants": [
                {"name": "a", "prompt_file": "a.txt"},
                {"name": "b", "model": "claude-sonnet-4-20250514"},
            ],
        }
        with pytest.raises(ee.SweepValidationError, match="replay-sweep v1"):
            ee.validate_sweep_document(doc, base_dir=tmp_path)

    def test_model_sweep_rejects_variant_with_prompt_file(self, tmp_path) -> None:
        doc = {
            "version": 1, "name": "n", "axis": "model",
            "variants": [
                {"name": "a", "model": "claude-haiku-4-5-20251001"},
                {"name": "b", "prompt_file": "b.txt"},
            ],
        }
        with pytest.raises(ee.SweepValidationError, match="replay-sweep v1"):
            ee.validate_sweep_document(doc, base_dir=tmp_path)

    def test_model_sweep_rejects_shared_top_level_model(self, tmp_path) -> None:
        doc = {
            "version": 1, "name": "n", "axis": "model", "model": "claude-haiku-4-5-20251001",
            "variants": [
                {"name": "a", "model": "claude-haiku-4-5-20251001"},
                {"name": "b", "model": "claude-sonnet-4-20250514"},
            ],
        }
        with pytest.raises(ee.SweepValidationError, match="replay-sweep v1"):
            ee.validate_sweep_document(doc, base_dir=tmp_path)

    def test_fewer_than_two_variants_rejected(self, tmp_path) -> None:
        doc = {
            "version": 1, "name": "n", "axis": "model",
            "variants": [{"name": "a", "model": "claude-haiku-4-5-20251001"}],
        }
        with pytest.raises(ee.SweepValidationError, match="replay-sweep v1"):
            ee.validate_sweep_document(doc, base_dir=tmp_path)

    def test_duplicate_variant_names_rejected(self, tmp_path) -> None:
        doc = {
            "version": 1, "name": "n", "axis": "model",
            "variants": [
                {"name": "a", "model": "claude-haiku-4-5-20251001"},
                {"name": "a", "model": "claude-sonnet-4-20250514"},
            ],
        }
        with pytest.raises(ee.SweepValidationError, match="unique"):
            ee.validate_sweep_document(doc, base_dir=tmp_path)

    def test_unknown_model_rejected(self, tmp_path) -> None:
        doc = {
            "version": 1, "name": "n", "axis": "model",
            "variants": [
                {"name": "a", "model": "claude-haiku-4-5-20251001"},
                {"name": "b", "model": "not-a-real-provider"},
            ],
        }
        with pytest.raises(ee.SweepValidationError, match="not routable"):
            ee.validate_sweep_document(doc, base_dir=tmp_path)

    def test_duplicate_model_values_rejected(self, tmp_path) -> None:
        doc = {
            "version": 1, "name": "n", "axis": "model",
            "variants": [
                {"name": "a", "model": "claude-sonnet-4-20250514"},
                {"name": "b", "model": "claude-sonnet-4-20250514"},
            ],
        }
        with pytest.raises(ee.SweepValidationError, match="semantically distinct"):
            ee.validate_sweep_document(doc, base_dir=tmp_path)

    def test_duplicate_resolved_prompt_content_rejected(self, tmp_path) -> None:
        (tmp_path / "a.txt").write_text("Same prompt text.")
        (tmp_path / "b.txt").write_text("Same prompt text.")
        doc = {
            "version": 1, "name": "n", "axis": "prompt", "model": "claude-haiku-4-5-20251001",
            "variants": [
                {"name": "a", "prompt_file": "a.txt"},
                {"name": "b", "prompt_file": "b.txt"},
            ],
        }
        with pytest.raises(ee.SweepValidationError, match="semantically distinct"):
            ee.validate_sweep_document(doc, base_dir=tmp_path)

    def test_distinct_prompt_content_with_different_filenames_accepted(self, tmp_path) -> None:
        (tmp_path / "a.txt").write_text("Prompt A.")
        (tmp_path / "b.txt").write_text("Prompt B.")
        doc = {
            "version": 1, "name": "n", "axis": "prompt", "model": "claude-haiku-4-5-20251001",
            "variants": [
                {"name": "a", "prompt_file": "a.txt"},
                {"name": "b", "prompt_file": "b.txt"},
            ],
        }
        sweep = ee.validate_sweep_document(doc, base_dir=tmp_path)
        assert [v.name for v in sweep.variants] == ["a", "b"]

    def test_missing_prompt_file_rejected(self, tmp_path) -> None:
        doc = {
            "version": 1, "name": "n", "axis": "prompt", "model": "claude-haiku-4-5-20251001",
            "variants": [
                {"name": "a", "prompt_file": "missing-a.txt"},
                {"name": "b", "prompt_file": "missing-b.txt"},
            ],
        }
        with pytest.raises(ee.SweepValidationError, match="could not read"):
            ee.validate_sweep_document(doc, base_dir=tmp_path)

    def test_load_sweep_document_rejects_non_object(self, tmp_path) -> None:
        path = tmp_path / "sweep.yaml"
        path.write_text("- just\n- a\n- list\n")
        with pytest.raises(ee.SweepValidationError, match="must decode to an object"):
            ee.load_sweep_document(path)

    def test_load_sweep_document_accepts_plain_json(self, tmp_path) -> None:
        path = tmp_path / "sweep.json"
        path.write_text(json.dumps({
            "version": 1, "name": "n", "axis": "model",
            "variants": [
                {"name": "a", "model": "claude-haiku-4-5-20251001"},
                {"name": "b", "model": "claude-sonnet-4-20250514"},
            ],
        }))
        sweep = ee.load_and_validate_sweep(path)
        assert sweep.name == "n"


class TestSweepExecution:
    async def test_sweep_creates_one_experiment_run_per_variant(
        self, state, tracer, feedback, monkeypatch
    ) -> None:
        _patch_resolve_dossier(monkeypatch)
        phase_run_id, _ = await _seed_reply_draft_correction(
            state, tracer, feedback, model="claude-opus-4-20250514",
        )
        client_haiku = _FakeLLMClient(
            [_submit_response(_GRADED_CANDIDATE_PAYLOAD)], model="claude-haiku-4-5-20251001",
        )
        client_sonnet = _FakeLLMClient(
            [_submit_response(_GRADED_CANDIDATE_PAYLOAD)], model="claude-sonnet-4-20250514",
        )
        _stub_from_model(monkeypatch, {
            "claude-haiku-4-5-20251001": client_haiku,
            "claude-sonnet-4-20250514": client_sonnet,
        })

        sweep = ee.SweepDefinition(
            name="model-tune", axis="model", shared_model=None, shared_prompt_file=None,
            variants=(
                ee.SweepVariant("haiku", "claude-haiku-4-5-20251001", None),
                ee.SweepVariant("sonnet", "claude-sonnet-4-20250514", None),
            ),
        )
        variants = ee.batch_variants_for_sweep(sweep, base_dir=Path("/unused"))
        selector = ee.BatchSelector.by_phase_run_ids([phase_run_id])
        catalog = _pricing_catalog_for_tests()
        plan = await ee.build_batch_plan(
            state=state, tracer=tracer, selector=selector, variants=variants,
            skip_policy=ee.SkipPolicy(), pricing_catalog=catalog, dossier_root=Path("/unused"),
            sweep=sweep,
        )
        outcome = await ee.execute_batch_replay(
            state=state, tracer=tracer, feedback=feedback, name="model-tune",
            selector=selector, variants=variants, skip_policy=ee.SkipPolicy(),
            authorize_plan_sha256=plan.plan_sha256, pricing_catalog=catalog,
            dossier_root=Path("/unused"), sweep=sweep,
        )
        assert set(outcome.experiment_run_ids) == {"haiku", "sonnet"}
        assert len(outcome.attempts) == 2
        for variant_name, run_id in outcome.experiment_run_ids.items():
            run = state.get_experiment_run(run_id)
            assert run is not None
            assert run["status"] == "complete"
            config = json.loads(run["candidate_config"])
            assert config["version"] == ee.BATCH_CANDIDATE_CONFIG_VERSION
            assert config["variant_name"] == variant_name
            assert config["sweep"]["name"] == "model-tune"
