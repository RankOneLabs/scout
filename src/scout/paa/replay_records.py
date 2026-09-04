"""Read-only PAA projection of durable reply_draft batch replay attempts.

Source models mirror evaluation_experiments, experiment_runs.candidate_config,
baseline_evidence, trace_comparisons.score_evidence, and Jig LLM_CALL spans.
Output records mirror paa-contracts 0.2.0. Costs are catalog estimates over
candidate usage, not billed actuals; raw recorded costs remain in provenance.
No verdict here claims acceptance, Phase 1 qualification, or authority.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal

from jig import Span, SpanKind, TracingLogger
from paa_runtime import (
    OperatingPrice,
    OperatingRecord,
    RecordSubject,
    RecordTimestamps,
    WorkerIdentity,
    decode_operating_record,
)
from paa_runtime.declarations import PaaEvaluationBasis, PaaEvaluator
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scout.grading.correction import NORMALIZED_EDIT_DISTANCE_GRADER_VERSION
from scout.replay.experiments import ReplayWorkerConfiguration
from scout.replay.pricing import ModelRate, PricingCatalog
from scout.result import Err, Ok, Result
from scout.storage.state import StateManager

EXPORT_SCHEMA = "scout-paa-replay/1"
PAYLOAD_SCHEMA = "urn:scout:reply-draft-measurement:1"


class SourceModel(BaseModel):
    """Typed boundary reader; unrelated persisted fields are not projected."""

    model_config = ConfigDict(extra="ignore", frozen=True, allow_inf_nan=False)


@dataclass(frozen=True, slots=True)
class SkippedPair:
    """One durable candidate_config.skipped_pairs entry."""

    phase_run_id: int
    classification: Literal["no_op", "unscored", "unpriceable"]
    reason: str | None
    baseline_model: str
    baseline_prompt_sha256: str


class ReplayParent(SourceModel):
    """Batch candidate_config v4; raw prompt text is deliberately excluded."""

    version: Literal[4]
    phase: Literal["reply_draft"]
    variant_name: str
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    phase_run_ids: tuple[int, ...]
    dropped_duplicate_phase_run_ids: tuple[int, ...]
    skipped_pairs: tuple[SkippedPair, ...]


class ReplayCase(SourceModel):
    """build_batch_case_evidence v1, using pinned identities rather than live grades."""

    version: Literal[1]
    recorded_input_sha256: str
    baseline_model: str
    baseline_prompt_sha256: str
    candidate_model: str
    candidate_prompt_sha256: str
    correction_sha256: str
    reply_revision_id: int
    grader_version: Literal["normalized_edit_distance/v1"]
    assembler_version: str
    worker_configuration: ReplayWorkerConfiguration | None = None


class ReplayAttempt(SourceModel):
    """Terminal evaluation_experiments row; includes superseded failed attempts."""

    id: int
    experiment_run_id: int
    phase_run_id: int
    attempt_number: int
    supersedes_experiment_id: int | None
    status: Literal["complete", "failed"]
    baseline_evidence: str
    candidate_trace_id: str | None
    candidate_cost: float | None = Field(ge=0)
    created_at: datetime
    completed_at: datetime


class ReplayScore(SourceModel):
    """Immutable normalized-edit-distance score evidence, not an acceptance rule."""

    grader_version: Literal["normalized_edit_distance/v1"]
    assembler_version: str
    correction_sha256: str
    reply_revision_id: int
    baseline_distance: float = Field(ge=0, le=1)
    candidate_distance: float = Field(ge=0, le=1)
    grader_attached: Literal[True]


@dataclass(frozen=True, slots=True)
class ReplayRecordError:
    operation: str
    entity_id: str
    detail: str


@dataclass(frozen=True, slots=True)
class CallUsageSource:
    """One constituent LLM_CALL, never an AGENT_RUN aggregate."""

    span_id: str
    trace_id: str
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    recorded_cost_usd: float | None
    catalog_rate: ModelRate | None
    catalog_estimate_usd: float | None


@dataclass(frozen=True, slots=True)
class AttemptSource:
    experiment_run_id: int
    experiment_id: int
    phase_run_id: int
    attempt_number: int
    supersedes_experiment_id: int | None
    status: str
    plan_sha256: str
    variant_name: str
    baseline_model: str
    baseline_prompt_sha256: str
    recorded_input_sha256: str
    configuration: ReplayWorkerConfiguration
    trace_id: str | None
    calls: tuple[CallUsageSource, ...]
    recorded_attempt_cost_usd: float | None
    pricing_catalog: PricingCatalog
    price_kind: Literal["catalog_estimate"] = "catalog_estimate"
    recorded_cost_kind: Literal["unverified_recorded_cost"] = "unverified_recorded_cost"


@dataclass(frozen=True, slots=True)
class SourceArtifact:
    reference: str
    content: AttemptSource


@dataclass(frozen=True, slots=True)
class EvidenceBoundary:
    input_ref: str
    output_ref: str | None


@dataclass(frozen=True, slots=True)
class ProducerIdentity:
    id: str
    version: str


@dataclass(frozen=True, slots=True)
class MeasurementVerdict:
    value: Literal["scored"]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReplayMeasurement:
    baseline_distance: float
    candidate_distance: float
    correction_sha256: str
    reply_revision_id: int
    assembler_version: str


@dataclass(frozen=True, slots=True)
class ReplayEvidenceRecord:
    """paa-evidence-record/0.2.0-draft envelope, typed by the shipped contract."""

    record_schema: str
    record_id: str
    task: str
    declaration_version: int
    scope: None
    subject: RecordSubject
    boundary: EvidenceBoundary
    evaluator: PaaEvaluator
    verdict: MeasurementVerdict
    producer: ProducerIdentity
    worker: WorkerIdentity
    timestamps: RecordTimestamps
    source_references: tuple[str, ...]
    payload_schema: str
    payload: ReplayMeasurement


@dataclass(frozen=True, slots=True)
class ReplayPaaExport:
    schema: str
    task: str
    deployment: str
    plan_sha256: str
    population_phase_run_ids: tuple[int, ...]
    variants: tuple[VariantCoverage, ...]
    operating_records: tuple[OperatingRecord, ...]
    evidence_records: tuple[ReplayEvidenceRecord, ...]
    sources: tuple[SourceArtifact, ...]
    acceptance_rule: None = None
    effective_cost: None = None
    operating_decision: None = None


@dataclass(frozen=True, slots=True)
class VariantCoverage:
    """Population and accounting coverage, separately for each exported variant."""

    experiment_run_id: int
    variant_name: str
    dropped_duplicate_phase_run_ids: tuple[int, ...]
    skipped_pairs: tuple[SkippedPair, ...]
    missing_phase_run_ids: tuple[int, ...]
    attempt_count: int
    failed_attempt_count: int
    priced_attempt_count: int
    unpriced_attempt_count: int


@dataclass(frozen=True, slots=True)
class AttemptSnapshot:
    parent: ReplayParent
    attempt: ReplayAttempt
    case: ReplayCase
    score: ReplayScore | None
    jig_revision: str | None


def _hash_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _utc_stamp(value: datetime) -> str:
    # Scout's stored datetimes are UTC; older SQLite rows may omit the suffix.
    return (
        value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    ).isoformat()


def project_call_usage(
    span: Span, configured_model: str, catalog: PricingCatalog
) -> CallUsageSource:
    """Price each call separately, retaining the model and exact rate allocation."""
    model_value = (span.metadata or {}).get("model")
    model = model_value if isinstance(model_value, str) and model_value else None
    # Jig removes only the routing prefix for OpenRouter trace identities.
    rate_model = (
        configured_model if model == configured_model.removeprefix("openrouter/") else model
    )
    rate = catalog.rate_for(rate_model) if rate_model is not None else None
    usage = span.usage
    estimate = None
    if usage is not None and rate is not None:
        estimate = (
            usage.input_tokens * rate.input_usd_per_million
            + usage.output_tokens * rate.output_usd_per_million
        ) / 1_000_000
    return CallUsageSource(
        span.id,
        span.trace_id,
        model,
        usage.input_tokens if usage is not None else None,
        usage.output_tokens if usage is not None else None,
        usage.cost if usage is not None else None,
        rate,
        estimate,
    )


def trace_has_single_rooted_tree(spans: Sequence[Span]) -> bool:
    """Reject orphaned/cyclic constituents rather than attribute their usage."""
    by_id = {span.id: span for span in spans}
    for span in spans:
        visited: set[str] = set()
        current = span
        while current.parent_id is not None:
            if current.id in visited or current.parent_id not in by_id:
                return False
            visited.add(current.id)
            current = by_id[current.parent_id]
        if current.kind != SpanKind.AGENT_RUN:
            return False
    return True


def project_operating_record(
    attempt: ReplayAttempt,
    source: SourceArtifact,
    subject: RecordSubject,
) -> OperatingRecord:
    """Exactly one accounting record per attempt, independent of verdict count."""
    calls = source.content.calls
    usage: dict[str, int | float | None] | None = None
    price: OperatingPrice | None = None
    if calls:
        usage = {
            "llm_calls": len(calls),
            "input_tokens": (
                sum(call.input_tokens for call in calls if call.input_tokens is not None)
                if all(call.input_tokens is not None for call in calls)
                else None
            ),
            "output_tokens": (
                sum(call.output_tokens for call in calls if call.output_tokens is not None)
                if all(call.output_tokens is not None for call in calls)
                else None
            ),
        }
        if all(call.catalog_estimate_usd is not None for call in calls):
            price = OperatingPrice(
                currency="USD",
                amount=sum(
                    call.catalog_estimate_usd
                    for call in calls
                    if call.catalog_estimate_usd is not None
                ),
                basis="urn:scout:replay-pricing:sha256:"
                + source.content.pricing_catalog.catalog_hash,
            )
    return OperatingRecord(
        record_schema="paa-operating-record/0.1.0-draft",
        record_id="scout-operating:" + source.reference.rsplit(":", 1)[-1],
        task="reply_draft",
        declaration_version=1,
        scope=None,
        subject=subject,
        worker=WorkerIdentity(
            id="scout-reply-drafter",
            version="replay/v1",
            configuration_ref="urn:scout:configuration:sha256:"
            + _hash_json(asdict(source.content.configuration)),
        ),
        usage=usage,
        price=price,
        timestamps=RecordTimestamps(
            started_at=_utc_stamp(attempt.created_at),
            completed_at=_utc_stamp(attempt.completed_at),
            recorded_at=_utc_stamp(attempt.completed_at),
        ),
        source_references=[source.reference],
    )


def project_evidence_record(
    score: ReplayScore,
    operating: OperatingRecord,
    source: AttemptSource,
) -> ReplayEvidenceRecord:
    """A measured distance is a scored verdict, not a fabricated pass/fail threshold."""
    return ReplayEvidenceRecord(
        record_schema="paa-evidence-record/0.2.0-draft",
        record_id=operating["record_id"].replace("scout-operating:", "scout-evidence:"),
        task="reply_draft",
        declaration_version=1,
        scope=None,
        subject=operating["subject"],
        boundary=EvidenceBoundary(
            input_ref="urn:scout:replay-input:sha256:" + source.recorded_input_sha256,
            output_ref="urn:scout:trace:" + source.trace_id if source.trace_id else None,
        ),
        evaluator=PaaEvaluator(
            property="correction_distance",
            target="output",
            technique="deterministic",
            evaluation_basis=PaaEvaluationBasis(
                kind="reference_label", ref="pinned_reply_correction"
            ),
            epistemic_status="proxy",
            version=NORMALIZED_EDIT_DISTANCE_GRADER_VERSION,
            authority="advisory",
        ),
        verdict=MeasurementVerdict("scored", ("distance_measured_not_acceptance",)),
        producer=ProducerIdentity("scout-reply-correction-grader", score.grader_version),
        worker=operating["worker"],
        timestamps=operating["timestamps"],
        source_references=tuple(operating["source_references"]),
        payload_schema=PAYLOAD_SCHEMA,
        payload=ReplayMeasurement(
            score.baseline_distance,
            score.candidate_distance,
            score.correction_sha256,
            score.reply_revision_id,
            score.assembler_version,
        ),
    )


async def build_replay_paa_export(
    state: StateManager,
    tracer: TracingLogger,
    *,
    experiment_run_ids: Sequence[int],
    catalog: PricingCatalog,
) -> Result[ReplayPaaExport, ReplayRecordError]:
    """Read durable rows/traces only. No model calls, source writes, or policy decisions."""
    try:
        return await _read_replay_export(state, tracer, experiment_run_ids, catalog)
    except ValidationError as error:
        # Pydantic's default message includes raw rejected input (possibly prompt text).
        detail = "; ".join(
            f"{'.'.join(map(str, item['loc']))}: {item['type']}"
            for item in error.errors(include_input=False, include_context=False)
        )
        return Err(ReplayRecordError("export_replay", str(list(experiment_run_ids)), detail))
    except (ValueError, TypeError, OSError) as error:
        return Err(ReplayRecordError("export_replay", str(list(experiment_run_ids)), str(error)))


async def _read_replay_export(
    state: StateManager,
    tracer: TracingLogger,
    run_ids: Sequence[int],
    catalog: PricingCatalog,
) -> Result[ReplayPaaExport, ReplayRecordError]:
    if not run_ids or len(set(run_ids)) != len(run_ids):
        return Err(
            ReplayRecordError("export_replay", str(list(run_ids)), "empty or duplicate run IDs")
        )
    parents: list[tuple[int, ReplayParent]] = []
    attempts: list[AttemptSnapshot] = []
    with state.db.read_transaction():
        for run_id in sorted(run_ids):
            run = state.get_experiment_run(run_id)
            if run is None:
                return Err(ReplayRecordError("read_run", str(run_id), "run not found"))
            parent = ReplayParent.model_validate_json(run["candidate_config"])
            parents.append((run_id, parent))
            for row in state.list_experiment_attempts(run_id):
                attempt = ReplayAttempt.model_validate(row)
                case = ReplayCase.model_validate_json(attempt.baseline_evidence)
                comparison = state.get_trace_comparison(attempt.id)
                score = None
                revision = None
                if comparison is not None:
                    revision = comparison["jig_revision"]
                    if comparison["score_evidence"] is not None:
                        score = ReplayScore.model_validate_json(comparison["score_evidence"])
                if attempt.status == "complete" and score is None:
                    return Err(ReplayRecordError("read_score", str(attempt.id), "missing score"))
                if attempt.status == "failed" and score is not None:
                    return Err(
                        ReplayRecordError("read_score", str(attempt.id), "failed with score")
                    )
                if score is not None and (
                    score.correction_sha256 != case.correction_sha256
                    or score.reply_revision_id != case.reply_revision_id
                    or score.assembler_version != case.assembler_version
                ):
                    return Err(
                        ReplayRecordError("read_score", str(attempt.id), "pinned identity drift")
                    )
                attempts.append(AttemptSnapshot(parent, attempt, case, score, revision))
    if len({parent.plan_sha256 for _, parent in parents}) != 1:
        return Err(ReplayRecordError("export_replay", str(list(run_ids)), "mixed authorized plans"))
    if len({parent.phase_run_ids for _, parent in parents}) != 1:
        return Err(ReplayRecordError("export_replay", str(list(run_ids)), "population drift"))
    sources: list[SourceArtifact] = []
    operating: list[OperatingRecord] = []
    evidence: list[ReplayEvidenceRecord] = []
    for snapshot in attempts:
        parent, attempt, case = snapshot.parent, snapshot.attempt, snapshot.case
        score, revision = snapshot.score, snapshot.jig_revision
        if attempt.phase_run_id not in parent.phase_run_ids:
            return Err(ReplayRecordError("read_attempt", str(attempt.id), "outside population"))
        spans = (
            await tracer.get_trace(attempt.candidate_trace_id) if attempt.candidate_trace_id else []
        )
        if spans and (
            len({span.id for span in spans}) != len(spans)
            or any(span.trace_id != attempt.candidate_trace_id for span in spans)
        ):
            return Err(
                ReplayRecordError("read_trace", str(attempt.id), "duplicate or foreign spans")
            )
        roots = [
            span for span in spans if span.parent_id is None and span.kind == SpanKind.AGENT_RUN
        ]
        if spans and (len(roots) != 1 or not trace_has_single_rooted_tree(spans)):
            return Err(ReplayRecordError("read_trace", str(attempt.id), "invalid agent root"))
        captured = case.worker_configuration
        if captured is None:
            return Err(
                ReplayRecordError(
                    "attribute_configuration",
                    str(attempt.id),
                    "historical attempt lacks a complete configuration source; no safe backfill",
                )
            )
        if (
            captured.model != case.candidate_model
            or captured.system_prompt_sha256 != case.candidate_prompt_sha256
            or captured.phase != "reply_draft"
            or captured.grader_version != case.grader_version
            or captured.assembler_version != case.assembler_version
            or (revision is not None and captured.jig_revision != revision)
        ):
            return Err(
                ReplayRecordError("attribute_configuration", str(attempt.id), "identity drift")
            )
        content = AttemptSource(
            attempt.experiment_run_id,
            attempt.id,
            attempt.phase_run_id,
            attempt.attempt_number,
            attempt.supersedes_experiment_id,
            attempt.status,
            parent.plan_sha256,
            parent.variant_name,
            case.baseline_model,
            case.baseline_prompt_sha256,
            case.recorded_input_sha256,
            captured,
            attempt.candidate_trace_id,
            tuple(
                project_call_usage(span, case.candidate_model, catalog)
                for span in spans
                if span.kind == SpanKind.LLM_CALL
            ),
            attempt.candidate_cost,
            catalog,
        )
        source = SourceArtifact(
            "urn:scout:replay-attempt:sha256:" + _hash_json(asdict(content)), content
        )
        subject = RecordSubject(
            kind="case", id=f"{parent.plan_sha256}:phase-run:{attempt.phase_run_id}"
        )
        record = project_operating_record(attempt, source, subject)
        decode_operating_record(json.dumps(record, allow_nan=False).encode())
        operating.append(record)
        sources.append(source)
        if score is not None:
            evidence.append(project_evidence_record(score, record, content))
    variants = tuple(
        project_variant_coverage(run_id, parent, sources, operating) for run_id, parent in parents
    )
    return Ok(
        ReplayPaaExport(
            EXPORT_SCHEMA,
            "reply_draft",
            "shadow",
            parents[0][1].plan_sha256,
            parents[0][1].phase_run_ids,
            variants,
            tuple(operating),
            tuple(evidence),
            tuple(sources),
        )
    )


def project_variant_coverage(
    run_id: int,
    parent: ReplayParent,
    sources: Sequence[SourceArtifact],
    records: Sequence[OperatingRecord],
) -> VariantCoverage:
    pairs = [
        (source.content, record)
        for source, record in zip(sources, records, strict=True)
        if source.content.experiment_run_id == run_id
    ]
    accounted = {source.phase_run_id for source, _ in pairs} | {
        skipped.phase_run_id for skipped in parent.skipped_pairs
    }
    priced = sum(record["price"] is not None for _, record in pairs)
    return VariantCoverage(
        run_id,
        parent.variant_name,
        parent.dropped_duplicate_phase_run_ids,
        parent.skipped_pairs,
        tuple(sorted(set(parent.phase_run_ids) - accounted)),
        len(pairs),
        sum(source.status == "failed" for source, _ in pairs),
        priced,
        len(pairs) - priced,
    )


def render_replay_paa_export(bundle: ReplayPaaExport) -> str:
    return json.dumps(asdict(bundle), sort_keys=True, indent=2, allow_nan=False) + "\n"
