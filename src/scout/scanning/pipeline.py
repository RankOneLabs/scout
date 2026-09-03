"""Scout per-message pipeline with explicit Jig-backed phases."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jig import AgentConfig, PipelineConfig, Span, SpanKind, Step, TracingLogger, run_agent

from scout.dossiers.resolver import DossierSummary
from scout.errors import LLMError, ParseError
from scout.result import Err, Ok, Result
from scout.scanning.agent import (
    ScoutExecutionContext,
    ScoutPhaseConfigs,
    format_critic_input,
    format_message_input,
    format_reply_draft_input,
    format_routed_message_input,
)
from scout.scanning.prefilter import RoutedMessage
from scout.scanning.schemas import (
    CritiquePhaseOutput,
    RelevancePhaseOutput,
    ReplyCandidate,
    StructuredDraftOutput,
)
from scout.storage.state import StateManager

logger = logging.getLogger("scout.scanning.pipeline")


@dataclass(frozen=True, slots=True)
class PhaseExecution[T]:
    """One phase's successful, durably-recorded execution.

    By the time this value exists, its trace has already been finalized,
    flushed, read back from the configured trace store, and verified to
    resolve to an AGENT_RUN root — trace_id and phase_run_id are never
    speculative. `model` is the resolved model string that produced
    `parsed`, threaded in by the caller rather than introspected from Jig.
    """

    parsed: T
    trace_id: str
    phase_run_id: int
    phase: str
    model: str


class _TraceEvidenceError(RuntimeError):
    """A phase's trace could not be finalized, flushed, read back, and
    verified as an AGENT_RUN root — never raised for a model-call failure,
    only for evidence-persistence itself failing after a real attempt."""


class _TraceIdCapturingTracer(TracingLogger):  # type: ignore[misc]
    """Narrowly-scoped, single-call wrapper around a real TracingLogger.

    Captures the trace id Jig's run_agent assigns synchronously inside
    start_trace — before run_agent's first await — so the id survives even
    if run_agent later raises or the awaiting task is cancelled mid-run. A
    fresh instance must be constructed per _run_phase call so concurrent
    phase executions never share captured state. Every operation delegates
    to `inner`; this wrapper adds no tracing behavior of its own.
    """

    def __init__(self, inner: TracingLogger) -> None:
        self._inner = inner
        self.captured_trace_id: str | None = None

    def start_trace(
        self,
        name: str,
        metadata: dict[str, Any] | None = None,
        kind: SpanKind = SpanKind.AGENT_RUN,
    ) -> Span:
        span = self._inner.start_trace(name, metadata, kind=kind)
        self.captured_trace_id = span.trace_id
        return span

    def start_span(
        self,
        parent_id: str,
        kind: SpanKind,
        name: str,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> Span:
        return self._inner.start_span(parent_id, kind, name, input=input, metadata=metadata)

    def end_span(
        self,
        span_id: str,
        output: Any = None,
        error: str | None = None,
        usage: Any = None,
    ) -> None:
        self._inner.end_span(span_id, output=output, error=error, usage=usage)

    async def get_trace(self, trace_id: str) -> list[Span]:
        spans: list[Span] = await self._inner.get_trace(trace_id)
        return spans

    async def list_traces(
        self,
        since: Any = None,
        limit: int = 50,
        name: str | None = None,
    ) -> list[Span]:
        spans: list[Span] = await self._inner.list_traces(since=since, limit=limit, name=name)
        return spans

    async def flush(self) -> None:
        await self._inner.flush()


def _verify_agent_run_root(spans: list[Span], trace_id: str) -> None:
    root = next((s for s in spans if s.parent_id is None), None)
    if root is None:
        raise _TraceEvidenceError(
            f"trace {trace_id!r} did not resolve to any stored root span"
        )
    if root.kind != SpanKind.AGENT_RUN or root.trace_id != trace_id:
        raise _TraceEvidenceError(
            f"trace {trace_id!r} root span is not an AGENT_RUN root"
        )


async def _finalize_and_persist_phase_run(
    tracer: _TraceIdCapturingTracer,
    trace_id: str,
    *,
    state: StateManager,
    scan_id: int,
    post_id: int,
    snapshot_phase_id: int,
    phase: str,
    model: str,
    status: str,
) -> int:
    """Flush the tracer, read the trace back, verify it resolves to an
    AGENT_RUN root, and only then insert the durable phase-run row.

    Raises _TraceEvidenceError — never inserting a row — if flush,
    read-back, or verification fails: an absent phase-run is more truthful
    than one pointing at unavailable or mistyped evidence. Opens no
    transaction until after this verification, and the insert itself is a
    single short StateManager transaction.
    """
    try:
        await tracer.flush()
        spans = await tracer.get_trace(trace_id)
    except Exception as e:
        raise _TraceEvidenceError(f"trace flush/read-back failed: {e}") from e
    _verify_agent_run_root(spans, trace_id)
    return state.insert_phase_run(
        scan_id=scan_id,
        post_id=post_id,
        snapshot_phase_id=snapshot_phase_id,
        phase=phase,
        trace_id=trace_id,
        model=model,
        status=status,
    )


async def score_and_draft_step(
    ctx: dict[str, Any],
) -> Result[ReplyCandidate, LLMError | ParseError]:
    """Run Scout's relevance, draft, and critic phases for one message."""
    msg_input = ctx["input"]
    phase_configs: ScoutPhaseConfigs = ctx["phase_configs"]
    dossier_summaries: Mapping[str, DossierSummary] = ctx.get("dossier_summaries", {})
    execution: ScoutExecutionContext = ctx["execution_context"]

    if isinstance(msg_input, RoutedMessage):
        msg = msg_input.message
        formatted_input = format_routed_message_input(msg_input)
        project_key: str | None = (
            msg_input.keyword_route.project_key if msg_input.keyword_route else None
        )
    else:
        msg = msg_input
        formatted_input = format_message_input(msg)
        project_key = None

    relevance: Result[PhaseExecution[RelevancePhaseOutput], LLMError | ParseError] = (
        await _run_phase(
            phase="relevance",
            config=phase_configs.relevance,
            input_text=formatted_input,
            message_id=msg.platform_id,
            state=execution.state,
            scan_id=execution.scan_id,
            post_id=execution.post_id,
            snapshot_phase_id=execution.relevance.snapshot_phase_id,
            model=execution.relevance.model,
        )
    )
    if isinstance(relevance, Err):
        return relevance
    relevance_output = relevance.value.parsed
    contributor_ids = [relevance.value.phase_run_id]

    if not relevance_output.relevant:
        return Ok(
            ReplyCandidate(
                relevant=False,
                score=relevance_output.score,
                reason=relevance_output.reason,
                relevant_to=relevance_output.relevant_to,
                contributor_phase_run_ids=tuple(contributor_ids),
            )
        )

    return await _draft_and_critic(
        msg=msg,
        formatted_input=formatted_input,
        project_key=project_key,
        phase_configs=phase_configs,
        dossier_summaries=dossier_summaries,
        execution=execution,
        relevance_output=relevance_output,
        contributor_ids=contributor_ids,
    )


