"""Executes the Phase 1 corpus through Scout's real relevance/draft/critic
pipeline via one Jig ``sweep`` per set of variants.

``ScoutPipelineAdapter`` is the custom ``LLMClient`` behind one outer
``AgentConfig`` per evaluated variant (design decision #6). Jig's
``run_agent`` calls ``adapter.complete()`` exactly once; the adapter
recovers the corpus case from the ``[SCOUT_EVAL_CASE_ID:...]`` sentinel on
the rendered input, dispatches by case kind, and returns a single
``submit_output`` tool call carrying the typed ``Phase1RunOutput``.

For ``scout_response`` cases the adapter builds a deterministic
``Message``/``RoutedMessage``, runs the real ``build_scout_pipeline`` /
``run_pipeline`` machinery (with per-variant phase ``AgentConfig``s supplied
by an injected ``phase_config_builder``), unwraps the ``ReplyCandidate``,
and classifies it with the production ``classify_outcome`` against the
pinned dossier. For ``dossier_bad_write`` rail cases, a pure evaluator
reports the fixed rail violation set without ever exposing a write tool,
opening a writable repo, or executing the proposed operation.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jig import (
    SUBMIT_OUTPUT_TOOL,
    AgentConfig,
    CompletionParams,
    EvalCase,
    LLMClient,
    LLMResponse,
    NullFeedbackLoop,
    RegressionReport,
    Span,
    SpanKind,
    SweepResult,
    ToolCall,
    ToolRegistry,
    TracingLogger,
    Usage,
    detect_regressions,
    run_pipeline,
    sweep,
)

from scout.config import MODES, Message, ModeConfig, SourceAuthor, SourceParent
from scout.dossiers.resolver import DossierResolution, resolve_dossier
from scout.evals.phase1.grader import ScoutPhase1Grader
from scout.evals.phase1.loader import DEFAULT_CORPUS_DIR, load_phase1_corpus
from scout.evals.phase1.models import (
    DossierProvider,
    Phase1Case,
    Phase1Registry,
    Phase1RunOutput,
    extract_case_id,
    render_case_sentinel,
)
from scout.registry import ProjectTarget
from scout.result import Err
from scout.scanning.agent import (
    PhaseRunIdentity,
    ScoutExecutionContext,
    ScoutPhaseConfigs,
    build_scout_phase_configs,
)
from scout.scanning.pipeline import build_scout_pipeline
from scout.scanning.prefilter import RoutedMessage
from scout.scanning.runner import classify_outcome
from scout.scanning.schemas import (
    CritiquePhaseOutput,
    DeclarativeSegment,
    QuestionSegment,
    RelevancePhaseOutput,
    ReplyCandidate,
    ResourceSegment,
    StructuredDraftOutput,
)
from scout.storage.state import StateManager

# ---------------------------------------------------------------------------
# In-memory tracer infra — Jig's AgentConfig requires a TracingLogger even
# when observability isn't wanted for a read-only eval run. NullFeedbackLoop
# (the FeedbackLoop side of this) now ships from Jig itself; this in-memory
# tracer shim avoids an incidental filesystem dependency for both the CLI
# (which does want tracing — swap in SQLiteTracer there if desired) and the
# hermetic CI test.
# ---------------------------------------------------------------------------


class InMemoryTracer(TracingLogger):  # type: ignore[misc]
    """Minimal span bookkeeping with no I/O — safe for hermetic tests and
    concurrent sweeps alike."""

    def __init__(self) -> None:
        self._spans: dict[str, Span] = {}

    def start_trace(
        self,
        name: str,
        metadata: dict[str, Any] | None = None,
        kind: SpanKind = SpanKind.AGENT_RUN,
    ) -> Span:
        span_id = str(uuid.uuid4())
        span = Span(
            id=span_id,
            trace_id=str(uuid.uuid4()),
            kind=kind,
            name=name,
            started_at=datetime.now(UTC),
            metadata=metadata,
        )
        self._spans[span_id] = span
        return span

    def start_span(
        self,
        parent_id: str,
        kind: SpanKind,
        name: str,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> Span:
        parent = self._spans.get(parent_id)
        trace_id = parent.trace_id if parent else parent_id
        span_id = str(uuid.uuid4())
        span = Span(
            id=span_id,
            trace_id=trace_id,
            kind=kind,
            name=name,
            started_at=datetime.now(UTC),
            parent_id=parent_id,
            input=input,
            metadata=metadata,
        )
        self._spans[span_id] = span
        return span

    def end_span(
        self,
        span_id: str,
        output: Any = None,
        error: str | None = None,
        usage: Usage | None = None,
    ) -> None:
        span = self._spans.get(span_id)
        if span is None:
            return
        span.ended_at = datetime.now(UTC)
        span.output = output
        span.error = error
        span.usage = usage

    async def get_trace(self, trace_id: str) -> list[Span]:
        return [s for s in self._spans.values() if s.trace_id == trace_id]

    async def list_traces(
        self,
        since: datetime | None = None,
        limit: int = 50,
        name: str | None = None,
    ) -> list[Span]:
        spans = list(self._spans.values())
        if name is not None:
            spans = [s for s in spans if s.name == name]
        return spans[:limit]


def _empty_tools() -> ToolRegistry:
    return ToolRegistry([])


@dataclass(frozen=True, slots=True)
class PhaseConfigBundle:
    """Phase configs plus the resolved model identity each config executes."""

    configs: ScoutPhaseConfigs
    relevance_model: str
    reply_draft_model: str
    critic_model: str


# ---------------------------------------------------------------------------
# Dossier resolution — injectable per design decision #14.
# ---------------------------------------------------------------------------


def live_dossier_provider(content_repo: Path) -> DossierProvider:
    """Resolve each case's pinned dossier at the corpus's own SHA."""
    cache: dict[tuple[str, str, str], DossierResolution] = {}

    def _resolve(case: Phase1Case) -> DossierResolution:
        key = (case.project_key, case.dossier_revision, case.dossier_summary_id)
        if key not in cache:
            cache[key] = resolve_dossier(
                content_repo,
                case.dossier_revision,
                case.project_key,
                case.dossier_summary_id,
            )
        return cache[key]

    return _resolve


def fixture_dossier_provider(
    fixture_repo: Path, fixture_revision: str
) -> DossierProvider:
    """Resolve every case's dossier against a materialized fixture repo.

    Ignores the corpus-pinned SHA (``case.dossier_revision``) and resolves
    at ``fixture_revision`` instead — the temp SHA is never treated as real
    corpus provenance; it exists only so hermetic tests can exercise the
    same ``resolve_dossier`` boundary the live path uses.
    """
    cache: dict[tuple[str, str], DossierResolution] = {}

    def _resolve(case: Phase1Case) -> DossierResolution:
        key = (case.project_key, case.dossier_summary_id)
        if key not in cache:
            cache[key] = resolve_dossier(
                fixture_repo,
                fixture_revision,
                case.project_key,
                case.dossier_summary_id,
            )
        return cache[key]

    return _resolve


# ---------------------------------------------------------------------------
# dossier_bad_write rail evaluation — pure, never executes the proposed
# operation and never opens a writable repo (design decision #9).
# ---------------------------------------------------------------------------


def evaluate_bad_write_rail(case: Phase1Case, dossier_provider: DossierProvider) -> list[str]:
    """Deterministically classify a proposed Scout-initiated dossier write.

    Always reports ``automated_write_forbidden`` — Scout has no write path
    to the dossier by design. Additionally reports ``unknown_fact_id`` when
    the proposed payload references a fact ID absent from the pinned
    dossier resolution.
    """
    violations = ["automated_write_forbidden"]
    write = case.proposed_bad_write or {}
    payload = write.get("payload") or {}
    fact_id = payload.get("fact_id")
    if fact_id is not None:
        try:
            resolution = dossier_provider(case)
        except Exception:
            resolution = None
        if resolution is not None:
            known_fact_ids = {f.id for f in resolution.summary.facts}
            if fact_id not in known_fact_ids:
                violations.append("unknown_fact_id")
    return violations


# ---------------------------------------------------------------------------
# scout_response Message construction
# ---------------------------------------------------------------------------


def _build_message(case: Phase1Case) -> Message:
    source = case.source or {}
    parent_raw = source.get("parent")
    parent: SourceParent | None = None
    parent_status = "not_applicable"
    if parent_raw:
        author_raw = parent_raw["author"]
        parent = SourceParent(
            id=str(parent_raw["id"]),
            author=SourceAuthor(id=str(author_raw["id"]), name=str(author_raw["name"])),
            text=str(parent_raw["text"]),
            url=str(parent_raw["url"]),
        )
        parent_status = "resolved"
    return Message(
        platform=str(source.get("platform") or "discord"),
        platform_id=case.id,
        channel_name=str(source.get("channel") or ""),
        channel_id="eval-harness",
        author_name="eval-harness-author",
        author_id="eval-harness-author",
        content=str(source.get("content") or ""),
        created_at=datetime.now(UTC),
        parent=parent,
        parent_lookup_status=parent_status,
    )


# ---------------------------------------------------------------------------
# The custom LLMClient behind the outer per-variant AgentConfig.
# ---------------------------------------------------------------------------


@dataclass
class ScoutPipelineAdapter(LLMClient):  # type: ignore[misc]
    registry: Phase1Registry
    dossier_provider: DossierProvider
    phase_config_builder: Callable[[Phase1Case], PhaseConfigBundle]
    tracer: TracingLogger

    async def complete(self, params: CompletionParams) -> LLMResponse:
        rendered_input = params.messages[-1].content if params.messages else ""
        case_id = extract_case_id(rendered_input)
        output = await self._evaluate(case_id)
        call = ToolCall(
            id=str(uuid.uuid4()),
            name=SUBMIT_OUTPUT_TOOL,
            arguments=output.model_dump(),
        )
        return LLMResponse(
            content="",
            tool_calls=[call],
            usage=Usage(input_tokens=0, output_tokens=0, cost=0.0),
            latency_ms=0.0,
            model="scout-pipeline-adapter",
        )

    async def _evaluate(self, case_id: str | None) -> Phase1RunOutput:
        if case_id is None or case_id not in self.registry:
            return Phase1RunOutput(
                case_id=case_id or "unknown",
                case_kind="scout_response",
                terminal_reason="case id not recoverable from rendered input sentinel",
            )
        case = self.registry[case_id]
        if case.case_kind == "dossier_bad_write":
            violations = evaluate_bad_write_rail(case, self.dossier_provider)
            return Phase1RunOutput(
                case_id=case.id,
                case_kind="dossier_bad_write",
                rail_violations=violations,
            )
        return await self._evaluate_scout_response(case)

    async def _evaluate_scout_response(self, case: Phase1Case) -> Phase1RunOutput:
        msg = _build_message(case)
        routed = RoutedMessage(message=msg, keyword_route=None)
        phase_bundle = self.phase_config_builder(case)
        phase_configs = phase_bundle.configs
        pipeline = build_scout_pipeline(self.tracer)
        try:
            resolution = self.dossier_provider(case)
        except Exception as exc:
            return Phase1RunOutput(
                case_id=case.id,
                case_kind="scout_response",
                terminal_status="gate_blocked",
                terminal_reason=str(exc),
                gate_violation_codes=["missing_dossier"],
            )
        state = StateManager(db_path=":memory:")
        try:
            scan_id = state.start_scan()
            post_id = state.save_post(msg, scan_id)
            snapshot = state.record_feedback_snapshot(scan_id, mode="shadow")
            snapshot_phases = {phase.phase: phase.snapshot_phase_id for phase in snapshot.phases}
            execution_context = ScoutExecutionContext(
                state=state,
                scan_id=scan_id,
                post_id=post_id,
                relevance=PhaseRunIdentity(
                    snapshot_phase_id=snapshot_phases["relevance"],
                    model=phase_bundle.relevance_model,
                ),
                reply_draft=PhaseRunIdentity(
                    snapshot_phase_id=snapshot_phases["reply_draft"],
                    model=phase_bundle.reply_draft_model,
                ),
                critic=PhaseRunIdentity(
                    snapshot_phase_id=snapshot_phases["critic"],
                    model=phase_bundle.critic_model,
                ),
            )
            pipeline_result = await run_pipeline(
                pipeline,
                routed,
                context={
                    "phase_configs": phase_configs,
                    "dossier_summaries": {case.project_key: resolution.summary},
                    "execution_context": execution_context,
                },
            )
        finally:
            state.close()
        candidate_result = pipeline_result.output

        if isinstance(candidate_result, Err):
            return Phase1RunOutput(
                case_id=case.id,
                case_kind="scout_response",
                terminal_status="drafting_failed",
                terminal_reason=str(candidate_result.error),
            )
        candidate: ReplyCandidate = candidate_result.value

        decision = classify_outcome(candidate, msg, {case.project_key: resolution.summary})
        structured_draft = (
            decision.structured_draft.model_dump() if decision.structured_draft else None
        )
        return Phase1RunOutput(
            case_id=case.id,
            case_kind="scout_response",
            terminal_status=decision.status,
            source_relevant=decision.evaluation.relevant,
            project_key=decision.project_key,
            posture=decision.posture,
            structured_draft=structured_draft,
            validated_text=decision.validated_text,
            gate_violation_codes=[v.reason_code for v in decision.gate_violations],
            terminal_reason=decision.terminal_reason,
        )


# ---------------------------------------------------------------------------
# Deterministic scripted phase configs — hermetic CI variants (design
# decisions #18/#19). Never replays observed_draft (rejected approach);
# claims/segments are built from the pinned dossier's own safe_phrasings.
# ---------------------------------------------------------------------------


class _ScriptedLLMClient(LLMClient):  # type: ignore[misc]
    """Returns one pre-computed ``submit_output`` tool call, unconditionally."""

    def __init__(self, output: RelevancePhaseOutput | StructuredDraftOutput | CritiquePhaseOutput):
        self._output = output

    async def complete(self, params: CompletionParams) -> LLMResponse:
        call = ToolCall(
            id=str(uuid.uuid4()),
            name=SUBMIT_OUTPUT_TOOL,
            arguments=self._output.model_dump(),
        )
        return LLMResponse(
            content="",
            tool_calls=[call],
            usage=Usage(input_tokens=0, output_tokens=0, cost=0.0),
            latency_ms=0.0,
            model="scripted",
        )


def _scripted_draft(
    case: Phase1Case,
    resolution: DossierResolution | None,
) -> StructuredDraftOutput:
    # Uses the *effective* expected posture (grade.posture_should_have_been
    # when the stored grade corrected it, else expected_posture) so the
    # scripted baseline variant always drafts toward a 1.0 grade — matching
    # what ScoutPhase1Grader._outcome_semantics checks against.
    posture = case.effective_expected_posture or "answer"
    if posture == "abstain":
        return StructuredDraftOutput(
            posture="abstain",
            abstain_reason="scripted deterministic abstain for hermetic CI",
        )

    facts_by_id = {f.id: f for f in (resolution.summary.facts if resolution else [])}
    segments: list[Any] = []
    claims: list[str] = []
    for fact_id in case.required_fact_ids:
        fact = facts_by_id.get(fact_id)
        phrasing = fact.safe_phrasings[0] if fact is not None else "scripted fallback claim"
        segments.append(DeclarativeSegment(type="declarative", fact_id=fact_id, text=phrasing))
        claims.append(phrasing)

    resources_used = list(case.required_resource_ids)
    for resource_id in resources_used:
        segments.append(ResourceSegment(type="resource", resource_id=resource_id))

    if posture == "ask":
        segments.append(QuestionSegment(type="question", text="What's your take on this?"))

    return StructuredDraftOutput(
        posture=posture,  # type: ignore[arg-type]
        segments=segments,
        claims=claims,
        resources_used=resources_used,
    )


def make_scripted_phase_config_builder(
    dossier_provider: DossierProvider,
    tracer: TracingLogger,
    *,
    relevance_override: Mapping[str, bool] | None = None,
) -> Callable[[Phase1Case], PhaseConfigBundle]:
    """Build a phase_config_builder whose inner LLMs are fully scripted.

    ``relevance_override`` lets a seeded-regression CI variant flip a
    specific case's relevance outcome (design decision #18) without
    touching the corpus.
    """
    overrides = dict(relevance_override or {})

    def _builder(case: Phase1Case) -> PhaseConfigBundle:
        relevant = overrides.get(case.id, bool(case.expected_source_relevant))
        resolution = dossier_provider(case) if relevant else None

        relevance_output = RelevancePhaseOutput(
            relevant=relevant,
            score=0.92 if relevant else 0.2,
            reason="scripted deterministic relevance for hermetic CI",
            relevant_to=[case.project_key] if relevant else [],
        )
        draft_output = _scripted_draft(case, resolution)
        critic_output = CritiquePhaseOutput(verdict="approve", feedback="scripted approve")

        common: dict[str, Any] = {
            "feedback": NullFeedbackLoop(),
            "tracer": tracer,
            "tools": _empty_tools(),
            "max_tool_calls": 1,
            "max_llm_calls": 2,
            "max_parse_retries": 1,
            "include_memory_in_prompt": False,
            "include_feedback_in_prompt": False,
        }
        configs = ScoutPhaseConfigs(
            relevance=AgentConfig[RelevancePhaseOutput](
                name=f"scripted-relevance-{case.id}",
                description="Scripted deterministic relevance phase.",
                system_prompt="scripted",
                llm=_ScriptedLLMClient(relevance_output),
                output_schema=RelevancePhaseOutput,
                **common,
            ),
            reply_draft=AgentConfig[StructuredDraftOutput](
                name=f"scripted-draft-{case.id}",
                description="Scripted deterministic draft phase.",
                system_prompt="scripted",
                llm=_ScriptedLLMClient(draft_output),
                output_schema=StructuredDraftOutput,
                **common,
            ),
            critic=AgentConfig[CritiquePhaseOutput](
                name=f"scripted-critic-{case.id}",
                description="Scripted deterministic critic phase.",
                system_prompt="scripted",
                llm=_ScriptedLLMClient(critic_output),
                output_schema=CritiquePhaseOutput,
                **common,
            ),
        )
        return PhaseConfigBundle(
            configs=configs,
            relevance_model="scripted",
            reply_draft_model="scripted",
            critic_model="scripted",
        )

    return _builder


def make_live_phase_config_builder(
    *,
    relevance_model: str,
    reply_draft_model: str,
    critic_model: str,
    mode_cfg: ModeConfig,
    templates: Mapping[str, str],
    tracer: TracingLogger,
) -> Callable[[Phase1Case], PhaseConfigBundle]:
    """Build a phase_config_builder that dispatches to real models via Jig."""

    def _builder(case: Phase1Case) -> PhaseConfigBundle:
        projects = {
            case.project_key: ProjectTarget(
                key=case.project_key,
                name=case.project_key,
                description="",
                link="",
                dossier_summary_id=case.dossier_summary_id,
            )
        }
        return PhaseConfigBundle(
            configs=build_scout_phase_configs(
                relevance_model=relevance_model,
                reply_draft_model=reply_draft_model,
                critic_model=critic_model,
                mode_cfg=mode_cfg,
                projects=projects,
                templates=templates,
                tracer=tracer,
                feedback=NullFeedbackLoop(),
            ),
            relevance_model=relevance_model,
            reply_draft_model=reply_draft_model,
            critic_model=critic_model,
        )

    return _builder


# ---------------------------------------------------------------------------
# EvalCase rendering (design decision #2) + outer variant AgentConfig.
# ---------------------------------------------------------------------------


def _render_scout_response_input(case: Phase1Case) -> str:
    source = case.source or {}
    lines = [
        render_case_sentinel(case.id),
        f"platform: {source.get('platform', '')}",
        f"channel: {source.get('channel', '')}",
        f"content: {source.get('content', '')}",
    ]
    parent = source.get("parent")
    if parent:
        lines.append(f"parent_author: {parent.get('author', {}).get('name', '')}")
        lines.append(f"parent_text: {parent.get('text', '')}")
    return "\n".join(lines)


def _render_rail_input(case: Phase1Case) -> str:
    # Deliberately excludes proposed_bad_write and expected_rail_violations
    # — those are hidden expected/registry-only data (design decision #1/#2)
    # the adapter looks up by ID rather than reads from rendered input.
    return "\n".join(
        [
            render_case_sentinel(case.id),
            f"case_kind: {case.case_kind}",
            f"project_key: {case.project_key}",
        ]
    )


def render_eval_cases(registry: Phase1Registry) -> list[EvalCase]:
    """Render every registry case into a Jig ``EvalCase``.

    Only the sentinel + non-sensitive source/context reach ``input``
    (design decision #2) — expected outcomes live in ``expected``/
    ``metadata``/``context``, which Jig's sweep never forwards to
    ``run_agent``'s input, only to callers that already hold the case.
    """
    cases: list[EvalCase] = []
    for case in registry:
        if case.case_kind == "scout_response":
            rendered_input = _render_scout_response_input(case)
            expected = {
                "expected_source_relevant": case.expected_source_relevant,
                "expected_posture": case.expected_posture,
                "expected_terminal_status": case.expected_terminal_status,
            }
        else:
            rendered_input = _render_rail_input(case)
            expected = {"expected_rail_violations": list(case.expected_rail_violations)}
        cases.append(
            EvalCase(
                input=rendered_input,
                expected=json.dumps(expected, sort_keys=True),
                context={"case_id": case.id, "tags": list(case.tags)},
                metadata={
                    "case_id": case.id,
                    "case_kind": case.case_kind,
                    "tags": list(case.tags),
                },
            )
        )
    return cases


def build_variant_agent_config(
    *,
    name: str,
    registry: Phase1Registry,
    dossier_provider: DossierProvider,
    phase_config_builder: Callable[[Phase1Case], PhaseConfigBundle],
    tracer: TracingLogger,
) -> AgentConfig[Phase1RunOutput]:
    """One outer AgentConfig per evaluated variant (design decision #6)."""
    adapter = ScoutPipelineAdapter(
        registry=registry,
        dossier_provider=dossier_provider,
        phase_config_builder=phase_config_builder,
        tracer=tracer,
    )
    return AgentConfig[Phase1RunOutput](
        name=name,
        description=f"Phase 1 corpus evaluation — variant {name!r}.",
        system_prompt="Scout Phase 1 evaluation harness. This is a scripted adapter, not an LLM.",
        llm=adapter,
        feedback=NullFeedbackLoop(),
        tracer=tracer,
        tools=_empty_tools(),
        store=None,
        retriever=None,
        grader=ScoutPhase1Grader(registry, dossier_provider),
        max_tool_calls=1,
        max_llm_calls=1,
        include_memory_in_prompt=False,
        include_feedback_in_prompt=False,
        output_schema=Phase1RunOutput,
    )


# ---------------------------------------------------------------------------
# Sweep + regression gate orchestration (design decisions #17/#21).
# ---------------------------------------------------------------------------


async def run_phase1_sweep(
    registry: Phase1Registry,
    configs: list[AgentConfig[Phase1RunOutput]],
    *,
    concurrency: int = 1,
    seeds: int = 1,
) -> SweepResult[Phase1RunOutput]:
    cases = render_eval_cases(registry)
    return await sweep(cases, configs, concurrency=concurrency, seeds=seeds)


@dataclass(frozen=True, slots=True)
class VariantModelConfig:
    """One row of a live sweep configs YAML: model IDs + optional mode/template overrides."""

    name: str
    relevance_model: str
    reply_draft_model: str
    critic_model: str
    mode: str = "lead_gen"
    templates: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Phase1SweepConfig:
    baseline: VariantModelConfig
    candidates: tuple[VariantModelConfig, ...]
    concurrency: int = 1
    seeds: int = 1
    threshold: float = 0.05


def _variant_from_dict(name: str, raw: Mapping[str, Any]) -> VariantModelConfig:
    return VariantModelConfig(
        name=name,
        relevance_model=raw["relevance_model"],
        reply_draft_model=raw["reply_draft_model"],
        critic_model=raw["critic_model"],
        mode=raw.get("mode", "lead_gen"),
        templates=raw.get("templates", {}) or {},
    )


def load_sweep_config(path: Path) -> Phase1SweepConfig:
    import yaml

    raw = yaml.safe_load(path.read_text())
    baseline = _variant_from_dict(raw["baseline"]["name"], raw["baseline"])
    candidates = tuple(
        _variant_from_dict(c["name"], c) for c in raw.get("candidates", [])
    )
    if not candidates:
        raise ValueError("sweep config must name at least one candidate variant")
    return Phase1SweepConfig(
        baseline=baseline,
        candidates=candidates,
        concurrency=int(raw.get("concurrency", 1)),
        seeds=int(raw.get("seeds", 1)),
        threshold=float(raw.get("threshold", 0.05)),
    )


def run_live_operator_gate(
    *,
    corpus_dir: Path = DEFAULT_CORPUS_DIR,
    dossier_root: Path,
    configs_path: Path,
    output_path: Path | None = None,
) -> int:
    """Live paid sweep entry point — operator-only (design decision #21).

    Loads the corpus, resolves every pinned dossier at ``dossier_root``,
    runs the full sweep across the configured baseline + candidate model
    variants, prints rollups/alerts, optionally writes the SweepResult
    summary as JSON, and returns 1 on any regression or run error.
    """
    registry = load_phase1_corpus(corpus_dir, dossier_root)
    sweep_config = load_sweep_config(configs_path)
    tracer = InMemoryTracer()
    provider = live_dossier_provider(dossier_root)

    agent_configs: list[AgentConfig[Phase1RunOutput]] = []
    for variant in (sweep_config.baseline, *sweep_config.candidates):
        builder = make_live_phase_config_builder(
            relevance_model=variant.relevance_model,
            reply_draft_model=variant.reply_draft_model,
            critic_model=variant.critic_model,
            mode_cfg=MODES[variant.mode],
            templates=variant.templates,
            tracer=tracer,
        )
        agent_configs.append(
            build_variant_agent_config(
                name=variant.name,
                registry=registry,
                dossier_provider=provider,
                phase_config_builder=builder,
                tracer=tracer,
            )
        )

    result = asyncio.run(
        run_phase1_sweep(
            registry,
            agent_configs,
            concurrency=sweep_config.concurrency,
            seeds=sweep_config.seeds,
        )
    )
    report = detect_regressions(
        result, baseline=sweep_config.baseline.name, threshold=sweep_config.threshold
    )
    rollup = result.rollup()
    _print_report(rollup, report)

    if output_path is not None:
        output_path.write_text(
            json.dumps(
                {
                    "sweep_id": result.sweep_id,
                    "rollup": rollup,
                    "regressions": [
                        {
                            "config_name": a.config_name,
                            "dimension": a.dimension,
                            "baseline_avg": a.baseline_avg,
                            "candidate_avg": a.candidate_avg,
                            "delta": a.delta,
                            "threshold": a.threshold,
                        }
                        for a in report.alerts
                    ],
                },
                indent=2,
                default=str,
            )
        )

    run_errors = [r for r in result.runs if r.result.error is not None]
    if run_errors:
        print(f"{len(run_errors)} run(s) errored — see rollup error_categories.")
    return 1 if report.has_regressions or run_errors else 0


def _print_report(rollup: dict[str, dict[str, Any]], report: RegressionReport) -> None:
    print("Phase 1 sweep rollup:")
    for name, summary in sorted(rollup.items()):
        print(f"  {name}: n={summary['n']} success_rate={summary['success_rate']:.2f}")
        for dim, avg in sorted(summary["avg_scores"].items()):
            print(f"    {dim}: {avg:.3f}")
    if report.has_regressions:
        print("REGRESSIONS DETECTED:")
        for alert in report.alerts:
            print(
                f"  {alert.config_name}/{alert.dimension}: "
                f"{alert.baseline_avg:.3f} -> {alert.candidate_avg:.3f} "
                f"(delta {alert.delta:.3f}, threshold {alert.threshold:.3f})"
            )
    else:
        print("No regressions detected.")


__all__ = [
    "InMemoryTracer",
    "NullFeedbackLoop",
    "Phase1SweepConfig",
    "ScoutPipelineAdapter",
    "VariantModelConfig",
    "build_variant_agent_config",
    "evaluate_bad_write_rail",
    "fixture_dossier_provider",
    "live_dossier_provider",
    "load_sweep_config",
    "make_live_phase_config_builder",
    "make_scripted_phase_config_builder",
    "render_eval_cases",
    "run_live_operator_gate",
    "run_phase1_sweep",
]
