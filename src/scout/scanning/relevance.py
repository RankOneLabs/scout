"""Relevance classifier selection, action semantics, and phase evidence.

Exactly one classifier decides relevance. `llm` is the default and the
rollback path; `jev` routes the phase through Typesafe System One and the
ported router. There is no dual execution and no fallback from one to the
other: a failed JEV attempt is a retryable relevance failure that leaves the
post unevaluated, never a silent handover.

Both classifiers produce the same `RelevancePhaseOutput`, so drafting, the
critic, the verifier gates and grading are unchanged by which one ran.

This module also owns the phase-evidence machinery both classifiers record
through — the trace-id capturing tracer, the AGENT_RUN root verification, and
the durable `evaluation_phase_runs` insert. It lives here rather than in
pipeline.py so the JEV phase can use it without a cycle; pipeline.py imports
it from here for the LLM phases.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from jig import Span, SpanKind, TracingLogger

from scout.config import RELEVANCE_THRESHOLD
from scout.errors import LLMError
from scout.result import Err, Ok, Result
from scout.scanning.jev import JevEndpoint, JevRequest, call_jev
from scout.scanning.jev_catalogue import JevCatalogue
from scout.scanning.jev_router import ROUTER_VERSION, JevAction, RouteDecision, route
from scout.scanning.jev_state import JevProject, build_jev_state, state_input_from_message
from scout.scanning.schemas import RelevancePhaseOutput
from scout.storage.state import StateManager

logger = logging.getLogger("scout.scanning.relevance")

#: Compatibility scores for a JEV decision. The evaluation row requires a
#: number and JEV produces an action, not a calibrated probability. Nothing
#: downstream may read these as confidence: the action is the decision, which
#: is why classify_outcome does not apply RELEVANCE_THRESHOLD to a JEV result.
JEV_RELEVANT_SCORE = 1.0
JEV_IRRELEVANT_SCORE = 0.0


@dataclass(frozen=True, slots=True)
class JevRuntime:
    """Everything one scan needs to run JEV, resolved once at scan start.

    The runner builds this only under `RELEVANCE_CLASSIFIER=jev`, so its
    presence in the pipeline context is the classifier selection: the
    pipeline step never reads configuration and never runs both classifiers.
    `transport` is a seam for tests; production leaves it None.
    """

    catalogue: JevCatalogue
    endpoint: JevEndpoint
    transport: httpx.AsyncBaseTransport | None = None


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


@dataclass(frozen=True, slots=True)
class JevPhaseResult:
    """A JEV relevance phase that decided, with everything worth storing.

    `execution` carries the phase output and its verified evidence;
    `decision` and `answers` are the full router decision and validated
    answer vector, kept so the stored evaluation can be re-explained.
    """

    execution: PhaseExecution[RelevancePhaseOutput]
    decision: RouteDecision
    answers: dict[str, Any]
    catalogue_id: str
    catalogue_version: str


class TraceEvidenceError(RuntimeError):
    """A phase's trace could not be finalized, flushed, read back, and
    verified as an AGENT_RUN root — never raised for a model-call failure,
    only for evidence-persistence itself failing after a real attempt."""


class TraceIdCapturingTracer(TracingLogger):  # type: ignore[misc]
    """Narrowly-scoped, single-call wrapper around a real TracingLogger.

    Captures the trace id Jig's run_agent assigns synchronously inside
    start_trace — before run_agent's first await — so the id survives even
    if run_agent later raises or the awaiting task is cancelled mid-run. A
    fresh instance must be constructed per phase call so concurrent
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


def verify_agent_run_root(spans: list[Span], trace_id: str) -> None:
    root = next((s for s in spans if s.parent_id is None), None)
    if root is None:
        raise TraceEvidenceError(
            f"trace {trace_id!r} did not resolve to any stored root span"
        )
    if root.kind != SpanKind.AGENT_RUN or root.trace_id != trace_id:
        raise TraceEvidenceError(
            f"trace {trace_id!r} root span is not an AGENT_RUN root"
        )


