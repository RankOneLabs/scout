"""CLI-only offline replay: single-phase candidate execution and comparison.

Replays exactly one recorded Scout phase (relevance, reply_draft, or
critic) against a trusted baseline resolved from a stored, complete
``evaluation_phase_runs`` row, optionally varying the model and/or system
prompt, and persists an immutable comparison (Jig's native ``TraceDiff``
plus Scout's own canonical structured-field ``domain_diff``) once the
candidate's AGENT_RUN trace has been generated, flushed, and verified.

A ``reply_draft`` replay additionally requires a resolved correction
oracle — a unique grade whose recorded correction and pinned dossier can
both be resolved (see ``resolve_reply_correction_oracle``) — before any
spend; the candidate then runs with Scout's ``ReplyCorrectionGrader``
attached so its live output is scored against that correction, alongside
an independently computed historical baseline distance, into an immutable
``trace_comparisons.score_evidence`` row.

Every experiment attempt is a child of a versioned ``experiment_runs``
parent: ``execute_replay`` opens a fresh single-attempt run per call (the
CLI's one-baseline-per-invocation shape), while ``state_manager``'s
``insert_experiment_attempt`` also supports batching several baseline
cases under one run and retrying a case as a new linked attempt.

Everything here is read-only until ``execute_replay`` reaches its first
database write (``StateManager.create_experiment_run``); ``preview_replay``
never inserts a row or calls a model.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema
import yaml
from jig import (
    AgentConfig,
    FeedbackLoop,
    LLMClient,
    Score,
    Span,
    SpanKind,
    ToolRegistry,
    TraceDiff,
    TracingLogger,
    from_model,
)
from jig import replay as jig_replay
from jig import trace_diff as jig_trace_diff
from jig.core.runner import (
    ROOT_OUTPUT_BYTE_LENGTH_KEY,
    ROOT_OUTPUT_COMPLETE_KEY,
    ROOT_OUTPUT_KIND_KEY,
    ROOT_OUTPUT_SHA256_KEY,
)
from pydantic import ValidationError

import scout.config as _config
from scout.dossiers.resolver import DossierResolutionError, DossierSummary, resolve_dossier
from scout.grading.correction import (
    NORMALIZED_EDIT_DISTANCE_GRADER_VERSION,
    ReplyCorrectionGrader,
    normalized_edit_distance,
)
from scout.replay.pricing import (
    PriceEstimate,
    PricingCatalog,
    TokenUsage,
    aggregate_baseline_usage,
    load_pricing_catalog,
    price_pair,
)
from scout.resources import runtime_resource
from scout.scanning.schemas import CritiquePhaseOutput, RelevancePhaseOutput, StructuredDraftOutput
from scout.storage.state import ExperimentCASError, StateManager
from scout.verifier import DRAFT_TEXT_ASSEMBLER_VERSION, assemble_draft_text

# The exact reviewed Jig commit this build is pinned to (pyproject.toml /
# uv.lock's tool.uv.sources.jig.rev) — persisted verbatim on every
# trace_comparisons row so stored comparison evidence identifies the exact
# upstream semantics that produced it without a runtime git checkout.
# tests/test_jig_contract.py contract-tests this constant against the pin.
JIG_REVISION = "4fae89bb04768d57be6db4cd2bdef859d1e17322"

# v2: candidate-only (experiment_runs.candidate_config). Per-baseline
# provenance moved out to baseline_evidence — see BASELINE_EVIDENCE_VERSION.
CANDIDATE_CONFIG_VERSION = 2

# v2: per-baseline-case provenance (evaluation_experiments.baseline_evidence).
BASELINE_EVIDENCE_VERSION = 2

# v2 additionally pins the complete controlled worker configuration.
PLAN_SCHEMA_VERSION = 2

# replay-sweep v1 (contracts/replay-sweep.v1.schema.json).
SWEEP_SCHEMA_VERSION = 1
SWEEP_SCHEMA_PATH = runtime_resource("contracts", "replay-sweep.v1.schema.json")

# v4: one batch/sweep experiment_runs parent's shared override policy
# (experiment_runs.candidate_config) -- distinct from CANDIDATE_CONFIG_VERSION
# (v2), which is the single-replay CLI's fully-resolved one-case shape. A
# batch/sweep parent covers many baseline cases that may each have their own
# recorded system prompt, so only the *override* (what changed, if anything)
# belongs on the shared parent; each case's own fully resolved candidate
# identity is recorded on its own child row (see BATCH_CASE_EVIDENCE_VERSION).
# v4 adds the authorized plan_sha256, the full resolved population
# (phase_run_ids/dropped_duplicate_phase_run_ids, identical across every
# variant of one batch/sweep), and this variant's own skipped_pairs
# (classification + reason for every non-'scored' pair) -- a skipped pair
# never produces its own evaluation_experiments row, so this is the only
# durable record of it; without it, a report built after the fact could
# not reconstruct correction coverage or exclusions for excluded cases.
BATCH_CANDIDATE_CONFIG_VERSION = 4

# v1: one batch/sweep case's fully-resolved candidate identity, pinned
# correction oracle, and repriced spend estimate (evaluation_experiments.
# baseline_evidence for a batch/sweep attempt).
BATCH_CASE_EVIDENCE_VERSION = 1

DEFAULT_BATCH_VARIANT_NAME = "default"


class ReplayError(Exception):
    """Base class for every offline-replay domain error."""


class BaselineResolutionError(ReplayError):
    """The requested phase_run_id does not resolve to a trusted, complete
    baseline with a verifiable AGENT_RUN trace."""


class CorrectionOracleResolutionError(ReplayError):
    """A reply_draft phase_run does not resolve to a unique, valid
    correction oracle (grade, correction, and pinned dossier) — replay is
    refused before any spend."""


class ModelResolutionError(ReplayError):
    """--model (or the baseline's recorded model) failed Scout's trusted
    model resolver and routing validation."""


class NoOpReplayError(ReplayError):
    """The candidate configuration is identical to the baseline: same
    routed model and same system-prompt hash."""


class CandidateExecutionError(ReplayError):
    """The authorized candidate phase execution itself failed."""


class CandidateTraceEvidenceError(ReplayError):
    """The candidate (or baseline) trace could not be flushed, read back,
    and verified as an AGENT_RUN root."""


class CorrectionEvidenceIntegrityError(ReplayError):
    """The pinned correction revision or candidate grader score could not
    be re-verified at scoring time."""


class ComparisonConstructionError(ReplayError):
    """trace_diff, domain_diff, or score_evidence could not be constructed
    or serialized."""


class SelectorResolutionError(ReplayError):
    """A batch selector's arguments, or its resolved population, are
    invalid -- an unresolvable id, a phase mismatch, a malformed --from/--to
    bound, or an empty population."""


class SweepValidationError(ReplayError):
    """A sweep document failed replay-sweep v1 structural or semantic
    validation -- schema violation, duplicate variant identity, or an
    unroutable model. Raised before any write."""


class PlanAuthorizationError(ReplayError):
    """--authorize-plan-sha256 does not match the freshly recomputed
    canonical plan hash -- the population, configuration, pricing catalog,
    or skip policy changed since preview. Paid execution is refused before
    any row insertion or provider call."""


class NonExecutablePopulationError(ReplayError):
    """The plan contains at least one non-'scored' pair (unscored, no_op,
    or unpriceable) that the skip policy does not explicitly exclude.
    Execution refuses by default; pass the matching --skip-* flag to
    exclude it."""


class RetryResolutionError(ReplayError):
    """A batch retry request does not resolve to a valid, non-empty set of
    failed, latest-attempt cases under the given experiment_run_id."""


# Fixed, closed phase map: the sole source of output schema and phase
# runner configuration for replay. Never resolved from CLI input, stored
# trace text, or any dynamic import — this is what keeps replay pinned to
# Scout's own approved phase contracts. Values mirror the shared `common`
# dict in scout_agent.build_scout_phase_configs.
@dataclass(frozen=True, slots=True)
class PhaseReplayConfig:
    output_schema: type[Any]
    max_tool_calls: int
    max_llm_calls: int
    max_parse_retries: int


PHASE_REPLAY_CONFIGS: dict[str, PhaseReplayConfig] = {
    "relevance": PhaseReplayConfig(
        output_schema=RelevancePhaseOutput, max_tool_calls=1, max_llm_calls=4, max_parse_retries=2,
    ),
    "reply_draft": PhaseReplayConfig(
        output_schema=StructuredDraftOutput, max_tool_calls=1, max_llm_calls=4, max_parse_retries=2,
    ),
    "critic": PhaseReplayConfig(
        output_schema=CritiquePhaseOutput, max_tool_calls=1, max_llm_calls=4, max_parse_retries=2,
    ),
}


_STAGE_MESSAGES: dict[str, str] = {
    "candidate_execution": "Candidate phase execution failed before producing a valid result.",
    "candidate_trace_evidence": (
        "The candidate trace could not be verified as a stored AGENT_RUN root."
    ),
    "candidate_grading": (
        "The candidate's reply-correction score could not be verified or was not produced."
    ),
    "diff_construction": "The trace or domain comparison could not be constructed or serialized.",
}


@dataclass(frozen=True, slots=True)
class BaselineRecord:
    """Trusted baseline resolved from one complete evaluation_phase_runs
    row and its verified AGENT_RUN trace. Every field here is read
    directly from a durable, stored authority — never rebuilt from
    current mode configuration or re-derived from live application state."""

    phase_run_id: int
    phase: str
    baseline_trace_id: str
    baseline_model: str
    baseline_system_prompt: str
    recorded_input: str
    root_span: Span
    snapshot_phase_id: int
    snapshot_id: int
    feedback_policy_version: str


@dataclass(frozen=True, slots=True)
class ReplyCorrectionOracle:
    """The unique, valid reply_draft correction oracle for one baseline
    case: the human correction a candidate replay's grading is scored
    against, and the exact pinned dossier it was authored against.

    Every field is read directly from durable, stored authority (the
    current grades/reply_draft_revisions/draft_comments chain and a
    resolved dossier revision) — resolved once, before any spend, and
    reused for both the live candidate grader and the independently
    computed historical baseline distance.
    """

    grade_id: int
    reply_revision_id: int
    correction_text: str
    correction_sha256: str
    project_key: str
    dossier_summary_id: str
    dossier_revision: str
    dossier: DossierSummary


@dataclass(frozen=True, slots=True)
class ReplayWorkerConfiguration:
    """Exact controlled replay worker settings, captured before an attempt can spend."""

    phase: str
    model: str
    system_prompt_sha256: str
    output_schema_sha256: str
    max_tool_calls: int
    max_llm_calls: int
    max_parse_retries: int
    jig_revision: str
    grader_version: str | None
    assembler_version: str
    tools: tuple[str, ...]
    include_memory_in_prompt: bool
    include_feedback_in_prompt: bool


@dataclass(frozen=True, slots=True)
class CandidateReplayPlan:
    """The frozen candidate description decided before any model call."""

    phase: str
    candidate_model: str
    candidate_system_prompt: str
    candidate_config_json: str
    baseline_prompt_sha256: str
    candidate_prompt_sha256: str
    baseline_prompt_reused: bool
    recorded_input_sha256: str
    grader_attached: bool
    is_no_op: bool


@dataclass(frozen=True, slots=True)
class ReplayPreview:
    """Everything --execute-paid-replay's dry-run mode prints — built with
    zero database writes and zero model calls."""

    phase: str
    baseline_model: str
    candidate_model: str
    baseline_prompt_sha256: str
    candidate_prompt_sha256: str
    baseline_prompt_reused: bool
    recorded_input_sha256: str
    recorded_input_reused: bool
    snapshot_phase_id: int
    snapshot_id: int
    feedback_policy_version: str
    max_llm_calls: int
    is_no_op: bool
    grader_attached: bool


@dataclass(frozen=True, slots=True)
class ExperimentOutcome:
    """The durable result of one completed execute_replay call."""

    experiment_id: int
    experiment_run_id: int
    candidate_trace_id: str
    candidate_llm_call_count: int
    candidate_cost: float | None


def _sha256_utf8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    )


def _verify_agent_run_root(spans: list[Span], trace_id: str, error_cls: type[ReplayError]) -> Span:
    """Return the verified AGENT_RUN root span for `trace_id`, or raise
    `error_cls` — the single trust check every trace (baseline or
    candidate) must pass before its evidence is used for anything."""
    root = next((s for s in spans if s.parent_id is None), None)
    if root is None:
        raise error_cls(f"trace {trace_id!r} did not resolve to any stored root span")
    if root.kind != SpanKind.AGENT_RUN or root.trace_id != trace_id:
        raise error_cls(f"trace {trace_id!r} root span is not a verified AGENT_RUN root")
    return root


def _has_complete_cost_evidence(spans: list[Span]) -> bool:
    """True only when every LLM call exposes an explicit cost.

    Candidate summary cost is persisted only with complete per-call
    evidence. Jig's native TraceDiff remains byte-for-byte intact; the web
    read model independently marks its numeric delta unavailable when
    either trace lacks the corresponding evidence.
    """
    llm_calls = [span for span in spans if span.kind == SpanKind.LLM_CALL]
    return bool(llm_calls) and all(
        span.usage is not None and span.usage.cost is not None for span in llm_calls
    )


def _model_identity_matches(recorded_model_id: object, configured_model: object) -> bool:
    """Compare Scout's routed model id with Jig's adapter-level trace id.

    OpenRouter routing is configured as ``openrouter/<vendor>/<model>`` in
    Scout, while Jig deliberately records the provider-native
    ``<vendor>/<model>`` id on the AGENT_RUN.  Accept only that documented
    prefix translation (or an exact match); all other differences remain
    trust-boundary failures.
    """
    if not isinstance(recorded_model_id, str) or not isinstance(configured_model, str):
        return False
    if recorded_model_id == configured_model:
        return True
    prefix = "openrouter/"
    return (
        configured_model.startswith(prefix)
        and configured_model[len(prefix) :] == recorded_model_id
    )


async def resolve_baseline(
    state: StateManager, tracer: TracingLogger, phase_run_id: int
) -> BaselineRecord:
    """Resolve a trusted baseline from a stored, complete
    evaluation_phase_runs row and its verified AGENT_RUN trace.

    Read-only: only queries state (wrapped in a mechanically read-only
    snapshot) and the trace store. Raises BaselineResolutionError for
    every way the requested phase_run_id is not a trustworthy baseline —
    missing, non-complete, an unknown phase, a trace that doesn't verify
    as AGENT_RUN, a missing/callable system_prompt, a model_id that
    disagrees with the stored phase_run row, or missing recorded input.
    """
    if phase_run_id <= 0:
        raise BaselineResolutionError("phase_run_id must be a positive integer")

    with state.db.read_transaction():
        phase_run = state.get_phase_run(phase_run_id)
        if phase_run is None:
            raise BaselineResolutionError(
                f"no evaluation_phase_runs row with id={phase_run_id}"
            )
        if phase_run["status"] != "complete":
            raise BaselineResolutionError(
                f"evaluation_phase_runs {phase_run_id} has status "
                f"{phase_run['status']!r}, not 'complete'"
            )
        phase = phase_run["phase"]
        if phase not in PHASE_REPLAY_CONFIGS:
            raise BaselineResolutionError(f"unknown replayable phase: {phase!r}")

        snapshot_row = state.conn.execute(
            "SELECT fsp.snapshot_id AS snapshot_id, fs.policy_version AS policy_version "
            "FROM feedback_snapshot_phases fsp "
            "JOIN feedback_snapshots fs ON fs.id = fsp.snapshot_id "
            "WHERE fsp.id = ?",
            (phase_run["snapshot_phase_id"],),
        ).fetchone()
        if snapshot_row is None:
            raise BaselineResolutionError(
                f"no feedback_snapshot_phases row for snapshot_phase_id="
                f"{phase_run['snapshot_phase_id']}"
            )

    baseline_trace_id = phase_run["trace_id"]
    spans = await tracer.get_trace(baseline_trace_id)
    root = _verify_agent_run_root(spans, baseline_trace_id, BaselineResolutionError)

    metadata = root.metadata or {}
    snapshot = metadata.get("config")
    if not isinstance(snapshot, dict):
        raise BaselineResolutionError(
            f"trace {baseline_trace_id!r} has no recorded config snapshot"
        )
    if snapshot.get("system_prompt_is_callable"):
        raise BaselineResolutionError(
            f"trace {baseline_trace_id!r} recorded a callable system_prompt, "
            "which cannot be replayed"
        )
    system_prompt = snapshot.get("system_prompt")
    if not isinstance(system_prompt, str):
        raise BaselineResolutionError(
            f"trace {baseline_trace_id!r} has no recorded literal system_prompt"
        )
    recorded_model_id = snapshot.get("model_id")
    if not _model_identity_matches(recorded_model_id, phase_run["model"]):
        raise BaselineResolutionError(
            f"trace {baseline_trace_id!r} model_id {recorded_model_id!r} disagrees "
            f"with evaluation_phase_runs.model {phase_run['model']!r}"
        )
    recorded_input = metadata.get("input")
    if not isinstance(recorded_input, str):
        raise BaselineResolutionError(
            f"trace {baseline_trace_id!r} has no recorded input text"
        )

    return BaselineRecord(
        phase_run_id=phase_run_id,
        phase=phase,
        baseline_trace_id=baseline_trace_id,
        baseline_model=phase_run["model"],
        baseline_system_prompt=system_prompt,
        recorded_input=recorded_input,
        root_span=root,
        snapshot_phase_id=phase_run["snapshot_phase_id"],
        snapshot_id=snapshot_row["snapshot_id"],
        feedback_policy_version=snapshot_row["policy_version"],
    )


def resolve_reply_correction_oracle(
    state: StateManager, phase_run: dict[str, Any], *, dossier_root: Path,
) -> ReplyCorrectionOracle:
    """Resolve the unique, valid reply_draft correction oracle for one
    baseline phase_run.

    A case is eligible only when: the phase_run is linked to a real
    evaluation; that evaluation has a grade (grades.evaluation_id is
    already unique at the storage level, so "a grade" is "exactly one");
    that grade's reply_revision_id is non-null and resolves to a
    reply_draft_revisions row belonging to a draft_comments row for the
    *same* evaluation (never a moved or mismatched pointer); that
    revision's reply_text is non-blank; and the draft's pinned
    project_key/dossier_summary_id/dossier_revision resolve via
    dossier.resolve_dossier. Raises CorrectionOracleResolutionError for
    every way a case fails this — read-only, and always called before any
    candidate spend, so an ineligible case never reaches from_model or
    jig_replay.
    """
    evaluation_id = phase_run["evaluation_id"]
    if evaluation_id is None:
        raise CorrectionOracleResolutionError(
            f"phase_run {phase_run['id']} is not linked to an evaluation; "
            "reply_draft replay requires a scored, linked baseline"
        )

    with state.db.read_transaction():
        grade_id = state.get_grade_id_for_evaluation(evaluation_id)
        if grade_id is None:
            raise CorrectionOracleResolutionError(
                f"evaluation {evaluation_id} has no grade; reply_draft replay requires "
                "a human correction to score against"
            )
        grade_row = state.get_grade_row_by_id(grade_id)
        if grade_row is None:
            raise CorrectionOracleResolutionError(f"grade {grade_id} could not be read back")
        reply_revision_id = grade_row["reply_revision_id"]
        if reply_revision_id is None:
            raise CorrectionOracleResolutionError(
                f"grade {grade_id} has no reply_revision_id; reply_draft replay requires "
                "a recorded correction"
            )
        correction_text = grade_row["edited_text"]
        if correction_text is None or not correction_text.strip():
            raise CorrectionOracleResolutionError(
                f"grade {grade_id}'s correction (reply_revision_id={reply_revision_id}) is blank"
            )
        owner_row = state.conn.execute(
            "SELECT dc.evaluation_id AS owner_evaluation_id, dc.project_key, "
            "dc.dossier_summary_id, dc.dossier_revision "
            "FROM reply_draft_revisions rdr "
            "JOIN draft_comments dc ON dc.id = rdr.draft_comment_id "
            "WHERE rdr.id = ?",
            (reply_revision_id,),
        ).fetchone()
        if owner_row is None:
            raise CorrectionOracleResolutionError(
                f"reply_draft_revisions {reply_revision_id} does not resolve to a draft"
            )
        if owner_row["owner_evaluation_id"] != evaluation_id:
            raise CorrectionOracleResolutionError(
                f"reply_draft_revisions {reply_revision_id} belongs to evaluation "
                f"{owner_row['owner_evaluation_id']}, not {evaluation_id} — mismatched "
                "revision ownership"
            )
        project_key = owner_row["project_key"]
        dossier_summary_id = owner_row["dossier_summary_id"]
        dossier_revision = owner_row["dossier_revision"]
        if not project_key or not dossier_summary_id or not dossier_revision:
            raise CorrectionOracleResolutionError(
                f"draft for evaluation {evaluation_id} has no pinned "
                "project_key/dossier_summary_id/dossier_revision"
            )

    try:
        resolution = resolve_dossier(
            dossier_root, dossier_revision, project_key, dossier_summary_id,
        )
    except DossierResolutionError as exc:
        raise CorrectionOracleResolutionError(
            f"pinned dossier could not be resolved: {exc}"
        ) from exc

    return ReplyCorrectionOracle(
        grade_id=grade_id,
        reply_revision_id=reply_revision_id,
        correction_text=correction_text,
        correction_sha256=_sha256_utf8(correction_text),
        project_key=project_key,
        dossier_summary_id=dossier_summary_id,
        dossier_revision=dossier_revision,
        dossier=resolution.summary,
    )


def _resolve_dossier_root(dossier_root: Path | str | None) -> Path:
    if dossier_root is not None:
        return Path(dossier_root)
    return Path(_config.SCOUT_DOSSIER_ROOT)


def _verify_correction_hash(state: StateManager, oracle: ReplyCorrectionOracle) -> None:
    """Re-read the pinned correction revision by id and verify its bytes
    still hash to what was pinned before any spend — a defensive check
    against a moved or mutated pointer between resolution and scoring,
    even though reply_draft_revisions is immutable by construction."""
    row = state.conn.execute(
        "SELECT reply_text FROM reply_draft_revisions WHERE id = ?",
        (oracle.reply_revision_id,),
    ).fetchone()
    if row is None or _sha256_utf8(row["reply_text"]) != oracle.correction_sha256:
        raise CorrectionEvidenceIntegrityError(
            f"reply_draft_revisions {oracle.reply_revision_id} no longer matches the "
            "pinned correction hash"
        )


def _extract_candidate_distance(scores: list[Score] | None) -> float:
    """Return the candidate's normalized_edit_distance/v1 score value, or
    raise if grading did not run or did not produce one — it must not be
    silently treated as zero or omitted."""
    if scores is not None:
        for score in scores:
            if score.dimension == NORMALIZED_EDIT_DISTANCE_GRADER_VERSION:
                return float(score.value)
    raise ComparisonConstructionError(_STAGE_MESSAGES["candidate_grading"])


def _resolve_baseline_structured_draft(root_span: Span) -> StructuredDraftOutput:
    """Validate the baseline's own complete, stored structured output as a
    StructuredDraftOutput — the only source the historical baseline
    distance is computed from. Raises CorrectionOracleResolutionError
    (refusing spend) for incomplete or malformed baseline evidence."""
    side = _side_evidence(root_span)
    if not side.complete:
        raise CorrectionOracleResolutionError(
            f"baseline trace {root_span.trace_id!r} has no complete structured output "
            f"({side.incomplete_reason}); cannot compute a historical baseline distance"
        )
    try:
        return StructuredDraftOutput.model_validate(side.value)
    except ValidationError as exc:
        raise CorrectionOracleResolutionError(
            f"baseline trace {root_span.trace_id!r} structured output does not validate "
            f"as StructuredDraftOutput: {exc}"
        ) from exc


def build_candidate_plan(
    baseline: BaselineRecord,
    *,
    model_override: str | None,
    system_prompt_override: str | None,
) -> CandidateReplayPlan:
    """Decide the candidate's model and system prompt, hash both prompts
    and the recorded input, and serialize the frozen v2 candidate-only
    candidate_config — all before any database write or model call.

    Raises ModelResolutionError if `model_override` (or the baseline's own
    recorded model, when absent) does not route through Scout's trusted
    model resolver.
    """
    candidate_model = model_override if model_override else baseline.baseline_model
    try:
        from_model(candidate_model)
    except ValueError as exc:
        raise ModelResolutionError(str(exc)) from exc

    candidate_system_prompt = (
        system_prompt_override
        if system_prompt_override is not None
        else baseline.baseline_system_prompt
    )

    baseline_prompt_sha256 = _sha256_utf8(baseline.baseline_system_prompt)
    candidate_prompt_sha256 = _sha256_utf8(candidate_system_prompt)
    baseline_prompt_reused = candidate_prompt_sha256 == baseline_prompt_sha256
    recorded_input_sha256 = _sha256_utf8(baseline.recorded_input)
    is_no_op = candidate_model == baseline.baseline_model and baseline_prompt_reused
    grader_attached = baseline.phase == "reply_draft"

    candidate_config_json = _canonical_json(
        {
            "version": CANDIDATE_CONFIG_VERSION,
            "phase": baseline.phase,
            "model": candidate_model,
            "system_prompt": candidate_system_prompt,
            "system_prompt_sha256": candidate_prompt_sha256,
            "grader_attached": grader_attached,
        }
    )

    return CandidateReplayPlan(
        phase=baseline.phase,
        candidate_model=candidate_model,
        candidate_system_prompt=candidate_system_prompt,
        candidate_config_json=candidate_config_json,
        baseline_prompt_sha256=baseline_prompt_sha256,
        candidate_prompt_sha256=candidate_prompt_sha256,
        baseline_prompt_reused=baseline_prompt_reused,
        recorded_input_sha256=recorded_input_sha256,
        grader_attached=grader_attached,
        is_no_op=is_no_op,
    )


def build_baseline_evidence(
    baseline: BaselineRecord,
    plan: CandidateReplayPlan,
    oracle: ReplyCorrectionOracle | None,
) -> str:
    """Serialize the frozen v2 per-baseline-case provenance
    (evaluation_experiments.baseline_evidence) — the base shape always,
    plus the full correction-oracle pin only when `oracle` is not None
    (a reply_draft-eligible attempt)."""
    evidence: dict[str, Any] = {
        "version": BASELINE_EVIDENCE_VERSION,
        "recorded_input_sha256": plan.recorded_input_sha256,
        "baseline_prompt_reused": plan.baseline_prompt_reused,
    }
    if oracle is not None:
        evidence.update(
            {
                "baseline_model": baseline.baseline_model,
                "baseline_prompt_sha256": plan.baseline_prompt_sha256,
                "reply_revision_id": oracle.reply_revision_id,
                "correction_sha256": oracle.correction_sha256,
                "project_key": oracle.project_key,
                "dossier_summary_id": oracle.dossier_summary_id,
                "dossier_revision": oracle.dossier_revision,
                "grader_version": NORMALIZED_EDIT_DISTANCE_GRADER_VERSION,
                "assembler_version": DRAFT_TEXT_ASSEMBLER_VERSION,
            }
        )
    return _canonical_json(evidence)


def build_score_evidence(
    oracle: ReplyCorrectionOracle, *, baseline_distance: float, candidate_distance: float,
) -> str:
    """Serialize the immutable trace_comparisons.score_evidence document
    for one graded reply_draft comparison. delta = candidate_distance -
    baseline_distance: negative unambiguously means the candidate is
    closer to the correction than the baseline was."""
    return _canonical_json(
        {
            "grader_version": NORMALIZED_EDIT_DISTANCE_GRADER_VERSION,
            "assembler_version": DRAFT_TEXT_ASSEMBLER_VERSION,
            "correction_sha256": oracle.correction_sha256,
            "reply_revision_id": oracle.reply_revision_id,
            "baseline_distance": baseline_distance,
            "candidate_distance": candidate_distance,
            "delta": candidate_distance - baseline_distance,
            "grader_attached": True,
        }
    )


async def preview_replay(
    *,
    state: StateManager,
    tracer: TracingLogger,
    phase_run_id: int,
    model_override: str | None,
    system_prompt_override: str | None,
    dossier_root: Path | str | None = None,
) -> ReplayPreview:
    """Read-only preview: resolves the baseline (and, for a reply_draft
    phase, the correction oracle), builds the candidate plan, and reports
    everything a caller needs to decide whether to authorize execution.
    Never writes to the database and never calls a model (from_model only
    constructs a client, it issues no requests).
    """
    baseline = await resolve_baseline(state, tracer, phase_run_id)
    if baseline.phase == "reply_draft":
        phase_run = state.get_phase_run(phase_run_id)
        assert phase_run is not None
        resolve_reply_correction_oracle(
            state, phase_run, dossier_root=_resolve_dossier_root(dossier_root),
        )
    plan = build_candidate_plan(
        baseline, model_override=model_override, system_prompt_override=system_prompt_override,
    )
    phase_config = PHASE_REPLAY_CONFIGS[baseline.phase]
    return ReplayPreview(
        phase=baseline.phase,
        baseline_model=baseline.baseline_model,
        candidate_model=plan.candidate_model,
        baseline_prompt_sha256=plan.baseline_prompt_sha256,
        candidate_prompt_sha256=plan.candidate_prompt_sha256,
        baseline_prompt_reused=plan.baseline_prompt_reused,
        recorded_input_sha256=plan.recorded_input_sha256,
        recorded_input_reused=True,
        snapshot_phase_id=baseline.snapshot_phase_id,
        snapshot_id=baseline.snapshot_id,
        feedback_policy_version=baseline.feedback_policy_version,
        max_llm_calls=phase_config.max_llm_calls,
        is_no_op=plan.is_no_op,
        grader_attached=plan.grader_attached,
    )


def _build_candidate_agent_config(
    baseline: BaselineRecord,
    plan: CandidateReplayPlan,
    *,
    llm: LLMClient,
    tracer: TracingLogger,
    feedback: FeedbackLoop,
) -> AgentConfig[Any]:
    """Build the candidate's AgentConfig entirely from the fixed phase map
    and the candidate plan — never from a dynamically imported schema."""
    phase_config = PHASE_REPLAY_CONFIGS[baseline.phase]
    return AgentConfig(
        name=f"scout_replay_{baseline.phase}",
        description=f"Scout offline replay candidate for phase_run_id={baseline.phase_run_id}.",
        system_prompt=plan.candidate_system_prompt,
        llm=llm,
        feedback=feedback,
        tracer=tracer,
        tools=ToolRegistry([]),
        output_schema=phase_config.output_schema,
        max_tool_calls=phase_config.max_tool_calls,
        max_llm_calls=phase_config.max_llm_calls,
        max_parse_retries=phase_config.max_parse_retries,
        include_memory_in_prompt=False,
        include_feedback_in_prompt=False,
    )


@dataclass(frozen=True, slots=True)
class _DomainDiffSide:
    complete: bool
    value: Any = None
    sha256: str | None = None
    utf8_byte_length: int | None = None
    incomplete_reason: str | None = None


def _side_evidence(root: Span) -> _DomainDiffSide:
    """Extract complete-output evidence from an AGENT_RUN root the same
    way jig.replay.diff._complete_output_evidence does, independently
    re-derived per side so domain_diff can report each side's own
    incomplete_reason (TraceDiff itself only exposes one combined
    reason)."""
    output = root.output if isinstance(root.output, dict) else {}
    if ROOT_OUTPUT_KIND_KEY not in output:
        return _DomainDiffSide(complete=False, incomplete_reason="preview_only_output")
    if ROOT_OUTPUT_COMPLETE_KEY not in output:
        return _DomainDiffSide(complete=False, incomplete_reason="structured_output_unavailable")
    value = output[ROOT_OUTPUT_COMPLETE_KEY]
    sha256 = output.get(ROOT_OUTPUT_SHA256_KEY)
    byte_length = output.get(ROOT_OUTPUT_BYTE_LENGTH_KEY)
    if not isinstance(sha256, str) or not isinstance(byte_length, int):
        return _DomainDiffSide(complete=False, incomplete_reason="structured_output_unavailable")
    return _DomainDiffSide(complete=True, value=value, sha256=sha256, utf8_byte_length=byte_length)


def _escape_pointer_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _diff_values(
    a: Any, b: Any, pointer: str, additions: list[str], removals: list[str], changes: list[str],
) -> None:
    """Recursive RFC 6901 field diff: object keys traversed lexicographically,
    arrays by numeric index, excess indices reported as additions/removals,
    a type-or-scalar mismatch reported once at its pointer (never recursed
    into further), root pointer is the empty string."""
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            child = f"{pointer}/{_escape_pointer_token(key)}"
            if key in a and key not in b:
                removals.append(child)
            elif key in b and key not in a:
                additions.append(child)
            else:
                _diff_values(a[key], b[key], child, additions, removals, changes)
    elif isinstance(a, list) and isinstance(b, list):
        for i in range(max(len(a), len(b))):
            child = f"{pointer}/{i}"
            if i >= len(a):
                additions.append(child)
            elif i >= len(b):
                removals.append(child)
            else:
                _diff_values(a[i], b[i], child, additions, removals, changes)
    else:
        if a != b:
            changes.append(pointer)


def build_domain_diff(baseline_root: Span, candidate_root: Span) -> dict[str, Any]:
    """Build Scout's canonical domain_diff from two AGENT_RUN roots' own
    persisted complete-output evidence. additions/removals/changes are
    included only when both sides are complete; each side always reports
    its own complete/value/sha256/utf8_byte_length/incomplete_reason."""
    baseline_side = _side_evidence(baseline_root)
    candidate_side = _side_evidence(candidate_root)

    def _side_dict(side: _DomainDiffSide) -> dict[str, Any]:
        rendered: dict[str, Any] = {
            "complete": side.complete,
            "sha256": side.sha256,
            "utf8_byte_length": side.utf8_byte_length,
            "incomplete_reason": side.incomplete_reason,
        }
        if side.complete:
            rendered["value"] = side.value
        return rendered

    domain_diff: dict[str, Any] = {
        "baseline": _side_dict(baseline_side),
        "candidate": _side_dict(candidate_side),
        "grader_not_attached": True,
    }
    if baseline_side.complete and candidate_side.complete:
        additions: list[str] = []
        removals: list[str] = []
        changes: list[str] = []
        _diff_values(baseline_side.value, candidate_side.value, "", additions, removals, changes)
        additions.sort()
        removals.sort()
        changes.sort()
        domain_diff["additions"] = additions
        domain_diff["removals"] = removals
        domain_diff["changes"] = changes
    return domain_diff


def _serialize_domain_diff(domain_diff: dict[str, Any]) -> str:
    try:
        return json.dumps(
            domain_diff, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        )
    except (ValueError, TypeError) as exc:
        raise ComparisonConstructionError(
            "domain_diff contains a non-finite or unsupported value"
        ) from exc


def _serialize_trace_diff(diff: TraceDiff) -> str:
    try:
        return json.dumps(
            dataclasses.asdict(diff),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (ValueError, TypeError) as exc:
        raise ComparisonConstructionError(
            "trace_diff contains a non-finite or unsupported value"
        ) from exc


async def _run_candidate_and_complete(
    *,
    state: StateManager,
    tracer: TracingLogger,
    feedback: FeedbackLoop,
    experiment_id: int,
    baseline: BaselineRecord,
    plan: CandidateReplayPlan,
    oracle: ReplyCorrectionOracle | None,
    baseline_distance: float | None,
) -> tuple[str, int, float | None]:
    """Shared candidate-execution core for one already-'running' attempt:
    runs the candidate, records its trace, scores it (when `oracle` is
    given), builds the comparison, and CASes the attempt to 'complete'.

    Used by both execute_replay (single-case) and batch/sweep execution
    (evaluation_experiments._execute_one_batch_attempt) — the CAS/trace/
    comparison sequence must never diverge between the two call sites.
    Always CASes the attempt to 'failed' with a fixed, stage-specific,
    operator-safe error_detail before re-raising the matching ReplayError
    subclass; callers decide whether that aborts the whole call
    (execute_replay) or is caught and recorded per-case (batch execution).
    Returns (candidate_trace_id, candidate_llm_call_count, candidate_cost).
    """
    candidate_llm = from_model(plan.candidate_model)
    candidate_agent_config = _build_candidate_agent_config(
        baseline, plan, llm=candidate_llm, tracer=tracer, feedback=feedback,
    )
    grader = (
        ReplyCorrectionGrader(dossier=oracle.dossier, correction_text=oracle.correction_text)
        if oracle is not None
        else None
    )

    try:
        agent_result = await jig_replay(
            baseline.baseline_trace_id,
            candidate_agent_config,
            tracer=tracer,
            llm=candidate_llm,
            feedback=feedback,
            grader=grader,
        )
    except Exception as exc:
        state.fail_experiment(experiment_id, error_detail=_STAGE_MESSAGES["candidate_execution"])
        raise CandidateExecutionError(_STAGE_MESSAGES["candidate_execution"]) from exc

    candidate_trace_id = agent_result.trace_id
    try:
        candidate_spans = await tracer.get_trace(candidate_trace_id)
        candidate_root = _verify_agent_run_root(
            candidate_spans, candidate_trace_id, CandidateTraceEvidenceError,
        )
    except CandidateTraceEvidenceError:
        state.fail_experiment(
            experiment_id, error_detail=_STAGE_MESSAGES["candidate_trace_evidence"],
        )
        raise

    llm_call_count = sum(1 for s in candidate_spans if s.kind == SpanKind.LLM_CALL)
    candidate_cost_evidence_complete = _has_complete_cost_evidence(candidate_spans)
    costs = [
        s.usage.cost
        for s in candidate_spans
        if s.kind == SpanKind.LLM_CALL and s.usage is not None and s.usage.cost is not None
    ]
    candidate_cost = sum(costs) if candidate_cost_evidence_complete else None

    try:
        state.record_candidate_trace(
            experiment_id,
            candidate_trace_id=candidate_trace_id,
            candidate_llm_call_count=llm_call_count,
            candidate_cost=candidate_cost,
        )
    except ExperimentCASError:
        state.fail_experiment(
            experiment_id, error_detail=_STAGE_MESSAGES["candidate_trace_evidence"],
        )
        raise

    # Failed outputs still incur costs. Preserve their verified trace and
    # per-attempt accounting before closing the attempt as failed.
    if agent_result.error is not None or agent_result.parsed is None:
        state.fail_experiment(experiment_id, error_detail=_STAGE_MESSAGES["candidate_execution"])
        raise CandidateExecutionError(_STAGE_MESSAGES["candidate_execution"])

    score_evidence_json: str | None = None
    if oracle is not None:
        assert baseline_distance is not None
        try:
            _verify_correction_hash(state, oracle)
            candidate_distance = _extract_candidate_distance(agent_result.scores)
        except (CorrectionEvidenceIntegrityError, ComparisonConstructionError):
            state.fail_experiment(experiment_id, error_detail=_STAGE_MESSAGES["candidate_grading"])
            raise
        score_evidence_json = build_score_evidence(
            oracle, baseline_distance=baseline_distance, candidate_distance=candidate_distance,
        )

    try:
        diff = await jig_trace_diff(baseline.baseline_trace_id, candidate_trace_id, tracer=tracer)
        domain_diff = build_domain_diff(baseline.root_span, candidate_root)
        trace_diff_json = _serialize_trace_diff(diff)
        domain_diff_json = _serialize_domain_diff(domain_diff)
    except ComparisonConstructionError:
        state.fail_experiment(experiment_id, error_detail=_STAGE_MESSAGES["diff_construction"])
        raise
    except Exception as exc:
        state.fail_experiment(experiment_id, error_detail=_STAGE_MESSAGES["diff_construction"])
        raise ComparisonConstructionError(_STAGE_MESSAGES["diff_construction"]) from exc

    state.complete_experiment_with_comparison(
        experiment_id,
        jig_revision=JIG_REVISION,
        trace_diff=trace_diff_json,
        domain_diff=domain_diff_json,
        score_evidence=score_evidence_json,
    )
    return candidate_trace_id, llm_call_count, candidate_cost


async def execute_replay(
    *,
    state: StateManager,
    tracer: TracingLogger,
    feedback: FeedbackLoop,
    phase_run_id: int,
    name: str,
    model_override: str | None,
    system_prompt_override: str | None,
    dossier_root: Path | str | None = None,
) -> ExperimentOutcome:
    """Execute one explicitly authorized paid replay end to end.

    All read-only validation (baseline resolution, correction-oracle
    resolution for a reply_draft phase, candidate planning, no-op
    rejection) completes before the first write — an ineligible
    reply_draft case never reaches from_model or jig_replay. Opens a
    fresh, single-attempt experiment_runs parent per call. Failure after
    the queued attempt is inserted always CASes it to 'failed' with a
    fixed, stage-specific, operator-safe error_detail — never a raw
    exception, prompt, input, or provider payload — before re-raising,
    which also recomputes the parent run's projected status.
    """
    baseline = await resolve_baseline(state, tracer, phase_run_id)

    oracle: ReplyCorrectionOracle | None = None
    baseline_distance: float | None = None
    if baseline.phase == "reply_draft":
        phase_run = state.get_phase_run(phase_run_id)
        assert phase_run is not None
        oracle = resolve_reply_correction_oracle(
            state, phase_run, dossier_root=_resolve_dossier_root(dossier_root),
        )
        baseline_draft = _resolve_baseline_structured_draft(baseline.root_span)
        baseline_text = assemble_draft_text(baseline_draft, oracle.dossier)
        baseline_distance = normalized_edit_distance(baseline_text, oracle.correction_text)

    plan = build_candidate_plan(
        baseline, model_override=model_override, system_prompt_override=system_prompt_override,
    )
    if plan.is_no_op:
        raise NoOpReplayError(
            "candidate model and system prompt are identical to the baseline; "
            "supply --model and/or --prompt-file to change at least one variable"
        )

    baseline_evidence_json = build_baseline_evidence(baseline, plan, oracle)
    experiment_run_id = state.create_experiment_run(
        name=name, candidate_config=plan.candidate_config_json,
    )
    experiment_id = state.insert_experiment_attempt(
        experiment_run_id=experiment_run_id,
        phase_run_id=phase_run_id,
        baseline_evidence=baseline_evidence_json,
    )
    state.cas_experiment_to_running(experiment_id)

    candidate_trace_id, llm_call_count, candidate_cost = await _run_candidate_and_complete(
        state=state,
        tracer=tracer,
        feedback=feedback,
        experiment_id=experiment_id,
        baseline=baseline,
        plan=plan,
        oracle=oracle,
        baseline_distance=baseline_distance,
    )
    return ExperimentOutcome(
        experiment_id=experiment_id,
        experiment_run_id=experiment_run_id,
        candidate_trace_id=candidate_trace_id,
        candidate_llm_call_count=llm_call_count,
        candidate_cost=candidate_cost,
    )


# ---------------------------------------------------------------------------
# replay-sweep v1: one-axis named variant sets (contracts/replay-sweep.v1.schema.json)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SweepVariant:
    """One named variant of a sweep. Exactly one of `model`/`prompt_file`
    is set, matching the sweep's declared axis — enforced by
    replay-sweep v1 schema validation before this is ever constructed."""

    name: str
    model: str | None
    prompt_file: str | None


@dataclass(frozen=True, slots=True)
class SweepDefinition:
    """A validated replay-sweep v1 document: every variant changes exactly
    one axis (`axis`) relative to a shared, axis-invariant setting
    (`shared_model` for a prompt sweep, `shared_prompt_file` for a model
    sweep — either may be None, meaning "reuse each baseline case's own
    recorded value")."""

    name: str
    axis: str
    shared_model: str | None
    shared_prompt_file: str | None
    variants: tuple[SweepVariant, ...]


def _load_sweep_schema() -> dict[str, Any]:
    with SWEEP_SCHEMA_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)  # type: ignore[no-any-return]


def load_sweep_document(path: Path | str) -> dict[str, Any]:
    """Parse one sweep file — canonical YAML or JSON, by content rather
    than extension — into a plain dict. Raises SweepValidationError for a
    missing file, invalid syntax, or a non-object document."""
    sweep_path = Path(path)
    try:
        raw = sweep_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SweepValidationError(f"could not read sweep file {sweep_path!r}: {exc}") from exc
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise SweepValidationError(
            f"sweep file {sweep_path!r} is not valid YAML or JSON: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise SweepValidationError(f"sweep file {sweep_path!r} must decode to an object")
    return document


def _read_utf8_text_file(path: Path, *, error_cls: type[ReplayError]) -> str:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise error_cls(f"could not read {path!r}: {exc}") from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise error_cls(f"{path!r} is not valid UTF-8: {exc}") from exc


def validate_sweep_document(document: dict[str, Any], *, base_dir: Path) -> SweepDefinition:
    """Validate one parsed sweep document against replay-sweep v1 and
    Scout's own semantic rules, and resolve it into a SweepDefinition.

    Structural rules (schema): >= 2 variants, and each variant carries only
    the field matching the declared axis (a prompt sweep's variants may
    only set prompt_file; a model sweep's variants may only set model).
    Semantic rules (here): variant names must be unique, every referenced
    model must route through Scout's trusted resolver (from_model), every
    referenced prompt file must exist and be valid UTF-8, and no two
    variants may be semantically identical — a model-axis sweep rejects
    two variants naming the same model, and a prompt-axis sweep rejects
    two variants whose resolved prompt file content is byte-identical
    (compared by SHA-256, not just by filename) — since a sweep exists to
    compare *distinct* configurations. Raises SweepValidationError for any
    violation — always before any write.
    """
    schema = _load_sweep_schema()
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.path))
    if errors:
        messages = "; ".join(e.message for e in errors)
        raise SweepValidationError(
            f"sweep document failed replay-sweep v1 validation: {messages}"
        )

    axis = document["axis"]
    raw_variants = document["variants"]
    names = [v["name"] for v in raw_variants]
    if len(names) != len(set(names)):
        duplicates = sorted({n for n in names if names.count(n) > 1})
        raise SweepValidationError(f"sweep variant names must be unique; duplicated: {duplicates}")

    shared_model = document.get("model")
    shared_prompt_file = document.get("prompt_file")
    if shared_model is not None:
        try:
            from_model(shared_model)
        except ValueError as exc:
            raise SweepValidationError(
                f"sweep model {shared_model!r} is not routable: {exc}"
            ) from exc
    if shared_prompt_file is not None:
        _read_utf8_text_file(base_dir / shared_prompt_file, error_cls=SweepValidationError)

    variants: list[SweepVariant] = []
    seen_models: dict[str, str] = {}
    seen_prompt_hashes: dict[str, str] = {}
    for raw in raw_variants:
        variant_model = raw.get("model")
        variant_prompt_file = raw.get("prompt_file")
        if variant_model is not None:
            try:
                from_model(variant_model)
            except ValueError as exc:
                raise SweepValidationError(
                    f"sweep variant {raw['name']!r} model {variant_model!r} is not routable: {exc}"
                ) from exc
            if variant_model in seen_models:
                raise SweepValidationError(
                    f"sweep variants {seen_models[variant_model]!r} and {raw['name']!r} both use "
                    f"model {variant_model!r} — every variant must be semantically distinct"
                )
            seen_models[variant_model] = raw["name"]
        if variant_prompt_file is not None:
            text = _read_utf8_text_file(
                base_dir / variant_prompt_file, error_cls=SweepValidationError,
            )
            text_sha256 = _sha256_utf8(text)
            if text_sha256 in seen_prompt_hashes:
                raise SweepValidationError(
                    f"sweep variants {seen_prompt_hashes[text_sha256]!r} and {raw['name']!r} "
                    "resolve to identical prompt content — every variant must be semantically "
                    "distinct"
                )
            seen_prompt_hashes[text_sha256] = raw["name"]
        variants.append(
            SweepVariant(name=raw["name"], model=variant_model, prompt_file=variant_prompt_file)
        )

    return SweepDefinition(
        name=document["name"],
        axis=axis,
        shared_model=shared_model,
        shared_prompt_file=shared_prompt_file,
        variants=tuple(variants),
    )


