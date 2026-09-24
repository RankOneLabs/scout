"""Classifier selection, action semantics, and durable relevance evidence.

Covers the three integration claims: exactly one classifier runs and the
configured one never calls the other; a JEV run resolves to a verified
AGENT_RUN root or claims no evaluation at all; and a JEV action bypasses the
numeric threshold while every downstream gate still applies.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from jig import SpanKind, SQLiteTracer

from scout.config import Account, Message
from scout.registry import ProjectTarget
from scout.result import Err, Ok
from scout.scanning.jev import JevEndpoint
from scout.scanning.jev_catalogue import load_jev_catalogue
from scout.scanning.jev_router import RouteDecision
from scout.scanning.jev_state import JevProject
from scout.scanning.pipeline import score_and_draft_step
from scout.scanning.relevance import (
    JEV_IRRELEVANT_SCORE,
    JEV_RELEVANT_SCORE,
    JevRuntime,
    jev_relevance_output,
    llm_relevance_action,
    run_jev_relevance_phase,
)
from scout.scanning.runner import classify_outcome
from scout.scanning.schemas import (
    RecordedRelevanceDecision,
    RelevancePhaseOutput,
    ReplyCandidate,
)
from scout.storage.state import StateManager

CATALOGUE = Path("tests/fixtures/relevance/routed-features.fixture.yaml")
PROJECT = JevProject(key="agent-ops", name="Agent Ops", description="A description.")
TARGET = ProjectTarget(
    key="agent-ops", name="Agent Ops", description="A description.", link="https://x.invalid"
)


def _message(platform_id: str = "0xabc") -> Message:
    return Message(
        platform="farcaster",
        platform_id=platform_id,
        channel_name="agents",
        channel_id="agents",
        author=Account(platform="farcaster", id="42", name="Ada", handle="ada"),
        content="our planner retries every failed step twice",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        url=f"https://warpcast.com/ada/{platform_id}",
    )


@pytest.fixture
def runtime() -> JevRuntime:
    loaded = load_jev_catalogue(CATALOGUE)
    assert isinstance(loaded, Ok), loaded
    return JevRuntime(
        catalogue=loaded.value,
        endpoint=JevEndpoint(api_key="secret-key"),
    )


@pytest.fixture
def state(tmp_path: Path) -> Generator[StateManager, None, None]:
    manager = StateManager(db_path=str(tmp_path / "evidence.db"))
    yield manager
    manager.commit()
    manager.close()


@pytest.fixture
def tracer(tmp_path: Path) -> SQLiteTracer:
    return SQLiteTracer(db_path=str(tmp_path / "traces.db"))


def _answering(**probabilities: float) -> httpx.MockTransport:
    """A transport answering every catalogue question, overridable per name."""

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        questions = json.loads(request.content)["questions"]
        answers = {
            name: {"type": "noul", "noul": probabilities.get(name, 0.0)}
            for name in questions
        }
        return httpx.Response(200, json={"answers": answers})

    return httpx.MockTransport(handler)


def _seed(state: StateManager, platform_id: str = "0xabc") -> tuple[int, int, int]:
    """A scan, a post, and this scan's relevance snapshot phase row."""
    scan_id = state.start_scan(environment="test")
    post_id = state.save_post(_message(platform_id), scan_id)
    now = datetime.now(UTC).isoformat()
    with state.db.begin_immediate():
        state.conn.execute(
            "INSERT INTO feedback_snapshots (scan_id, policy_version, mode, as_of, "
            "lookback_days, max_grades, segment_min_grades, note_max_chars, "
            "relevance_token_budget, reply_draft_token_budget, critic_token_budget, "
            "population_count, eligible_count, excluded_count, created_at) "
            "VALUES (?, 'evaluation-feedback/v1', 'shadow', ?, 90, 200, 5, 240, "
            "800, 800, 1000, 0, 0, 0, ?)",
            (scan_id, now, now),
        )
        cursor = state.conn.execute(
            "INSERT INTO feedback_snapshot_phases (snapshot_id, phase, token_budget, "
            "token_estimate, truncated, structured_summary, rendered_text, "
            "rendered_sha256, created_at) "
            "VALUES (last_insert_rowid(), 'relevance', 800, 0, 0, '{}', '', 'x', ?)",
            (now,),
        )
        snapshot_phase_id = cursor.lastrowid
    assert snapshot_phase_id is not None
    return scan_id, post_id, snapshot_phase_id


# --------------------------------------------------------------------------
# Action semantics
# --------------------------------------------------------------------------


def _decision(action: str, line: str = "respond") -> RouteDecision:
    return RouteDecision(
        action=action,  # type: ignore[arg-type]
        path_action=action,  # type: ignore[arg-type]
        line=line,  # type: ignore[arg-type]
        margin=(),
        exclusion=None,
        features={},
    )