async def finalize_and_persist_phase_run(
    tracer: TraceIdCapturingTracer,
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

    Raises TraceEvidenceError — never inserting a row — if flush,
    read-back, or verification fails: an absent phase-run is more truthful
    than one pointing at unavailable or mistyped evidence. Opens no
    transaction until after this verification, and the insert itself is a
    single short StateManager transaction.
    """
    try:
        await tracer.flush()
        spans = await tracer.get_trace(trace_id)
    except Exception as e:
        raise TraceEvidenceError(f"trace flush/read-back failed: {e}") from e
    verify_agent_run_root(spans, trace_id)
    return state.insert_phase_run(
        scan_id=scan_id,
        post_id=post_id,
        snapshot_phase_id=snapshot_phase_id,
        phase=phase,
        trace_id=trace_id,
        model=model,
        status=status,
    )


# ---------------------------------------------------------------------------
# Action semantics
# ---------------------------------------------------------------------------


def jev_relevance_output(
    decision: RouteDecision, project_key: str
) -> RelevancePhaseOutput:
    """Adapt one routing decision into the shared relevance phase output.

    `respond` and `review` are both relevant and both continue to drafting;
    the distinction survives on the recorded action rather than here. `drop`
    is negative. The score is a compatibility value, not confidence.
    """
    relevant = decision.action in ("respond", "review")
    return RelevancePhaseOutput(
        relevant=relevant,
        score=JEV_RELEVANT_SCORE if relevant else JEV_IRRELEVANT_SCORE,
        reason=decision.reason,
        relevant_to=[project_key] if relevant else [],
    )


def llm_relevance_action(output: RelevancePhaseOutput) -> JevAction:
    """The action an LLM relevance decision records.

    `respond` when the LLM says relevant and its score is at or above the
    threshold in force at decision time, otherwise `drop`. The LLM never
    records `review`. Recording the action at decision time is what lets an
    ungraded release act on it later without recomputing a threshold against
    a value that may since have changed.
    """
    if output.relevant and output.score >= RELEVANCE_THRESHOLD:
        return "respond"
    return "drop"


# ---------------------------------------------------------------------------
# The JEV relevance phase
# ---------------------------------------------------------------------------


async def run_jev_relevance_phase(
    *,
    tracer: TracingLogger,
    catalogue: JevCatalogue,
    endpoint: JevEndpoint,
    message: Any,
    project: JevProject,
    state: StateManager,
    scan_id: int,
    post_id: int,
    snapshot_phase_id: int,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Result[JevPhaseResult, LLMError]:
    """Call JEV for one post, route the answers, and record the evidence.

    A direct HTTP classifier runs no jig agent, so it opens the AGENT_RUN
    root itself and wraps the request and the routing in it. The same flush,
    read-back and root verification then runs unchanged, and no evaluation is
    claimed for work whose root cannot be verified.

    A failed attempt still records its failed phase run, without evaluating
    the post: the failure stays visible and the post stays retryable.
    """
    capturing = TraceIdCapturingTracer(tracer)
    root = capturing.start_trace(
        "scout_relevance_jev",
        {
            "classifier": "jev",
            "model": endpoint.model,
            "catalogue_id": catalogue.id,
            "catalogue_version": catalogue.version,
            "router_version": ROUTER_VERSION,
            "project_key": project.key,
        },
        kind=SpanKind.AGENT_RUN,
    )
    trace_id = capturing.captured_trace_id or root.trace_id

    async def _persist(status: str) -> int | None:
        try:
            return await finalize_and_persist_phase_run(
                capturing,
                trace_id,
                state=state,
                scan_id=scan_id,
                post_id=post_id,
                snapshot_phase_id=snapshot_phase_id,
                phase="relevance",
                model=endpoint.model,
                status=status,
            )
        except TraceEvidenceError:
            logger.error(
                "JEV relevance evidence persistence failed for post %s", post_id,
                exc_info=True,
            )
            return None

    def _failure(detail: str) -> LLMError:
        return LLMError(
            operation="relevance",
            message_id=getattr(message, "platform_id", str(post_id)),
            detail=detail,
        )

    request = JevRequest(
        endpoint=endpoint,
        state=build_jev_state(state_input_from_message(message), project),
        questions=catalogue.questions,
    )

    started = time.monotonic()
    answered = await call_jev(request, transport=transport)
    if isinstance(answered, Err):
        capturing.end_span(root.id, error=answered.error.detail)
        await _persist("error")
        return Err(_failure(f"{answered.error.operation}: {answered.error.detail}"))

    routed = route(answered.value.probabilities)
    if isinstance(routed, Err):
        capturing.end_span(root.id, error=routed.error.detail)
        await _persist("error")
        return Err(_failure(f"{routed.error.operation}: {routed.error.detail}"))

    decision = routed.value
    output = jev_relevance_output(decision, project.key)
    capturing.end_span(
        root.id,
        output={
            "action": decision.action,
            "line": decision.line,
            "exclusion": decision.exclusion,
            "latency_ms": int((time.monotonic() - started) * 1000),
        },
    )

    phase_run_id = await _persist("complete")
    if phase_run_id is None:
        return Err(
            _failure(
                "phase evidence persistence failed: trace did not resolve to a "
                "verified AGENT_RUN root"
            )
        )

    return Ok(
        JevPhaseResult(
            execution=PhaseExecution(
                parsed=output,
                trace_id=trace_id,
                phase_run_id=phase_run_id,
                phase="relevance",
                model=endpoint.model,
            ),
            decision=decision,
            answers=dict(answered.value.raw),
            catalogue_id=catalogue.id,
            catalogue_version=catalogue.version,
        )
    )
