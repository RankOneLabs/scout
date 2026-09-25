"""Scout per-message pipeline with explicit Jig-backed phases."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import Mapping
from typing import Any, Protocol

from jig import AgentConfig, PipelineConfig, Step, TracingLogger, run_agent

from scout.dossiers.resolver import DossierSummary
from scout.errors import LLMError, ParseError
from scout.registry import ProjectTarget
from scout.result import Err, Ok, Result
from scout.scanning.agent import (
    ScoutExecutionContext,
    ScoutPhaseConfigs,
    format_critic_input,
    format_message_input,
    format_reply_draft_input,
    format_routed_message_input,
)
from scout.scanning.jev_router import ROUTER_VERSION
from scout.scanning.jev_state import project_from_target
from scout.scanning.prefilter import RoutedMessage
from scout.scanning.relevance import (
    JevRuntime,
    PhaseExecution,
    TraceEvidenceError,
    TraceIdCapturingTracer,
    finalize_and_persist_phase_run,
    llm_relevance_action,
    run_jev_relevance_phase,
)
from scout.scanning.schemas import (
    CritiquePhaseOutput,
    HoldoutCaptureRef,
    HoldoutDraw,
    RecordedRelevanceDecision,
    RelevancePhaseOutput,
    ReplyCandidate,
    StructuredDraftOutput,
)
from scout.storage.holdouts import HoldoutStorageError, SamplingWrite
from scout.storage.state import StateManager

logger = logging.getLogger("scout.scanning.pipeline")

#: blake2b personalization for the holdout draw. Domain-separates this hash
#: from every other digest in the codebase, so a key that happens to collide
#: elsewhere cannot steer the sample.
HOLDOUT_DRAW_PERSON = b"scout-holdout-1"


def holdout_decision_key(*, platform: str, platform_id: str) -> str:
    """The immutable post identity one holdout draw is derived from.

    Platform identity only. Nothing that can change between two passes over
    the same post — no scan id, no timestamp, no score — may enter here, or
    a retry would roll a second time.
    """
    return f"{platform}:{platform_id}"


def stable_holdout_draw(*, decision_key: str, rate: float) -> HoldoutDraw:
    """Draw once for one post, from its identity alone.

    Deliberately not random. A crash retry, a rescore, and a second worker
    reaching the same post all re-derive the same draw, so a decision that
    was not selected can never become selected on a later pass and one that
    was cannot be lost. The rate is recorded beside the draw rather than
    folded into it, so a stored hold says which rate produced it.
    """
    digest = hashlib.blake2b(
        decision_key.encode("utf-8"), digest_size=8, person=HOLDOUT_DRAW_PERSON
    ).digest()
    value = int.from_bytes(digest, "big") / float(1 << 64)
    return HoldoutDraw(
        decision_key=decision_key, rate=rate, value=value, selected=value < rate
    )


class HoldoutSampler(Protocol):
    """Decides whether one decided post is held back before drafting."""

    def __call__(self, *, platform: str, platform_id: str) -> HoldoutDraw: ...


def build_holdout_sampler(rate: float) -> HoldoutSampler:
    """A sampler at `rate` over the stable draw.

    Rejects a rate outside [0, 1] rather than clamping: a misconfigured rate
    that silently became 1.0 would hold every post in production.
    """
    if not 0.0 <= rate <= 1.0:
        raise ValueError(f"holdout rate must be between 0.0 and 1.0, got {rate!r}")

    def sample(*, platform: str, platform_id: str) -> HoldoutDraw:
        return stable_holdout_draw(
            decision_key=holdout_decision_key(platform=platform, platform_id=platform_id),
            rate=rate,
        )

    return sample


def capture_owner_for_scan(scan_id: int) -> str:
    """Which attempt a capture belongs to.

    One scan is one worker's pass over a post, so the scan is the finest
    identity that distinguishes two attempts on the same post — including two
    workers racing it, which each run their own scan.
    """
    return f"scan-{scan_id}"


def settle_holdout_draw(
    *,
    state: StateManager,
    post_id: int,
    scan_id: int,
    drawn: HoldoutDraw,
    relevance_phase_run_id: int | None,
) -> Result[tuple[HoldoutDraw, HoldoutCaptureRef], HoldoutStorageError]:
    """Record this post's draw durably, before drafting, and take its capture.

    The draw handed in is this pass's; the draw handed back is the one that is
    durable. They differ exactly when an earlier attempt already recorded one
    and the rate has moved since — the recorded answer wins, in both
    directions, which is what stops a crash retry under a changed
    `RELEVANCE_HOLDOUT_RATE` from re-deciding a post.

    Commits on its own, before any drafting call is made, so the selected or
    unselected outcome survives a crash anywhere downstream of here.
    """
    captured = state.holdouts.capture_sampling(
        SamplingWrite(
            post_id=post_id,
            decision_key=drawn.decision_key,
            rate=drawn.rate,
            draw=drawn.value,
            selected=drawn.selected,
            owner=capture_owner_for_scan(scan_id),
            relevance_phase_run_id=relevance_phase_run_id,
        )
    )
    if isinstance(captured, Err):
        return captured
    capture = captured.value
    settled = HoldoutDraw(
        decision_key=capture.decision_key,
        rate=capture.rate,
        value=capture.draw,
        selected=capture.selected,
    )
    if capture.resumed and settled != drawn:
        logger.info(
            "post %s resumes its recorded draw (rate %s, selected=%s) instead of "
            "this pass's (rate %s, selected=%s)",
            post_id,
            capture.rate,
            capture.selected,
            drawn.rate,
            drawn.selected,
        )
    return Ok(
        (
            settled,
            HoldoutCaptureRef(
                sampling_id=capture.id,
                post_id=capture.post_id,
                fence=capture.fence,
                settled_evaluation_id=capture.evaluation_id,
                resumed=capture.resumed,
            ),
        )
    )


async def score_and_draft_step(
    ctx: dict[str, Any],
) -> Result[ReplyCandidate, LLMError | ParseError | HoldoutStorageError]:
    """Run Scout's relevance, draft, and critic phases for one message.

    A refused holdout capture is returned as the storage error it is rather
    than dressed up as a model failure: the decision itself succeeded, and what
    failed is the attempt to record it. The runner treats it like any other
    retryable per-post failure — the post stays saved and unevaluated.
    """
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

    # Exactly one classifier runs. The runtime's presence is the selection:
    # the runner builds it only under RELEVANCE_CLASSIFIER=jev, so this step
    # never reads configuration itself and never calls both.
    jev_runtime: JevRuntime | None = ctx.get("jev_runtime")
    if jev_runtime is not None:
        decided = await _run_jev_relevance(
            runtime=jev_runtime,
            msg=msg,
            project_key=project_key,
            projects=ctx.get("projects", {}),
            execution=execution,
            tracer=phase_configs.relevance.tracer,
        )
        if isinstance(decided, Err):
            return decided
        relevance_output, recorded, contributor_ids = decided.value
    else:
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
        recorded = RecordedRelevanceDecision(
            classifier="llm",
            model=relevance.value.model,
            action=llm_relevance_action(relevance_output),
            reason=relevance_output.reason,
            phase_run_id=relevance.value.phase_run_id,
        )

    # The relevance boundary. Sampling happens here — on a decision that
    # succeeded, and before any drafting — so a held post costs exactly one
    # relevance call. A failed relevance call returned above and is never
    # sampled: there is no decision to hold. The draw covers every action,
    # drops included, because the holdout population is the decision
    # population and not just the positives.
    #
    # The draw is durable before the next line runs, and taking it reserves
    # this post: a crash between here and the evaluation leaves the recorded
    # answer to resume from, and a second worker that decided the same post
    # concurrently is refused at persistence rather than writing a competing
    # decision.
    sampler: HoldoutSampler | None = ctx.get("holdout_sampler")
    holdout: HoldoutDraw | None = None
    holdout_capture: HoldoutCaptureRef | None = None
    if sampler is not None:
        settled = settle_holdout_draw(
            state=execution.state,
            post_id=execution.post_id,
            scan_id=execution.scan_id,
            drawn=sampler(platform=msg.platform, platform_id=msg.platform_id),
            relevance_phase_run_id=recorded.phase_run_id,
        )
        if isinstance(settled, Err):
            return settled
        holdout, holdout_capture = settled.value

    if holdout is not None and holdout.selected:
        # Terminal here. `relevant` is carried through unchanged so the
        # recorded evaluation still says what the classifier decided; the
        # hold is what stops it, not an invented irrelevance.
        return Ok(
            ReplyCandidate(
                relevant=relevance_output.relevant,
                score=relevance_output.score,
                reason=relevance_output.reason,
                relevant_to=relevance_output.relevant_to,
                project_key=project_key,
                contributor_phase_run_ids=tuple(contributor_ids),
                relevance_decision=recorded,
                holdout=holdout,
                holdout_capture=holdout_capture,
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
                contributor_phase_run_ids=tuple(contributor_ids),
                relevance_decision=recorded,
                holdout=holdout,
                holdout_capture=holdout_capture,
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
        relevance_decision=recorded,
        holdout=holdout,
        holdout_capture=holdout_capture,
    )


async def _run_jev_relevance(
    *,
    runtime: JevRuntime,
    msg: Any,
    project_key: str | None,
    projects: Mapping[str, ProjectTarget],
    execution: ScoutExecutionContext,
    tracer: TracingLogger,
) -> Result[
    tuple[RelevancePhaseOutput, RecordedRelevanceDecision, list[int]],
    LLMError | ParseError,
]:
    """Decide one post with JEV, or fail retryably without evaluating it.

    An unrouted post has no project and therefore no defined JEV input. It
    fails here, before any HTTP request is made, rather than being sent with
    an invented project. Configuration validation already refuses
    `KEYWORD_PREFILTER=false` under JEV, so this is the unexpected case.
    """
    target = projects.get(project_key) if project_key else None
    if target is None:
        return Err(
            LLMError(
                operation="relevance",
                message_id=msg.platform_id,
                detail=(
                    "JEV requires a routed project; post reached the relevance "
                    f"phase with project_key={project_key!r}"
                ),
            )
        )

    decided = await run_jev_relevance_phase(
        tracer=tracer,
        catalogue=runtime.catalogue,
        endpoint=runtime.endpoint,
        message=msg,
        project=project_from_target(target),
        state=execution.state,
        scan_id=execution.scan_id,
        post_id=execution.post_id,
        snapshot_phase_id=execution.relevance.snapshot_phase_id,
        transport=runtime.transport,
    )
    if isinstance(decided, Err):
        return decided

    result = decided.value
    recorded = RecordedRelevanceDecision(
        classifier="jev",
        model=result.execution.model,
        action=result.decision.action,
        reason=result.decision.reason,
        phase_run_id=result.execution.phase_run_id,
        catalogue_id=result.catalogue_id,
        catalogue_version=result.catalogue_version,
        router_version=ROUTER_VERSION,
        answers=result.answers,
        decision=result.decision.as_record(),
    )
    return Ok((result.execution.parsed, recorded, [result.execution.phase_run_id]))


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
    relevance_decision: RecordedRelevanceDecision | None = None,
    holdout: HoldoutDraw | None = None,
    holdout_capture: HoldoutCaptureRef | None = None,
) -> Result[ReplyCandidate, LLMError | ParseError]:
    """Shared reply-draft and critic implementation for model and human relevance.

    `holdout` is only ever an unselected draw here — a selected one is
    terminal at the relevance boundary and never reaches drafting. It and its
    capture are carried so the persisted decision records that this post was
    drawn and passed over, and settles the capture it was drawn under.
    """
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
                relevance_decision=relevance_decision,
                holdout=holdout,
                holdout_capture=holdout_capture,
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
                relevance_decision=relevance_decision,
                holdout=holdout,
                holdout_capture=holdout_capture,
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
            relevance_decision=relevance_decision,
            holdout=holdout,
            holdout_capture=holdout_capture,
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
    capturing = TraceIdCapturingTracer(config.tracer)
    phase_config = config.with_(tracer=capturing)

    try:
        agent_result = await run_agent(phase_config, input_text)
    except asyncio.CancelledError:
        trace_id = capturing.captured_trace_id
        if trace_id is not None:
            confirmed_trace_id: str = trace_id

            async def _cleanup() -> None:
                with contextlib.suppress(Exception):
                    await finalize_and_persist_phase_run(
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
                await finalize_and_persist_phase_run(
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
                await finalize_and_persist_phase_run(
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
                await finalize_and_persist_phase_run(
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
        phase_run_id = await finalize_and_persist_phase_run(
            capturing, trace_id, state=state, scan_id=scan_id, post_id=post_id,
            snapshot_phase_id=snapshot_phase_id, phase=phase, model=model,
            status="complete",
        )
    except TraceEvidenceError as e:
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
