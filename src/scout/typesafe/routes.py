"""Pure route policies and the typesafe shadow route selector."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from scout.registry import KeywordRoute

AGENT_OPS_PROJECT_KEY = "agent-ops"

Action = Literal["respond", "review", "drop"]

EXCLUSION_PREFIX = "excl_"
ROUTED = ("needs_thread", "answerable_from_post", "about_agent_work", "points_somewhere")


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Every threshold starts at 0.5 and the margin at 0.1; nothing is fitted."""

    exclusion: float = 0.5
    thread: float = 0.5
    answerable: float = 0.5
    about: float = 0.5
    points: float = 0.5
    margin: float = 0.1


@dataclass(frozen=True, slots=True)
class RouteDecision:
    action: Action
    path_action: Action
    line: str
    margin: tuple[str, ...]
    exclusion: str | None
    features: Mapping[str, float]


DEFAULT = Thresholds()


def _noul(answers: Mapping[str, float], name: str) -> float:
    value = answers.get(name)
    if not isinstance(value, (int, float)):
        raise ValueError(f"missing noul for {name}")
    return float(value)


def route(answers: Mapping[str, float], t: Thresholds = DEFAULT) -> RouteDecision:
    """Decide respond / review / drop from one post's feature probabilities."""
    excl = {
        key.removeprefix(EXCLUSION_PREFIX): _noul(answers, key)
        for key in answers
        if key.startswith(EXCLUSION_PREFIX)
    }
    if not excl:
        raise ValueError("no excl_* answers")
    top = max(excl, key=excl.__getitem__)
    thread = _noul(answers, "needs_thread")
    answerable = _noul(answers, "answerable_from_post")
    about = _noul(answers, "about_agent_work")
    points = _noul(answers, "points_somewhere")

    consulted: list[tuple[str, float, float]] = [(f"excl_{top}", excl[top], t.exclusion)]
    if excl[top] >= t.exclusion:
        action: Action = "drop"
        line = "exclusion"
    else:
        consulted.append(("needs_thread", thread, t.thread))
        if thread >= t.thread:
            action, line = "review", "needs_thread"
        else:
            consulted += [
                ("answerable_from_post", answerable, t.answerable),
                ("about_agent_work", about, t.about),
            ]
            if answerable >= t.answerable and about >= t.about:
                action, line = "respond", "respond"
            else:
                consulted.append(("points_somewhere", points, t.points))
                if points >= t.points:
                    action, line = "review", "points_somewhere"
                else:
                    action, line = "drop", "otherwise"

    close = tuple(
        name for name, value, threshold in consulted if abs(value - threshold) <= t.margin
    )
    return RouteDecision(
        action="review" if close else action,
        path_action=action,
        line=line,
        margin=close,
        exclusion=top if excl[top] >= t.exclusion else None,
        features={
            **{f"excl_{name}": value for name, value in excl.items()},
            "needs_thread": thread,
            "answerable_from_post": answerable,
            "about_agent_work": about,
            "points_somewhere": points,
        },
    )


def is_agent_ops_route(route: KeywordRoute | None) -> bool:
    return route is not None and route.project_key == AGENT_OPS_PROJECT_KEY
