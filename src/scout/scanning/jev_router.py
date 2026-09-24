"""The JEV routing decision over a catalogue's feature probabilities.

Ported from ``RankOneLabs/assay``, ``experiments/typesafe_relevance/route.py``
at commit ``282479cdc877d1d470840684e8cbea891f9c54d3``. See
docs/relevance-holdouts.md for why that SHA is the pin and why the source
itself, rather than a written round spec, is the specification of record.

The decision, in order, stopping at the first match::

    any excl_* >= T_ex                                       -> drop
    needs_thread >= T_t                                      -> review
    answerable_from_post >= T_a and about_agent_work >= T_w   -> respond
    points_somewhere >= T_p                                  -> review
    otherwise                                                -> drop

Then one override: if any feature the path actually consulted is within +/- m
of its threshold, the outcome becomes ``review``. The exclusion line consults
one value, the largest ``excl_*`` probability, since that alone decides whether
"any" holds — so a feature the path never reached is never close enough to
matter, even sitting exactly on its threshold. Every answer named ``excl_*`` is
an exclusion, so the catalogue decides which exclusions exist.

The port differs from the source in one way only: it returns a typed
``Result``/``RouteDecision`` rather than raising and returning a dict. The
decision's content, ordering and threshold defaults are unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from scout.result import Err, Ok, Result

JevAction = Literal["respond", "review", "drop"]
DecidingLine = Literal[
    "exclusion", "needs_thread", "respond", "points_somewhere", "otherwise"
]

EXCLUSION_PREFIX = "excl_"
#: The features ``route`` reads besides the exclusions.
ROUTED: tuple[str, ...] = (
    "needs_thread",
    "answerable_from_post",
    "about_agent_work",
    "points_somewhere",
)

#: Identifies which router produced a stored decision. Bump when the decision
#: changes, so an evaluation's provenance keeps explaining the row beside it.
ROUTER_VERSION = "jev-route/round4"


@dataclass(frozen=True, slots=True)
class JevRouteError:
    """An answer vector could not yield a routing decision."""

    operation: str
    detail: str


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Every threshold starts at 0.5 and the margin at 0.1; nothing is fitted."""

    exclusion: float = 0.5
    thread: float = 0.5
    answerable: float = 0.5
    about: float = 0.5
    points: float = 0.5
    margin: float = 0.1


DEFAULT_THRESHOLDS = Thresholds()


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """One post's complete routing decision, as stored.

    ``path_action`` is the action the five ordered lines produced before the
    margin override; ``action`` is what the post is actually routed on. Keeping
    both is what lets a review that came from a near-threshold feature be told
    apart from one the path chose outright.
    """

    action: JevAction
    path_action: JevAction
    line: DecidingLine
    margin: tuple[str, ...]
    exclusion: str | None
    features: Mapping[str, float]

    def as_record(self) -> dict[str, Any]:
        """The decision as stored JSON: every field the router produced."""
        return {
            "action": self.action,
            "path_action": self.path_action,
            "line": self.line,
            "margin": list(self.margin),
            "exclusion": self.exclusion,
            "features": dict(self.features),
            "router_version": ROUTER_VERSION,
        }

    @property
    def reason(self) -> str:
        """The line that decided this, and the exclusion when one fired."""
        if self.exclusion is not None:
            return f"{self.line}: {self.exclusion}"
        if self.margin:
            return f"{self.line} (margin: {', '.join(self.margin)})"
        return self.line


def _noul(answers: Mapping[str, float], name: str) -> float | None:
    return answers.get(name)


def route(
    answers: Mapping[str, float], thresholds: Thresholds = DEFAULT_THRESHOLDS
) -> Result[RouteDecision, JevRouteError]:
    """Decide respond / review / drop from one post's feature probabilities.

    ``answers`` is the validated probability per feature name — see
    ``jev.validate_answers``, which rejects a malformed or short answer vector
    before this is called. A missing routed feature here is still an error
    rather than a silent negative.
    """
    exclusions = {
        name.removeprefix(EXCLUSION_PREFIX): value
        for name, value in answers.items()
        if name.startswith(EXCLUSION_PREFIX)
    }
    if not exclusions:
        return Err(JevRouteError(operation="route", detail="no excl_* answers"))

    missing = [name for name in ROUTED if _noul(answers, name) is None]
    if missing:
        return Err(
            JevRouteError(
                operation="route",
                detail=f"missing routed features: {', '.join(sorted(missing))}",
            )
        )

    top = max(exclusions, key=exclusions.__getitem__)
    thread = answers["needs_thread"]
    answerable = answers["answerable_from_post"]
    about = answers["about_agent_work"]
    points = answers["points_somewhere"]

    consulted: list[tuple[str, float, float]] = [
        (f"{EXCLUSION_PREFIX}{top}", exclusions[top], thresholds.exclusion)
    ]
    action: JevAction
    line: DecidingLine
    if exclusions[top] >= thresholds.exclusion:
        action, line = "drop", "exclusion"
    else:
        consulted.append(("needs_thread", thread, thresholds.thread))
        if thread >= thresholds.thread:
            action, line = "review", "needs_thread"
        else:
            consulted += [
                ("answerable_from_post", answerable, thresholds.answerable),
                ("about_agent_work", about, thresholds.about),
            ]
            if answerable >= thresholds.answerable and about >= thresholds.about:
                action, line = "respond", "respond"
            else:
                consulted.append(("points_somewhere", points, thresholds.points))
                if points >= thresholds.points:
                    action, line = "review", "points_somewhere"
                else:
                    action, line = "drop", "otherwise"

    close = tuple(
        name
        for name, value, threshold in consulted
        if abs(value - threshold) <= thresholds.margin
    )
    return Ok(
        RouteDecision(
            action="review" if close else action,
            path_action=action,
            line=line,
            margin=close,
            exclusion=top if exclusions[top] >= thresholds.exclusion else None,
            features={
                **{
                    f"{EXCLUSION_PREFIX}{name}": value
                    for name, value in exclusions.items()
                },
                "needs_thread": thread,
                "answerable_from_post": answerable,
                "about_agent_work": about,
                "points_somewhere": points,
            },
        )
    )