def load_and_validate_sweep(path: Path | str) -> SweepDefinition:
    """Load and validate one sweep file in one call. Prompt files inside
    the sweep are resolved relative to the sweep file's own directory."""
    sweep_path = Path(path)
    document = load_sweep_document(sweep_path)
    return validate_sweep_document(document, base_dir=sweep_path.parent)


def _sweep_variant_overrides(
    sweep: SweepDefinition, variant: SweepVariant, base_dir: Path,
) -> tuple[str | None, str | None]:
    """Resolve one sweep variant's (model_override, system_prompt_override)
    — reading its prompt file's text fresh, since validate_sweep_document
    only checked readability/UTF-8 at load time."""
    if sweep.axis == "model":
        prompt_override = (
            _read_utf8_text_file(
                base_dir / sweep.shared_prompt_file, error_cls=SweepValidationError,
            )
            if sweep.shared_prompt_file is not None
            else None
        )
        return variant.model, prompt_override
    assert variant.prompt_file is not None
    prompt_text = _read_utf8_text_file(
        base_dir / variant.prompt_file, error_cls=SweepValidationError,
    )
    return sweep.shared_model, prompt_text


@dataclass(frozen=True, slots=True)
class BatchVariant:
    """One candidate override policy applied uniformly to every baseline
    case in a batch. A plain (non-sweep) batch has exactly one, named
    DEFAULT_BATCH_VARIANT_NAME; a sweep has one per named variant."""

    name: str
    model_override: str | None
    system_prompt_override: str | None


