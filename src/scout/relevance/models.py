"""Shared types and routing constants for live zero-shot relevance."""

from dataclasses import dataclass
from typing import Literal

from scout.scanning.schemas import RelevancePhaseOutput

# The only projects with a measured v6 relevance catalogue.  Setup, live
# dispatch, and holdout selection all import this single routing authority.
ZEROSHOT_PROJECT_KEYS = frozenset({"agent-ops", "agent-evals"})

RelevanceAction = Literal["respond", "review", "drop"]


class ZeroShotRelevanceOutput(RelevancePhaseOutput):
    """Shared relevance fields plus the complete zero-shot routing evidence."""

    action: RelevanceAction
    answers: dict[str, float]
    line: str
    margin: tuple[str, ...]
    exclusion: str | None


@dataclass(frozen=True, slots=True)
class ZeroShotRelevanceError(Exception):
    """A retryable failure from the zero-shot relevance executor."""

    operation: str
    message_id: str
    detail: str

    def __str__(self) -> str:
        return f"{self.operation} failed for {self.message_id}: {self.detail}"


__all__ = [
    "ZEROSHOT_PROJECT_KEYS",
    "ZeroShotRelevanceError",
    "ZeroShotRelevanceOutput",
    "RelevanceAction",
]