def test_respond_is_relevant_and_names_the_routed_project() -> None:
    output = jev_relevance_output(_decision("respond"), "agent-ops")
    assert (output.relevant, output.score, output.relevant_to) == (
        True,
        JEV_RELEVANT_SCORE,
        ["agent-ops"],
    )


def test_review_is_relevant_and_continues_like_respond() -> None:
    output = jev_relevance_output(_decision("review", "needs_thread"), "agent-ops")
    assert (output.relevant, output.score) == (True, JEV_RELEVANT_SCORE)


def test_drop_is_negative_and_names_no_project() -> None:
    output = jev_relevance_output(_decision("drop", "otherwise"), "agent-ops")
    assert (output.relevant, output.score, output.relevant_to) == (
        False,
        JEV_IRRELEVANT_SCORE,
        [],
    )


def test_an_exclusion_reason_names_the_exclusion() -> None:
    decision = RouteDecision(
        action="drop", path_action="drop", line="exclusion", margin=(),
        exclusion="weather", features={},
    )
    assert jev_relevance_output(decision, "agent-ops").reason == "exclusion: weather"


def test_llm_records_respond_at_or_above_the_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scout.scanning.relevance as relevance_module

    monkeypatch.setattr(relevance_module, "RELEVANCE_THRESHOLD", 0.7)
    output = RelevancePhaseOutput(relevant=True, score=0.7, reason="r")
    assert llm_relevance_action(output) == "respond"


def test_llm_records_drop_below_the_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    import scout.scanning.relevance as relevance_module

    monkeypatch.setattr(relevance_module, "RELEVANCE_THRESHOLD", 0.7)
    output = RelevancePhaseOutput(relevant=True, score=0.69, reason="r")
    assert llm_relevance_action(output) == "drop"


def test_llm_records_drop_when_not_relevant() -> None:
    output = RelevancePhaseOutput(relevant=False, score=1.0, reason="r")
    assert llm_relevance_action(output) == "drop"


# --------------------------------------------------------------------------
# Durable evidence
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_decided_post_records_a_verified_agent_run_root(
    runtime: JevRuntime, state: StateManager, tracer: SQLiteTracer
) -> None:
    scan_id, post_id, snapshot_phase_id = _seed(state)
    decided = await run_jev_relevance_phase(
        tracer=tracer,
        catalogue=runtime.catalogue,
        endpoint=runtime.endpoint,
        message=_message(),
        project=PROJECT,
        state=state,
        scan_id=scan_id,
        post_id=post_id,
        snapshot_phase_id=snapshot_phase_id,
        transport=_answering(answerable_from_post=0.9, about_agent_work=0.9),
    )
    assert isinstance(decided, Ok), decided
    spans = await tracer.get_trace(decided.value.execution.trace_id)
    root = next(span for span in spans if span.parent_id is None)
    assert root.kind == SpanKind.AGENT_RUN


@pytest.mark.asyncio
async def test_a_decided_post_records_the_actual_classifier_and_model(
    runtime: JevRuntime, state: StateManager, tracer: SQLiteTracer
) -> None:
    scan_id, post_id, snapshot_phase_id = _seed(state)
    decided = await run_jev_relevance_phase(
        tracer=tracer, catalogue=runtime.catalogue, endpoint=runtime.endpoint,
        message=_message(), project=PROJECT, state=state, scan_id=scan_id,
        post_id=post_id, snapshot_phase_id=snapshot_phase_id,
        transport=_answering(answerable_from_post=0.9, about_agent_work=0.9),
    )
    assert isinstance(decided, Ok)
    row = state.get_phase_run(decided.value.execution.phase_run_id)
    assert row is not None
    assert (row["phase"], row["model"], row["status"]) == (
        "relevance",
        "jev-latest",
        "complete",
    )


@pytest.mark.asyncio
async def test_a_failed_attempt_records_its_phase_run_and_no_evaluation(
    runtime: JevRuntime, state: StateManager, tracer: SQLiteTracer
) -> None:
    scan_id, post_id, snapshot_phase_id = _seed(state)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    decided = await run_jev_relevance_phase(
        tracer=tracer, catalogue=runtime.catalogue, endpoint=runtime.endpoint,
        message=_message(), project=PROJECT, state=state, scan_id=scan_id,
        post_id=post_id, snapshot_phase_id=snapshot_phase_id,
        transport=httpx.MockTransport(handler),
    )
    assert isinstance(decided, Err)
    statuses = [
        row["status"]
        for row in state.conn.execute(
            "SELECT status FROM evaluation_phase_runs WHERE post_id = ?", (post_id,)
        )
    ]
    assert statuses == ["error"]
    evaluations = state.conn.execute(
        "SELECT COUNT(*) AS n FROM evaluations WHERE post_id = ?", (post_id,)
    ).fetchone()["n"]
    assert evaluations == 0


