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

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, TypeAdapter

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
    try:
        revision_matches = grade_revision_comparison_shape(current_grade) == json.loads(
            revision.payload
        )
    except (ValueError, TypeError):
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
        items = tuple(_read_frozen_input(conn, row, dossier_root) for row in rows)
        return Ok(FrozenGradePopulation(observed_at=datetime.now(UTC).isoformat(), items=items))
    except (sqlite3.Error, ValueError, TypeError, OSError):
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


def select_corpus(population: FrozenGradePopulation, selection: CorpusSelection) -> CorpusSnapshot:
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
                        input_digest=digest_artifact(item.model_dump_json().encode()),
                    )
                )
    return CorpusSnapshot(selection=selection, members=tuple(members), exclusions=tuple(exclusions))


def build_snapshot_bundle(
    population: FrozenGradePopulation, selection: CorpusSelection, environment: bytes
) -> ArtifactBundle:
    """Retain observations, per-item inputs, configuration, environment and output."""
    snapshot = select_corpus(population, selection)
    population_bytes = population.model_dump_json().encode()
    config_bytes = selection.model_dump_json().encode()
    output_bytes = snapshot.model_dump_json().encode()
    contents = (
        population_bytes,
        config_bytes,
        environment,
        output_bytes,
        *(item.model_dump_json().encode() for item in population.items),
    )
    unique = {digest_artifact(content): content for content in contents}
    lineage = ArtifactLineage(
        kind=TransformKind("scout.corpus.snapshot"),
        inputs=(digest_artifact(population_bytes),),
        process=ArtifactProcess(
            id=ProcessId("scout.corpus.select"),
            version="1",
            config_digest=digest_artifact(config_bytes),
            environment=EnvironmentIdentity(digest_artifact(environment)),
        ),
        outputs=(digest_artifact(output_bytes),),
    )
    return ArtifactBundle(
        artifacts=tuple(
            RetainedArtifact(digest=digest, content=unique[digest]) for digest in sorted(unique)
        ),
        lineages=(lineage,),
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
        if lineage.kind != "scout.corpus.snapshot":
            continue
        if (
            lineage.process.id != "scout.corpus.select"
            or lineage.process.version != "1"
            or len(lineage.inputs) != 1
            or len(lineage.outputs) != 1
        ):
            return Err(
                ArtifactError("verify_snapshot_replay", None, "Unsupported snapshot producer")
            )
        try:
            population = FrozenGradePopulation.model_validate_json(contents[lineage.inputs[0]])
            selection = CorpusSelection.model_validate_json(contents[lineage.process.config_digest])
        except ValueError:
            return Err(
                ArtifactError(
                    "verify_snapshot_replay", lineage.inputs[0], "Invalid snapshot inputs"
                )
            )
        snapshot = select_corpus(population, selection)
        expected = snapshot.model_dump_json().encode()
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
