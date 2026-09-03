"""evaluation-feedback/v1: phase-specific grading feedback.

Selects recent human grades by graded_at recency (not by originating scan —
that was the legacy bug: a human judgment entered today on an evaluation
from an old scan was silently omitted from `get_recent_grading_signals`),
classifies every in-window grade for eligibility, projects the eligible
population into three phase-specific populations (relevance, reply_draft,
critic), aggregates and bounds evidence for each, and persists one
immutable snapshot per scan before any model call.

The pipeline is eight named pure-ish boundaries, in order:

    load_grade_population        -> tuple[GradePopulationRow, ...]
    classify_feedback_eligibility -> tuple[EligibilityResult, ...]
    project_phase_evidence        -> dict[PhaseName, PhaseProjection]
    aggregate_phase_evidence      -> dict[PhaseName, PhaseAggregate]
    select_phase_examples         -> dict[PhaseName, tuple[FeedbackExample, ...]]
    render_feedback_sections      -> dict[PhaseName, RenderedFeedbackSection]
    persist_feedback_snapshot     -> PersistedFeedbackSnapshot
    load_committed_phase_bundle   -> PhaseFeedbackBundle

`load_grade_population`, `persist_feedback_snapshot`, and
`load_committed_phase_bundle` touch the database; each takes an
already-open `sqlite3.Connection` and never manages its own transaction.
The caller (StateManager) opens exactly one `begin_immediate()`
transaction around selection through persistence so that whole pipeline
reflects one stable view; `load_committed_phase_bundle` is called only
after that transaction has committed, and reads the just-persisted rows
back plainly (no transaction of its own) so it observes exactly what is
durable rather than the in-memory `RenderedFeedbackSection` values from
the same call.

This module never renders text into a prompt. It resolves a per-scan
`FeedbackMode` ("shadow" or "active"), stored verbatim on the snapshot
header. In shadow mode the snapshot is recorded and logged by
id/count/hash only, and no phase row is read back for prompt use. In
active mode the caller loads the committed phase bundle via
`load_committed_phase_bundle` and supplies each phase's stored
`rendered_text` — never a second render — to its own prompt only.

Exclusion precedence (first match wins, applied in this order):

    schema_version                 grade.schema_version != HUMAN_GRADE_SCHEMA_VERSION
    needs_regrade                  grade.needs_regrade
    missing_evaluation_linkage     no evaluations row resolves for the grade
    mismatched_evaluation_identity evaluation.post_id/scan_id disagree with the grade
    shared_contract_invalid        the grade's draft_comments row (if any) disagrees
                                    with its evaluation on project_key,
                                    dossier_summary_id, dossier_revision, or posture
    manual_exclude                 grade_usage_overrides.mode = 'exclude'

A grade that clears every check is globally eligible, subject to
`max_grades`: the newest `max_grades` valid grades are `eligible`; any
further (older) valid grades are excluded with reason `eligible_cap`.

The reply_draft and critic phases share one further population filter
beyond global eligibility — the v1 draft-quality population: relevance
judgment `correct`, linked evaluation `relevant=1`, and a draft_comments
row referencing that evaluation. A globally eligible grade outside that
population is recorded, for those two phases only, as excluded with reason
`not_draft_quality_population`.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Any, Literal, get_args

from scout.config import HUMAN_GRADE_SCHEMA_VERSION

PhaseName = Literal["relevance", "reply_draft", "critic"]
PHASE_NAMES: tuple[PhaseName, ...] = ("relevance", "reply_draft", "critic")

FeedbackMode = Literal["shadow", "active"]

ExclusionReason = Literal[
    "schema_version",
    "needs_regrade",
    "missing_evaluation_linkage",
    "mismatched_evaluation_identity",
    "shared_contract_invalid",
    "manual_exclude",
    "eligible_cap",
    "not_draft_quality_population",
]

# First-match-wins order for the six exclusion checks that apply to every
# in-window grade, regardless of phase. `eligible_cap` and
# `not_draft_quality_population` are not part of this precedence chain —
# they are assigned after a grade has already cleared every check here (by
# the grade cap and by phase projection, respectively).
GLOBAL_EXCLUSION_PRECEDENCE: tuple[ExclusionReason, ...] = (
    "schema_version",
    "needs_regrade",
    "missing_evaluation_linkage",
    "mismatched_evaluation_identity",
    "shared_contract_invalid",
    "manual_exclude",
)

ALL_EXCLUSION_REASONS: tuple[ExclusionReason, ...] = tuple(get_args(ExclusionReason))

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
NO_ELIGIBLE_FEEDBACK_REASON = "no_eligible_feedback"


# --------------------------------------------------------------------------
# Typed values
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeedbackPolicyConfig:
    """Resolved evaluation-feedback/v1 operational parameters. Every field
    is stored verbatim on the persisted snapshot so a rendered snapshot
    stays explainable after environment tuning changes the live defaults."""

    policy_version: str
    lookback_days: int
    max_grades: int
    segment_min_grades: int
    relevance_example_limit: int
    reply_draft_example_limit: int
    critic_example_limit: int
    note_max_chars: int
    relevance_token_budget: int
    reply_draft_token_budget: int
    critic_token_budget: int

    def example_limit(self, phase: PhaseName) -> int:
        return {
            "relevance": self.relevance_example_limit,
            "reply_draft": self.reply_draft_example_limit,
            "critic": self.critic_example_limit,
        }[phase]

    def token_budget(self, phase: PhaseName) -> int:
        return {
            "relevance": self.relevance_token_budget,
            "reply_draft": self.reply_draft_token_budget,
            "critic": self.critic_token_budget,
        }[phase]


def resolve_feedback_policy_config() -> FeedbackPolicyConfig:
    """Build a FeedbackPolicyConfig from config.py's resolved env values."""
    import scout.config as _config

    return FeedbackPolicyConfig(
        policy_version=_config.FEEDBACK_POLICY_VERSION,
        lookback_days=_config.FEEDBACK_LOOKBACK_DAYS,
        max_grades=_config.FEEDBACK_MAX_GRADES,
        segment_min_grades=_config.FEEDBACK_SEGMENT_MIN_GRADES,
        relevance_example_limit=_config.FEEDBACK_RELEVANCE_EXAMPLE_LIMIT,
        reply_draft_example_limit=_config.FEEDBACK_REPLY_DRAFT_EXAMPLE_LIMIT,
        critic_example_limit=_config.FEEDBACK_CRITIC_EXAMPLE_LIMIT,
        note_max_chars=_config.FEEDBACK_NOTE_MAX_CHARS,
        relevance_token_budget=_config.FEEDBACK_RELEVANCE_TOKEN_BUDGET,
        reply_draft_token_budget=_config.FEEDBACK_REPLY_DRAFT_TOKEN_BUDGET,
        critic_token_budget=_config.FEEDBACK_CRITIC_TOKEN_BUDGET,
    )


