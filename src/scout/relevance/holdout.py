"""Pure eligibility and random draw for zero-shot relevance holdouts."""

from __future__ import annotations

from typing import Literal, Protocol

from scout.relevance.models import ZEROSHOT_PROJECT_KEYS, RelevanceAction


class RandomSource(Protocol):
    def random(self) -> float: ...


def draw_relevance_holdout(
    *,
    classifier: Literal["llm", "zeroshot"],
    project_key: str | None,
    production_action: RelevanceAction,
    rate: float,
    rng: RandomSource,
) -> bool:
    """Draw only for zero-shot-routed projects, covering every production action."""
    if classifier != "zeroshot" or project_key not in ZEROSHOT_PROJECT_KEYS:
        return False
    return rng.random() < rate


__all__ = ["RandomSource", "draw_relevance_holdout"]
