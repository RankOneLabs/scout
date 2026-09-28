"""Zero-shot relevance executor, trace, and phase persistence tests."""

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
from scout.relevance.executor import _reason, run_zeroshot_relevance
from scout.relevance.loader import RelevanceCatalogue, load_catalogue_bytes
from scout.relevance.models import ZeroShotRelevanceError, ZeroShotRelevanceOutput
from scout.replay.experiments import BaselineResolutionError, build_domain_diff, resolve_baseline
from scout.result import Ok
from scout.scanning.pipeline import _run_phase
from scout.storage.state import StateManager
from scout.typesafe.routes import route


class _UnusedLLM(LLMClient):
    async def complete(self, params: CompletionParams) -> LLMResponse:
        del params
        raise AssertionError("the zero-shot executor must not call the LLM client")


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


class _FailingJevStub:
    model = "jev-latest"

    def __init__(self, detail: str) -> None:
        self.detail = detail

    async def evaluate(self, state: Any, questions: Any) -> JevResult:
        del state, questions
        raise RuntimeError(self.detail)


def _catalogue() -> RelevanceCatalogue:
    question = {
        "type": "noul",
        "instructions": "Synthetic question",
        "criteria": {"true": {"what": "yes"}, "false": {"what": "no"}},
    }
    document = {
        "id": "test-relevance",
        "decide": "agent_ops_route/v1",
        "description": "Test zero-shot relevance catalogue.",
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


def _answers(**overrides: float) -> dict[str, float]:
    return {
        "excl_noise": 0.0,
        "needs_thread": 0.0,
        "answerable_from_post": 0.0,
        "about_agent_work": 0.0,
        "points_somewhere": 0.0,
        **overrides,
    }


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        (
            _answers(excl_hype=0.9),
            "Zero-shot: excluded (hype).",
        ),
        (
            _answers(needs_thread=0.9),
            "Zero-shot: review; the post needs its thread for context.",
        ),
        (
            _answers(answerable_from_post=0.9, about_agent_work=0.9),
            "Zero-shot: respond; answerable from the post and about agent work.",
        ),
        (
            _answers(points_somewhere=0.9),
            "Zero-shot: review; the post points to something outside itself.",
        ),
        (
            _answers(),
            "Zero-shot: no respond or review signal.",
        ),
    ],
)
def test_route_reason_strings(answers: dict[str, float], expected: str) -> None:
    assert _reason(route(answers)) == expected


def test_route_reason_appends_close_call_names_in_consulted_order() -> None:
    decision = route(
        _answers(
            excl_noise=0.45,
            needs_thread=0.45,
            answerable_from_post=0.9,
            about_agent_work=0.9,
        )
    )

    assert decision.margin == ("excl_noise", "needs_thread")
    assert _reason(decision) == (
        "Zero-shot: respond; answerable from the post and about agent work. "
        "Close call on excl_noise, needs_thread; sent to review."
    )


def _config(tracer: SQLiteTracer) -> AgentConfig[ZeroShotRelevanceOutput]:
    return AgentConfig[ZeroShotRelevanceOutput](
        name="unused-llm-config",
        description="unused",
        system_prompt="unused",
        llm=_UnusedLLM(),
        feedback=NullFeedbackLoop(),
        tracer=tracer,
        tools=ToolRegistry([]),
        output_schema=ZeroShotRelevanceOutput,
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
        run_zeroshot_relevance,
        client=client,
        catalogue=catalogue,
        questions=catalogue.questions,
        zeroshot_state={"post": {"text": "agent retries"}},
        project_key="agent-ops",
        message_id="post-1",
    )