@dataclass(frozen=True, slots=True)
class GradePopulationRow:
    """One in-window grade plus everything needed to classify it and
    project it into phase evidence, read as one consistent snapshot."""

    grade_id: int
    post_id: int
    scan_id: int | None
    graded_at: str
    schema_version: int
    needs_regrade: bool
    relevance_judgment: str
    action_judgment: str | None
    # Parsed JSON value from grades.dimensions. Valid grades carry a list of
    # strings (or None); malformed migration/corruption values are retained as
    # their decoded value, or the raw string when JSON decoding fails, so the
    # shared grading contract can classify them instead of aborting the scan.
    dimensions: object
    failure_note: str | None
    factual_disposition: str | None
    factual_offending_claim: str | None
    factual_contradicting_evidence: str | None
    context_missing_input: str | None
    posture_should_have_been: str | None
    implication_implied_claim: str | None
    implication_missing_support: str | None
    platform: str | None
    evaluation_id: int | None
    evaluation_post_id: int | None
    evaluation_scan_id: int | None
    evaluation_relevant: int | None
    evaluation_project_key: str | None
    evaluation_dossier_summary_id: str | None
    evaluation_dossier_revision: str | None
    evaluation_posture: str | None
    draft_comment_id: int | None
    draft_project_key: str | None
    draft_dossier_summary_id: str | None
    draft_dossier_revision: str | None
    draft_posture: str | None
    override_mode: str
    override_reason: str | None
    pinned_revision_id: int
    pinned_revision_number: int


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    row: GradePopulationRow
    status: Literal["eligible", "excluded"]
    reason: ExclusionReason | None


@dataclass(frozen=True, slots=True)
class PhaseProjection:
    phase: PhaseName
    population: tuple[EligibilityResult, ...]
    not_draft_quality: tuple[EligibilityResult, ...]


@dataclass(frozen=True, slots=True)
class DimensionCount:
    dimension: str
    count: int


@dataclass(frozen=True, slots=True)
class SegmentStat:
    segment_type: Literal["project", "platform"]
    key: str
    grade_count: int
    correct_count: int
    false_positive_count: int
    false_negative_count: int


@dataclass(frozen=True, slots=True)
class RelevanceAggregate:
    total_count: int
    correct_count: int
    false_positive_count: int
    false_negative_count: int
    segments: tuple[SegmentStat, ...]


@dataclass(frozen=True, slots=True)
class DraftQualityAggregate:
    total_count: int
    accept_count: int
    fail_count: int
    dimension_counts: tuple[DimensionCount, ...]
    posture_correction_count: int
    factual_unsupported_count: int
    factual_contradicted_count: int


PhaseAggregate = RelevanceAggregate | DraftQualityAggregate


@dataclass(frozen=True, slots=True)
class FeedbackExample:
    grade_id: int
    post_id: int
    graded_at: str
    relevance_judgment: str
    dimension: str | None
    note: str


@dataclass(frozen=True, slots=True)
class RenderedFeedbackSection:
    phase: PhaseName
    token_budget: int
    token_estimate: int
    truncated: bool
    structured_summary: str
    rendered_text: str
    rendered_sha256: str


@dataclass(frozen=True, slots=True)
class PersistedFeedbackPhase:
    phase: PhaseName
    snapshot_phase_id: int
    rendered_sha256: str
    token_estimate: int
    token_budget: int
    aggregate_count: int
    example_count: int
    excluded_count: int


@dataclass(frozen=True, slots=True)
class PersistedFeedbackSnapshot:
    snapshot_id: int
    scan_id: int
    policy_version: str
    mode: FeedbackMode
    as_of: str
    population_count: int
    eligible_count: int
    excluded_count: int
    phases: tuple[PersistedFeedbackPhase, PersistedFeedbackPhase, PersistedFeedbackPhase]


# --------------------------------------------------------------------------
# Phase-keyed feedback bundle: what a live prompt is allowed to consume
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LegacySection:
    """Disabled-mode (shadow) feedback text: the pre-existing
    `limit_scans=3` legacy grading-signal formatting, unchanged. The same
    `LegacySection` value is used for all three phases — the legacy path
    never differentiated feedback by phase."""

    text: str


@dataclass(frozen=True, slots=True)
class SnapshotBody:
    """Active-mode feedback text: one phase's committed,
    audited `rendered_text` from an evaluation-feedback/v1 snapshot,
    verbatim — never a second render."""

    text: str


PhaseFeedbackEntry = LegacySection | SnapshotBody


@dataclass(frozen=True, slots=True)
class PhaseFeedbackBundle:
    """Phase-keyed feedback for one scan's model calls. Exactly one entry
    per phase, each independently either a `LegacySection` (flag disabled)
    or a `SnapshotBody` (flag enabled) — the discriminant prevents a
    relevance correction from silently reaching the draft or critic
    prompt, or vice versa."""

    relevance: PhaseFeedbackEntry
    reply_draft: PhaseFeedbackEntry
    critic: PhaseFeedbackEntry

    def for_phase(self, phase: PhaseName) -> PhaseFeedbackEntry:
        return {
            "relevance": self.relevance,
            "reply_draft": self.reply_draft,
            "critic": self.critic,
        }[phase]


def legacy_feedback_bundle(text: str) -> PhaseFeedbackBundle:
    """Build a disabled-mode bundle: the same legacy section text,
    unchanged, in all three phase keys."""
    section = LegacySection(text=text)
    return PhaseFeedbackBundle(relevance=section, reply_draft=section, critic=section)


class FeedbackBundleIntegrityError(RuntimeError):
    """Raised when the committed `feedback_snapshot_phases` rows for an
    active-mode snapshot cannot be trusted as prompt input: a missing
    phase, a stored hash mismatch, a snapshot read failure, or a stored
    mode that disagrees with the resolved scan mode. There is no legacy
    fallback for evaluation-feedback/v1 active mode — this must be
    treated as a pre-model scan failure."""


# --------------------------------------------------------------------------
# 1. load_grade_population
# --------------------------------------------------------------------------

