"""Jev relevance execution with Jig-compatible trace evidence."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any, Protocol

from jig import AgentConfig, AgentResult, Span, SpanKind, Usage
from jig.jev import JevClient, JevError, NoulAnswer, NoulQuestion
from jig.jev.tracing import to_jig_usage

import scout.config as _config
from scout.relevance.loader import RelevanceCatalogue
from scout.relevance.models import JevRelevanceError, JevRelevanceOutput
from scout.typesafe.routes import route


class PhaseTracer(Protocol):
    """The trace operations shared by phase execution and persistence."""

    def start_trace(
        self,
        name: str,
        metadata: dict[str, Any] | None = None,
        kind: SpanKind = SpanKind.AGENT_RUN,
    ) -> Span: ...

    def start_span(
        self,
        parent_id: str,
        kind: SpanKind,
        name: str,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> Span: ...

    def end_span(
        self,
        span_id: str,
        output: Any = None,
        error: str | None = None,
        usage: Usage | None = None,
    ) -> None: ...

    async def get_trace(self, trace_id: str) -> list[Span]: ...

    async def flush(self) -> None: ...


def _config_snapshot(catalogue: RelevanceCatalogue, model: str) -> dict[str, Any]:
    """Return the literal replay fields required by resolve_baseline."""
    return {
        "agent_name": "scout_relevance_jev",
        "description": catalogue.document.description,
        "system_prompt": catalogue.document.description,
        "system_prompt_is_callable": False,
        "output_schema": "scout.relevance.models:JevRelevanceOutput",
        "structured_output_mode": "native",
        "model_id": f"jev:{model}",
    }


def _reason(line: str, exclusion: str | None) -> str:
    return f"{line}: {exclusion}" if line == "exclusion" and exclusion else line


def _redact_api_key(value: str | None) -> str | None:
    """Remove the configured provider credential from trace-facing strings."""
    api_key = _config.TYPESAFE_API_KEY
    if value is None or not api_key:
        return value
    return value.replace(api_key, "[REDACTED]")


async def run_jev_relevance(
    config: AgentConfig[JevRelevanceOutput],
    input_text: str,
    *,
    client: JevClient,
    catalogue: RelevanceCatalogue,
    questions: Sequence[NoulQuestion],
    jev_state: dict[str, Any],
    project_key: str,
    message_id: str,
) -> AgentResult[JevRelevanceOutput]:
    """Evaluate one routed post and return a run_agent-shaped phase result."""
    tracer: PhaseTracer = config.tracer
    started = time.monotonic()
    root = tracer.start_trace(
        "scout_relevance_jev",
        {
            "input": input_text,
            "config": _config_snapshot(catalogue, client.model),
        },
        kind=SpanKind.AGENT_RUN,
    )
    child_metadata: dict[str, Any] = {}
    child = tracer.start_span(
        root.id,
        SpanKind.PROVIDER_CALL,
        "jev.call",
        input={
            "state": jev_state,
            "question_ids": [question.id for question in questions],
        },
        metadata=child_metadata,
    )

    try:
        result = await client.evaluate(jev_state, questions)
        # Jig's SQLite and stdout tracers retain this mapping by reference and
        # serialize it at flush. PhaseTracer deliberately requires that Jig
        # behavior because its interface has no metadata-update operation.
        child_metadata.update(
            {
                "call_id": _redact_api_key(result.call_id),
                "provider_request_id": _redact_api_key(result.provider_request_id),
                "model": _redact_api_key(result.model),
                "latency_ms": result.latency_ms,
                "attempts": result.attempts,
            }
        )
        tracer.end_span(
            child.id,
            output={"answer_count": len(result.answers)},
            usage=to_jig_usage(result.usage),
        )

        answers: dict[str, float] = {}
        for question_id, answer in result.answers.items():
            if not isinstance(answer, NoulAnswer):
                raise JevRelevanceError(
                    operation="relevance",
                    message_id=message_id,
                    detail=f"Jev returned a non-noul answer for {question_id!r}",
                )
            answers[answer.question_id] = answer.noul
        decision = route(answers)
        relevant = decision.action in ("respond", "review")
        output = JevRelevanceOutput(
            relevant=relevant,
            score=1.0,
            reason=_reason(decision.line, decision.exclusion),
            relevant_to=[project_key] if relevant else [],
            action=decision.action,
            answers=answers,
            line=decision.line,
            margin=decision.margin,
            exclusion=decision.exclusion,
        )
        serialized = output.model_dump(mode="json")
        tracer.end_span(root.id, output=serialized)
        await tracer.flush()
        return AgentResult(
            output=output.model_dump_json(),
            trace_id=root.trace_id,
            usage={
                "total_input_tokens": result.usage.input_tokens,
                "total_output_tokens": result.usage.output_tokens,
                "total_cost": 0.0,
                "llm_calls": 0,
                "tool_calls": 0,
            },
            scores=None,
            duration_ms=(time.monotonic() - started) * 1000,
            parsed=output,
        )
    except JevError as exc:
        error_detail = _redact_api_key(str(exc)) or ""
        child_metadata.update(
            {
                "call_id": _redact_api_key(exc.call_id),
                "provider_request_id": _redact_api_key(exc.provider_request_id),
                "model": _redact_api_key(client.model),
                "latency_ms": exc.elapsed_ms,
                "attempts": exc.attempts,
            }
        )
        tracer.end_span(child.id, error=error_detail)
        tracer.end_span(root.id, error=error_detail)
        await tracer.flush()
        raise JevRelevanceError(
            operation="relevance",
            message_id=message_id,
            detail=error_detail,
        ) from None
    except BaseException as exc:
        if child.ended_at is None:
            tracer.end_span(child.id, error=f"{type(exc).__name__}: {exc}")
        if root.ended_at is None:
            tracer.end_span(root.id, error=f"{type(exc).__name__}: {exc}")
        await tracer.flush()
        raise


__all__ = ["PhaseTracer", "run_jev_relevance"]