async def draft_and_critic_step(
    ctx: dict[str, Any],
) -> Result[ReplyCandidate, LLMError | ParseError]:
    """Run only the normal response phases after a human relevance override.

    The caller supplies a validated, relevant ``RelevancePhaseOutput`` in
    ``ctx['relevance_output']``. No relevance model call is made: the human
    false-negative grade is the relevance authority for this workflow.
    """
    msg_input = ctx["input"]
    phase_configs: ScoutPhaseConfigs = ctx["phase_configs"]
    dossier_summaries: Mapping[str, DossierSummary] = ctx.get("dossier_summaries", {})
    execution: ScoutExecutionContext = ctx["execution_context"]
    relevance_output: RelevancePhaseOutput = ctx["relevance_output"]
    if not relevance_output.relevant:
        raise ValueError("draft_and_critic_step requires a relevant human override")

    if isinstance(msg_input, RoutedMessage):
        msg = msg_input.message
        formatted_input = format_routed_message_input(msg_input)
        project_key: str | None = (
            msg_input.keyword_route.project_key if msg_input.keyword_route else None
        )
    else:
        msg = msg_input
        formatted_input = format_message_input(msg)
        project_key = None

    return await _draft_and_critic(
        msg=msg,
        formatted_input=formatted_input,
        project_key=project_key,
        phase_configs=phase_configs,
        dossier_summaries=dossier_summaries,
        execution=execution,
        relevance_output=relevance_output,
        contributor_ids=[],
    )