_POPULATION_SQL = """
SELECT
    g.id AS grade_id,
    g.post_id AS post_id,
    g.scan_id AS scan_id,
    g.graded_at AS graded_at,
    g.schema_version AS schema_version,
    g.needs_regrade AS needs_regrade,
    g.relevance_judgment AS relevance_judgment,
    g.action_judgment AS action_judgment,
    g.dimensions AS dimensions,
    g.failure_note AS failure_note,
    g.factual_disposition AS factual_disposition,
    g.factual_offending_claim AS factual_offending_claim,
    g.factual_contradicting_evidence AS factual_contradicting_evidence,
    g.context_missing_input AS context_missing_input,
    g.posture_should_have_been AS posture_should_have_been,
    g.implication_implied_claim AS implication_implied_claim,
    g.implication_missing_support AS implication_missing_support,
    p.platform AS platform,
    e.id AS evaluation_id,
    e.post_id AS evaluation_post_id,
    e.scan_id AS evaluation_scan_id,
    e.relevant AS evaluation_relevant,
    e.project_key AS evaluation_project_key,
    e.dossier_summary_id AS evaluation_dossier_summary_id,
    e.dossier_revision AS evaluation_dossier_revision,
    e.posture AS evaluation_posture,
    dc.id AS draft_comment_id,
    dc.project_key AS draft_project_key,
    dc.dossier_summary_id AS draft_dossier_summary_id,
    dc.dossier_revision AS draft_dossier_revision,
    dc.posture AS draft_posture,
    COALESCE(o.mode, 'auto') AS override_mode,
    o.reason AS override_reason,
    (SELECT gr.id FROM grade_revisions gr
       WHERE gr.grade_id = g.id ORDER BY gr.revision DESC LIMIT 1) AS pinned_revision_id,
    (SELECT gr.revision FROM grade_revisions gr
       WHERE gr.grade_id = g.id ORDER BY gr.revision DESC LIMIT 1) AS pinned_revision_number
FROM grades g
JOIN posts p ON p.id = g.post_id
LEFT JOIN evaluations e ON e.id = g.evaluation_id
LEFT JOIN draft_comments dc ON dc.evaluation_id = e.id
LEFT JOIN grade_usage_overrides o ON o.grade_id = g.id
WHERE g.graded_at >= ?
ORDER BY g.graded_at DESC, g.id DESC
"""


def load_grade_population(
    conn: sqlite3.Connection, *, as_of: datetime, config: FeedbackPolicyConfig
) -> tuple[GradePopulationRow, ...]:
    """Load every grade with graded_at >= as_of - lookback_days (inclusive),
    newest-first with grade_id as tie-breaker, pinning each grade's latest
    grade_revision in the same read.

    `as_of` must be a single UTC instant captured once by the caller after
    start_scan — the inclusive boundary and stable ordering make selection
    depend only on human grading recency, not on the originating scan.
    """
    if as_of.tzinfo is None:
        raise ValueError("load_grade_population requires a timezone-aware as_of")
    boundary = as_of.astimezone(UTC) - timedelta(days=config.lookback_days)
    boundary_text = _format_instant(boundary)

    rows = conn.execute(_POPULATION_SQL, (boundary_text,)).fetchall()
    population: list[GradePopulationRow] = []
    for row in rows:
        pinned_revision_id = row["pinned_revision_id"]
        if pinned_revision_id is None:
            raise ValueError(
                f"grade {row['grade_id']} has no grade_revisions row to pin; "
                "every grades write must append one atomically"
            )
        dims_raw = row["dimensions"]
        dimensions: object = None
        if dims_raw is not None:
            try:
                dimensions = json.loads(dims_raw)
            except (json.JSONDecodeError, TypeError):
                # Preserve an invalid value for shared-contract classification.
                # A malformed row is an excluded audit fact, not a reason to
                # fail snapshot construction for every otherwise-valid grade.
                dimensions = dims_raw
        population.append(
            GradePopulationRow(
                grade_id=row["grade_id"],
                post_id=row["post_id"],
                scan_id=row["scan_id"],
                graded_at=row["graded_at"],
                schema_version=row["schema_version"],
                needs_regrade=bool(row["needs_regrade"]),
                relevance_judgment=row["relevance_judgment"],
                action_judgment=row["action_judgment"],
                dimensions=dimensions,
                failure_note=row["failure_note"],
                factual_disposition=row["factual_disposition"],
                factual_offending_claim=row["factual_offending_claim"],
                factual_contradicting_evidence=row["factual_contradicting_evidence"],
                context_missing_input=row["context_missing_input"],
                posture_should_have_been=row["posture_should_have_been"],
                implication_implied_claim=row["implication_implied_claim"],
                implication_missing_support=row["implication_missing_support"],
                platform=row["platform"],
                evaluation_id=row["evaluation_id"],
                evaluation_post_id=row["evaluation_post_id"],
                evaluation_scan_id=row["evaluation_scan_id"],
                evaluation_relevant=row["evaluation_relevant"],
                evaluation_project_key=row["evaluation_project_key"],
                evaluation_dossier_summary_id=row["evaluation_dossier_summary_id"],
                evaluation_dossier_revision=row["evaluation_dossier_revision"],
                evaluation_posture=row["evaluation_posture"],
                draft_comment_id=row["draft_comment_id"],
                draft_project_key=row["draft_project_key"],
                draft_dossier_summary_id=row["draft_dossier_summary_id"],
                draft_dossier_revision=row["draft_dossier_revision"],
                draft_posture=row["draft_posture"],
                override_mode=row["override_mode"],
                override_reason=row["override_reason"],
                pinned_revision_id=pinned_revision_id,
                pinned_revision_number=row["pinned_revision_number"],
            )
        )
    return tuple(population)


def _format_instant(when: datetime) -> str:
    as_utc = when.astimezone(UTC)
    millis = as_utc.microsecond // 1000
    return as_utc.strftime("%Y-%m-%dT%H:%M:%S") + f".{millis:03d}Z"


# --------------------------------------------------------------------------
# 2. classify_feedback_eligibility
# --------------------------------------------------------------------------


def classify_feedback_eligibility(
    population: Sequence[GradePopulationRow], *, config: FeedbackPolicyConfig
) -> tuple[EligibilityResult, ...]:
    """Classify every population row with the fixed exclusion precedence,
    then cap the newest `max_grades` valid rows as eligible — population is
    assumed already ordered graded_at DESC, grade_id DESC."""
    results: list[EligibilityResult] = []
    valid_count = 0
    for row in population:
        reason = _first_global_exclusion_reason(row)
        if reason is not None:
            results.append(EligibilityResult(row=row, status="excluded", reason=reason))
            continue
        valid_count += 1
        if valid_count <= config.max_grades:
            results.append(EligibilityResult(row=row, status="eligible", reason=None))
        else:
            results.append(EligibilityResult(row=row, status="excluded", reason="eligible_cap"))
    return tuple(results)


