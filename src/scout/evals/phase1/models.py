"""Typed representations of the Phase 1 corpus.

``Phase1Case`` and ``Phase1Registry`` are the boundary between the raw YAML
corpus and everything downstream (rendering, the pipeline adapter, the
grader). Loading/validation happens once in ``scout.evals.phase1.loader``; every
other module consumes the immutable registry this file defines.

``Phase1RunOutput`` is the Jig ``output_schema`` the outer per-variant
``AgentConfig`` submits via ``submit_output`` — see ``scout.evals.phase1.runner``.
"""

from __future__ import annotations

import re
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from scout.dossiers.resolver import DossierResolution

CaseKind = Literal["scout_response", "dossier_bad_write"]

TerminalStatus = Literal[
    "not_relevant",
    "abstained",
    "surfaced",
    "gate_blocked",
    "critic_rejected",
    "drafting_failed",
]

ObservedTerminalStatus = Literal[
    "not_relevant",
    "abstained",
    "surfaced",
    "gate_blocked",
    "critic_rejected",
    "drafting_failed",
    "low_relevance",
]

# First line every rendered EvalCase.input carries so the grader (which Jig
# only hands the rendered input + output, never EvalCase.metadata/context at
# this pin) can recover which corpus case produced a given run.
_SENTINEL_TEMPLATE = "[SCOUT_EVAL_CASE_ID:{case_id}]"
_SENTINEL_RE = re.compile(
    r"^\[SCOUT_EVAL_CASE_ID:(?P<case_id>phase1-\d{4}|export-evaluation-\d+)\]"
)


def render_case_sentinel(case_id: str) -> str:
    """Render the first-line sentinel for a case's rendered EvalCase.input."""
    return _SENTINEL_TEMPLATE.format(case_id=case_id)


def extract_case_id(text: str) -> str | None:
    """Recover a case ID from text carrying the sentinel as its first line.

    Returns ``None`` when no sentinel is present (malformed/foreign input).
    """
    if not text:
        return None
    first_line = text.split("\n", 1)[0]
    match = _SENTINEL_RE.match(first_line)
    return match.group("case_id") if match else None


@dataclass(frozen=True, slots=True)
class Phase1Case:
    """One immutable, validated corpus case plus its resolved identity.

    ``raw`` is the complete original case mapping (read-only) — callers that
    need a field this class doesn't surface as a property read it from
    there rather than the loader growing a property per schema field.
    """

    id: str
    case_kind: CaseKind
    origin: str
    tags: tuple[str, ...]
    project_key: str
    dossier_summary_id: str
    dossier_revision: str
    reserved_phase2_rail: bool
    raw: Mapping[str, Any]

    def __post_init__(self) -> None:
        # raw is the sole property below that returns its mapping directly
        # rather than through a MappingProxyType wrapper — freeze it here so
        # callers can't mutate the loader's underlying case dict through it.
        object.__setattr__(self, "raw", types.MappingProxyType(dict(self.raw)))

    @property
    def expected_source_relevant(self) -> bool | None:
        return self.raw.get("expected_source_relevant")

    @property
    def expected_posture(self) -> str | None:
        return self.raw.get("expected_posture")

    @property
    def expected_terminal_status(self) -> TerminalStatus | None:
        return self.raw.get("expected_terminal_status")

    @property
    def effective_expected_posture(self) -> str | None:
        """The posture a correct run should be graded against.

        ``grade.posture_should_have_been`` wins when the stored grade
        corrected the posture (a "fail" grade with the ``posture``
        dimension); otherwise falls back to ``expected_posture`` (design
        decision #5). Used identically by the grader's outcome_semantics
        check and by the hermetic scripted baseline draft builder, so the
        baseline variant can always draft toward a 1.0 score.
        """
        grade = self.grade or {}
        return grade.get("posture_should_have_been") or self.expected_posture

    @property
    def required_fact_ids(self) -> tuple[str, ...]:
        return tuple(self.raw.get("required_fact_ids") or ())

    @property
    def required_resource_ids(self) -> tuple[str, ...]:
        return tuple(self.raw.get("required_resource_ids") or ())

    @property
    def grade(self) -> Mapping[str, Any] | None:
        grade = self.raw.get("grade")
        return grade if grade is None else types.MappingProxyType(dict(grade))

    @property
    def source(self) -> Mapping[str, Any] | None:
        source = self.raw.get("source")
        return source if source is None else types.MappingProxyType(dict(source))

    @property
    def proposed_bad_write(self) -> Mapping[str, Any] | None:
        write = self.raw.get("proposed_bad_write")
        return write if write is None else types.MappingProxyType(dict(write))

    @property
    def expected_rail_violations(self) -> tuple[str, ...]:
        return tuple(self.raw.get("expected_rail_violations") or ())

    @property
    def provenance(self) -> Mapping[str, Any] | None:
        prov = self.raw.get("provenance")
        return prov if prov is None else types.MappingProxyType(dict(prov))


@dataclass(frozen=True, slots=True)
class Phase1Registry:
    """Immutable, ID-keyed registry of the loaded, validated corpus."""

    cases: Mapping[str, Phase1Case]
    manifest: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "cases", types.MappingProxyType(dict(self.cases)))
        object.__setattr__(self, "manifest", types.MappingProxyType(dict(self.manifest)))

    def __getitem__(self, case_id: str) -> Phase1Case:
        return self.cases[case_id]

    def __contains__(self, case_id: object) -> bool:
        return case_id in self.cases

    def __iter__(self) -> Any:
        return iter(self.cases.values())

    def __len__(self) -> int:
        return len(self.cases)

    def get(self, case_id: str) -> Phase1Case | None:
        return self.cases.get(case_id)


class Phase1RunOutput(BaseModel):
    """Jig ``output_schema`` for the outer per-variant ``AgentConfig``.

    ``ScoutPipelineAdapter`` (scout.evals.phase1.runner) is the sole producer;
    ``ScoutPhase1Grader`` (scout.evals.phase1.grader) is the sole consumer. Every
    field is present for both case kinds; fields that don't apply to a kind
    are left at their default.
    """

    case_id: str
    case_kind: CaseKind
    # The observed domain includes production's low_relevance outcome even
    # though the corpus's expected TerminalStatus deliberately excludes it.
    # A live low_relevance run remains representable and fails exact expected
    # outcome grading without opening this boundary to arbitrary strings.
    terminal_status: ObservedTerminalStatus | None = None
    source_relevant: bool | None = None
    project_key: str | None = None
    posture: str | None = None
    structured_draft: dict[str, Any] | None = None
    validated_text: str | None = None
    gate_violation_codes: list[str] = Field(default_factory=list)
    rail_violations: list[str] = Field(default_factory=list)
    terminal_reason: str | None = None


# Injectable, read-only dossier resolution — live runs call resolve_dossier
# at the pinned corpus SHA; hermetic tests materialize the committed
# tests/fixtures/dossier_source snapshot into a temp git repo and map fixture
# resolution internally (design decision #14).
DossierProvider = Callable[["Phase1Case"], "DossierResolution"]


__all__ = [
    "CaseKind",
    "DossierProvider",
    "ObservedTerminalStatus",
    "Phase1Case",
    "Phase1Registry",
    "Phase1RunOutput",
    "TerminalStatus",
    "extract_case_id",
    "render_case_sentinel",
]