@pytest.mark.asyncio
async def test_zeroshot_executor_writes_prescribed_trace_without_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    tracer = SQLiteTracer(db_path=str(tmp_path / "traces.db"))
    secret = "typesafe-secret-must-not-appear"
    monkeypatch.setattr(scout_config, "TYPESAFE_API_KEY", secret)
    client = _JevStub(replace(_result(), call_id=f"call-{secret}"), api_key=secret)

    result = await run_zeroshot_relevance(
        _config(tracer),
        "formatted post",
        client=cast(Any, client),
        catalogue=_catalogue(),
        questions=_catalogue().questions,
        zeroshot_state={"post": {"text": "agent retries"}},
        project_key="agent-ops",
        message_id="post-1",
    )

    spans = await tracer.get_trace(result.trace_id)
    roots = [span for span in spans if span.parent_id is None]
    children = [span for span in spans if span.parent_id == roots[0].id]
    assert [(span.name, span.kind) for span in roots] == [
        ("scout_relevance_zeroshot", SpanKind.AGENT_RUN)
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
    domain_diff = build_domain_diff(roots[0], roots[0])
    assert domain_diff["baseline"] == domain_diff["candidate"]
    assert domain_diff["baseline"]["complete"] is True
    assert domain_diff["baseline"]["value"] == result.parsed.model_dump(mode="json")
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
async def test_generic_executor_failure_redacts_api_key_from_trace(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    tracer = SQLiteTracer(db_path=str(tmp_path / "traces.db"))
    secret = "typesafe-secret-in-runtime-error"
    monkeypatch.setattr(scout_config, "TYPESAFE_API_KEY", secret)
    catalogue = _catalogue()

    with pytest.raises(RuntimeError, match="provider runtime failed"):
        await run_zeroshot_relevance(
            _config(tracer),
            "formatted post",
            client=cast(Any, _FailingJevStub(f"provider runtime failed: {secret}")),
            catalogue=catalogue,
            questions=catalogue.questions,
            zeroshot_state={"post": {"text": "agent retries"}},
            project_key="agent-ops",
            message_id="post-1",
        )

    roots = await tracer.list_traces(name="scout_relevance_zeroshot")
    assert len(roots) == 1
    spans = await tracer.get_trace(roots[0].trace_id)
    serialized = json.dumps(
        [{"metadata": span.metadata, "error": span.error} for span in spans],
        default=str,
    )
    assert secret not in serialized
    assert "provider runtime failed: [REDACTED]" in serialized
    await tracer.close()


@pytest.mark.asyncio
async def test_zeroshot_phase_is_refused_as_replay_baseline(tmp_path) -> None:
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
        model="zeroshot:jev-latest",
        executor=_executor(_JevStub(_result()), catalogue),
    )

    assert isinstance(phase, Ok)
    assert isinstance(phase.value.parsed, ZeroShotRelevanceOutput)
    with pytest.raises(BaselineResolutionError, match="produced by zero-shot"):
        await resolve_baseline(state, tracer, phase.value.phase_run_id)
    phase_run_count = state.conn.execute(
        "SELECT COUNT(*) FROM evaluation_phase_runs WHERE post_id = ?", (post_id,)
    ).fetchone()[0]
    assert phase_run_count == 1
    await tracer.close()
    state.close()


@pytest.mark.asyncio
async def test_cancelled_zeroshot_phase_uses_phase_cleanup_path(tmp_path) -> None:
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
            model="zeroshot:jev-latest",
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
async def test_zeroshot_error_is_returned_with_retryable_scoring_fields(tmp_path) -> None:
    tracer = SQLiteTracer(db_path=str(tmp_path / "traces.db"))
    state = StateManager(db_path=str(tmp_path / "state.db"))
    scan_id, post_id, snapshot_phase_id = _seed(state)

    async def failed_executor(config, input_text):
        del config, input_text
        raise ZeroShotRelevanceError(
            operation="zeroshot.evaluate",
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
        model="zeroshot:jev-latest",
        executor=failed_executor,
    )

    assert not isinstance(result, Ok)
    assert result.error.operation == "zeroshot.evaluate"
    assert result.error.detail == "provider unavailable"
    await tracer.close()
    state.close()
