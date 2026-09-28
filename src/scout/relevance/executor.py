"""Zero-shot relevance execution with Jig-compatible trace evidence."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from typing import Any, Protocol

from jig import AgentConfig, AgentResult, Span, SpanKind, Usage
from jig.core.runner import (
    ROOT_OUTPUT_BYTE_LENGTH_KEY,
    ROOT_OUTPUT_COMPLETE_KEY,
    ROOT_OUTPUT_KIND_KEY,
    ROOT_OUTPUT_SHA256_KEY,
)
from jig.jev import JevClient, JevError, NoulAnswer, NoulQuestion
from jig.jev.tracing import to_jig_usage

import scout.config as _config
from scout.relevance.classifier_identity import zeroshot_classifier
from scout.relevance.loader import RelevanceCatalogue
from scout.relevance.models import ZeroShotRelevanceError, ZeroShotRelevanceOutput
from scout.typesafe.routes import RouteDecision, route


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
        "agent_name": "scout_relevance_zeroshot",
        "description": catalogue.document.description,
        "system_prompt": catalogue.document.description,
        "system_prompt_is_callable": False,
        "output_schema": "scout.relevance.models:ZeroShotRelevanceOutput",
        "structured_output_mode": "native",
        "model_id": zeroshot_classifier(model),
    }


def _reason(decision: RouteDecision) -> str:
    reasons = {
        "exclusion": f"Zero-shot: excluded ({decision.exclusion}).",
        "needs_thread": "Zero-shot: review; the post needs its thread for context.",
        "respond": "Zero-shot: respond; answerable from the post and about agent work.",
        "points_somewhere": (
            "Zero-shot: review; the post points to something outside itself."
        ),
        "otherwise": "Zero-shot: no respond or review signal.",
    }
    reason = reasons[decision.line]
    if decision.margin:
        reason += f" Close call on {', '.join(decision.margin)}; sent to review."
    return reason


def _redact_api_key(value: str | None) -> str | None:
    """Remove the configured provider credential from trace-facing strings."""
    api_key = _config.TYPESAFE_API_KEY
    if value is None or not api_key:
        return value
    return value.replace(api_key, "[REDACTED]")


def _structured_output_envelope(
    output: ZeroShotRelevanceOutput,
) -> tuple[str, dict[str, Any]]:
    """Build the complete structured-output evidence emitted by Jig."""
    rendered = output.model_dump_json()
    complete = output.model_dump(mode="json")
    canonical = json.dumps(
        complete,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return rendered, {
        "output": rendered[:200],
        "scores": None,
        ROOT_OUTPUT_KIND_KEY: "structured",
        ROOT_OUTPUT_COMPLETE_KEY: complete,
        ROOT_OUTPUT_SHA256_KEY: hashlib.sha256(canonical).hexdigest(),
        ROOT_OUTPUT_BYTE_LENGTH_KEY: len(canonical),
    }


async def run_zeroshot_relevance(
    config: AgentConfig[ZeroShotRelevanceOutput],
    input_text: str,
    *,
    client: JevClient,
    catalogue: RelevanceCatalogue,
    questions: Sequence[NoulQuestion],
    zeroshot_state: dict[str, Any],
    project_key: str,
    message_id: str,
) -> AgentResult[ZeroShotRelevanceOutput]:
    """Evaluate one routed post and return a run_agent-shaped phase result."""
    tracer: PhaseTracer = config.tracer
    started = time.monotonic()
    root = tracer.start_trace(
        "scout_relevance_zeroshot",
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
            "state": zeroshot_state,
            "question_ids": [question.id for question in questions],
        },
        metadata=child_metadata,
    )

    try:
        result = await client.evaluate(zeroshot_state, questions)
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
                raise ZeroShotRelevanceError(
                    operation="relevance",
                    message_id=message_id,
                    detail=f"Jev returned a non-noul answer for {question_id!r}",
                )
            answers[answer.question_id] = answer.noul
        decision = route(answers)
        # A review decision is deliberately withheld from automatic drafting.
        # The action and project stay on the candidate for later inspection.
        relevant = decision.action == "respond"
        output = ZeroShotRelevanceOutput(
            relevant=relevant,
            score=1.0,
            reason=_reason(decision),
            relevant_to=[project_key] if relevant else [],
            action=decision.action,
            answers=answers,
            line=decision.line,
            margin=decision.margin,
            exclusion=decision.exclusion,
        )
        rendered, trace_output = _structured_output_envelope(output)
        tracer.end_span(root.id, output=trace_output)
        await tracer.flush()
        return AgentResult(
            output=rendered,
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
        raise ZeroShotRelevanceError(
            operation="relevance",
            message_id=message_id,
            detail=error_detail,
        ) from None
    except BaseException as exc:
        error_detail = _redact_api_key(f"{type(exc).__name__}: {exc}") or ""
        if child.ended_at is None:
            tracer.end_span(child.id, error=error_detail)
        if root.ended_at is None:
            tracer.end_span(root.id, error=error_detail)
        await tracer.flush()
        raise


__all__ = ["PhaseTracer", "run_zeroshot_relevance"]