def _first_global_exclusion_reason(row: GradePopulationRow) -> ExclusionReason | None:
    if row.schema_version != HUMAN_GRADE_SCHEMA_VERSION:
        return "schema_version"
    if row.needs_regrade:
        return "needs_regrade"
    if row.evaluation_id is None:
        return "missing_evaluation_linkage"
    if row.evaluation_post_id != row.post_id:
        return "mismatched_evaluation_identity"
    if row.scan_id is not None and row.evaluation_scan_id != row.scan_id:
        return "mismatched_evaluation_identity"
    if _shared_contract_invalid(row):
        return "shared_contract_invalid"
    if row.override_mode == "exclude":
        return "manual_exclude"
    return None


def _shared_contract_invalid(row: GradePopulationRow) -> bool:
    """Return whether the row fails Scout's shared grading contract.

    ``web/grading_schema.json`` is the sole causal-grade authority used by
    ordinary saves. Snapshot eligibility must call that same validator rather
    than treating cross-table provenance equality as a substitute. We retain
    the existing draft/evaluation provenance check as an additional contract
    guard because those independently stored identities describe the drafted
    artifact the grade purports to judge.
    """
    # Imported lazily because grading.py imports StateManager, which imports
    # this module for the snapshot pipeline.
    from scout.grading.service import validate_grade_envelope

    dimensions = row.dimensions
    if isinstance(dimensions, tuple):
        dimensions = list(dimensions)
    payload = {
        "relevance_judgment": row.relevance_judgment,
        "action_judgment": row.action_judgment,
        "dimensions": dimensions,
        "failure_note": row.failure_note,
        "factual_offending_claim": row.factual_offending_claim,
        "factual_disposition": row.factual_disposition,
        "factual_contradicting_evidence": row.factual_contradicting_evidence,
        "context_missing_input": row.context_missing_input,
        "posture_should_have_been": row.posture_should_have_been,
        "implication_implied_claim": row.implication_implied_claim,
        "implication_missing_support": row.implication_missing_support,
    }
    if validate_grade_envelope(payload, row.evaluation_posture):
        return True

    if row.draft_comment_id is None:
        return False
    return (
        row.draft_project_key != row.evaluation_project_key
        or row.draft_dossier_summary_id != row.evaluation_dossier_summary_id
        or row.draft_dossier_revision != row.evaluation_dossier_revision
        or row.draft_posture != row.evaluation_posture
    )


def _grade_dimensions(row: GradePopulationRow) -> tuple[str, ...]:
    """Return contract-valid dimensions in an aggregation-friendly shape."""
    if isinstance(row.dimensions, (list, tuple)) and all(
        isinstance(value, str) for value in row.dimensions
    ):
        return tuple(row.dimensions)
    return ()


# --------------------------------------------------------------------------
# 3. project_phase_evidence
# --------------------------------------------------------------------------


def project_phase_evidence(
    eligibility: Sequence[EligibilityResult],
) -> dict[PhaseName, PhaseProjection]:
    """Project the globally eligible rows into each phase's population.

    Relevance takes every globally eligible grade. reply_draft and critic
    share the v1 draft-quality population: relevance_judgment 'correct',
    linked evaluation relevant=1, and a draft_comments row referencing that
    evaluation. Globally eligible rows outside that population are carried
    as `not_draft_quality` for those two phases so persistence can record
    them as excluded (reason `not_draft_quality_population`) rather than
    silently dropping them.
    """
    eligible = tuple(r for r in eligibility if r.status == "eligible")
    draft_population = tuple(r for r in eligible if _is_draft_quality(r.row))
    not_draft_quality = tuple(r for r in eligible if not _is_draft_quality(r.row))
    return {
        "relevance": PhaseProjection(
            phase="relevance", population=eligible, not_draft_quality=()
        ),
        "reply_draft": PhaseProjection(
            phase="reply_draft",
            population=draft_population,
            not_draft_quality=not_draft_quality,
        ),
        "critic": PhaseProjection(
            phase="critic",
            population=draft_population,
            not_draft_quality=not_draft_quality,
        ),
    }


def _is_draft_quality(row: GradePopulationRow) -> bool:
    return (
        row.relevance_judgment == "correct"
        and row.evaluation_relevant == 1
        and row.draft_comment_id is not None
    )


# --------------------------------------------------------------------------
# 4. aggregate_phase_evidence
# --------------------------------------------------------------------------


def aggregate_phase_evidence(
    projections: Mapping[PhaseName, PhaseProjection], *, config: FeedbackPolicyConfig
) -> dict[PhaseName, PhaseAggregate]:
    return {
        "relevance": _aggregate_relevance(projections["relevance"], config=config),
        "reply_draft": _aggregate_draft_quality(projections["reply_draft"]),
        "critic": _aggregate_draft_quality(projections["critic"]),
    }


def _aggregate_relevance(
    projection: PhaseProjection, *, config: FeedbackPolicyConfig
) -> RelevanceAggregate:
    rows = [r.row for r in projection.population]
    correct = sum(1 for r in rows if r.relevance_judgment == "correct")
    fp = sum(1 for r in rows if r.relevance_judgment == "false_positive")
    fn = sum(1 for r in rows if r.relevance_judgment == "false_negative")

    project_counts: dict[str, list[int]] = {}
    platform_counts: dict[str, list[int]] = {}
    for r in rows:
        judgment_index = {"correct": 0, "false_positive": 1, "false_negative": 2}[
            r.relevance_judgment
        ]
        if r.evaluation_project_key:
            bucket = project_counts.setdefault(r.evaluation_project_key, [0, 0, 0, 0])
            bucket[0] += 1
            bucket[judgment_index + 1] += 1
        if r.platform:
            bucket = platform_counts.setdefault(r.platform, [0, 0, 0, 0])
            bucket[0] += 1
            bucket[judgment_index + 1] += 1

    segments: list[SegmentStat] = []
    for segment_type, counts in (("project", project_counts), ("platform", platform_counts)):
        for key, (total, c, p, n) in counts.items():
            if total < config.segment_min_grades:
                continue
            segments.append(
                SegmentStat(
                    segment_type=segment_type,  # type: ignore[arg-type]
                    key=key,
                    grade_count=total,
                    correct_count=c,
                    false_positive_count=p,
                    false_negative_count=n,
                )
            )
    segments.sort(key=lambda s: (s.segment_type, -s.grade_count, s.key))

    return RelevanceAggregate(
        total_count=len(rows),
        correct_count=correct,
        false_positive_count=fp,
        false_negative_count=fn,
        segments=tuple(segments),
    )


