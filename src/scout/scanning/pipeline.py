"""Scout per-message pipeline with explicit Jig-backed phases."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from jig import (
    AgentConfig,
    AgentResult,
    PipelineConfig,
    Span,
    SpanKind,
    Step,
    TracingLogger,
    run_agent,
)

import scout.config as _config
from scout.dossiers.resolver import DossierSummary
from scout.errors import LLMError, ParseError
from scout.relevance.binding import RelevancePostStateSource
from scout.relevance.executor import PhaseTracer, run_zeroshot_relevance
from scout.relevance.models import (
    ZEROSHOT_PROJECT_KEYS,
    RelevanceAction,
    ZeroShotRelevanceError,
    ZeroShotRelevanceOutput,
)
from scout.relevance.setup import ZeroShotScanContext
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
from scout.typesafe.state import build_state

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


class _TraceIdCapturingTracer(TracingLogger):
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
    tracer: PhaseTracer,
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

    use_zeroshot = (
        _config.RELEVANCE_CLASSIFIER == "zeroshot"
        and project_key in ZEROSHOT_PROJECT_KEYS
    )
    executor: Callable[
        [AgentConfig[RelevancePhaseOutput], str],
        Awaitable[AgentResult[RelevancePhaseOutput]],
    ] = run_agent
    if use_zeroshot:
        zeroshot_context: ZeroShotScanContext | None = ctx.get("zeroshot_context")
        target = (
            zeroshot_context.projects.get(project_key)
            if zeroshot_context is not None and project_key is not None
            else None
        )
        if zeroshot_context is None or target is None or project_key is None:
            return Err(
                LLMError(
                    operation="relevance",
                    message_id=msg.platform_id,
                    detail=f"Zero-shot relevance context is missing project {project_key!r}",
                )
            )
        source: RelevancePostStateSource = {
            "platform": msg.platform,
            "channel_name": msg.channel_name,
            "url": msg.url,
            "content": msg.content,
            "parent_author_name": msg.parent.author.name if msg.parent else None,
            "parent_text": msg.parent.text if msg.parent else None,
            "author_name": msg.author.name,
            "author_handle": msg.author.handle,
        }
        zeroshot_state = build_state(source, target, zeroshot_context.catalogue)

        async def _execute_zeroshot(
            config: AgentConfig[RelevancePhaseOutput], text: str
        ) -> AgentResult[RelevancePhaseOutput]:
            result = await run_zeroshot_relevance(
                cast(AgentConfig[ZeroShotRelevanceOutput], config),
                text,
                client=zeroshot_context.client,
                catalogue=zeroshot_context.catalogue,
                questions=zeroshot_context.questions,
                zeroshot_state=zeroshot_state,
                project_key=project_key,
                message_id=msg.platform_id,
            )
            return cast(AgentResult[RelevancePhaseOutput], result)

        executor = _execute_zeroshot

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
            executor=executor,
        )
    )
    if isinstance(relevance, Err):
        return relevance
    relevance_output = relevance.value.parsed
    contributor_ids = [relevance.value.phase_run_id]
    relevance_classifier = relevance.value.model
    relevance_action: RelevanceAction = (
        relevance_output.action
        if isinstance(relevance_output, ZeroShotRelevanceOutput)
        else (
            "respond"
            if relevance_output.relevant
            and relevance_output.score >= _config.RELEVANCE_THRESHOLD
            else "drop"
        )
    )

    holdout_draw: Callable[[str, str | None, RelevanceAction], bool] | None = ctx.get(
        "holdout_draw"
    )
    if holdout_draw is not None and holdout_draw(
        "zeroshot" if use_zeroshot else "llm", project_key, relevance_action
    ):
        return Ok(
            ReplyCandidate(
                relevant=relevance_output.relevant,
                score=relevance_output.score,
                reason=relevance_output.reason,
                relevant_to=relevance_output.relevant_to,
                project_key=project_key,
                relevance_output=relevance_output,
                relevance_classifier=relevance_classifier,
                relevance_action=relevance_action,
                held=True,
                contributor_phase_run_ids=tuple(contributor_ids),
            )
        )

    if not relevance_output.relevant:
        return Ok(
            ReplyCandidate(
                relevant=False,
                score=relevance_output.score,
                reason=relevance_output.reason,
                relevant_to=relevance_output.relevant_to,
                project_key=project_key,
                relevance_output=relevance_output,
                relevance_classifier=relevance_classifier,
                relevance_action=relevance_action,
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
        relevance_classifier=relevance_classifier,
        relevance_action=relevance_action,
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
    relevance_action = cast(RelevanceAction, ctx.get("relevance_action", "respond"))
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
        relevance_classifier="human",
        relevance_action=relevance_action,
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
    relevance_classifier: str,
    relevance_action: RelevanceAction,
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
                relevance_output=relevance_output,
                relevance_classifier=relevance_classifier,
                relevance_action=relevance_action,
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
                relevance_output=relevance_output,
                relevance_classifier=relevance_classifier,
                relevance_action=relevance_action,
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
            relevance_output=relevance_output,
            relevance_classifier=relevance_classifier,
            relevance_action=relevance_action,
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
    executor: Callable[[AgentConfig[T], str], Awaitable[AgentResult[T]]] = run_agent,
) -> Result[PhaseExecution[T], LLMError | ParseError]:
    capturing = _TraceIdCapturingTracer(config.tracer)
    phase_config = config.with_(tracer=capturing)

    try:
        agent_result = await executor(phase_config, input_text)
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
    except ZeroShotRelevanceError as e:
        logger.error(
            "%s phase failed for message %s: %s", phase, message_id, e.detail
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
                operation=e.operation,
                message_id=e.message_id,
                detail=e.detail,
            )
        )
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