def batch_variants_for_sweep(sweep: SweepDefinition, *, base_dir: Path) -> tuple[BatchVariant, ...]:
    """Resolve every one of a sweep's variants into a BatchVariant."""
    resolved = []
    for variant in sweep.variants:
        model_override, prompt_override = _sweep_variant_overrides(sweep, variant, base_dir)
        resolved.append(
            BatchVariant(
                name=variant.name, model_override=model_override,
                system_prompt_override=prompt_override,
            )
        )
    return tuple(resolved)


# ---------------------------------------------------------------------------
# Batch population selectors
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BatchSelector:
    """Exactly one mutually exclusive batch-population selection strategy.
    Construct via one of the classmethods, never the bare constructor."""

    kind: str
    phase_run_ids: tuple[int, ...] = ()
    scan_id: int | None = None
    from_utc: str | None = None
    to_utc: str | None = None

    @classmethod
    def by_phase_run_ids(cls, phase_run_ids: list[int] | tuple[int, ...]) -> BatchSelector:
        return cls(kind="phase_run_ids", phase_run_ids=tuple(phase_run_ids))

    @classmethod
    def by_scan_id(cls, scan_id: int) -> BatchSelector:
        return cls(kind="scan_id", scan_id=scan_id)

    @classmethod
    def by_window(cls, from_utc: str, to_utc: str) -> BatchSelector:
        return cls(kind="window", from_utc=from_utc, to_utc=to_utc)

    @classmethod
    def graded_with_corrections(cls) -> BatchSelector:
        return cls(kind="graded_with_corrections")

    def canonical(self) -> dict[str, Any]:
        if self.kind == "phase_run_ids":
            return {"kind": self.kind, "phase_run_ids": sorted(self.phase_run_ids)}
        if self.kind == "scan_id":
            return {"kind": self.kind, "scan_id": self.scan_id}
        if self.kind == "window":
            return {"kind": self.kind, "from": self.from_utc, "to": self.to_utc}
        if self.kind == "graded_with_corrections":
            return {"kind": self.kind}
        raise AssertionError(f"unknown selector kind: {self.kind!r}")


