"""Freeze recorded grading inputs, then derive a reproducible relevance corpus.

The DB and dossier reads end at FrozenGradePopulation. Selection and encoding
consume only that retained document; later regrades and repository changes do
not affect an existing snapshot. This module does not fit or run any model.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue, StrictBool, StrictInt, TypeAdapter

from scout.dossiers.resolver import DossierResolution, DossierResolutionError, resolve_dossier
from scout.grading.artifacts import (
    ArtifactBundle,
    ArtifactDigest,
    ArtifactError,
    ArtifactLineage,
    ArtifactProcess,
    EnvironmentIdentity,
    ProcessId,
    RetainedArtifact,
    TransformKind,
    digest_artifact,
    validate_bundle,
)
from scout.grading.feedback import (
    ExclusionReason,
    GradePopulationRow,
    grade_exclusion_reason,
    load_corpus_grade_population,
)
from scout.grading.relevance_targets import (
    TargetExclusionReason,
    derive_relevance_target,
    project_relevance_target_source,
)
from scout.grading.wire import ArrayWire, encode_wire_v1, record_wire
from scout.result import Err, Ok, Result
from scout.storage.evaluations import PhaseRun
from scout.storage.grades import GradeRevision
from scout.storage.migrations import grade_revision_comparison_shape


class RecordedPost(BaseModel):
    """Complete posts row (storage/schema.py), including recorded parent context."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    id: int
    platform: str
    platform_msg_id: str
    channel_name: str | None
    channel_id: str | None
    author_name: str | None
    author_id: str | None
    content: str | None
    url: str | None
    created_at: str | None
    scan_id: int | None
    parent_lookup_status: str
    parent_id: str | None
    parent_author_id: str | None
    parent_author_name: str | None
    parent_text: str | None
    parent_url: str | None


class RecordedEvaluation(BaseModel):
    """Recorded evaluations columns from storage/schema.py, excluding abstain_reason.

    Unlike the operational EvaluationRow, capture retains the INTEGER relevant
    value even when it is outside 0/1; selection classifies invalid decisions.
    StrictBool preserves the encoding of previously retained v1 populations so
    their per-input digests still replay unchanged.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    id: int
    post_id: int
    relevant: StrictInt | StrictBool
    score: float
    reason: str | None
    relevant_to: str | None
    keyword_route_id: int | None
    scan_id: int | None
    created_at: str | None
    project_key: str | None
    posture: str | None
    surface_status: str
    failure_reason: str | None
    dossier_summary_id: str | None
    dossier_revision: str | None


class RecordedExposure(BaseModel):
    """Existing feedback_snapshot_items joined to their phase and active snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    snapshot_id: int
    scan_id: int
    phase: str
    grade_revision_id: int
    role: str


class FrozenGradeInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    grade: GradePopulationRow
    revision: GradeRevision
    revision_matches: bool
    post: RecordedPost | None
    evaluation: RecordedEvaluation | None
    phase_runs: tuple[PhaseRun, ...]
    # GradeRevision.payload already retains correction pointer AND edited_text.
    # Keeping its exact payload avoids following a mutable "latest correction".
    context: DossierResolution | None
    exposures: tuple[RecordedExposure, ...]


class FrozenGradePopulation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    format: Literal["scout.grade-population/v1"] = "scout.grade-population/v1"
    observed_at: str
    items: tuple[FrozenGradeInput, ...]


class PopulationItems(BaseModel):
    """Selection inputs reconstructed from a v2 manifest, without capture time."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    items: tuple[FrozenGradeInput, ...]


class PopulationManifest(BaseModel):
    """V2 frozen population: ordered references to retained per-grade inputs."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    format: Literal["scout.grade-population/v2"] = "scout.grade-population/v2"
    items: tuple[ArtifactDigest, ...]


class PopulationCapture(BaseModel):
    """Small source observation, separate from deterministic population identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    format: Literal["scout.population-capture/v1"] = "scout.population-capture/v1"
    population_digest: ArtifactDigest
    observed_at: str


class GradeRevisionPayload(BaseModel):
    """Shape written by migrations.grade_revision_comparison_shape since v26.

    Legacy revisions can predate reply correction fields. Human judgment values
    are not validated here; this checks the storage record, not eligibility.
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    id: int
    evaluation_id: int | None
    post_id: int
    scan_id: int | None
    source: str
    graded_at: str
    relevance_judgment: str
    rejection_reason: str | None
    comment_quality: int | None
    comment_issue: str | None
    schema_version: int
    needs_regrade: int
    action_judgment: str | None
    dimensions: JsonValue
    failure_note: str | None
    factual_offending_claim: str | None
    factual_disposition: str | None
    factual_contradicting_evidence: str | None
    context_missing_input: str | None
    posture_should_have_been: str | None
    implication_implied_claim: str | None
    implication_missing_support: str | None
    reply_revision_id: int | None = None
    edited_text: str | None = None


