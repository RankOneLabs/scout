"""Pydantic schemas for scout's agent pipeline.

`ReplyCandidate` is the single output of `run_agent`. It folds the
evaluate / generate / critique results into one shape so the agent can
commit to a final answer in one `submit_output` call.

`unpack_candidate` splits the relevance and critique metadata back out for
callers that verify and persist the result; it never mints publishable text.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from scout.config import CritiqueResult, Message, RelevanceResult

# ---------------------------------------------------------------------------
# Structured draft output types (inbound reply drafting pipeline)
# ---------------------------------------------------------------------------

DraftPosture = Literal["answer", "engage", "ask", "abstain"]


class DeclarativeSegment(BaseModel):
    """A factual claim segment backed by a dossier fact."""

    type: Literal["declarative"]
    fact_id: str
    text: str

    @field_validator("fact_id", "text")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be non-empty")
        return v


class ResourceSegment(BaseModel):
    """A resource citation segment referencing a dossier resource."""

    type: Literal["resource"]
    resource_id: str

    @field_validator("resource_id")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be non-empty")
        return v


class QuestionSegment(BaseModel):
    """A question segment for engage/ask postures."""

    type: Literal["question"]
    text: str

    @field_validator("text")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be non-empty")
        return v


DraftSegment = Annotated[
    DeclarativeSegment | ResourceSegment | QuestionSegment,
    Field(discriminator="type"),
]


class StructuredDraftOutput(BaseModel):
    """LLM-structured reply draft ready for gate verification."""

    posture: DraftPosture
    segments: list[DraftSegment] = Field(default_factory=list)
    claims: list[str] = Field(default_factory=list)
    resources_used: list[str] = Field(default_factory=list)
    abstain_reason: str | None = None

    @model_validator(mode="after")
    def _abstain_reason_contract(self) -> StructuredDraftOutput:
        if self.posture == "abstain":
            if self.abstain_reason is None or not self.abstain_reason.strip():
                raise ValueError(
                    "abstain_reason required and non-whitespace when posture='abstain'"
                )
            if self.segments or self.claims or self.resources_used:
                raise ValueError(
                    "segments, claims, and resources_used must be empty when posture='abstain'"
                )
        elif self.abstain_reason is not None:
            raise ValueError("abstain_reason must be None unless posture='abstain'")
        return self


# ---------------------------------------------------------------------------
# Content engine schemas
# ---------------------------------------------------------------------------

POST_TEXT_MAX_CHARS = 300
ARTICLE_TEXT_MAX_CHARS = 8000  # ~1500 words; generous safety net, not a soft target
FARCASTER_POST_MAX_BYTES = 320  # Farcaster cast limit is UTF-8 bytes, not code points


def validate_farcaster_byte_length(text: str) -> str | None:
    """Return an error message if text exceeds FARCASTER_POST_MAX_BYTES UTF-8 bytes.

    Returns None when the text is within the limit.
    """
    byte_len = len(text.encode("utf-8"))
    if byte_len > FARCASTER_POST_MAX_BYTES:
        return (
            f"text is {byte_len} UTF-8 bytes; "
            f"Farcaster limit is {FARCASTER_POST_MAX_BYTES} bytes"
        )
    return None

Verdict = Literal["approve", "revise", "reject"]

class ReplyCandidate(BaseModel):
    """Agent output: relevance verdict + optional draft + optional self-critique."""

    relevant: bool
    score: float = Field(ge=0.0, le=1.0)
    reason: str
    relevant_to: list[str] = Field(default_factory=list)

    project_key: str | None = None
    critique_verdict: Verdict | None = None
    critique_feedback: str | None = None
    structured_draft: StructuredDraftOutput | None = None

    # Ordered, deduplicated evaluation_phase_runs ids of every phase this
    # pipeline run actually executed successfully before returning this
    # candidate — relevance only for an irrelevant verdict; relevance +
    # reply_draft for an abstain; all three when critic ran. Consumed by
    # classify_outcome/persist_outcome to link exactly this evidence to the
    # evaluation it produces. Never populated from any source but the
    # pipeline's own PhaseExecution results — see pipeline.score_and_draft_step.
    contributor_phase_run_ids: tuple[int, ...] = ()


class RelevancePhaseOutput(BaseModel):
    """Relevance phase output: cheap triage before any reply drafting."""

    relevant: bool
    score: float = Field(ge=0.0, le=1.0)
    reason: str
    relevant_to: list[str] = Field(default_factory=list)


class CritiquePhaseOutput(BaseModel):
    """Critic phase output: approve, reject, or revise the draft."""

    verdict: Verdict
    feedback: str
    revised_draft: StructuredDraftOutput | None = None

    @model_validator(mode="after")
    def _revision_required_when_revising(self) -> CritiquePhaseOutput:
        if self.verdict == "revise" and self.revised_draft is None:
            raise ValueError("revised_draft required when verdict='revise'")
        return self


def unpack_candidate(
    candidate: ReplyCandidate,
    message: Message,
) -> tuple[RelevanceResult, CritiqueResult | None]:
    """Split a ReplyCandidate into (relevance, critique) metadata.

    This only carries what's needed before dossier-aware gate verification
    runs. It never constructs publishable text — a StructuredDraftOutput
    cannot be validly turned into shipping text until the verifier's
    assemble_draft_text has expanded it against a dossier. Callers that need
    the draft or project read
    `candidate.structured_draft` / `candidate.project_key` directly.
    """
    relevance = RelevanceResult(
        message=message,
        relevant=candidate.relevant,
        score=candidate.score,
        reason=candidate.reason,
        relevant_to=tuple(candidate.relevant_to),
    )

    critique: CritiqueResult | None = None
    if candidate.critique_verdict is not None:
        critique = CritiqueResult(
            verdict=candidate.critique_verdict,
            feedback=candidate.critique_feedback or "",
        )

    return relevance, critique
