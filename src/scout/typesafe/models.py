"""Stable wire models shared by catalogue, backends, and persistence."""

from __future__ import annotations

from typing import Annotated, Literal, NewType

from pydantic import BaseModel, ConfigDict, Field, model_validator

CatalogueVersion = NewType("CatalogueVersion", str)
Probability = Annotated[float, Field(ge=0, le=1)]


class ProbabilityAnswer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["probability"] = "probability"
    probability: Probability
    confidence: Probability = 1.0


class ChoiceAnswer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["choice"] = "choice"
    probabilities: dict[str, Probability] = Field(min_length=1)
    confidence: Probability


class LevelProbability(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    level: str
    probability: Probability


class ScoreAnswer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["score"] = "score"
    levels: list[LevelProbability]
    confidence: Probability

    @model_validator(mode="after")
    def levels_are_unique(self) -> ScoreAnswer:
        names = [item.level for item in self.levels]
        if len(names) != len(set(names)):
            raise ValueError("score answer levels must be unique")
        return self


Answer = Annotated[
    ProbabilityAnswer | ChoiceAnswer | ScoreAnswer,
    Field(discriminator="kind"),
]


class Usage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class Answers(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    answers: dict[str, Answer]
    request_id: str = Field(min_length=1)
    model: str
    usage: Usage = Field(default_factory=Usage)
    latency_ms: int = Field(default=0, ge=0)


class DecisionRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    eligible: bool
    p_eligible: float = Field(ge=0, le=1)
    uncertain: bool
    reason: str
    account_label: str | None = None
    account_confidence: float | None = Field(default=None, ge=0, le=1)
    details: dict[str, object] = Field(default_factory=dict)


class BackendError(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    operation: str
    entity_id: str
    status: int | None = None
    request_id: str | None = None
    detail: str | None = None
