"""Review observations: queue/evaluation identity and the existing grade contract.

Mirrors ReviewQueue sources, grade_revisions.id, and reviewer browser actions.
Observations are source data, not a claim to replay a person's decision.
"""

from __future__ import annotations

from typing import Annotated, Literal, NewType, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from scout.grading.artifacts import DigestReference, NonblankText

ReviewActionId = NewType("ReviewActionId", str)
type ActionId = Annotated[
    ReviewActionId, StringConstraints(pattern=r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
]


class ReviewDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class ReviewGrade(ReviewDocument):
    """GradeInput fields; shared grading schema and StateManager validate semantics."""

    relevance_judgment: Literal["correct", "false_positive", "false_negative"]
    action_judgment: Literal["accept", "fail"]
    dimensions: list[str] | None = None
    failure_note: str | None = None
    factual_offending_claim: str | None = None
    factual_disposition: str | None = None
    factual_contradicting_evidence: str | None = None
    context_missing_input: str | None = None
    posture_should_have_been: str | None = None
    implication_implied_claim: str | None = None
    implication_missing_support: str | None = None
    edited_text: str | None = None


class ReviewPriceBasis(ReviewDocument):
    usd_per_hour: float = Field(ge=0)
    basis: NonblankText


class ReviewTiming(ReviewDocument):
    elapsed_ms: int | None = Field(ge=0)
    method: Literal["active-visible-idle60/v1"] | None

    @model_validator(mode="after")
    def paired_measurement(self) -> Self:
        if (self.elapsed_ms is None) != (self.method is None):
            raise ValueError("Unavailable timing requires both elapsed_ms and method null")
        return self


class GradeAction(ReviewDocument):
    kind: Literal["grade"]
    grade: ReviewGrade


class SkipAction(ReviewDocument):
    kind: Literal["skip"]
    reason: NonblankText


class ReconcileAction(ReviewDocument):
    kind: Literal["reconcile"]


type ReviewAction = Annotated[
    GradeAction | SkipAction | ReconcileAction, Field(discriminator="kind")
]


class ReviewRequest(ReviewDocument):
    action_id: ActionId
    expected_grade_revision_id: int | None = Field(gt=0)
    expected_action_id: ActionId | None
    action: ReviewAction
    timing: ReviewTiming
    pricing: ReviewPriceBasis | None = None

    @model_validator(mode="after")
    def reconciliation_is_not_review_time(self) -> Self:
        if self.action.kind == "reconcile" and (
            self.timing.elapsed_ms is not None or self.pricing is not None
        ):
            raise ValueError("Reconciliation cannot manufacture review time or price")
        return self


class ReviewDisposition(ReviewDocument):
    format: Literal["scout.review-disposition/v1"] = "scout.review-disposition/v1"
    action_id: ActionId
    queue_digest: DigestReference
    evaluation_id: int = Field(gt=0)
    action: ReviewAction
    recorded_at: str
    timing: ReviewTiming
    pricing: ReviewPriceBasis | None
    grade_revision_id: int | None = Field(gt=0)

    @model_validator(mode="after")
    def revision_matches_action(self) -> Self:
        if (self.action.kind == "skip") != (self.grade_revision_id is None):
            raise ValueError("Only skip has no grade revision; grade/reconcile must pin one")
        return self


class ReviewError(ReviewDocument):
    operation: Literal["review_action"] = "review_action"
    evaluation_id: int
    detail: str
    status: Literal[400, 404, 409, 500]