@dataclass(frozen=True, slots=True)
class BatchPopulation:
    """A resolved, deduplicated, stably ordered batch population."""

    phase_run_ids: tuple[int, ...]
    dropped_duplicate_phase_run_ids: tuple[int, ...]


def _parse_utc_bound(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SelectorResolutionError(
            f"{value!r} is not a valid ISO-8601 date/time: {exc}"
        ) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def resolve_batch_population(state: StateManager, selector: BatchSelector) -> BatchPopulation:
    """Read-only. Resolve one batch selector to its complete reply_draft
    phase_run_id population, in stable ascending phase_run_id order.

    Duplicate baselines — more than one phase_run sharing the same
    evaluation_id — are removed before planning: the highest phase_run_id
    (the most recent attempt) is kept, and every earlier duplicate is
    reported in `dropped_duplicate_phase_run_ids` rather than silently
    disappearing. Raises SelectorResolutionError for a malformed selector,
    an id that does not resolve to a complete reply_draft phase_run, or a
    population that resolves empty.
    """
    with state.db.read_transaction():
        if selector.kind == "phase_run_ids":
            if not selector.phase_run_ids:
                raise SelectorResolutionError("--phase-run-id requires at least one id")
            rows = []
            for phase_run_id in sorted(set(selector.phase_run_ids)):
                phase_run = state.get_phase_run(phase_run_id)
                if phase_run is None:
                    raise SelectorResolutionError(
                        f"no evaluation_phase_runs row with id={phase_run_id}"
                    )
                if phase_run["phase"] != "reply_draft":
                    raise SelectorResolutionError(
                        f"phase_run_id={phase_run_id} is phase {phase_run['phase']!r}, "
                        "not 'reply_draft'"
                    )
                if phase_run["status"] != "complete":
                    raise SelectorResolutionError(
                        f"phase_run_id={phase_run_id} has status {phase_run['status']!r}, "
                        "not 'complete'"
                    )
                rows.append(phase_run)
        elif selector.kind == "scan_id":
            all_rows = state.list_complete_reply_draft_phase_runs()
            rows = [row for row in all_rows if row["scan_id"] == selector.scan_id]
        elif selector.kind == "window":
            assert selector.from_utc is not None and selector.to_utc is not None
            from_dt = _parse_utc_bound(selector.from_utc)
            to_dt = _parse_utc_bound(selector.to_utc)
            if not from_dt < to_dt:
                raise SelectorResolutionError("--from must be strictly before --to")
            all_rows = state.list_complete_reply_draft_phase_runs()
            rows = [
                row for row in all_rows
                if from_dt <= _parse_utc_bound(row["created_at"]) < to_dt
            ]
        elif selector.kind == "graded_with_corrections":
            all_rows = state.list_complete_reply_draft_phase_runs()
            eligible = state.list_evaluation_ids_with_reply_correction()
            rows = [row for row in all_rows if row["evaluation_id"] in eligible]
        else:
            raise AssertionError(f"unknown selector kind: {selector.kind!r}")

    if not rows:
        raise SelectorResolutionError("selector resolved to an empty population")

    best_by_evaluation: dict[int, dict[str, Any]] = {}
    unlinked_ids: list[int] = []
    for row in rows:
        evaluation_id = row["evaluation_id"]
        if evaluation_id is None:
            unlinked_ids.append(row["id"])
            continue
        current = best_by_evaluation.get(evaluation_id)
        if current is None or row["id"] > current["id"]:
            best_by_evaluation[evaluation_id] = row

    kept_ids = {row["id"] for row in best_by_evaluation.values()} | set(unlinked_ids)
    all_ids = {row["id"] for row in rows}
    dropped = sorted(all_ids - kept_ids)
    return BatchPopulation(
        phase_run_ids=tuple(sorted(kept_ids)), dropped_duplicate_phase_run_ids=tuple(dropped),
    )


# ---------------------------------------------------------------------------
# Per-case resolution and pair classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BatchCase:
    """One resolved baseline case shared across every variant of a batch —
    correction-oracle eligibility and token usage depend only on the
    baseline, never on a candidate variant, so both are resolved once per
    case rather than once per (case, variant) pair."""

    phase_run_id: int
    baseline: BaselineRecord
    oracle: ReplyCorrectionOracle | None
    oracle_error: str | None
    baseline_usage: TokenUsage | None


async def _resolve_batch_case(
    state: StateManager, tracer: TracingLogger, phase_run_id: int, *, dossier_root: Path,
) -> BatchCase:
    baseline = await resolve_baseline(state, tracer, phase_run_id)
    if baseline.phase != "reply_draft":
        raise SelectorResolutionError(
            f"phase_run_id={phase_run_id} resolved to phase {baseline.phase!r}, not 'reply_draft'"
        )
    phase_run = state.get_phase_run(phase_run_id)
    assert phase_run is not None

    oracle: ReplyCorrectionOracle | None = None
    oracle_error: str | None = None
    try:
        oracle = resolve_reply_correction_oracle(state, phase_run, dossier_root=dossier_root)
    except CorrectionOracleResolutionError as exc:
        oracle_error = str(exc)

    spans = await tracer.get_trace(baseline.baseline_trace_id)
    baseline_usage = aggregate_baseline_usage(spans)

    return BatchCase(
        phase_run_id=phase_run_id, baseline=baseline, oracle=oracle,
        oracle_error=oracle_error, baseline_usage=baseline_usage,
    )


@dataclass(frozen=True, slots=True)
class PairClassification:
    """One (baseline case, candidate variant) pair's eligibility.

    Precedence when more than one condition applies: a no-op pair is
    reported as `no_op` even when the case is also unscored or unpriceable
    (it would never spend regardless); otherwise an unscored case is
    reported as `unscored` even when also unpriceable (grading eligibility
    is checked first because it is case-level, not pair-level); only a
    scored, non-no-op pair is checked for priceability.
    """

    phase_run_id: int
    variant_name: str
    classification: str
    reason: str | None
    plan: CandidateReplayPlan
    price_estimate: PriceEstimate | None


def _classify_pair(
    case: BatchCase, plan: CandidateReplayPlan, catalog: PricingCatalog, variant_name: str,
) -> PairClassification:
    if plan.is_no_op:
        return PairClassification(
            phase_run_id=case.phase_run_id, variant_name=variant_name, classification="no_op",
            reason="candidate model and system prompt are identical to the baseline",
            plan=plan, price_estimate=None,
        )
    if case.oracle is None:
        return PairClassification(
            phase_run_id=case.phase_run_id, variant_name=variant_name, classification="unscored",
            reason=case.oracle_error, plan=plan, price_estimate=None,
        )
    estimate = price_pair(case.baseline_usage, plan.candidate_model, catalog)
    if estimate is None:
        reason = (
            "baseline trace has no complete recorded token usage"
            if case.baseline_usage is None
            else f"candidate model {plan.candidate_model!r} has no pricing catalog entry"
        )
        return PairClassification(
            phase_run_id=case.phase_run_id, variant_name=variant_name,
            classification="unpriceable", reason=reason, plan=plan, price_estimate=None,
        )
    return PairClassification(
        phase_run_id=case.phase_run_id, variant_name=variant_name, classification="scored",
        reason=None, plan=plan, price_estimate=estimate,
    )


@dataclass(frozen=True, slots=True)
class SkipPolicy:
    """Which non-'scored' classifications execution may exclude rather than
    refuse on. Every field defaults to False: by default every non-scored
    pair blocks execution."""

    skip_unscored: bool = False
    skip_no_op: bool = False
    skip_unpriceable: bool = False

    def allows(self, classification: str) -> bool:
        if classification == "scored":
            return True
        if classification == "unscored":
            return self.skip_unscored
        if classification == "no_op":
            return self.skip_no_op
        if classification == "unpriceable":
            return self.skip_unpriceable
        raise AssertionError(f"unknown classification: {classification!r}")


# ---------------------------------------------------------------------------
# Canonical plan v2: batch/sweep preview and --authorize-plan-sha256
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BatchPlan:
    """A fully resolved batch/sweep plan: population, every variant, every
    case, and every (case, variant) pair's classification — plus the
    canonical plan document and its SHA-256, which --authorize-plan-sha256
    pins. Rebuilding this from identical inputs is deterministic; any
    change to the population, a grade pointer, a candidate override, the
    controlled worker settings, pricing catalog, or skip policy changes `plan_sha256`.
    """

    plan_json: str
    plan_sha256: str
    phase_run_ids: tuple[int, ...]
    dropped_duplicate_phase_run_ids: tuple[int, ...]
    variants: tuple[BatchVariant, ...]
    cases: dict[int, BatchCase]
    pairs: tuple[PairClassification, ...]
    skip_policy: SkipPolicy
    pricing_catalog: PricingCatalog
    sweep: SweepDefinition | None

    def pair_for(self, phase_run_id: int, variant_name: str) -> PairClassification:
        for pair in self.pairs:
            if pair.phase_run_id == phase_run_id and pair.variant_name == variant_name:
                return pair
        raise KeyError((phase_run_id, variant_name))


def _build_canonical_plan_document(
    *,
    selector: BatchSelector,
    population: BatchPopulation,
    variants: tuple[BatchVariant, ...],
    cases: dict[int, BatchCase],
    pairs: list[PairClassification],
    skip_policy: SkipPolicy,
    pricing_catalog: PricingCatalog,
    sweep: SweepDefinition | None,
) -> dict[str, Any]:
    variant_docs = [
        {
            "name": variant.name,
            "model_override": variant.model_override,
            "system_prompt_override_sha256": (
                _sha256_utf8(variant.system_prompt_override)
                if variant.system_prompt_override is not None
                else None
            ),
        }
        for variant in variants
    ]
    case_docs = []
    for phase_run_id in population.phase_run_ids:
        case = cases[phase_run_id]
        case_docs.append(
            {
                "phase_run_id": phase_run_id,
                "baseline_model": case.baseline.baseline_model,
                "baseline_prompt_sha256": _sha256_utf8(case.baseline.baseline_system_prompt),
                "recorded_input_sha256": _sha256_utf8(case.baseline.recorded_input),
                "oracle": (
                    {
                        "reply_revision_id": case.oracle.reply_revision_id,
                        "correction_sha256": case.oracle.correction_sha256,
                        "dossier_summary_id": case.oracle.dossier_summary_id,
                        "dossier_revision": case.oracle.dossier_revision,
                    }
                    if case.oracle is not None
                    else None
                ),
                "oracle_error": case.oracle_error,
                "baseline_usage": (
                    {
                        "input_tokens": case.baseline_usage.input_tokens,
                        "output_tokens": case.baseline_usage.output_tokens,
                    }
                    if case.baseline_usage is not None
                    else None
                ),
            }
        )
    pair_docs = [
        {
            "phase_run_id": pair.phase_run_id,
            "variant": pair.variant_name,
            "classification": pair.classification,
            "reason": pair.reason,
            "candidate_model": pair.plan.candidate_model,
            "worker_configuration": dataclasses.asdict(replay_worker_configuration(pair.plan)),
            "estimated_usd": pair.price_estimate.estimated_usd if pair.price_estimate else None,
        }
        for pair in sorted(pairs, key=lambda p: (p.phase_run_id, p.variant_name))
    ]
    return {
        "version": PLAN_SCHEMA_VERSION,
        "selector": selector.canonical(),
        "phase_run_ids": list(population.phase_run_ids),
        "dropped_duplicate_phase_run_ids": list(population.dropped_duplicate_phase_run_ids),
        "sweep": (
            {"name": sweep.name, "axis": sweep.axis, "version": SWEEP_SCHEMA_VERSION}
            if sweep is not None
            else None
        ),
        "variants": variant_docs,
        "cases": case_docs,
        "pairs": pair_docs,
        "skip_policy": {
            "skip_unscored": skip_policy.skip_unscored,
            "skip_no_op": skip_policy.skip_no_op,
            "skip_unpriceable": skip_policy.skip_unpriceable,
        },
        "pricing": {
            "catalog_version": pricing_catalog.version,
            "catalog_hash": pricing_catalog.catalog_hash,
            "source_url": pricing_catalog.source_url,
            "as_of": pricing_catalog.as_of,
        },
        "max_llm_calls_per_case": PHASE_REPLAY_CONFIGS["reply_draft"].max_llm_calls,
    }


async def build_batch_plan(
    *,
    state: StateManager,
    tracer: TracingLogger,
    selector: BatchSelector,
    variants: tuple[BatchVariant, ...],
    skip_policy: SkipPolicy,
    pricing_catalog: PricingCatalog,
    dossier_root: Path,
    sweep: SweepDefinition | None = None,
) -> BatchPlan:
    """Resolve one batch selector and every (case, variant) pair's
    classification, and build the canonical plan document and its hash.

    Entirely read-only: never inserts a row and never calls a model. Called
    identically by preview (to report) and by execution (to recompute and
    verify --authorize-plan-sha256 before any write) — the two must never
    diverge, which is why both paths call this one function.
    """
    if not variants:
        raise SelectorResolutionError("a batch plan requires at least one candidate variant")

    population = resolve_batch_population(state, selector)
    cases: dict[int, BatchCase] = {}
    for phase_run_id in population.phase_run_ids:
        cases[phase_run_id] = await _resolve_batch_case(
            state, tracer, phase_run_id, dossier_root=dossier_root,
        )

    pairs: list[PairClassification] = []
    for variant in variants:
        for phase_run_id in population.phase_run_ids:
            case = cases[phase_run_id]
            plan = build_candidate_plan(
                case.baseline,
                model_override=variant.model_override,
                system_prompt_override=variant.system_prompt_override,
            )
            pairs.append(_classify_pair(case, plan, pricing_catalog, variant.name))

    plan_document = _build_canonical_plan_document(
        selector=selector, population=population, variants=variants, cases=cases,
        pairs=pairs, skip_policy=skip_policy, pricing_catalog=pricing_catalog, sweep=sweep,
    )
    plan_json = _canonical_json(plan_document)
    plan_sha256 = _sha256_utf8(plan_json)
    return BatchPlan(
        plan_json=plan_json,
        plan_sha256=plan_sha256,
        phase_run_ids=population.phase_run_ids,
        dropped_duplicate_phase_run_ids=population.dropped_duplicate_phase_run_ids,
        variants=variants,
        cases=cases,
        pairs=tuple(pairs),
        skip_policy=skip_policy,
        pricing_catalog=pricing_catalog,
        sweep=sweep,
    )


@dataclass(frozen=True, slots=True)
class BatchPreview:
    """Everything batch/sweep preview prints — built with zero database
    writes and zero model calls."""

    plan: BatchPlan
    total_estimated_usd_by_model: dict[str, float]
    total_estimated_usd: float
    scored_count: int
    unscored_count: int
    no_op_count: int
    unpriceable_count: int
    selected_count: int
    skipped_count: int
    max_llm_calls_per_case: int
    aggregate_max_llm_calls: int


async def preview_batch_replay(
    *,
    state: StateManager,
    tracer: TracingLogger,
    selector: BatchSelector,
    variants: tuple[BatchVariant, ...],
    skip_policy: SkipPolicy,
    pricing_catalog: PricingCatalog | None = None,
    dossier_root: Path | str | None = None,
    sweep: SweepDefinition | None = None,
) -> BatchPreview:
    """Read-only batch/sweep preview: resolves the full plan and summarizes
    per-model and total estimated spend, classification totals, and the
    aggregate maximum LLM call ceiling. Never writes to the database and
    never calls a model."""
    catalog = pricing_catalog if pricing_catalog is not None else load_pricing_catalog()
    plan = await build_batch_plan(
        state=state, tracer=tracer, selector=selector, variants=variants,
        skip_policy=skip_policy, pricing_catalog=catalog,
        dossier_root=_resolve_dossier_root(dossier_root), sweep=sweep,
    )
    counts = Counter(pair.classification for pair in plan.pairs)
    totals_by_model: dict[str, float] = {}
    total = 0.0
    for pair in plan.pairs:
        if pair.classification == "scored" and pair.price_estimate is not None:
            totals_by_model[pair.plan.candidate_model] = (
                totals_by_model.get(pair.plan.candidate_model, 0.0)
                + pair.price_estimate.estimated_usd
            )
            total += pair.price_estimate.estimated_usd
    max_calls = PHASE_REPLAY_CONFIGS["reply_draft"].max_llm_calls
    scored = counts.get("scored", 0)
    return BatchPreview(
        plan=plan,
        total_estimated_usd_by_model=totals_by_model,
        total_estimated_usd=total,
        scored_count=scored,
        unscored_count=counts.get("unscored", 0),
        no_op_count=counts.get("no_op", 0),
        unpriceable_count=counts.get("unpriceable", 0),
        selected_count=scored,
        skipped_count=len(plan.pairs) - scored,
        max_llm_calls_per_case=max_calls,
        aggregate_max_llm_calls=max_calls * scored,
    )


def _enforce_skip_policy(plan: BatchPlan) -> None:
    blocked = [
        pair for pair in plan.pairs
        if pair.classification != "scored" and not plan.skip_policy.allows(pair.classification)
    ]
    if not blocked:
        return
    summary = ", ".join(
        f"phase_run_id={pair.phase_run_id} variant={pair.variant_name!r} ({pair.classification})"
        for pair in blocked[:10]
    )
    more = "" if len(blocked) <= 10 else f", and {len(blocked) - 10} more"
    raise NonExecutablePopulationError(
        f"{len(blocked)} non-executable pair(s) blocked execution: {summary}{more}; "
        "pass the matching --skip-unscored/--skip-no-op/--skip-unpriceable flag to exclude them"
    )


def build_batch_candidate_config(
    *,
    phase: str,
    variant_name: str,
    model_override: str | None,
    system_prompt_override: str | None,
    grader_attached: bool,
    sweep: SweepDefinition | None,
    plan_sha256: str,
    phase_run_ids: tuple[int, ...],
    dropped_duplicate_phase_run_ids: tuple[int, ...],
    skipped_pairs: tuple[dict[str, Any], ...],
) -> str:
    """Serialize one batch/sweep experiment_runs parent's shared override
    policy (see BATCH_CANDIDATE_CONFIG_VERSION for why the resolved
    candidate text differs from the single-replay CLI's fully-resolved v2
    shape), plus the authorized plan identity and this variant's full
    pair evidence — the durable record a report reconstructs correction
    coverage, exclusions, and common populations from, since a skipped
    pair never produces its own evaluation_experiments row.

    `phase_run_ids`/`dropped_duplicate_phase_run_ids` are the authorized
    plan's full resolved population, identical across every variant of one
    batch/sweep. `skipped_pairs` is this variant's own non-'scored' pairs
    only — each a plain dict with phase_run_id/classification/reason/
    baseline_model/baseline_prompt_sha256, never a raw prompt/correction
    value — carrying enough identity for a report to segment a skipped
    pair exactly as it would an attempted one.
    """
    return _canonical_json(
        {
            "version": BATCH_CANDIDATE_CONFIG_VERSION,
            "phase": phase,
            "variant_name": variant_name,
            "model_override": model_override,
            "system_prompt_override": system_prompt_override,
            "system_prompt_override_sha256": (
                _sha256_utf8(system_prompt_override) if system_prompt_override is not None else None
            ),
            "grader_attached": grader_attached,
            "sweep": (
                {"name": sweep.name, "axis": sweep.axis, "version": SWEEP_SCHEMA_VERSION}
                if sweep is not None
                else None
            ),
            "plan_sha256": plan_sha256,
            "phase_run_ids": sorted(phase_run_ids),
            "dropped_duplicate_phase_run_ids": sorted(dropped_duplicate_phase_run_ids),
            "skipped_pairs": [
                {
                    "phase_run_id": pair["phase_run_id"],
                    "classification": pair["classification"],
                    "reason": pair["reason"],
                    "baseline_model": pair["baseline_model"],
                    "baseline_prompt_sha256": pair["baseline_prompt_sha256"],
                }
                for pair in sorted(skipped_pairs, key=lambda p: p["phase_run_id"])
            ],
        }
    )


def replay_worker_configuration(plan: CandidateReplayPlan) -> ReplayWorkerConfiguration:
    """Mirror the phase runner settings used by _build_candidate_agent_config."""
    phase = PHASE_REPLAY_CONFIGS[plan.phase]
    return ReplayWorkerConfiguration(
        phase=plan.phase, model=plan.candidate_model,
        system_prompt_sha256=plan.candidate_prompt_sha256,
        output_schema_sha256=_sha256_utf8(_canonical_json(phase.output_schema.model_json_schema())),
        max_tool_calls=phase.max_tool_calls, max_llm_calls=phase.max_llm_calls,
        max_parse_retries=phase.max_parse_retries, jig_revision=JIG_REVISION,
        grader_version=NORMALIZED_EDIT_DISTANCE_GRADER_VERSION if plan.grader_attached else None,
        assembler_version=DRAFT_TEXT_ASSEMBLER_VERSION,
        tools=(), include_memory_in_prompt=False, include_feedback_in_prompt=False,
    )


def build_batch_case_evidence(
    case: BatchCase,
    plan: CandidateReplayPlan,
    oracle: ReplyCorrectionOracle,
    price_estimate: PriceEstimate | None,
) -> str:
    """Serialize one batch/sweep case's fully-resolved candidate identity,
    pinned correction oracle, and repriced spend estimate at authorization
    time (see BATCH_CASE_EVIDENCE_VERSION) — the evidence a batch/sweep
    child (evaluation_experiments.baseline_evidence) carries, since the
    parent's candidate_config only records the override *policy*."""
    return _canonical_json(
        {
            "version": BATCH_CASE_EVIDENCE_VERSION,
            "recorded_input_sha256": plan.recorded_input_sha256,
            "baseline_model": case.baseline.baseline_model,
            "baseline_prompt_sha256": plan.baseline_prompt_sha256,
            "baseline_prompt_reused": plan.baseline_prompt_reused,
            "candidate_model": plan.candidate_model,
            "candidate_prompt_sha256": plan.candidate_prompt_sha256,
            "worker_configuration": dataclasses.asdict(replay_worker_configuration(plan)),
            "estimated_usd": price_estimate.estimated_usd if price_estimate is not None else None,
            "reply_revision_id": oracle.reply_revision_id,
            "correction_sha256": oracle.correction_sha256,
            "project_key": oracle.project_key,
            "dossier_summary_id": oracle.dossier_summary_id,
            "dossier_revision": oracle.dossier_revision,
            "grader_version": NORMALIZED_EDIT_DISTANCE_GRADER_VERSION,
            "assembler_version": DRAFT_TEXT_ASSEMBLER_VERSION,
        }
    )


@dataclass(frozen=True, slots=True)
class BatchAttemptOutcome:
    """The durable result of one executed (case, variant) attempt."""

    phase_run_id: int
    variant_name: str
    status: str
    experiment_id: int
    candidate_trace_id: str | None
    candidate_llm_call_count: int | None
    candidate_cost: float | None
    error_detail: str | None


@dataclass(frozen=True, slots=True)
class BatchExecutionOutcome:
    """The durable result of one execute_batch_replay or retry_batch_replay
    call: every variant's experiment_runs parent id, and every attempt
    actually executed."""

    experiment_run_ids: dict[str, int]
    attempts: tuple[BatchAttemptOutcome, ...]


async def _execute_one_batch_attempt(
    *,
    state: StateManager,
    tracer: TracingLogger,
    feedback: FeedbackLoop,
    experiment_run_id: int,
    case: BatchCase,
    pair: PairClassification,
    supersedes_experiment_id: int | None = None,
) -> BatchAttemptOutcome:
    """Execute one already-classified 'scored' pair as a new attempt under
    `experiment_run_id`. A ReplayError raised anywhere in the shared
    execution core is caught here (never propagated) and reported as a
    'failed' BatchAttemptOutcome — batch execution is resilient to one
    case's failure; the caller loop continues to the next case."""
    plan = pair.plan
    oracle = case.oracle
    assert oracle is not None, "a 'scored' pair always has a resolved oracle"

    baseline_draft = _resolve_baseline_structured_draft(case.baseline.root_span)
    baseline_text = assemble_draft_text(baseline_draft, oracle.dossier)
    baseline_distance = normalized_edit_distance(baseline_text, oracle.correction_text)

    baseline_evidence_json = build_batch_case_evidence(case, plan, oracle, pair.price_estimate)
    experiment_id = state.insert_experiment_attempt(
        experiment_run_id=experiment_run_id,
        phase_run_id=case.phase_run_id,
        baseline_evidence=baseline_evidence_json,
        supersedes_experiment_id=supersedes_experiment_id,
    )
    state.cas_experiment_to_running(experiment_id)

    try:
        candidate_trace_id, llm_call_count, candidate_cost = await _run_candidate_and_complete(
            state=state, tracer=tracer, feedback=feedback, experiment_id=experiment_id,
            baseline=case.baseline, plan=plan, oracle=oracle, baseline_distance=baseline_distance,
        )
    except ReplayError as exc:
        current = state.get_experiment(experiment_id)
        assert current is not None
        return BatchAttemptOutcome(
            phase_run_id=case.phase_run_id, variant_name=pair.variant_name, status="failed",
            experiment_id=experiment_id, candidate_trace_id=current["candidate_trace_id"],
            candidate_llm_call_count=current["candidate_llm_call_count"],
            candidate_cost=current["candidate_cost"], error_detail=str(exc),
        )

    return BatchAttemptOutcome(
        phase_run_id=case.phase_run_id, variant_name=pair.variant_name, status="complete",
        experiment_id=experiment_id, candidate_trace_id=candidate_trace_id,
        candidate_llm_call_count=llm_call_count, candidate_cost=candidate_cost, error_detail=None,
    )


async def execute_batch_replay(
    *,
    state: StateManager,
    tracer: TracingLogger,
    feedback: FeedbackLoop,
    name: str,
    selector: BatchSelector,
    variants: tuple[BatchVariant, ...],
    skip_policy: SkipPolicy,
    authorize_plan_sha256: str,
    pricing_catalog: PricingCatalog | None = None,
    dossier_root: Path | str | None = None,
    sweep: SweepDefinition | None = None,
) -> BatchExecutionOutcome:
    """Execute one explicitly authorized batch or sweep replay end to end.

    Recomputes the exact same canonical plan preview would, and requires
    `authorize_plan_sha256` to match its hash byte-for-byte — any change to
    the population, a grade pointer, a candidate override, the pricing
    catalog, or the skip policy since preview changes the hash and fails
    this check before any row insertion or provider call. Then refuses to
    proceed if any non-'scored' pair is not explicitly excluded by
    `skip_policy`. Opens one experiment_runs parent per variant; each
    scored pair becomes one immutable evaluation_experiments attempt under
    its variant's parent. One case's failure never aborts the batch — every
    other case still executes, and the parent's projected status reflects
    exactly what happened (see StateManager._recompute_experiment_run_status).
    """
    catalog = pricing_catalog if pricing_catalog is not None else load_pricing_catalog()
    root = _resolve_dossier_root(dossier_root)
    plan = await build_batch_plan(
        state=state, tracer=tracer, selector=selector, variants=variants,
        skip_policy=skip_policy, pricing_catalog=catalog, dossier_root=root, sweep=sweep,
    )
    if plan.plan_sha256 != authorize_plan_sha256:
        raise PlanAuthorizationError(
            f"--authorize-plan-sha256 {authorize_plan_sha256!r} does not match the recomputed "
            f"canonical plan hash {plan.plan_sha256!r} -- the population, configuration, "
            "pricing catalog, or skip policy changed since preview; re-run preview and "
            "re-authorize before spending"
        )
    _enforce_skip_policy(plan)

    experiment_run_ids: dict[str, int] = {}
    attempts: list[BatchAttemptOutcome] = []
    for variant in plan.variants:
        skipped_pairs = tuple(
            {
                "phase_run_id": pair.phase_run_id,
                "classification": pair.classification,
                "reason": pair.reason,
                "baseline_model": plan.cases[pair.phase_run_id].baseline.baseline_model,
                "baseline_prompt_sha256": pair.plan.baseline_prompt_sha256,
            }
            for pair in plan.pairs
            if pair.variant_name == variant.name and pair.classification != "scored"
        )
        candidate_config_json = build_batch_candidate_config(
            phase="reply_draft", variant_name=variant.name,
            model_override=variant.model_override,
            system_prompt_override=variant.system_prompt_override,
            grader_attached=True, sweep=sweep, plan_sha256=plan.plan_sha256,
            phase_run_ids=plan.phase_run_ids,
            dropped_duplicate_phase_run_ids=plan.dropped_duplicate_phase_run_ids,
            skipped_pairs=skipped_pairs,
        )
        run_name = name if variant.name == DEFAULT_BATCH_VARIANT_NAME else f"{name}:{variant.name}"
        experiment_run_id = state.create_experiment_run(
            name=run_name, candidate_config=candidate_config_json,
        )
        experiment_run_ids[variant.name] = experiment_run_id

        attempted_variant = False
        for phase_run_id in plan.phase_run_ids:
            pair = plan.pair_for(phase_run_id, variant.name)
            if pair.classification != "scored":
                continue
            attempted_variant = True
            outcome = await _execute_one_batch_attempt(
                state=state, tracer=tracer, feedback=feedback,
                experiment_run_id=experiment_run_id, case=plan.cases[phase_run_id], pair=pair,
            )
            attempts.append(outcome)
        if not attempted_variant:
            state.complete_experiment_run_without_attempts(experiment_run_id)

    return BatchExecutionOutcome(experiment_run_ids=experiment_run_ids, attempts=tuple(attempts))


def _parse_batch_candidate_config(candidate_config_json: str) -> dict[str, Any]:
    try:
        document = json.loads(candidate_config_json)
    except json.JSONDecodeError as exc:
        raise RetryResolutionError(
            f"experiment_runs.candidate_config is not valid JSON: {exc}"
        ) from exc
    if not isinstance(document, dict) or document.get("version") != BATCH_CANDIDATE_CONFIG_VERSION:
        raise RetryResolutionError(
            f"experiment_runs.candidate_config version {document.get('version')!r} is not a "
            f"batch/sweep candidate_config (expected {BATCH_CANDIDATE_CONFIG_VERSION!r}); only a "
            "batch or sweep experiment_runs parent can be retried this way"
        )
    return document


async def retry_batch_replay(
    *,
    state: StateManager,
    tracer: TracingLogger,
    feedback: FeedbackLoop,
    experiment_run_id: int,
    phase_run_ids: tuple[int, ...] | None = None,
    pricing_catalog: PricingCatalog | None = None,
    dossier_root: Path | str | None = None,
) -> BatchExecutionOutcome:
    """Retry every (or a caller-selected subset of) failed latest-attempt
    cases under one existing batch/sweep experiment_runs parent, as new
    linked attempts (supersedes_experiment_id set) — never a fresh case,
    and never a case whose latest attempt is not 'failed'. Reuses the
    parent's own stored candidate_config verbatim (its model/prompt
    override policy), so a retry can never silently drift from what was
    originally authorized.
    """
    run = state.get_experiment_run(experiment_run_id)
    if run is None:
        raise RetryResolutionError(f"no experiment_runs row with id={experiment_run_id}")
    config = _parse_batch_candidate_config(run["candidate_config"])
    model_override = config.get("model_override")
    system_prompt_override = config.get("system_prompt_override")
    variant_name = config.get("variant_name", DEFAULT_BATCH_VARIANT_NAME)

    attempts = state.list_experiment_attempts(experiment_run_id)
    latest_by_case: dict[int, dict[str, Any]] = {}
    for attempt in attempts:
        current = latest_by_case.get(attempt["phase_run_id"])
        if current is None or attempt["attempt_number"] > current["attempt_number"]:
            latest_by_case[attempt["phase_run_id"]] = attempt

    failed_cases = {
        phase_run_id: attempt
        for phase_run_id, attempt in latest_by_case.items()
        if attempt["status"] == "failed"
    }
    if phase_run_ids is not None:
        unknown = sorted(set(phase_run_ids) - set(latest_by_case))
        if unknown:
            raise RetryResolutionError(
                f"phase_run_id(s) {unknown} are not part of experiment_run {experiment_run_id}"
            )
        not_failed = sorted(pid for pid in phase_run_ids if pid not in failed_cases)
        if not_failed:
            raise RetryResolutionError(
                f"phase_run_id(s) {not_failed} are not experiment_run {experiment_run_id}'s "
                "latest failed attempt"
            )
        failed_cases = {pid: failed_cases[pid] for pid in phase_run_ids}

    if not failed_cases:
        raise RetryResolutionError(
            f"experiment_run {experiment_run_id} has no failed cases to retry"
        )

    catalog = pricing_catalog if pricing_catalog is not None else load_pricing_catalog()
    root = _resolve_dossier_root(dossier_root)

    attempts_out: list[BatchAttemptOutcome] = []
    for phase_run_id, failed_attempt in sorted(failed_cases.items()):
        try:
            pinned_evidence = json.loads(failed_attempt["baseline_evidence"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise RetryResolutionError(
                f"phase_run_id={phase_run_id} has malformed pinned baseline evidence"
            ) from exc
        if not isinstance(pinned_evidence, dict):
            raise RetryResolutionError(
                f"phase_run_id={phase_run_id} has malformed pinned baseline evidence"
            )
        case = await _resolve_batch_case(state, tracer, phase_run_id, dossier_root=root)
        if case.oracle is None:
            raise RetryResolutionError(
                f"phase_run_id={phase_run_id} no longer resolves a correction oracle: "
                f"{case.oracle_error}"
            )
        plan = build_candidate_plan(
            case.baseline, model_override=model_override,
            system_prompt_override=system_prompt_override,
        )
        if plan.is_no_op:
            raise RetryResolutionError(
                f"phase_run_id={phase_run_id} is now a no-op candidate on retry"
            )
        price_estimate = price_pair(case.baseline_usage, plan.candidate_model, catalog)
        if price_estimate is None:
            raise RetryResolutionError(
                f"phase_run_id={phase_run_id} is no longer priceable on retry"
            )
        pair = PairClassification(
            phase_run_id=phase_run_id, variant_name=variant_name, classification="scored",
            reason=None, plan=plan, price_estimate=price_estimate,
        )
        current_evidence = json.loads(
            build_batch_case_evidence(case, plan, case.oracle, price_estimate)
        )
        if current_evidence != pinned_evidence:
            changed_fields = sorted(
                key
                for key in set(current_evidence) | set(pinned_evidence)
                if current_evidence.get(key) != pinned_evidence.get(key)
            )
            raise RetryResolutionError(
                f"phase_run_id={phase_run_id} no longer matches its pinned failed-attempt "
                f"evidence (changed fields: {changed_fields}); start and authorize a new "
                "batch instead of retrying this run"
            )
        outcome = await _execute_one_batch_attempt(
            state=state, tracer=tracer, feedback=feedback, experiment_run_id=experiment_run_id,
            case=case, pair=pair, supersedes_experiment_id=failed_attempt["id"],
        )
        attempts_out.append(outcome)

    return BatchExecutionOutcome(
        experiment_run_ids={variant_name: experiment_run_id}, attempts=tuple(attempts_out),
    )


__all__ = [
    "BASELINE_EVIDENCE_VERSION",
    "BATCH_CANDIDATE_CONFIG_VERSION",
    "BATCH_CASE_EVIDENCE_VERSION",
    "CANDIDATE_CONFIG_VERSION",
    "DEFAULT_BATCH_VARIANT_NAME",
    "JIG_REVISION",
    "PHASE_REPLAY_CONFIGS",
    "PLAN_SCHEMA_VERSION",
    "SWEEP_SCHEMA_PATH",
    "SWEEP_SCHEMA_VERSION",
    "BaselineRecord",
    "BaselineResolutionError",
    "BatchAttemptOutcome",
    "BatchCase",
    "BatchExecutionOutcome",
    "BatchPlan",
    "BatchPopulation",
    "BatchPreview",
    "BatchSelector",
    "BatchVariant",
    "CandidateExecutionError",
    "CandidateReplayPlan",
    "CandidateTraceEvidenceError",
    "ComparisonConstructionError",
    "CorrectionEvidenceIntegrityError",
    "CorrectionOracleResolutionError",
    "ExperimentOutcome",
    "ModelResolutionError",
    "NoOpReplayError",
    "NonExecutablePopulationError",
    "PairClassification",
    "PhaseReplayConfig",
    "PlanAuthorizationError",
    "ReplayError",
    "ReplayPreview",
    "ReplyCorrectionOracle",
    "RetryResolutionError",
    "SelectorResolutionError",
    "SkipPolicy",
    "SweepDefinition",
    "SweepValidationError",
    "SweepVariant",
    "batch_variants_for_sweep",
    "build_baseline_evidence",
    "build_batch_candidate_config",
    "build_batch_case_evidence",
    "build_candidate_plan",
    "build_domain_diff",
    "build_score_evidence",
    "execute_batch_replay",
    "execute_replay",
    "load_and_validate_sweep",
    "load_sweep_document",
    "preview_batch_replay",
    "preview_replay",
    "resolve_baseline",
    "resolve_batch_population",
    "resolve_reply_correction_oracle",
    "retry_batch_replay",
    "validate_sweep_document",
]