def _aggregate_draft_quality(projection: PhaseProjection) -> DraftQualityAggregate:
    rows = [r.row for r in projection.population]
    accept = sum(1 for r in rows if r.action_judgment == "accept")
    fail = sum(1 for r in rows if r.action_judgment == "fail")

    dim_counts: dict[str, int] = {}
    posture_corrections = 0
    factual_unsupported = 0
    factual_contradicted = 0
    for r in rows:
        for dim in _grade_dimensions(r):
            dim_counts[dim] = dim_counts.get(dim, 0) + 1
        if r.posture_should_have_been:
            posture_corrections += 1
        if r.factual_disposition == "unsupported":
            factual_unsupported += 1
        elif r.factual_disposition == "contradicted":
            factual_contradicted += 1

    ranked = tuple(
        DimensionCount(dimension=d, count=c)
        for d, c in sorted(dim_counts.items(), key=lambda item: (-item[1], item[0]))
    )

    return DraftQualityAggregate(
        total_count=len(rows),
        accept_count=accept,
        fail_count=fail,
        dimension_counts=ranked,
        posture_correction_count=posture_corrections,
        factual_unsupported_count=factual_unsupported,
        factual_contradicted_count=factual_contradicted,
    )


# --------------------------------------------------------------------------
# 5. select_phase_examples
# --------------------------------------------------------------------------


def select_phase_examples(
    projections: Mapping[PhaseName, PhaseProjection], *, config: FeedbackPolicyConfig
) -> dict[PhaseName, tuple[FeedbackExample, ...]]:
    return {
        "relevance": _select_relevance_examples(projections["relevance"], config=config),
        "reply_draft": _select_reply_draft_examples(projections["reply_draft"], config=config),
        "critic": _select_critic_examples(projections["critic"], config=config),
    }


def _make_example(row: GradePopulationRow, *, config: FeedbackPolicyConfig) -> FeedbackExample:
    note = (row.failure_note or "").strip()[: config.note_max_chars]
    dimensions = _grade_dimensions(row)
    return FeedbackExample(
        grade_id=row.grade_id,
        post_id=row.post_id,
        graded_at=row.graded_at,
        relevance_judgment=row.relevance_judgment,
        dimension=dimensions[0] if dimensions else None,
        note=note,
    )


def _select_relevance_examples(
    projection: PhaseProjection, *, config: FeedbackPolicyConfig
) -> tuple[FeedbackExample, ...]:
    limit = config.relevance_example_limit
    if limit <= 0:
        return ()
    candidates = [
        r.row
        for r in projection.population
        if r.row.relevance_judgment != "correct" and r.row.failure_note
    ]
    return tuple(_make_example(row, config=config) for row in candidates[:limit])


def _select_reply_draft_examples(
    projection: PhaseProjection, *, config: FeedbackPolicyConfig
) -> tuple[FeedbackExample, ...]:
    limit = config.reply_draft_example_limit
    if limit <= 0:
        return ()
    candidates = [
        r.row
        for r in projection.population
        if r.row.action_judgment == "fail" and r.row.failure_note
    ]
    return tuple(_make_example(row, config=config) for row in candidates[:limit])


def _select_critic_examples(
    projection: PhaseProjection, *, config: FeedbackPolicyConfig
) -> tuple[FeedbackExample, ...]:
    limit = config.critic_example_limit
    if limit <= 0:
        return ()
    seen_dimensions: set[str | None] = set()
    selected: list[GradePopulationRow] = []
    for r in projection.population:
        row = r.row
        if row.action_judgment != "fail" or not row.failure_note:
            continue
        dimensions = _grade_dimensions(row)
        primary_dimension = dimensions[0] if dimensions else None
        if primary_dimension in seen_dimensions:
            continue
        seen_dimensions.add(primary_dimension)
        selected.append(row)
        if len(selected) >= limit:
            break
    return tuple(_make_example(row, config=config) for row in selected)


# --------------------------------------------------------------------------
# 6. render_feedback_sections
# --------------------------------------------------------------------------


def render_feedback_sections(
    aggregates: Mapping[PhaseName, PhaseAggregate],
    examples: Mapping[PhaseName, tuple[FeedbackExample, ...]],
    *,
    config: FeedbackPolicyConfig,
) -> dict[PhaseName, RenderedFeedbackSection]:
    return {
        phase: _render_phase_section(
            phase, aggregates[phase], examples[phase], config=config
        )
        for phase in PHASE_NAMES
    }


