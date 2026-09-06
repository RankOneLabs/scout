"""Versioned Scout selector payloads, derived from retained corpus and DB rows.

These are payloads of the existing artifact/lineage store, not new authorities.
Human labels come only from CorpusMember; rejected decisions never become labels.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, NewType

from pydantic import BaseModel, ConfigDict, Field

from scout.dossiers.resolver import DossierResolution
from scout.grading.artifacts import ArtifactDigest, DigestReference
from scout.grading.snapshots import RecordedEvaluation, RecordedPost

GroupId = NewType("GroupId", str)


class AssistanceDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class RejectedInput(AssistanceDocument):
    """Recorded evaluation, post, pinned dossier, and grade-presence observation."""

    evaluation: RecordedEvaluation
    post: RecordedPost | None
    context: DossierResolution | None
    has_grade: bool


class RejectedPopulation(AssistanceDocument):
    format: Literal["scout.rejected-population/v1"] = "scout.rejected-population/v1"
    project_key: str
    items: tuple[RejectedInput, ...]


class TfidfSelector(AssistanceDocument):
    kind: Literal["tfidf_logistic"] = "tfidf_logistic"
    count: int = Field(default=20, ge=1)
    regularization_c: float = Field(default=1.0, gt=0)
    class_weight: Literal["balanced"] | None = "balanced"
    max_features: int = Field(default=20000, ge=1)
    max_iterations: int = Field(default=1000, ge=1)
    # Versioned producer fixes lowercase word unigrams/bigrams, L2 TF-IDF,
    # smooth IDF, raw term counts, liblinear/L2, and probability threshold 0.5.


class RandomSelector(AssistanceDocument):
    kind: Literal["seeded_random"] = "seeded_random"
    count: int = Field(ge=1)
    seed: int = Field(ge=0, le=4294967295)
    design: Literal["srs_without_replacement_evaluations/v1"] = (
        "srs_without_replacement_evaluations/v1"
    )


type SelectorConfig = Annotated[TfidfSelector | RandomSelector, Field(discriminator="kind")]


class AssistanceConfig(AssistanceDocument):
    format: Literal["scout.assistance-config/v1"] = "scout.assistance-config/v1"
    seed: int = Field(default=0, ge=0, le=4294967295)
    heldout_fraction: float = Field(default=0.2, gt=0, lt=1)
    ranked: TfidfSelector | None = Field(default_factory=TfidfSelector)
    random: RandomSelector


class SelectionReference(AssistanceDocument):
    queue_digest: DigestReference
    method: Literal["ranked", "random"]


@dataclass(frozen=True, slots=True)
class TrainingExample:
    """Projection of CorpusMember and its exact FrozenGradeInput."""

    evaluation_id: int
    input_digest: ArtifactDigest
    post: RecordedPost
    is_relevant: bool
    exposed: bool
    provenance: tuple[SelectionReference, ...]


class PartitionMember(AssistanceDocument):
    evaluation_id: int
    input_digest: DigestReference
    group_id: GroupId
    partition: Literal["train", "heldout"]
    exposed: bool
    provenance: tuple[SelectionReference, ...]


class FrozenPartition(AssistanceDocument):
    format: Literal["scout.assistance-partition/v1"] = "scout.assistance-partition/v1"
    snapshot_digest: DigestReference
    members: tuple[PartitionMember, ...]


class FittedTfidf(AssistanceDocument):
    format: Literal["scout.tfidf-logistic/v1"] = "scout.tfidf-logistic/v1"
    train_evaluation_ids: tuple[int, ...]
    vocabulary: tuple[str, ...]
    idf: tuple[float, ...]
    coefficients: tuple[float, ...]
    intercept: float
    iterations: int


class TermContribution(AssistanceDocument):
    term: str
    contribution: float


class ScoredCandidate(AssistanceDocument):
    evaluation_id: int
    probability: float = Field(ge=0, le=1)
    explanation: tuple[TermContribution, ...]


class SelectorResult(AssistanceDocument):
    """Sampled IDs are evaluation units, never silently deduplicated for rates."""

    kind: Literal["tfidf_logistic", "seeded_random"]
    population_evaluation_ids: tuple[int, ...]
    selected_evaluation_ids: tuple[int, ...]
    scores: tuple[ScoredCandidate, ...] = ()
    explanation_method: Literal["tfidf-times-coefficient/v1"] | None = None


class QueueSource(AssistanceDocument):
    evaluation_id: int
    ranked_position: int | None
    random_position: int | None


class ReviewQueueItem(AssistanceDocument):
    """One display item; each source still requires its own exact grade write."""

    duplicate_key: ArtifactDigest
    sources: tuple[QueueSource, ...]


class ReviewQueue(AssistanceDocument):
    format: Literal["scout.review-queue/v1"] = "scout.review-queue/v1"
    project_key: str
    population_digest: DigestReference
    items: tuple[ReviewQueueItem, ...]
    ranked: SelectorResult | None
    random: SelectorResult


class ConfusionCounts(AssistanceDocument):
    true_positive: int
    true_negative: int
    false_positive: int
    false_negative: int


class HeldoutComparison(AssistanceDocument):
    sample_count: int
    random_provenance_count: int
    classifier: ConfusionCounts
    train_majority_baseline: ConfusionCounts
    # Diagnostic held-out performance, never a population rate estimate.


type CandidateExclusionReason = Literal[
    "outside_project",
    "missing_project_context",
    "not_rejected",
    "already_graded",
    "missing_post",
    "missing_post_text",
    "unavailable_pinned_context",
    "invalid_original_decision",
    "graded_group",
]


class CandidateExclusion(AssistanceDocument):
    evaluation_id: int
    reason: CandidateExclusionReason


class AssistanceReport(AssistanceDocument):
    format: Literal["scout.assistance-report/v1"] = "scout.assistance-report/v1"
    project_key: str
    source_count: int
    candidate_count: int
    candidate_duplicate_count: int
    selected_evaluation_count: int
    queue_item_count: int
    duplicates_removed: int
    overlap_count: int
    exclusions: tuple[CandidateExclusion, ...]
    heldout: HeldoutComparison | None


@dataclass(frozen=True, slots=True)
class AssistanceOutputs:
    partition: FrozenPartition
    model: FittedTfidf | None
    queue: ReviewQueue
    report: AssistanceReport


class ExecutionObservation(AssistanceDocument):
    """Measured source observation, not part of deterministic transform identity."""

    format: Literal["scout.assistance-execution/v1"] = "scout.assistance-execution/v1"
    queue_digest: DigestReference
    lineage_digest: DigestReference
    observed_at: str
    elapsed_ms: float
    cpu_ms: float
    process_peak_rss_bytes: int | None


class ReviewOutcome(AssistanceDocument):
    """A later pinned human label; absent/skip is represented by no outcome."""

    evaluation_id: int
    is_relevant: bool
    grade_revision_id: int


class ReviewYield(AssistanceDocument):
    selected_count: int
    reviewed_count: int
    relevant_count: int
    discovery_yield: float | None


class RandomRate(AssistanceDocument):
    population_count: int
    sample_count: int
    reviewed_count: int
    relevant_count: int
    unreviewed_evaluation_ids: tuple[int, ...]
    estimated_relevant_fraction: float | None
    wilson_95_low: float | None
    wilson_95_high: float | None


class QueueReviewReport(AssistanceDocument):
    queue_digest: DigestReference
    snapshot_digest: DigestReference | None = None
    outcomes: tuple[ReviewOutcome, ...]
    random: RandomRate
    ranked: ReviewYield | None
