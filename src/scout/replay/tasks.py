"""Experiment task inputs derived from Scout's retained corpus and phase rows.

Drafting keeps its existing correction population. Relevance uses a frozen
snapshot, optionally narrowed by a pinned train/heldout partition. An all-corpus
run is exploratory, never a population-rate or held-out estimate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal

from jig import Grader, Score, ScoreSource
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from scout.grading.artifacts import ArtifactError, DigestReference
from scout.grading.assistance import validate_partition
from scout.grading.assistance_scope import read_assistance_bundle
from scout.grading.assistance_store import load_corpus_snapshot, load_training_examples
from scout.grading.assistance_types import FrozenPartition, RejectedPopulation, SelectionReference
from scout.grading.snapshots import FrozenGradeInput
from scout.result import Err, Ok, Result
from scout.scanning.schemas import RelevancePhaseOutput
from scout.storage.evaluations import PhaseRun
from scout.storage.state import StateManager

RELEVANCE_GRADER_VERSION: Literal["relevance_exact_match/v1"] = "relevance_exact_match/v1"


@dataclass(frozen=True, slots=True)
class ReplayWorkerConfiguration:
    """Controlled replay settings captured before an attempt can spend."""

    phase: str
    model: str
    system_prompt_sha256: str
    output_schema_sha256: str
    max_tool_calls: int
    max_llm_calls: int
    max_parse_retries: int
    jig_revision: str
    grader_version: str | None
    assembler_version: str | None
    tools: tuple[str, ...]
    include_memory_in_prompt: bool
    include_feedback_in_prompt: bool
    # Old manifests had no explicit limit. Never backfill today's bound.
    max_output_tokens: int | None = None
    # The candidate's reasoning switch: None is the provider default.
    # Old manifests predate the field and mean the same thing.
    reasoning: bool | None = None


class RelevanceGrader(Grader[RelevancePhaseOutput]):  # type: ignore[misc]
    def __init__(self, target: RelevanceTarget) -> None:
        self.target = target

    async def grade(
        self,
        input: Any,
        output: RelevancePhaseOutput,
        context: dict[str, Any] | None = None,
    ) -> list[Score]:
        return [
            Score(
                dimension=RELEVANCE_GRADER_VERSION,
                value=float(output.relevant == self.target.is_relevant),
                source=ScoreSource.GROUND_TRUTH,
            )
        ]


class RelevanceTask(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["relevance"] = "relevance"
    snapshot_digest: DigestReference
    partition_digest: DigestReference | None = None
    partition: Literal["all", "train", "heldout"] = "all"

    @model_validator(mode="after")
    def require_partition_identity(self) -> RelevanceTask:
        if (self.partition == "all") != (self.partition_digest is None):
            raise ValueError("train/heldout requires a partition digest; all has no partition")
        return self


class ReplyDraftTask(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["reply_draft"] = "reply_draft"


type ExperimentTask = Annotated[ReplyDraftTask | RelevanceTask, Field(discriminator="kind")]
EXPERIMENT_TASK_ADAPTER: TypeAdapter[ExperimentTask] = TypeAdapter(ExperimentTask)


class RelevanceTarget(BaseModel):
    """Pinned CorpusMember label plus phase/partition identity, never a live grade."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    task: RelevanceTask
    evaluation_id: int
    grade_revision_id: int
    input_digest: DigestReference
    project_key: str
    is_relevant: bool
    provenance: tuple[SelectionReference, ...]