def _render_phase_section(
    phase: PhaseName,
    aggregate: PhaseAggregate,
    phase_examples: tuple[FeedbackExample, ...],
    *,
    config: FeedbackPolicyConfig,
) -> RenderedFeedbackSection:
    budget = config.token_budget(phase)
    if aggregate.total_count == 0:
        summary = _canonical_json(
            {
                "phase": phase,
                "policy_version": config.policy_version,
                "reason": NO_ELIGIBLE_FEEDBACK_REASON,
                "resolved_params": _resolved_params(phase, config),
            }
        )
        return RenderedFeedbackSection(
            phase=phase,
            token_budget=budget,
            token_estimate=0,
            truncated=False,
            structured_summary=summary,
            rendered_text="",
            rendered_sha256=_EMPTY_SHA256,
        )

    fields: _RelevanceRenderFields | _DraftQualityRenderFields
    if isinstance(aggregate, RelevanceAggregate):
        fields = _RelevanceRenderFields(
            aggregate=aggregate, examples=phase_examples
        )
    else:
        fields = _DraftQualityRenderFields(
            phase=phase, aggregate=aggregate, examples=phase_examples
        )
    summary, text, truncated = _truncate_to_budget(
        fields, budget=budget, config=config, phase=phase
    )

    token_estimate = _estimate_tokens(text)
    return RenderedFeedbackSection(
        phase=phase,
        token_budget=budget,
        token_estimate=token_estimate,
        truncated=truncated,
        structured_summary=summary,
        rendered_text=text,
        rendered_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def _resolved_params(phase: PhaseName, config: FeedbackPolicyConfig) -> dict[str, object]:
    params: dict[str, object] = {
        "token_budget": config.token_budget(phase),
        "example_limit": config.example_limit(phase),
        "note_max_chars": config.note_max_chars,
    }
    if phase == "relevance":
        params["segment_min_grades"] = config.segment_min_grades
    return params


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _estimate_tokens(text: str) -> int:
    byte_length = len(text.encode("utf-8"))
    return ceil(byte_length / 4) if byte_length else 0


@dataclass(frozen=True, slots=True)
class _RelevanceRenderFields:
    aggregate: RelevanceAggregate
    examples: tuple[FeedbackExample, ...]
    dropped_segments: tuple[str, ...] = field(default_factory=tuple)
    dropped_example_count: int = 0


@dataclass(frozen=True, slots=True)
class _DraftQualityRenderFields:
    phase: PhaseName
    aggregate: DraftQualityAggregate
    examples: tuple[FeedbackExample, ...]
    dropped_dimensions: tuple[str, ...] = field(default_factory=tuple)
    dropped_example_count: int = 0


def _pop_lowest_segment(
    segments: tuple[SegmentStat, ...],
) -> tuple[tuple[SegmentStat, ...], SegmentStat]:
    """Find and remove the globally lowest-count segment (lexical ties on
    segment_type then key), regardless of display order.

    `_aggregate_relevance` sorts segments by `(segment_type, -grade_count,
    key)` so same-type segments group together for display — the lowest
    count within the last type group is not necessarily the lowest count
    overall. Truncation must drop the true global minimum, so it searches
    rather than relying on list position.
    """
    dropped = min(segments, key=lambda s: (s.grade_count, s.segment_type, s.key))
    remaining = tuple(s for s in segments if s is not dropped)
    return remaining, dropped


def _truncate_to_budget(
    fields: _RelevanceRenderFields | _DraftQualityRenderFields,
    *,
    budget: int,
    config: FeedbackPolicyConfig,
    phase: PhaseName,
) -> tuple[str, str, bool]:
    """Deterministically drop evidence — for relevance: lowest-count
    segments first (lexical ties), then oldest examples; for reply_draft /
    critic: oldest examples, then lowest-ranked dimensions — until the
    rendered text estimate fits the phase budget or nothing removable is
    left. Mandatory totals are never in a removable list."""
    truncated = False
    while True:
        summary_obj = _build_structured_summary(fields, config=config, phase=phase)
        text = _render_prose(summary_obj)
        if _estimate_tokens(text) <= budget:
            return _canonical_json(summary_obj), text, truncated

        if isinstance(fields, _RelevanceRenderFields) and fields.aggregate.segments:
            remaining, dropped = _pop_lowest_segment(fields.aggregate.segments)
            fields = _RelevanceRenderFields(
                aggregate=RelevanceAggregate(
                    total_count=fields.aggregate.total_count,
                    correct_count=fields.aggregate.correct_count,
                    false_positive_count=fields.aggregate.false_positive_count,
                    false_negative_count=fields.aggregate.false_negative_count,
                    segments=remaining,
                ),
                examples=fields.examples,
                dropped_segments=fields.dropped_segments
                + (f"{dropped.segment_type}:{dropped.key}",),
                dropped_example_count=fields.dropped_example_count,
            )
            truncated = True
            continue

        if fields.examples:
            fields = _with_examples(fields, fields.examples[:-1])
            truncated = True
            continue

        if isinstance(fields, _DraftQualityRenderFields) and fields.aggregate.dimension_counts:
            remaining_dims = fields.aggregate.dimension_counts[:-1]
            dropped_dim = fields.aggregate.dimension_counts[-1]
            fields = _DraftQualityRenderFields(
                phase=fields.phase,
                aggregate=DraftQualityAggregate(
                    total_count=fields.aggregate.total_count,
                    accept_count=fields.aggregate.accept_count,
                    fail_count=fields.aggregate.fail_count,
                    dimension_counts=remaining_dims,
                    posture_correction_count=fields.aggregate.posture_correction_count,
                    factual_unsupported_count=fields.aggregate.factual_unsupported_count,
                    factual_contradicted_count=fields.aggregate.factual_contradicted_count,
                ),
                examples=fields.examples,
                dropped_dimensions=fields.dropped_dimensions + (dropped_dim.dimension,),
                dropped_example_count=fields.dropped_example_count,
            )
            truncated = True
            continue

        # Nothing removable remains (only mandatory totals left); the
        # rendering stands as-is, over budget.
        return _canonical_json(summary_obj), text, truncated


def _with_examples(
    fields: _RelevanceRenderFields | _DraftQualityRenderFields,
    remaining: tuple[FeedbackExample, ...],
) -> _RelevanceRenderFields | _DraftQualityRenderFields:
    dropped_count = fields.dropped_example_count + (len(fields.examples) - len(remaining))
    if isinstance(fields, _RelevanceRenderFields):
        return _RelevanceRenderFields(
            aggregate=fields.aggregate,
            examples=remaining,
            dropped_segments=fields.dropped_segments,
            dropped_example_count=dropped_count,
        )
    return _DraftQualityRenderFields(
        phase=fields.phase,
        aggregate=fields.aggregate,
        examples=remaining,
        dropped_dimensions=fields.dropped_dimensions,
        dropped_example_count=dropped_count,
    )


def _build_structured_summary(
    fields: _RelevanceRenderFields | _DraftQualityRenderFields,
    *,
    config: FeedbackPolicyConfig,
    phase: PhaseName,
) -> dict[str, Any]:
    omitted: dict[str, Any] = {}
    if fields.dropped_example_count:
        omitted["examples"] = fields.dropped_example_count

    if isinstance(fields, _RelevanceRenderFields):
        agg = fields.aggregate
        total = agg.total_count
        summary: dict[str, Any] = {
            "phase": phase,
            "policy_version": config.policy_version,
            "resolved_params": _resolved_params(phase, config),
            "totals": {
                "total_count": total,
                "correct_count": agg.correct_count,
                "false_positive_count": agg.false_positive_count,
                "false_negative_count": agg.false_negative_count,
            },
            "rates": {
                "correct_rate": round(agg.correct_count / total, 4),
                "false_positive_rate": round(agg.false_positive_count / total, 4),
                "false_negative_rate": round(agg.false_negative_count / total, 4),
            },
            "segments": [
                {
                    "segment_type": s.segment_type,
                    "key": s.key,
                    "grade_count": s.grade_count,
                    "correct_count": s.correct_count,
                    "false_positive_count": s.false_positive_count,
                    "false_negative_count": s.false_negative_count,
                }
                for s in agg.segments
            ],
            "examples": [_example_dict(e) for e in fields.examples],
        }
        if fields.dropped_segments:
            omitted["segments"] = list(fields.dropped_segments)
    else:
        draft_agg = fields.aggregate
        draft_total = draft_agg.total_count
        summary = {
            "phase": phase,
            "policy_version": config.policy_version,
            "resolved_params": _resolved_params(phase, config),
            "totals": {
                "total_count": draft_total,
                "accept_count": draft_agg.accept_count,
                "fail_count": draft_agg.fail_count,
            },
            "rates": {
                "accept_rate": round(draft_agg.accept_count / draft_total, 4),
            },
            "dimensions": [
                {"dimension": d.dimension, "count": d.count}
                for d in draft_agg.dimension_counts
            ],
            "corrections": {
                "posture_correction_count": draft_agg.posture_correction_count,
                "factual_unsupported_count": draft_agg.factual_unsupported_count,
                "factual_contradicted_count": draft_agg.factual_contradicted_count,
            },
            "examples": [_example_dict(e) for e in fields.examples],
        }
        if fields.dropped_dimensions:
            omitted["dimensions"] = list(fields.dropped_dimensions)

    if omitted:
        summary["omitted"] = omitted
    return summary


def _example_dict(example: FeedbackExample) -> dict[str, Any]:
    return {
        "grade_id": example.grade_id,
        "post_id": example.post_id,
        "graded_at": example.graded_at,
        "relevance_judgment": example.relevance_judgment,
        "dimension": example.dimension,
        "note": example.note,
    }


def _render_prose(summary: Mapping[str, Any]) -> str:
    phase = summary["phase"]
    policy_version = summary["policy_version"]
    lines = [f"## {_phase_title(phase)} Feedback ({policy_version})"]

    totals = summary["totals"]
    rates = summary["rates"]
    if phase == "relevance":
        lines.append(
            f"{totals['total_count']} graded: {totals['correct_count']} correct "
            f"({_pct(rates['correct_rate'])}), "
            f"{totals['false_positive_count']} false positives "
            f"({_pct(rates['false_positive_rate'])}), "
            f"{totals['false_negative_count']} false negatives "
            f"({_pct(rates['false_negative_rate'])})."
        )
        for segment in summary["segments"]:
            lines.append(
                f"- {segment['segment_type']}:{segment['key']} "
                f"(n={segment['grade_count']}, correct={segment['correct_count']}, "
                f"fp={segment['false_positive_count']}, fn={segment['false_negative_count']})"
            )
        if summary["examples"]:
            lines.append("Recent misses:")
            for example in summary["examples"]:
                lines.append(
                    f"- [grade #{example['grade_id']}] {example['relevance_judgment']}: "
                    f"{example['note']}"
                )
    elif phase == "reply_draft":
        corrections = summary["corrections"]
        lines.append(
            f"{totals['total_count']} graded: {totals['accept_count']} accepted "
            f"({_pct(rates['accept_rate'])}), {totals['fail_count']} failed."
        )
        dims = summary["dimensions"]
        if dims:
            dim_str = ", ".join(f"{d['dimension']} ({d['count']})" for d in dims)
            lines.append(f"Failure dimensions: {dim_str}.")
        if corrections["posture_correction_count"] or corrections["factual_unsupported_count"] \
                or corrections["factual_contradicted_count"]:
            lines.append(
                f"Corrections: posture {corrections['posture_correction_count']}, "
                f"factual-unsupported {corrections['factual_unsupported_count']}, "
                f"factual-contradicted {corrections['factual_contradicted_count']}."
            )
        if summary["examples"]:
            lines.append("Recent notes:")
            for example in summary["examples"]:
                dim = example["dimension"] or "general"
                lines.append(f"- [grade #{example['grade_id']}] {dim}: {example['note']}")
    else:
        # The critic consumes the same draft-quality population as the reply
        # drafter, but its prompt is failure-oriented: correction evidence and
        # dimensions intentionally precede acceptance-rate context.
        corrections = summary["corrections"]
        if (
            corrections["posture_correction_count"]
            or corrections["factual_unsupported_count"]
            or corrections["factual_contradicted_count"]
        ):
            lines.append(
                f"Corrections: posture {corrections['posture_correction_count']}, "
                f"factual-unsupported {corrections['factual_unsupported_count']}, "
                f"factual-contradicted {corrections['factual_contradicted_count']}."
            )
        dims = summary["dimensions"]
        if dims:
            dim_str = ", ".join(f"{d['dimension']} ({d['count']})" for d in dims)
            lines.append(f"Failure dimensions: {dim_str}.")
        if summary["examples"]:
            lines.append("Recent failure notes:")
            for example in summary["examples"]:
                dim = example["dimension"] or "general"
                lines.append(f"- [grade #{example['grade_id']}] {dim}: {example['note']}")
        lines.append(
            f"Outcome context: {totals['fail_count']} failed and "
            f"{totals['accept_count']} accepted out of {totals['total_count']} graded "
            f"({_pct(rates['accept_rate'])} accepted)."
        )

    return "\n".join(lines)


def _phase_title(phase: str) -> str:
    return {
        "relevance": "Relevance",
        "reply_draft": "Reply Draft Quality",
        "critic": "Critic",
    }[phase]


def _pct(rate: float) -> str:
    return f"{rate * 100:.1f}%"


# --------------------------------------------------------------------------
# 7. persist_feedback_snapshot
# --------------------------------------------------------------------------


def persist_feedback_snapshot(
    conn: sqlite3.Connection,
    *,
    scan_id: int,
    mode: FeedbackMode,
    as_of: datetime,
    config: FeedbackPolicyConfig,
    population: Sequence[GradePopulationRow],
    eligibility: Sequence[EligibilityResult],
    projections: Mapping[PhaseName, PhaseProjection],
    examples: Mapping[PhaseName, tuple[FeedbackExample, ...]],
    sections: Mapping[PhaseName, RenderedFeedbackSection],
    recorded_at: datetime,
) -> PersistedFeedbackSnapshot:
    """Insert the snapshot header, its three phase rows, and every pinned
    item row, all against the caller's already-open connection/transaction.

    `mode` must be resolved once by the caller before this call (from
    FEEDBACK_PROMPT_ENABLED) and is stored verbatim on the snapshot header
    — this function never reads configuration itself.

    Must run inside the caller's `begin_immediate()` context — this
    function opens no transaction of its own, so a raised exception here
    leaves the whole insert to the caller's rollback.
    """
    recorded_at_text = _format_instant(recorded_at)
    eligible_count = sum(1 for r in eligibility if r.status == "eligible")
    excluded_count = len(population) - eligible_count

    cursor = conn.execute(
        "INSERT INTO feedback_snapshots ("
        "  scan_id, policy_version, mode, as_of, lookback_days, max_grades, "
        "  segment_min_grades, note_max_chars, relevance_token_budget, "
        "  reply_draft_token_budget, critic_token_budget, "
        "  population_count, eligible_count, excluded_count, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scan_id,
            config.policy_version,
            mode,
            _format_instant(as_of),
            config.lookback_days,
            config.max_grades,
            config.segment_min_grades,
            config.note_max_chars,
            config.relevance_token_budget,
            config.reply_draft_token_budget,
            config.critic_token_budget,
            len(population),
            eligible_count,
            excluded_count,
            recorded_at_text,
        ),
    )
    snapshot_id = cursor.lastrowid
    assert snapshot_id is not None

    global_excluded = tuple(r for r in eligibility if r.status == "excluded")
    phases: list[PersistedFeedbackPhase] = []
    for phase in PHASE_NAMES:
        section = sections[phase]
        phase_cursor = conn.execute(
            "INSERT INTO feedback_snapshot_phases ("
            "  snapshot_id, phase, token_budget, token_estimate, truncated, "
            "  structured_summary, rendered_text, rendered_sha256, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot_id,
                phase,
                section.token_budget,
                section.token_estimate,
                int(section.truncated),
                section.structured_summary,
                section.rendered_text,
                section.rendered_sha256,
                recorded_at_text,
            ),
        )
        snapshot_phase_id = phase_cursor.lastrowid
        assert snapshot_phase_id is not None

        projection = projections[phase]
        phase_examples = examples[phase]
        example_ranks = {
            example.grade_id: rank
            for rank, example in enumerate(phase_examples, start=1)
        }

        item_rows: list[tuple[object, ...]] = []
        for result in global_excluded:
            item_rows.append(
                (
                    snapshot_phase_id,
                    result.row.grade_id,
                    result.row.pinned_revision_id,
                    "excluded",
                    result.reason,
                    result.reason,
                    None,
                    recorded_at_text,
                )
            )
        for result in projection.not_draft_quality:
            item_rows.append(
                (
                    snapshot_phase_id,
                    result.row.grade_id,
                    result.row.pinned_revision_id,
                    "excluded",
                    "not_draft_quality_population",
                    "not_draft_quality_population",
                    None,
                    recorded_at_text,
                )
            )
        for result in projection.population:
            item_rows.append(
                (
                    snapshot_phase_id,
                    result.row.grade_id,
                    result.row.pinned_revision_id,
                    "aggregate",
                    None,
                    "phase_population",
                    None,
                    recorded_at_text,
                )
            )
            example_rank = example_ranks.get(result.row.grade_id)
            if example_rank is not None:
                item_rows.append(
                    (
                        snapshot_phase_id,
                        result.row.grade_id,
                        result.row.pinned_revision_id,
                        "example",
                        None,
                        "selected_recent_note",
                        example_rank,
                        recorded_at_text,
                    )
                )

        conn.executemany(
            "INSERT INTO feedback_snapshot_items ("
            "  snapshot_phase_id, grade_id, grade_revision_id, role, reason, "
            "  selection_reason, rank, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            item_rows,
        )

        phases.append(
            PersistedFeedbackPhase(
                phase=phase,
                snapshot_phase_id=snapshot_phase_id,
                rendered_sha256=section.rendered_sha256,
                token_estimate=section.token_estimate,
                token_budget=section.token_budget,
                aggregate_count=len(projection.population),
                example_count=len(phase_examples),
                excluded_count=len(global_excluded) + len(projection.not_draft_quality),
            )
        )

    assert len(phases) == 3
    return PersistedFeedbackSnapshot(
        snapshot_id=snapshot_id,
        mode=mode,
        scan_id=scan_id,
        policy_version=config.policy_version,
        as_of=_format_instant(as_of),
        population_count=len(population),
        eligible_count=eligible_count,
        excluded_count=excluded_count,
        phases=(phases[0], phases[1], phases[2]),
    )