async def _draft_and_critic(
    *,
    msg: Any,
    formatted_input: str,
    project_key: str | None,
    phase_configs: ScoutPhaseConfigs,
    dossier_summaries: Mapping[str, DossierSummary],
    execution: ScoutExecutionContext,
    relevance_output: RelevancePhaseOutput,
    contributor_ids: list[int],
) -> Result[ReplyCandidate, LLMError | ParseError]:
    """Shared reply-draft and critic implementation for model and human relevance."""
    dossier = dossier_summaries.get(project_key) if project_key is not None else None

    draft_input = format_reply_draft_input(
        message_input=formatted_input,
        relevance=relevance_output,
        dossier=dossier,
    )
    draft: Result[PhaseExecution[StructuredDraftOutput], LLMError | ParseError] = (
        await _run_phase(
            phase="reply_draft",
            config=phase_configs.reply_draft,
            input_text=draft_input,
            message_id=msg.platform_id,
            state=execution.state,
            scan_id=execution.scan_id,
            post_id=execution.post_id,
            snapshot_phase_id=execution.reply_draft.snapshot_phase_id,
            model=execution.reply_draft.model,
        )
    )
    if isinstance(draft, Err):
        return draft
    draft_output: StructuredDraftOutput = draft.value.parsed
    contributor_ids.append(draft.value.phase_run_id)

    # Abstain: intentional no-reply, reached after a relevant relevance
    # phase — relevant stays true so classify_outcome sees the abstention
    # as its own terminal outcome rather than an irrelevance judgment.
    # Critic is not needed for a no-reply decision.
    if draft_output.posture == "abstain":
        return Ok(
            ReplyCandidate(
                relevant=True,
                score=relevance_output.score,
                reason=relevance_output.reason,
                relevant_to=relevance_output.relevant_to,
                project_key=project_key,
                structured_draft=draft_output,
                contributor_phase_run_ids=tuple(contributor_ids),
            )
        )

    critic_input = format_critic_input(
        message_input=formatted_input,
        relevance=relevance_output,
        draft=draft_output,
        dossier=dossier,
    )
    critique: Result[PhaseExecution[CritiquePhaseOutput], LLMError | ParseError] = (
        await _run_phase(
            phase="critic",
            config=phase_configs.critic,
            input_text=critic_input,
            message_id=msg.platform_id,
            state=execution.state,
            scan_id=execution.scan_id,
            post_id=execution.post_id,
            snapshot_phase_id=execution.critic.snapshot_phase_id,
            model=execution.critic.model,
        )
    )
    if isinstance(critique, Err):
        return critique
    critique_output = critique.value.parsed
    contributor_ids.append(critique.value.phase_run_id)

    if critique_output.verdict == "reject":
        return Ok(
            ReplyCandidate(
                relevant=False,
                score=relevance_output.score,
                reason=(
                    f"{relevance_output.reason} "
                    f"Critic rejected draft: {critique_output.feedback}"
                ),
                relevant_to=relevance_output.relevant_to,
                project_key=project_key,
                critique_verdict=critique_output.verdict,
                critique_feedback=critique_output.feedback,
                structured_draft=draft_output,
                contributor_phase_run_ids=tuple(contributor_ids),
            )
        )

    # The critic's nested draft has already been validated as part of the
    # CritiquePhaseOutput tool call. Keeping it structured avoids a fragile
    # second JSON encoding/parsing round trip.
    if critique_output.verdict == "revise":
        if critique_output.revised_draft is None:
            return Err(
                ParseError(
                    raw_text=str(critique_output),
                    detail=(
                        "critic phase returned verdict='revise' without revised_draft"
                    ),
                )
            )
        final_draft = critique_output.revised_draft
    else:
        final_draft = draft_output

    return Ok(
        ReplyCandidate(
            relevant=True,
            score=relevance_output.score,
            reason=relevance_output.reason,
            relevant_to=relevance_output.relevant_to,
            project_key=project_key,
            critique_verdict=critique_output.verdict,
            critique_feedback=critique_output.feedback,
            structured_draft=final_draft,
            contributor_phase_run_ids=tuple(contributor_ids),
        )
    )