class CorpusSelection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    project_key: str
    policy_version: Literal["scout.relevance-corpus/v1"] = "scout.relevance-corpus/v1"


class CorpusMember(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    grade_id: int
    evaluation_id: int
    grade_revision_id: int
    is_relevant: bool
    # Individual input digests keep membership identity independent of the
    # observation timestamp and of excluded rows elsewhere in the population.
    input_digest: ArtifactDigest


type CorpusExclusionReason = (
    ExclusionReason
    | TargetExclusionReason
    | Literal[
        "missing_post",
        "revision_mismatch",
        "missing_project_context",
        "outside_project",
        "unavailable_pinned_context",
        "missing_post_text",
    ]
)


class CorpusExclusion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    grade_id: int
    reason: CorpusExclusionReason


class CorpusSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    format: Literal["scout.relevance-corpus/v1"] = "scout.relevance-corpus/v1"
    selection: CorpusSelection
    members: tuple[CorpusMember, ...]
    exclusions: tuple[CorpusExclusion, ...]


class ExclusionCount(BaseModel):
    reason: CorpusExclusionReason
    count: int


class CorpusPreview(BaseModel):
    selection: CorpusSelection
    population_count: int
    eligible_count: int
    positive_count: int
    negative_count: int
    exclusions: tuple[ExclusionCount, ...]


# Frozen v1 projections, derived from the original DB rows and dossier contract.
# Do not derive these lists from runtime model/dataclass field order.
GRADE_WIRE_V1 = record_wire("""
    grade_id post_id scan_id graded_at schema_version needs_regrade relevance_judgment
    action_judgment dimensions failure_note factual_disposition factual_offending_claim
    factual_contradicting_evidence context_missing_input posture_should_have_been
    implication_implied_claim implication_missing_support platform evaluation_id
    evaluation_post_id evaluation_scan_id evaluation_relevant evaluation_project_key
    evaluation_dossier_summary_id evaluation_dossier_revision evaluation_posture
    draft_comment_id draft_project_key draft_dossier_summary_id draft_dossier_revision
    draft_posture override_mode override_reason pinned_revision_id pinned_revision_number
""")
REVISION_WIRE_V1 = record_wire(
    "id grade_id evaluation_id revision schema_version source payload recorded_at"
)
POST_WIRE_V1 = record_wire("""
    id platform platform_msg_id channel_name channel_id author_name author_id content url
    created_at scan_id parent_lookup_status parent_id parent_author_id parent_author_name
    parent_text parent_url
""")
EVALUATION_WIRE_V1 = record_wire("""
    id post_id relevant score reason relevant_to keyword_route_id scan_id created_at
    project_key posture surface_status failure_reason dossier_summary_id dossier_revision
""")
SUMMARY_WIRE_V1 = record_wire(
    "project_key last_reviewed reviewer facts resources prohibitions references",
    facts=ArrayWire(record_wire("id text safe_phrasings immutable_evidence")),
    resources=ArrayWire(record_wire("id label canonical_url immutable_evidence")),
    prohibitions=ArrayWire(record_wire("id mode pattern flags immutable_evidence")),
)
INPUT_WIRE_V1 = record_wire(
    "grade revision revision_matches post evaluation phase_runs context exposures",
    grade=GRADE_WIRE_V1,
    revision=REVISION_WIRE_V1,
    post=POST_WIRE_V1,
    evaluation=EVALUATION_WIRE_V1,
    phase_runs=ArrayWire(
        record_wire(
            "id scan_id post_id evaluation_id snapshot_phase_id phase trace_id "
            "model status created_at"
        )
    ),
    context=record_wire(
        "summary metadata known_gaps",
        summary=SUMMARY_WIRE_V1,
        metadata=record_wire("project_key summary_id revision path"),
    ),
    exposures=ArrayWire(record_wire("snapshot_id scan_id phase grade_revision_id role")),
)
POPULATION_WIRE_V1 = record_wire("format observed_at items", items=ArrayWire(INPUT_WIRE_V1))
SELECTION_WIRE_V1 = record_wire("project_key policy_version")
SNAPSHOT_WIRE_V1 = record_wire(
    "format selection members exclusions",
    selection=SELECTION_WIRE_V1,
    members=ArrayWire(
        record_wire("grade_id evaluation_id grade_revision_id is_relevant input_digest")
    ),
    exclusions=ArrayWire(record_wire("grade_id reason")),
)
MANIFEST_WIRE_V2 = record_wire("format items")
CAPTURE_WIRE_V1 = record_wire("format population_digest observed_at")


def supports_snapshot(lineage: ArtifactLineage) -> bool:
    return (
        lineage.kind == "scout.corpus.snapshot"
        and lineage.process.id == "scout.corpus.select"
        and lineage.process.version in ("1", "2")
    )


def validate_revision_payload(item: FrozenGradeInput) -> Result[None, ArtifactError]:
    try:
        payload = GradeRevisionPayload.model_validate_json(item.revision.payload)
    except ValueError:
        return Err(
            ArtifactError(
                "validate_revision_payload", str(item.revision.id), "Corrupt grade revision payload"
            )
        )
    if (
        payload.id != item.revision.grade_id
        or payload.evaluation_id != item.revision.evaluation_id
        or payload.schema_version != item.revision.schema_version
    ):
        return Err(
            ArtifactError(
                "validate_revision_payload",
                str(item.revision.id),
                "Revision payload identity does not match its row",
            )
        )
    return Ok(None)


def preview_corpus(snapshot: CorpusSnapshot) -> CorpusPreview:
    counts = Counter(exclusion.reason for exclusion in snapshot.exclusions)
    positive_count = sum(member.is_relevant for member in snapshot.members)
    return CorpusPreview(
        selection=snapshot.selection,
        population_count=len(snapshot.members) + len(snapshot.exclusions),
        eligible_count=len(snapshot.members),
        positive_count=positive_count,
        negative_count=len(snapshot.members) - positive_count,
        exclusions=tuple(
            ExclusionCount(reason=reason, count=counts[reason]) for reason in sorted(counts)
        ),
    )


def _read_frozen_input(
    conn: sqlite3.Connection, row: GradePopulationRow, dossier_root: Path
) -> FrozenGradeInput:
    revision_row = conn.execute(
        "SELECT * FROM grade_revisions WHERE id = ?", (row.pinned_revision_id,)
    ).fetchone()
    revision = TypeAdapter(GradeRevision).validate_python(dict(revision_row))
    post_row = conn.execute("SELECT * FROM posts WHERE id = ?", (row.post_id,)).fetchone()
    evaluation_row = conn.execute(
        "SELECT * FROM evaluations WHERE id = ?", (row.evaluation_id,)
    ).fetchone()
    # Capture the recorded decision, not the operational boolean projection.
    evaluation = (
        None
        if evaluation_row is None
        else RecordedEvaluation.model_validate(
            {name: evaluation_row[name] for name in RecordedEvaluation.model_fields}
        )
    )
    current_grade = conn.execute(
        "SELECT g.*, rr.reply_text AS edited_text FROM grades g "
        "LEFT JOIN reply_draft_revisions rr ON rr.id = g.reply_revision_id WHERE g.id = ?",
        (row.grade_id,),
    ).fetchone()
    # Malformed storage evidence must not be downgraded to an ordinary exclusion.
    GradeRevisionPayload.model_validate_json(revision.payload)
    recorded_payload = json.loads(revision.payload)
    try:
        revision_matches = grade_revision_comparison_shape(current_grade) == recorded_payload
    except (ValueError, TypeError):
        # Invalid mutable grades remain exclusions; the immutable payload was
        # validated separately and cannot be hidden by this comparison guard.
        revision_matches = False
    context = None
    if (
        row.evaluation_project_key
        and row.evaluation_dossier_revision
        and row.evaluation_dossier_summary_id
    ):
        # Missing historical context is an exclusion, never replaced by HEAD.
        with contextlib.suppress(DossierResolutionError):
            context = resolve_dossier(
                dossier_root,
                row.evaluation_dossier_revision,
                row.evaluation_project_key,
                row.evaluation_dossier_summary_id,
            )
    exposures = tuple(
        RecordedExposure.model_validate(dict(exposure))
        for exposure in conn.execute(
            "SELECT fs.id AS snapshot_id, fs.scan_id, fp.phase, fi.grade_revision_id, fi.role "
            "FROM feedback_snapshot_items fi "
            "JOIN feedback_snapshot_phases fp ON fp.id = fi.snapshot_phase_id "
            "JOIN feedback_snapshots fs ON fs.id = fp.snapshot_id "
            "WHERE fi.grade_id = ? AND fs.mode = 'active' AND fi.role != 'excluded' "
            "ORDER BY fs.id, fp.id, fi.id",
            (row.grade_id,),
        )
    )
    phase_runs = tuple(
        TypeAdapter(PhaseRun).validate_python(
            {name: phase[name] for name in PhaseRun.__dataclass_fields__}
        )
        for phase in conn.execute(
            "SELECT * FROM evaluation_phase_runs WHERE evaluation_id = ? ORDER BY id",
            (row.evaluation_id,),
        )
    )
    return FrozenGradeInput(
        grade=row,
        revision=revision,
        revision_matches=revision_matches,
        post=None if post_row is None else RecordedPost.model_validate(dict(post_row)),
        evaluation=evaluation,
        phase_runs=phase_runs,
        context=context,
        exposures=exposures,
    )


def read_grade_population(
    conn: sqlite3.Connection, dossier_root: Path
) -> Result[FrozenGradePopulation, ArtifactError]:
    """Caller holds a read transaction. This never initializes or migrates a DB."""
    if not conn.in_transaction:
        return Err(
            ArtifactError("read_grade_population", None, "A stable read transaction is required")
        )
    try:
        rows = load_corpus_grade_population(conn)
        if len({row.grade_id for row in rows}) != len(rows):
            return Err(
                ArtifactError("read_grade_population", None, "Ambiguous grade/draft linkage")
            )
        items: list[FrozenGradeInput] = []
        for row in rows:
            try:
                item = _read_frozen_input(conn, row, dossier_root)
            except (sqlite3.Error, ValueError, TypeError, OSError, IndexError, KeyError):
                return Err(
                    ArtifactError(
                        "freeze_grade_input",
                        str(row.grade_id),
                        "Invalid source row, revision payload, or missing column",
                    )
                )
            validated = validate_revision_payload(item)
            if isinstance(validated, Err):
                return validated
            items.append(item)
        return Ok(
            FrozenGradePopulation(observed_at=datetime.now(UTC).isoformat(), items=tuple(items))
        )
    except (sqlite3.Error, ValueError, TypeError, OSError, IndexError, KeyError):
        return Err(
            ArtifactError("read_grade_population", None, "Cannot freeze source rows and revisions")
        )


def _input_exclusion(
    item: FrozenGradeInput, selection: CorpusSelection
) -> CorpusExclusionReason | None:
    row = item.grade
    if item.post is None:
        return "missing_post"
    if reason := grade_exclusion_reason(row):
        return reason
    if not item.revision_matches:
        return "revision_mismatch"
    if not row.evaluation_project_key:
        return "missing_project_context"
    if row.evaluation_project_key != selection.project_key:
        return "outside_project"
    if item.context is None:
        return "unavailable_pinned_context"
    if not item.post.content or not item.post.content.strip():
        return "missing_post_text"
    return None


def select_corpus(
    population: FrozenGradePopulation | PopulationItems, selection: CorpusSelection
) -> CorpusSnapshot:
    """Pure, replayable selection using only retained inputs; no caps or lookback."""
    members: list[CorpusMember] = []
    exclusions: list[CorpusExclusion] = []
    for item in sorted(population.items, key=lambda value: value.grade.grade_id):
        reason = _input_exclusion(item, selection)
        if reason is not None:
            exclusions.append(CorpusExclusion(grade_id=item.grade.grade_id, reason=reason))
            continue
        match derive_relevance_target(project_relevance_target_source(item.grade)):
            case Err(error):
                exclusions.append(
                    CorpusExclusion(grade_id=item.grade.grade_id, reason=error.reason)
                )
            case Ok(target):
                members.append(
                    CorpusMember(
                        grade_id=target.grade_id,
                        evaluation_id=target.evaluation_id,
                        grade_revision_id=item.revision.id,
                        is_relevant=target.is_relevant,
                        input_digest=digest_artifact(encode_wire_v1(item, INPUT_WIRE_V1)),
                    )
                )
    return CorpusSnapshot(selection=selection, members=tuple(members), exclusions=tuple(exclusions))


def build_snapshot_bundle(
    population: FrozenGradePopulation, selection: CorpusSelection, environment: bytes
) -> Result[ArtifactBundle, ArtifactError]:
    """Retain observations, per-item inputs, configuration, environment and output."""
    if not population.items:
        return Err(
            ArtifactError(
                "build_snapshot_bundle",
                selection.project_key,
                "No source grades; refusing an empty snapshot",
            )
        )
    for item in population.items:
        validated = validate_revision_payload(item)
        if isinstance(validated, Err):
            return validated
    snapshot = select_corpus(population, selection)
    item_bytes = tuple(encode_wire_v1(item, INPUT_WIRE_V1) for item in population.items)
    manifest = PopulationManifest(items=tuple(digest_artifact(content) for content in item_bytes))
    population_bytes = encode_wire_v1(manifest, MANIFEST_WIRE_V2)
    capture = PopulationCapture(
        population_digest=digest_artifact(population_bytes), observed_at=population.observed_at
    )
    config_bytes = encode_wire_v1(selection, SELECTION_WIRE_V1)
    output_bytes = encode_wire_v1(snapshot, SNAPSHOT_WIRE_V1)
    contents = (
        population_bytes,
        config_bytes,
        environment,
        output_bytes,
        encode_wire_v1(capture, CAPTURE_WIRE_V1),
        *item_bytes,
    )
    unique = {digest_artifact(content): content for content in contents}
    lineage = ArtifactLineage(
        kind=TransformKind("scout.corpus.snapshot"),
        inputs=(digest_artifact(population_bytes),),
        process=ArtifactProcess(
            id=ProcessId("scout.corpus.select"),
            version="2",
            config_digest=digest_artifact(config_bytes),
            environment=EnvironmentIdentity(digest_artifact(environment)),
        ),
        outputs=(digest_artifact(output_bytes),),
    )
    return Ok(
        ArtifactBundle(
            artifacts=tuple(
                RetainedArtifact(digest=digest, content=unique[digest]) for digest in sorted(unique)
            ),
            lineages=(lineage,),
        )
    )


def verify_snapshot_replay(bundle: ArtifactBundle) -> Result[int, ArtifactError]:
    """Verify supported snapshot transforms from retained bytes, with no live DB."""
    match validate_bundle(bundle):
        case Err() as error:
            return error
        case Ok():
            pass
    contents = {artifact.digest: artifact.content for artifact in bundle.artifacts}
    verified = 0
    for lineage in bundle.lineages:
        if not supports_snapshot(lineage):
            continue
        if len(lineage.inputs) != 1 or len(lineage.outputs) != 1:
            return Err(
                ArtifactError("verify_snapshot_replay", None, "Invalid snapshot producer shape")
            )
        try:
            population: FrozenGradePopulation | PopulationItems
            if lineage.process.version == "1":
                population = FrozenGradePopulation.model_validate_json(contents[lineage.inputs[0]])
            else:
                manifest = PopulationManifest.model_validate_json(contents[lineage.inputs[0]])
                population = PopulationItems(
                    items=tuple(
                        FrozenGradeInput.model_validate_json(contents[digest])
                        for digest in manifest.items
                    )
                )
            selection = CorpusSelection.model_validate_json(contents[lineage.process.config_digest])
        except (ValueError, KeyError):
            return Err(
                ArtifactError(
                    "verify_snapshot_replay", lineage.inputs[0], "Invalid snapshot inputs"
                )
            )
        if not population.items:
            return Err(
                ArtifactError("verify_snapshot_replay", lineage.inputs[0], "Empty population")
            )
        for item in population.items:
            validated = validate_revision_payload(item)
            if isinstance(validated, Err):
                return validated
        snapshot = select_corpus(population, selection)
        expected = encode_wire_v1(snapshot, SNAPSHOT_WIRE_V1)
        if expected != contents[lineage.outputs[0]]:
            return Err(
                ArtifactError(
                    "verify_snapshot_replay", lineage.outputs[0], "Snapshot is not re-derivable"
                )
            )
        for member in snapshot.members:
            if member.input_digest not in contents:
                return Err(
                    ArtifactError(
                        "verify_snapshot_replay", member.input_digest, "Missing member input"
                    )
                )
        verified += 1
    return Ok(verified)
