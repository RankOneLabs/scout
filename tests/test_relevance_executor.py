"""Jev relevance executor, trace, and phase persistence tests."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from typing import Any, cast

import pytest
import yaml
from jig import (
    AgentConfig,
    CompletionParams,
    LLMClient,
    LLMResponse,
    SpanKind,
    SQLiteTracer,
    ToolRegistry,
)
from jig.feedback import NullFeedbackLoop
from jig.jev import JevResult, JevUsage, NoulAnswer

import scout.config as scout_config
from scout.config import Account, Message
from scout.relevance.executor import run_jev_relevance
from scout.relevance.loader import RelevanceCatalogue, load_catalogue_bytes
from scout.relevance.models import JevRelevanceError, JevRelevanceOutput
from scout.replay.experiments import resolve_baseline
from scout.result import Ok
from scout.scanning.pipeline import _run_phase
from scout.storage.state import StateManager


class _UnusedLLM(LLMClient):
    async def complete(self, params: CompletionParams) -> LLMResponse:
        del params
        raise AssertionError("the Jev executor must not call the LLM client")


class _JevStub:
    model = "jev-latest"

    def __init__(self, result: JevResult, api_key: str = "") -> None:
        self.result = result
        self.calls = 0
        self._api_key = api_key

    async def evaluate(self, state: Any, questions: Any) -> JevResult:
        del state, questions
        self.calls += 1
        return self.result


class _BlockingJevStub:
    model = "jev-latest"

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def evaluate(self, state: Any, questions: Any) -> JevResult:
        del state, questions
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


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


def _result() -> JevResult:
    values = {
        "excl_noise": 0.0,
        "needs_thread": 0.0,
        "answerable_from_post": 0.9,
        "about_agent_work": 0.9,
        "points_somewhere": 0.0,
    }
    return JevResult(
        model="jev-latest",
        answers={key: NoulAnswer(question_id=key, noul=value) for key, value in values.items()},
        usage=JevUsage(input_tokens=21, output_tokens=8),
        latency_ms=12.5,
        call_id="call-1",
        provider_request_id="provider-1",
        attempts=1,
    )


def _config(tracer: SQLiteTracer) -> AgentConfig[JevRelevanceOutput]:
    return AgentConfig[JevRelevanceOutput](
        name="unused-llm-config",
        description="unused",
        system_prompt="unused",
        llm=_UnusedLLM(),
        feedback=NullFeedbackLoop(),
        tracer=tracer,
        tools=ToolRegistry([]),
        output_schema=JevRelevanceOutput,
    )


def _message() -> Message:
    return Message(
        platform="bluesky",
        platform_id="post-1",
        channel_name="agents",
        channel_id="agents",
        author=Account(platform="bluesky", id="author-1", name="Ada", handle="ada"),
        content="How should an agent retry a failed tool call?",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        url="https://example.test/post-1",
    )


def _seed(state: StateManager) -> tuple[int, int, int]:
    scan_id = state.start_scan(environment="test")
    post_id = state.save_post(_message(), scan_id)
    snapshot = state.record_feedback_snapshot(scan_id, mode="shadow")
    relevance = next(phase for phase in snapshot.phases if phase.phase == "relevance")
    return scan_id, post_id, relevance.snapshot_phase_id


def _executor(client: Any, catalogue: RelevanceCatalogue) -> Any:
    return partial(
        run_jev_relevance,
        client=client,
        catalogue=catalogue,
        questions=catalogue.questions,
        jev_state={"post": {"text": "agent retries"}},
        project_key="agent-ops",
        message_id="post-1",
    )


@pytest.mark.asyncio
async def test_jev_executor_writes_prescribed_trace_without_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    tracer = SQLiteTracer(db_path=str(tmp_path / "traces.db"))
    secret = "typesafe-secret-must-not-appear"
    monkeypatch.setattr(scout_config, "TYPESAFE_API_KEY", secret)
    client = _JevStub(replace(_result(), call_id=f"call-{secret}"), api_key=secret)

    result = await run_jev_relevance(
        _config(tracer),
        "formatted post",
        client=cast(Any, client),
        catalogue=_catalogue(),
        questions=_catalogue().questions,
        jev_state={"post": {"text": "agent retries"}},
        project_key="agent-ops",
        message_id="post-1",
    )

    spans = await tracer.get_trace(result.trace_id)
    roots = [span for span in spans if span.parent_id is None]
    children = [span for span in spans if span.parent_id == roots[0].id]
    assert [(span.name, span.kind) for span in roots] == [
        ("scout_relevance_jev", SpanKind.AGENT_RUN)
    ]
    assert [(span.name, span.kind) for span in children] == [
        ("jev.call", SpanKind.PROVIDER_CALL)
    ]
    assert children[0].metadata == {
        "call_id": "call-[REDACTED]",
        "provider_request_id": "provider-1",
        "model": "jev-latest",
        "latency_ms": 12.5,
        "attempts": 1,
    }
    serialized = json.dumps(
        [
            {
                "input": span.input,
                "output": span.output,
                "metadata": span.metadata,
                "error": span.error,
            }
            for span in spans
        ],
        default=str,
    )
    assert secret not in serialized
    assert result.parsed is not None
    assert result.parsed.action == "respond"
    assert result.parsed.score == 1.0
    await tracer.close()


@pytest.mark.asyncio
async def test_jev_phase_resolves_as_replay_baseline(tmp_path) -> None:
    tracer = SQLiteTracer(db_path=str(tmp_path / "traces.db"))
    state = StateManager(db_path=str(tmp_path / "state.db"))
    scan_id, post_id, snapshot_phase_id = _seed(state)
    catalogue = _catalogue()

    phase = await _run_phase(
        phase="relevance",
        config=_config(tracer),
        input_text="formatted post",
        message_id="post-1",
        state=state,
        scan_id=scan_id,
        post_id=post_id,
        snapshot_phase_id=snapshot_phase_id,
        model="jev:jev-latest",
        executor=_executor(_JevStub(_result()), catalogue),
    )

    assert isinstance(phase, Ok)
    assert isinstance(phase.value.parsed, JevRelevanceOutput)
    baseline = await resolve_baseline(state, tracer, phase.value.phase_run_id)
    assert baseline.baseline_model == "jev:jev-latest"
    assert baseline.recorded_input == "formatted post"
    phase_run_count = state.conn.execute(
        "SELECT COUNT(*) FROM evaluation_phase_runs WHERE post_id = ?", (post_id,)
    ).fetchone()[0]
    assert phase_run_count == 1
    await tracer.close()
    state.close()


@pytest.mark.asyncio
async def test_cancelled_jev_phase_uses_phase_cleanup_path(tmp_path) -> None:
    tracer = SQLiteTracer(db_path=str(tmp_path / "traces.db"))
    state = StateManager(db_path=str(tmp_path / "state.db"))
    scan_id, post_id, snapshot_phase_id = _seed(state)
    client = _BlockingJevStub()
    catalogue = _catalogue()
    task = asyncio.create_task(
        _run_phase(
            phase="relevance",
            config=_config(tracer),
            input_text="formatted post",
            message_id="post-1",
            state=state,
            scan_id=scan_id,
            post_id=post_id,
            snapshot_phase_id=snapshot_phase_id,
            model="jev:jev-latest",
            executor=_executor(client, catalogue),
        )
    )
    await client.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    rows = state.conn.execute(
        "SELECT status FROM evaluation_phase_runs WHERE post_id = ?", (post_id,)
    ).fetchall()
    assert [row["status"] for row in rows] == ["cancelled"]
    await tracer.close()
    state.close()


@pytest.mark.asyncio
async def test_jev_error_is_returned_with_retryable_scoring_fields(tmp_path) -> None:
    tracer = SQLiteTracer(db_path=str(tmp_path / "traces.db"))
    state = StateManager(db_path=str(tmp_path / "state.db"))
    scan_id, post_id, snapshot_phase_id = _seed(state)

    async def failed_executor(config, input_text):
        del config, input_text
        raise JevRelevanceError(
            operation="jev.evaluate",
            message_id="post-1",
            detail="provider unavailable",
        )

    result = await _run_phase(
        phase="relevance",
        config=_config(tracer),
        input_text="formatted post",
        message_id="post-1",
        state=state,
        scan_id=scan_id,
        post_id=post_id,
        snapshot_phase_id=snapshot_phase_id,
        model="jev:jev-latest",
        executor=failed_executor,
    )

    assert not isinstance(result, Ok)
    assert result.error.operation == "jev.evaluate"
    assert result.error.detail == "provider unavailable"
    await tracer.close()
    state.close()