# --------------------------------------------------------------------------
# 8. load_committed_phase_bundle
# --------------------------------------------------------------------------


def load_committed_phase_bundle(
    conn: sqlite3.Connection,
    *,
    snapshot_id: int,
    expected_mode: FeedbackMode,
) -> PhaseFeedbackBundle:
    """Load the three committed `feedback_snapshot_phases` rows for
    `snapshot_id` and build a phase-keyed `PhaseFeedbackBundle` of stored
    `rendered_text` — the audited bytes, not a second render.

    Must be called only after the `persist_feedback_snapshot` transaction
    for this snapshot has committed, and only in active mode: it opens no
    transaction of its own and reads plainly, so it observes exactly what
    is durable. Raises `FeedbackBundleIntegrityError` — with no legacy
    fallback — on any of: the snapshot id not resolving, a stored mode
    that disagrees with `expected_mode`, a missing phase row, or a stored
    `rendered_sha256` that disagrees with a fresh hash of the stored
    `rendered_text`.
    """
    header = conn.execute(
        "SELECT mode FROM feedback_snapshots WHERE id = ?", (snapshot_id,)
    ).fetchone()
    if header is None:
        raise FeedbackBundleIntegrityError(
            f"feedback snapshot {snapshot_id} not found for committed-phase read"
        )
    stored_mode = header["mode"]
    if stored_mode != expected_mode:
        raise FeedbackBundleIntegrityError(
            f"feedback snapshot {snapshot_id} mode={stored_mode!r} does not match "
            f"resolved scan mode {expected_mode!r}"
        )

    rows = conn.execute(
        "SELECT phase, rendered_text, rendered_sha256 FROM feedback_snapshot_phases "
        "WHERE snapshot_id = ?",
        (snapshot_id,),
    ).fetchall()
    by_phase = {row["phase"]: row for row in rows}
    missing = [phase for phase in PHASE_NAMES if phase not in by_phase]
    if missing:
        raise FeedbackBundleIntegrityError(
            f"feedback snapshot {snapshot_id} missing phase row(s): {sorted(missing)}"
        )

    entries: dict[PhaseName, SnapshotBody] = {}
    for phase in PHASE_NAMES:
        row = by_phase[phase]
        rendered_text = row["rendered_text"]
        actual_sha256 = hashlib.sha256(rendered_text.encode("utf-8")).hexdigest()
        if actual_sha256 != row["rendered_sha256"]:
            raise FeedbackBundleIntegrityError(
                f"feedback snapshot {snapshot_id} phase={phase!r} rendered_text hash "
                "mismatch — stored bytes disagree with the recorded rendered_sha256"
            )
        entries[phase] = SnapshotBody(text=rendered_text)

    return PhaseFeedbackBundle(
        relevance=entries["relevance"],
        reply_draft=entries["reply_draft"],
        critic=entries["critic"],
    )