class RelevanceCaseEvidence(BaseModel):
    """Versioned projection of the baseline, resolved worker, and frozen target."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    version: Literal[3] = 3
    task: Literal["relevance"] = "relevance"
    recorded_input_sha256: DigestReference
    baseline_model: str
    baseline_prompt_sha256: DigestReference
    baseline_prompt_reused: bool
    candidate_model: str
    candidate_prompt_sha256: DigestReference
    worker_configuration: ReplayWorkerConfiguration
    estimated_usd: float | None
    target: RelevanceTarget


class RelevanceScore(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    format: Literal["scout.relevance-score/v1"] = "scout.relevance-score/v1"
    grader_version: Literal["relevance_exact_match/v1"] = RELEVANCE_GRADER_VERSION
    target: RelevanceTarget
    baseline_relevant: bool
    candidate_relevant: bool
    baseline_correct: bool
    candidate_correct: bool
    # Positive means improved accuracy, unlike drafting distance's negative delta.
    accuracy_delta: Literal[-1, 0, 1]

    @model_validator(mode="after")
    def validate_correctness(self) -> RelevanceScore:
        if (
            self.baseline_correct != (self.baseline_relevant == self.target.is_relevant)
            or self.candidate_correct != (self.candidate_relevant == self.target.is_relevant)
            or self.accuracy_delta != int(self.candidate_correct) - int(self.baseline_correct)
        ):
            raise ValueError("Relevance correctness/delta differs from predictions and target")
        return self


@dataclass(frozen=True, slots=True)
class RelevanceCaseSource:
    target: RelevanceTarget
    phase_run: PhaseRun
    source: FrozenGradeInput


class RelevanceSourceExclusion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    evaluation_id: int
    reason: Literal["missing_complete_relevance_phase"]


@dataclass(frozen=True, slots=True)
class RelevancePopulation:
    cases: tuple[RelevanceCaseSource, ...]
    exclusions: tuple[RelevanceSourceExclusion, ...]
    dropped_duplicate_phase_run_ids: tuple[int, ...]


def load_relevance_population(
    state: StateManager,
    task: RelevanceTask,
) -> Result[RelevancePopulation, ArtifactError]:
    """Read exact frozen phase links; later grades/scans cannot expand this run."""
    roots = (
        (task.snapshot_digest,)
        if task.partition_digest is None
        else (
            task.snapshot_digest,
            task.partition_digest,
        )
    )
    with state.db.read_transaction():
        retained = read_assistance_bundle(state.conn, roots)
    if isinstance(retained, Err):
        return retained
    bundle = retained.value
    snapshot_result = load_corpus_snapshot(bundle, task.snapshot_digest)
    if isinstance(snapshot_result, Err):
        return snapshot_result
    snapshot = snapshot_result.value
    contents = {artifact.digest: artifact.content for artifact in bundle.artifacts}
    try:
        partition = None
        if task.partition_digest is not None:
            partition = FrozenPartition.model_validate_json(contents[task.partition_digest])
            if partition.snapshot_digest != task.snapshot_digest:
                raise ValueError("Partition belongs to another snapshot")
            examples = load_training_examples(bundle, task.snapshot_digest)
            if isinstance(examples, Err):
                return examples
            # The recorded group IDs also retain grouping through posts outside the
            # corpus. Recheck direct corpus edges as well as those retained groups.
            checked = validate_partition(
                examples.value,
                RejectedPopulation(project_key=snapshot.selection.project_key, items=()),
                partition,
            )
            if isinstance(checked, Err):
                return checked
            assignments: dict[str, str] = {}
            for partition_member in partition.members:
                if (
                    partition_member.group_id in assignments
                    and assignments[partition_member.group_id] != partition_member.partition
                ):
                    raise ValueError("Recorded related group crosses partitions")
                assignments[partition_member.group_id] = partition_member.partition
        partition_members = (
            {}
            if partition is None
            else {member.evaluation_id: member for member in partition.members}
        )
        cases: list[RelevanceCaseSource] = []
        exclusions: list[RelevanceSourceExclusion] = []
        dropped: list[int] = []
        for member in snapshot.members:
            assigned = partition_members.get(member.evaluation_id)
            if assigned is not None and assigned.partition != task.partition:
                continue
            source = FrozenGradeInput.model_validate_json(contents[member.input_digest])
            phases = sorted(
                (
                    phase
                    for phase in source.phase_runs
                    if phase.phase == "relevance" and phase.status == "complete"
                ),
                key=lambda phase: phase.id,
            )
            if not phases:
                exclusions.append(
                    RelevanceSourceExclusion(
                        evaluation_id=member.evaluation_id,
                        reason="missing_complete_relevance_phase",
                    )
                )
                continue
            phase = phases[-1]
            if (
                phase.evaluation_id != member.evaluation_id
                or source.post is None
                or phase.post_id != source.post.id
            ):
                raise ValueError("Frozen phase does not belong to corpus evaluation/post")
            dropped.extend(prior.id for prior in phases[:-1])
            cases.append(
                RelevanceCaseSource(
                    target=RelevanceTarget(
                        task=task,
                        evaluation_id=member.evaluation_id,
                        grade_revision_id=member.grade_revision_id,
                        input_digest=member.input_digest,
                        project_key=snapshot.selection.project_key,
                        is_relevant=member.is_relevant,
                        provenance=() if assigned is None else assigned.provenance,
                    ),
                    phase_run=phase,
                    source=source,
                )
            )
        if not cases:
            raise ValueError("No complete relevance baselines in the selected corpus partition")
        return Ok(
            RelevancePopulation(
                cases=tuple(sorted(cases, key=lambda case: case.phase_run.id)),
                exclusions=tuple(exclusions),
                dropped_duplicate_phase_run_ids=tuple(sorted(dropped)),
            )
        )
    except ValidationError:
        return Err(
            ArtifactError(
                "relevance_population", task.snapshot_digest, "Retained corpus record is invalid"
            )
        )
    except (ValueError, KeyError) as exc:
        return Err(ArtifactError("relevance_population", task.snapshot_digest, str(exc)))


def relevance_score(
    target: RelevanceTarget,
    baseline_relevant: bool,
    candidate_relevant: bool,
) -> RelevanceScore:
    baseline_correct = baseline_relevant == target.is_relevant
    candidate_correct = candidate_relevant == target.is_relevant
    return RelevanceScore.model_validate(
        {
            "target": target,
            "baseline_relevant": baseline_relevant,
            "candidate_relevant": candidate_relevant,
            "baseline_correct": baseline_correct,
            "candidate_correct": candidate_correct,
            "accuracy_delta": int(candidate_correct) - int(baseline_correct),
        }
    )
