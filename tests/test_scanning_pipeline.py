"""Classifier dispatch tests for the live scanning pipeline."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
import yaml
from jig import (
    AgentConfig,
    CompletionParams,
    LLMClient,
    LLMResponse,
    SQLiteTracer,
    ToolRegistry,
    run_agent,
)
from jig.feedback import NullFeedbackLoop
from jig.jev import JevResult, JevUsage, NoulAnswer

import scout.scanning.pipeline as pipeline
from scout.config import Account, Message
from scout.registry import KeywordRoute, ProjectTarget
from scout.relevance.loader import RelevanceCatalogue, load_catalogue_bytes
from scout.relevance.models import JevRelevanceOutput
from scout.relevance.setup import JevScanContext
from scout.result import Ok
from scout.scanning.agent import PhaseRunIdentity, ScoutExecutionContext
from scout.scanning.pipeline import PhaseExecution, score_and_draft_step
from scout.scanning.prefilter import RoutedMessage
from scout.scanning.runner import classify_outcome
from scout.scanning.schemas import RelevancePhaseOutput
from scout.storage.state import StateManager
from scout.typesafe.routes import route


def _message() -> Message:
    return Message(
        platform="bluesky",
        platform_id="post-1",
        channel_name="agents",
        channel_id="agents",
        author=Account(platform="bluesky", id="author-1", name="Ada", handle="ada"),
        content="agent evaluation question",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        url="https://example.test/post-1",
    )


def _routed(project_key: str) -> RoutedMessage:
    return RoutedMessage(
        message=_message(),
        keyword_route=KeywordRoute(
            id=1,
            project_key=project_key,
            keyword="agent",
            evaluate_prompt=None,
            respond_prompt=None,
            critique_prompt=None,
            priority=0,
        ),
    )


def _context(project_key: str, model: str, jev_context: object | None = None) -> dict:
    identity = SimpleNamespace(snapshot_phase_id=1, model=model)
    return {
        "input": _routed(project_key),
        "phase_configs": SimpleNamespace(relevance=Mock()),
        "dossier_summaries": {},
        "execution_context": SimpleNamespace(
            state=Mock(),
            scan_id=1,
            post_id=1,
            relevance=identity,
            reply_draft=identity,
            critic=identity,
        ),
        "jev_context": jev_context,
    }


class _UnusedLLM(LLMClient):
    async def complete(self, params: CompletionParams) -> LLMResponse:
        del params
        raise AssertionError("the Jev executor must not call the LLM client")


class _CapturingJevClient:
    model = "jev-latest"

    def __init__(self, result: JevResult) -> None:
        self.result = result
        self.state: object | None = None

    async def evaluate(self, state: Any, questions: Any) -> JevResult:
        del questions
        self.state = state
        return self.result


def _catalogue() -> RelevanceCatalogue:
    question = {
        "type": "noul",
        "instructions": "Synthetic question",
        "criteria": {"true": {"what": "yes"}, "false": {"what": "no"}},
    }
    document = {
        "id": "test-relevance",
        "decide": "agent_ops_route/v1",
        "description": "Test Jev relevance catalogue.",
        "state": {
            "post": ["platform", "channel", "url", "text"],
            "parent_context_only": ["author_name", "text"],
            "author": ["name", "handle"],
            "project": ["key", "name", "description"],
        },
        "questions": {
            "excl_noise": question,
            "needs_thread": question,
            "answerable_from_post": question,
            "about_agent_work": question,
            "points_somewhere": question,
        },
    }
    return load_catalogue_bytes(yaml.safe_dump(document, sort_keys=False).encode())


def _jev_drop_result() -> JevResult:
    values = {
        "excl_noise": 0.9,
        "needs_thread": 0.0,
        "answerable_from_post": 0.0,
        "about_agent_work": 0.0,
        "points_somewhere": 0.0,
    }
    return JevResult(
        model="jev-latest",
        answers={
            key: NoulAnswer(question_id=key, noul=value) for key, value in values.items()
        },
        usage=JevUsage(input_tokens=21, output_tokens=8),
        latency_ms=12.5,
        call_id="call-1",
        provider_request_id="provider-1",
        attempts=1,
    )


def _relevance_config(tracer: SQLiteTracer) -> AgentConfig[RelevancePhaseOutput]:
    return AgentConfig[RelevancePhaseOutput](
        name="unused-llm-config",
        description="unused",
        system_prompt="unused",
        llm=_UnusedLLM(),
        feedback=NullFeedbackLoop(),
        tracer=tracer,
        tools=ToolRegistry([]),
        output_schema=RelevancePhaseOutput,
    )


@pytest.mark.asyncio
async def test_agent_ops_dispatches_jev_and_carries_whole_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = {
        "excl_noise": 0.9,
        "needs_thread": 0.0,
        "answerable_from_post": 0.0,
        "about_agent_work": 0.0,
        "points_somewhere": 0.0,
    }
    decision = route(answers)
    jev_output = JevRelevanceOutput(
        relevant=False,
        score=1.0,
        reason="exclusion: noise",
        relevant_to=[],
        action=decision.action,
        answers=answers,
        line=decision.line,
        margin=decision.margin,
        exclusion=decision.exclusion,
    )
    seen_executor: object | None = None

    async def fake_run_phase(**kwargs):
        nonlocal seen_executor
        seen_executor = kwargs["executor"]
        return Ok(
            PhaseExecution(
                parsed=jev_output,
                trace_id="jev-trace",
                phase_run_id=7,
                phase="relevance",
                model="jev:jev-latest",
            )
        )

    target = ProjectTarget("agent-ops", "Agent Ops", "desc", "")
    jev_context = SimpleNamespace(
        projects={"agent-ops": target},
        catalogue=Mock(),
        questions=(),
        client=SimpleNamespace(model="jev-latest"),
    )
    monkeypatch.setattr(pipeline._config, "RELEVANCE_CLASSIFIER", "jev")
    monkeypatch.setattr(pipeline, "build_state", Mock(return_value={}))
    monkeypatch.setattr(pipeline, "_run_phase", fake_run_phase)

    result = await score_and_draft_step(
        _context("agent-ops", "jev:jev-latest", jev_context)
    )

    assert isinstance(result, Ok)
    candidate = result.value
    assert seen_executor is not run_agent
    assert candidate.relevance_action == decision.action
    assert candidate.relevance_classifier == "jev:jev-latest"
    assert candidate.relevance_output is jev_output
    assert candidate.model_dump()["relevance_output"]["answers"] == answers
    assert candidate.score == 1.0
    outcome = classify_outcome(candidate, _message(), {})
    assert outcome.relevance_output is jev_output
    assert outcome.relevance_classifier == candidate.relevance_classifier
    assert outcome.relevance_action == candidate.relevance_action


@pytest.mark.asyncio
async def test_unmeasured_project_uses_llm_when_classifier_is_jev(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_executor: object | None = None

    async def fake_run_phase(**kwargs):
        nonlocal seen_executor
        seen_executor = kwargs["executor"]
        return Ok(
            PhaseExecution(
                parsed=RelevancePhaseOutput(
                    relevant=False, score=0.1, reason="not relevant"
                ),
                trace_id="llm-trace",
                phase_run_id=9,
                phase="relevance",
                model="llm-model",
            )
        )

    monkeypatch.setattr(pipeline._config, "RELEVANCE_CLASSIFIER", "jev")
    monkeypatch.setattr(pipeline, "_run_phase", fake_run_phase)

    result = await score_and_draft_step(_context("gateway", "llm-model"))

    assert isinstance(result, Ok)
    assert seen_executor is run_agent
    assert result.value.relevance_classifier == "llm-model"
    assert result.value.relevance_action == "drop"
    assert result.value.contributor_phase_run_ids == (9,)


@pytest.mark.asyncio
async def test_jev_dispatch_projects_state_and_returns_real_candidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    message = _message()
    tracer = SQLiteTracer(db_path=str(tmp_path / "traces.db"))
    state = StateManager(db_path=str(tmp_path / "state.db"))
    scan_id = state.start_scan(environment="test")
    post_id = state.save_post(message, scan_id)
    snapshot = state.record_feedback_snapshot(scan_id, mode="shadow")
    relevance_snapshot = next(
        phase for phase in snapshot.phases if phase.phase == "relevance"
    )
    identity = PhaseRunIdentity(
        snapshot_phase_id=relevance_snapshot.snapshot_phase_id,
        model="jev:jev-latest",
    )
    target = ProjectTarget("agent-ops", "Agent Ops", "Agent operations", "")
    catalogue = _catalogue()
    client = _CapturingJevClient(_jev_drop_result())
    jev_context = JevScanContext(
        catalogue=catalogue,
        questions=catalogue.questions,
        client=cast(Any, client),
        projects={"agent-ops": target},
    )
    execution = ScoutExecutionContext(
        state=state,
        scan_id=scan_id,
        post_id=post_id,
        relevance=identity,
        reply_draft=identity,
        critic=identity,
    )
    monkeypatch.setattr(pipeline._config, "RELEVANCE_CLASSIFIER", "jev")

    result = await score_and_draft_step(
        {
            "input": _routed("agent-ops"),
            "phase_configs": SimpleNamespace(relevance=_relevance_config(tracer)),
            "dossier_summaries": {},
            "execution_context": execution,
            "jev_context": jev_context,
        }
    )

    assert isinstance(result, Ok)
    assert isinstance(result.value.relevance_output, JevRelevanceOutput)
    assert result.value.relevance_output.answers == {
        key: answer.noul for key, answer in client.result.answers.items()
    }
    assert result.value.relevance_action == "drop"
    assert result.value.relevance_classifier == "jev:jev-latest"
    assert result.value.score == 1.0
    assert client.state == {
        "post": {
            "platform": "bluesky",
            "channel": "agents",
            "url": "https://example.test/post-1",
            "text": "agent evaluation question",
        },
        "parent_context_only": None,
        "author": {"name": "Ada", "handle": "ada"},
        "project": {
            "key": "agent-ops",
            "name": "Agent Ops",
            "description": "Agent operations",
        },
    }
    await tracer.close()
    state.close()


def test_relevance_phase_keeps_declared_base_output_result_type() -> None:
    source = inspect.getsource(score_and_draft_step)
    assert (
        "relevance: Result[PhaseExecution[RelevancePhaseOutput], LLMError | ParseError]"
        in source
    )