async def _run_phase[T](
    *,
    phase: str,
    config: AgentConfig[T],
    input_text: str,
    message_id: str,
    state: StateManager,
    scan_id: int,
    post_id: int,
    snapshot_phase_id: int,
    model: str,
) -> Result[PhaseExecution[T], LLMError | ParseError]:
    capturing = _TraceIdCapturingTracer(config.tracer)
    phase_config = config.with_(tracer=capturing)

    try:
        agent_result = await run_agent(phase_config, input_text)
    except asyncio.CancelledError:
        trace_id = capturing.captured_trace_id
        if trace_id is not None:
            confirmed_trace_id: str = trace_id

            async def _cleanup() -> None:
                with contextlib.suppress(Exception):
                    await _finalize_and_persist_phase_run(
                        capturing, confirmed_trace_id, state=state, scan_id=scan_id,
                        post_id=post_id, snapshot_phase_id=snapshot_phase_id,
                        phase=phase, model=model, status="cancelled",
                    )
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(_cleanup()), timeout=5.0)
        raise
    except Exception as e:
        logger.error(
            "%s phase raised for message %s: %s", phase, message_id, e, exc_info=True
        )
        trace_id = capturing.captured_trace_id
        if trace_id is not None:
            with contextlib.suppress(Exception):
                await _finalize_and_persist_phase_run(
                    capturing, trace_id, state=state, scan_id=scan_id, post_id=post_id,
                    snapshot_phase_id=snapshot_phase_id, phase=phase, model=model,
                    status="error",
                )
        return Err(
            LLMError(
                operation=phase,
                message_id=message_id,
                detail=str(e),
            )
        )

    trace_id = capturing.captured_trace_id or agent_result.trace_id

    if agent_result.error is not None:
        if trace_id is not None:
            with contextlib.suppress(Exception):
                await _finalize_and_persist_phase_run(
                    capturing, trace_id, state=state, scan_id=scan_id, post_id=post_id,
                    snapshot_phase_id=snapshot_phase_id, phase=phase, model=model,
                    status="error",
                )
        return Err(
            LLMError(
                operation=phase,
                message_id=message_id,
                detail=str(agent_result.error),
            )
        )

    if agent_result.parsed is None:
        if trace_id is not None:
            with contextlib.suppress(Exception):
                await _finalize_and_persist_phase_run(
                    capturing, trace_id, state=state, scan_id=scan_id, post_id=post_id,
                    snapshot_phase_id=snapshot_phase_id, phase=phase, model=model,
                    status="error",
                )
        return Err(
            ParseError(
                raw_text=agent_result.output,
                detail=f"{phase} phase returned no parsed output",
            )
        )

    assert trace_id is not None, "a successful run_agent result must carry a trace id"
    try:
        phase_run_id = await _finalize_and_persist_phase_run(
            capturing, trace_id, state=state, scan_id=scan_id, post_id=post_id,
            snapshot_phase_id=snapshot_phase_id, phase=phase, model=model,
            status="complete",
        )
    except _TraceEvidenceError as e:
        logger.error(
            "%s phase evidence persistence failed for message %s: %s",
            phase, message_id, e, exc_info=True,
        )
        return Err(
            LLMError(
                operation=phase,
                message_id=message_id,
                detail=f"phase evidence persistence failed: {e}",
            )
        )

    return Ok(
        PhaseExecution(
            parsed=agent_result.parsed,
            trace_id=trace_id,
            phase_run_id=phase_run_id,
            phase=phase,
            model=model,
        )
    )


def _extract_err(result: Any) -> str:
    """Render an Err into trace-friendly detail text."""
    if not isinstance(result, Err):
        return str(result)
    error = result.error
    operation = getattr(error, "operation", None)
    detail = getattr(error, "detail", None)
    if operation and detail is not None:
        return f"{operation}: {detail}"
    if detail is not None:
        return str(detail)
    return str(error)


def build_scout_pipeline(tracer: TracingLogger) -> PipelineConfig:
    """Build a PipelineConfig wrapping the single score_and_draft step."""
    return PipelineConfig(
        name="scout_message",
        steps=[Step(name="score_and_draft", fn=score_and_draft_step)],
        tracer=tracer,
        is_err=lambda r: isinstance(r, Err),
        extract_err=_extract_err,
    )