@pytest.mark.asyncio
async def test_a_malformed_answer_vector_leaves_the_post_unevaluated(
    runtime: JevRuntime, state: StateManager, tracer: SQLiteTracer
) -> None:
    scan_id, post_id, snapshot_phase_id = _seed(state)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answers": {"needs_thread": {"type": "noul"}}})

    decided = await run_jev_relevance_phase(
        tracer=tracer, catalogue=runtime.catalogue, endpoint=runtime.endpoint,
        message=_message(), project=PROJECT, state=state, scan_id=scan_id,
        post_id=post_id, snapshot_phase_id=snapshot_phase_id,
        transport=httpx.MockTransport(handler),
    )
    assert isinstance(decided, Err)
    assert decided.error.operation == "relevance"
    assert state.conn.execute(
        "SELECT COUNT(*) AS n FROM evaluations WHERE post_id = ?", (post_id,)
    ).fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_unverifiable_trace_evidence_prevents_a_durable_claim(
    runtime: JevRuntime, state: StateManager, tracer: SQLiteTracer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trace that cannot be read back claims no evaluation and no phase run."""
    scan_id, post_id, snapshot_phase_id = _seed(state)

    async def broken_get_trace(trace_id: str) -> list[Any]:
        raise RuntimeError("trace store unavailable")

    monkeypatch.setattr(tracer, "get_trace", broken_get_trace)
    decided = await run_jev_relevance_phase(
        tracer=tracer, catalogue=runtime.catalogue, endpoint=runtime.endpoint,
        message=_message(), project=PROJECT, state=state, scan_id=scan_id,
        post_id=post_id, snapshot_phase_id=snapshot_phase_id,
        transport=_answering(answerable_from_post=0.9, about_agent_work=0.9),
    )
    assert isinstance(decided, Err)
    assert "evidence persistence failed" in decided.error.detail
    assert state.conn.execute(
        "SELECT COUNT(*) AS n FROM evaluation_phase_runs WHERE post_id = ?", (post_id,)
    ).fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_the_stored_decision_carries_the_deciding_line_and_exclusion(
    runtime: JevRuntime, state: StateManager, tracer: SQLiteTracer
) -> None:
    scan_id, post_id, snapshot_phase_id = _seed(state)
    decided = await run_jev_relevance_phase(
        tracer=tracer, catalogue=runtime.catalogue, endpoint=runtime.endpoint,
        message=_message(), project=PROJECT, state=state, scan_id=scan_id,
        post_id=post_id, snapshot_phase_id=snapshot_phase_id,
        transport=_answering(excl_weather=0.9),
    )
    assert isinstance(decided, Ok)
    record = decided.value.decision.as_record()
    assert record["line"] == "exclusion"
    assert record["exclusion"] == "weather"
    assert set(record["features"]) == set(runtime.catalogue.questions)
    assert set(decided.value.answers) == set(runtime.catalogue.questions)


# --------------------------------------------------------------------------
# Selection: exactly one classifier runs
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_jev_selection_never_calls_the_llm_phase(
    runtime: JevRuntime, state: StateManager, tracer: SQLiteTracer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scout.scanning.pipeline as pipeline_module

    async def fail_if_called(**kwargs: Any) -> Any:
        raise AssertionError("the LLM relevance phase must not run under jev")

    monkeypatch.setattr(pipeline_module, "_run_phase", fail_if_called)
    scan_id, post_id, snapshot_phase_id = _seed(state)
    from scout.scanning.agent import PhaseRunIdentity, ScoutExecutionContext
    from scout.scanning.prefilter import RoutedMessage

    identity = PhaseRunIdentity(snapshot_phase_id=snapshot_phase_id, model="jev-latest")
    execution = ScoutExecutionContext(
        state=state, scan_id=scan_id, post_id=post_id,
        relevance=identity, reply_draft=identity, critic=identity,
    )
    from scout.registry import KeywordRoute

    route = KeywordRoute(
        id=1, project_key="agent-ops", keyword="agents", evaluate_prompt=None,
        respond_prompt=None, critique_prompt=None, priority=1,
    )
    phase_configs = type("Configs", (), {"relevance": type("C", (), {"tracer": tracer})()})()
    result = await score_and_draft_step(
        {
            "input": RoutedMessage(message=_message(), keyword_route=route),
            "phase_configs": phase_configs,
            "execution_context": execution,
            "projects": {"agent-ops": TARGET},
            "jev_runtime": JevRuntime(
                catalogue=runtime.catalogue,
                endpoint=runtime.endpoint,
                transport=_answering(excl_weather=0.9),
            ),
        }
    )
    assert isinstance(result, Ok)
    assert result.value.relevant is False
    assert result.value.relevance_decision is not None
    assert result.value.relevance_decision.classifier == "jev"


@pytest.mark.asyncio
async def test_an_unrouted_post_fails_before_any_request(
    runtime: JevRuntime, state: StateManager, tracer: SQLiteTracer
) -> None:
    """No project means no defined JEV input, and no HTTP call is made."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    scan_id, post_id, snapshot_phase_id = _seed(state)
    from scout.scanning.agent import PhaseRunIdentity, ScoutExecutionContext
    from scout.scanning.prefilter import RoutedMessage

    identity = PhaseRunIdentity(snapshot_phase_id=snapshot_phase_id, model="jev-latest")
    execution = ScoutExecutionContext(
        state=state, scan_id=scan_id, post_id=post_id,
        relevance=identity, reply_draft=identity, critic=identity,
    )
    phase_configs = type("Configs", (), {"relevance": type("C", (), {"tracer": tracer})()})()
    result = await score_and_draft_step(
        {
            "input": RoutedMessage(message=_message(), keyword_route=None),
            "phase_configs": phase_configs,
            "execution_context": execution,
            "projects": {"agent-ops": TARGET},
            "jev_runtime": JevRuntime(
                catalogue=runtime.catalogue,
                endpoint=runtime.endpoint,
                transport=httpx.MockTransport(handler),
            ),
        }
    )
    assert isinstance(result, Err)
    assert "requires a routed project" in result.error.detail
    assert calls == 0


# --------------------------------------------------------------------------
# Threshold bypass
# --------------------------------------------------------------------------


def _candidate(decision: RecordedRelevanceDecision | None, score: float) -> ReplyCandidate:
    return ReplyCandidate(
        relevant=True,
        score=score,
        reason="r",
        relevant_to=["agent-ops"],
        project_key="agent-ops",
        relevance_decision=decision,
    )


def _jev_decision(action: str = "respond") -> RecordedRelevanceDecision:
    return RecordedRelevanceDecision(
        classifier="jev",
        model="jev-latest",
        action=action,  # type: ignore[arg-type]
    )


def test_a_jev_action_ignores_the_numeric_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JEV respond with a score under the threshold must not be low_relevance."""
    import scout.scanning.runner as runner_module

    monkeypatch.setattr(runner_module, "RELEVANCE_THRESHOLD", 0.7)
    decision = classify_outcome(_candidate(_jev_decision(), 0.0), _message(), {})
    assert decision.status != "low_relevance"


def test_an_llm_decision_still_honours_the_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scout.scanning.runner as runner_module

    monkeypatch.setattr(runner_module, "RELEVANCE_THRESHOLD", 0.7)
    llm = RecordedRelevanceDecision(classifier="llm", model="m", action="drop")
    decision = classify_outcome(_candidate(llm, 0.5), _message(), {})
    assert decision.status == "low_relevance"


def test_a_candidate_without_a_decision_still_honours_the_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scout.scanning.runner as runner_module

    monkeypatch.setattr(runner_module, "RELEVANCE_THRESHOLD", 0.7)
    decision = classify_outcome(_candidate(None, 0.5), _message(), {})
    assert decision.status == "low_relevance"


def test_a_jev_decision_still_faces_the_downstream_gates() -> None:
    """Ignoring the threshold must not weaken anything else: a missing
    dossier is still a gate block, not a surfaced reply."""
    from scout.scanning.schemas import DeclarativeSegment, StructuredDraftOutput

    candidate = ReplyCandidate(
        relevant=True,
        score=JEV_RELEVANT_SCORE,
        reason="respond",
        relevant_to=["agent-ops"],
        project_key="agent-ops",
        structured_draft=StructuredDraftOutput(
            posture="answer",
            segments=[DeclarativeSegment(type="declarative", fact_id="f1", text="t")],
            claims=["t"],
        ),
        relevance_decision=_jev_decision(),
    )
    decision = classify_outcome(candidate, _message(), {})
    assert decision.status == "gate_blocked"
    assert [v.reason_code for v in decision.gate_violations] == ["missing_dossier"]


def test_a_jev_review_survives_as_the_recorded_action() -> None:
    candidate = _candidate(_jev_decision("review"), JEV_RELEVANT_SCORE)
    decision = classify_outcome(candidate, _message(), {})
    assert decision.relevance_decision is not None
    assert decision.relevance_decision.action == "review"
