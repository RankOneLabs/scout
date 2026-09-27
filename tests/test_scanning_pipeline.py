"""Classifier dispatch tests for the live scanning pipeline."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from jig import run_agent

import scout.scanning.pipeline as pipeline
from scout.config import Account, Message
from scout.registry import KeywordRoute, ProjectTarget
from scout.relevance.models import JevRelevanceOutput
from scout.replay.experiments import draw_relevance_holdout
from scout.result import Ok
from scout.scanning.pipeline import PhaseExecution, score_and_draft_step
from scout.scanning.prefilter import RoutedMessage
from scout.scanning.runner import classify_outcome
from scout.scanning.schemas import RelevancePhaseOutput
from scout.typesafe.routes import route


def _message() -> Message:
    from datetime import UTC, datetime

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


def test_holdout_draw_is_limited_to_shared_jev_project_keys() -> None:
    assert draw_relevance_holdout(project_key="agent-ops", rate=0.5, value=0.1)
    assert draw_relevance_holdout(project_key="agent-evals", rate=0.5, value=0.1)
    assert not draw_relevance_holdout(project_key="gateway", rate=0.5, value=0.1)


def test_relevance_phase_keeps_declared_base_output_result_type() -> None:
    source = inspect.getsource(score_and_draft_step)
    assert (
        "relevance: Result[PhaseExecution[RelevancePhaseOutput], LLMError | ParseError]"
        in source
    )
