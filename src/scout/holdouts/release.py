"""Release held posts, acting on stored labels where they exist.

Two halves. The first reads the assay label interchange — the saved
`assay.label-packet-labels/v3` file, the private `assay.label-packet-key/v2`
that maps each case back to a held evaluation, and optionally the blind
`assay.label-packet/v1` — verifies their digests agree, and resolves each
case to one of the four labels and the action it releases as. The second
claims each pending hold and runs it: a drop stops before generation, a
respond or review goes through the ordinary reply-draft and critic path with
every downstream gate intact.

What this deliberately does not do:

- It never recomputes a relevance threshold. An ungraded hold releases as the
  action recorded at decision time; a graded one releases as its label says.
- It never re-routes to another project. The label was written against the
  frozen project, so landing it on a different one would apply the grade to a
  decision nobody made. Human false-negative promotion may re-route; this
  may not.
- It never falls back silently. A wrong project, an unknown case, a duplicate
  or a malformed label is refused and reported, not guessed at.

See contracts/relevance/holdout-labels.v1.schema.json and
docs/relevance-holdouts.md.
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft7Validator
from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

import scout.config as _config
from scout.config import Message
from scout.dossiers.resolver import DossierSummary, get_pinned_dossier_revision
from scout.grading.promotion import build_response_phase_runtime
from scout.registry import KeywordRoute, ProjectTarget, RuntimeRegistry
from scout.resources import runtime_resource
from scout.result import Err, Ok, Result
from scout.scanning.pipeline import draft_and_critic_step
from scout.scanning.prefilter import RoutedMessage
from scout.scanning.runner import (
    PersistenceContext,
    classify_outcome,
    load_project_dossiers,
    persist_outcome,
)
from scout.scanning.schemas import RelevancePhaseOutput
from scout.storage.holdouts import (
    LABEL_ACTIONS,
    ClassifierAction,
    Holdout,
    HoldoutClaim,
    HoldoutLabel,
    KeyProvenance,
    LabelProvenance,
    ReleaseAuthority,
)
from scout.storage.state import StateManager, SurfaceRateLimitedError

logger = logging.getLogger("scout.holdouts.release")

HOLDOUT_LABELS_SCHEMA_VERSION: Literal[1] = 1

LABELS_FORMAT = "assay.label-packet-labels/v3"
KEY_FORMAT = "assay.label-packet-key/v2"
PACKET_FORMAT = "assay.label-packet/v1"

#: The committed schema each interchange file is checked against before it is
#: parsed. Reading these files through the contract Scout publishes — rather
#: than through the models below alone — is what makes the committed schema the
#: thing actually enforced: a model that drifts from it starts failing here
#: instead of quietly accepting a file the contract forbids.
ASSAY_SCHEMA_FILES: Mapping[str, str] = {
    LABELS_FORMAT: "assay-labels.v3.schema.json",
    KEY_FORMAT: "assay-key.v2.schema.json",
    PACKET_FORMAT: "assay-packet.v1.schema.json",
}

#: The one `exclusion` answer that means no exclusion fired.
#:
#: Confirmed against assay's own router rather than inferred: `route()` in
#: assay's `experiments/typesafe_relevance/packet.py` takes `exclusion` from a
#: closed set of catalogue names whose not-fired member is the literal
#: `"none"`, and raises on any value outside that set. Scout deliberately does
#: not restate the rest of that set — the exclusion names belong to the
#: catalogue and version with it — so any other non-blank value is read as the
#: name of the exclusion that fired. A blank is not a member of the set either,
#: and is refused rather than read as not-fired.
NO_EXCLUSION = "none"

#: `substance` values that resolve to a label once no exclusion fired. Mirrors
#: assay's SUBSTANCE_ROUTES, which routes the same three answers.
SUBSTANCE_LABELS: dict[str, HoldoutLabel] = {
    "in_post": "in_post",
    "pointer": "pointer",
    "none": "none",
}

#: How long one worker holds a claim before another may take it over.
RELEASE_CLAIM_TTL_SECONDS = 900

ReleaseErrorCategory = Literal[
    "validation", "config", "generation", "persistence", "claim", "unexpected"
]
ReleaseStatus = Literal["released", "failed", "skipped"]


@dataclass(frozen=True, slots=True)
class HoldoutReleaseError:
    """A release step was refused. Carries enough to trace which and why."""

    operation: str
    detail: str
    category: ReleaseErrorCategory = "validation"
    holdout_id: int | None = None
    evaluation_id: int | None = None
    case_id: int | None = None


# ---------------------------------------------------------------------------
# The label interchange
# ---------------------------------------------------------------------------


class AssayLabelCase(BaseModel):
    """One saved reviewer answer. Mirrors assay.label-packet-labels/v3."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: int
    exclusion: str
    needs_thread: bool
    substance: str | None
    note: str | None


class AssayLabelsFile(BaseModel):
    """The saved labels file, as assay writes it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal["assay.label-packet-labels/v3"]
    packet: str
    packet_digest: str
    plan_digest: str
    reviewer: str
    saved_at: str
    cases: list[AssayLabelCase]


class AssayKeyCase(BaseModel):
    """One case's private identity. Mirrors assay.label-packet-key/v2."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: int
    evaluation_id: int
    project_key: str
    production_decision: bool
    production_score: float


class AssayKeyFile(BaseModel):
    """The private answer key, which is what a label actually joins through.

    `seed` and `strata` are read but unused here: they are required by the
    committed v2 schema, so a file missing them is a truncated or hand-made
    key rather than one assay produced, and naming them keeps that a parse
    failure instead of a silent acceptance.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    format: Literal["assay.label-packet-key/v2"]
    name: str
    sitting: str
    digest: str
    plan_digest: str
    seed: str
    strata: dict[str, JsonValue]
    cases: list[AssayKeyCase]


class AssayPacketFile(BaseModel):
    """The blind packet. Optional here, and read only to verify its digests."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    format: Literal["assay.label-packet/v1"]
    name: str
    sitting: str
    digest: str
    plan_digest: str


@dataclass(frozen=True, slots=True)
class ReleaseInputs:
    """The files an operator supplied. All three are optional."""

    labels_path: Path | None = None
    key_path: Path | None = None
    packet_path: Path | None = None


@dataclass(frozen=True, slots=True)
class ResolvedLabel:
    """One label, resolved through the key to a held evaluation and an action."""

    case_id: int
    evaluation_id: int
    project_key: str
    label: HoldoutLabel
    action: ClassifierAction
    needs_thread: bool
    label_provenance: LabelProvenance
    key_provenance: KeyProvenance


@dataclass(frozen=True, slots=True)
class ReleaseLabels:
    """Every resolved label, keyed by the evaluation it applies to.

    Empty when no files were supplied, which is the ungraded release: every
    hold acts on its own recorded classifier action.
    """

    by_evaluation: Mapping[int, ResolvedLabel]

    @property
    def is_empty(self) -> bool:
        return not self.by_evaluation


def label_from_case(case: AssayLabelCase) -> Result[HoldoutLabel, HoldoutReleaseError]:
    """Resolve one reviewer answer to one of the four labels.

    Mirrors assay's own `route()` rather than reimplementing a rule beside
    it. Precedence, first match wins: an exclusion that fired, then `in_post`,
    then `pointer`, then `none`. `needs_thread` is recorded but does not enter
    the mapping — the four labels are the whole action vocabulary.

    Three answers are refused rather than resolved, each because assay cannot
    have produced them:

    - A blank exclusion. The question is always asked and always answered from
      the catalogue's closed set; a blank is not in it, so the file is not one
      assay saved.
    - An exclusion beside a substance. The exclusion question ends the case, so
      the substance question is never reached and its answer is absent, not a
      value. Assay refuses the pair outright; reading past it here would grade
      a post on an answer the reviewer was never shown.
    - A missing substance with no exclusion. That is an incomplete answer, not
      a `none`: the reviewer was asked what the post carries and did not say.
    """
    exclusion = case.exclusion.strip().casefold()
    substance = None if case.substance is None else case.substance.strip().casefold()
    if not exclusion:
        return Err(
            HoldoutReleaseError(
                operation="label_from_case",
                detail="exclusion is blank; no exclusion answer was recorded",
                case_id=case.case_id,
            )
        )
    if exclusion != NO_EXCLUSION:
        if substance is not None:
            return Err(
                HoldoutReleaseError(
                    operation="label_from_case",
                    detail=(
                        f"exclusion {case.exclusion!r} fired but substance "
                        f"{case.substance!r} was answered too; the substance "
                        "question is not asked on an excluded post"
                    ),
                    case_id=case.case_id,
                )
            )
        return Ok("exclusion")
    if substance is None:
        return Err(
            HoldoutReleaseError(
                operation="label_from_case",
                detail="no exclusion fired and substance is absent",
                case_id=case.case_id,
            )
        )
    resolved = SUBSTANCE_LABELS.get(substance)
    if resolved is None:
        return Err(
            HoldoutReleaseError(
                operation="label_from_case",
                detail=f"substance {case.substance!r} is not a known label",
                case_id=case.case_id,
            )
        )
    return Ok(resolved)


def action_for_label(label: HoldoutLabel) -> ClassifierAction:
    """The action a label releases as. One table, shared with storage."""
    return LABEL_ACTIONS[label]


def _verify_digests(
    labels: AssayLabelsFile,
    key: AssayKeyFile,
    packet: AssayPacketFile | None,
) -> Result[None, HoldoutReleaseError]:
    """Check the three files describe the same packet and the same plan.

    A labels file saved against one packet and a key generated for another
    would still join by case id and quietly grade the wrong posts. The
    digests are what makes that detectable.
    """
    mismatches: list[str] = []
    if labels.packet != key.name:
        mismatches.append(f"labels packet {labels.packet!r} != key name {key.name!r}")
    if labels.packet_digest != key.digest:
        mismatches.append("labels packet_digest != key digest")
    if labels.plan_digest != key.plan_digest:
        mismatches.append("labels plan_digest != key plan_digest")
    if packet is not None:
        if packet.name != key.name:
            mismatches.append(f"packet name {packet.name!r} != key name {key.name!r}")
        if packet.digest != key.digest:
            mismatches.append("packet digest != key digest")
        if packet.plan_digest != key.plan_digest:
            mismatches.append("packet plan_digest != key plan_digest")
    if mismatches:
        return Err(
            HoldoutReleaseError(
                operation="verify_digests",
                detail="; ".join(mismatches),
            )
        )
    return Ok(None)


@cache
def interchange_validator(interchange_format: str) -> Draft7Validator:
    """The committed validator for one assay interchange format."""
    schema_path = runtime_resource(
        "contracts", "relevance", ASSAY_SCHEMA_FILES[interchange_format]
    )
    return Draft7Validator(json.loads(schema_path.read_text()))


def _schema_violations(interchange_format: str, document: object) -> list[str]:
    """Every way `document` departs from its committed schema, in file order."""
    validator = interchange_validator(interchange_format)
    return [
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    ]


def _read_interchange[T: BaseModel](
    path: Path, model: type[T], interchange_format: str, *, operation: str
) -> Result[T, HoldoutReleaseError]:
    """Read one interchange file at the IO boundary, contract first.

    The committed schema is checked before the model parses, so a file that
    violates the published contract is reported as a contract violation with
    every offending path named, rather than as whichever field the model
    happened to reach first.
    """
    try:
        document = json.loads(path.read_bytes())
    except OSError as exc:
        return Err(
            HoldoutReleaseError(operation=operation, detail=f"cannot read {path}: {exc}")
        )
    except ValueError as exc:
        return Err(
            HoldoutReleaseError(operation=operation, detail=f"{path} is not valid JSON: {exc}")
        )

    violations = _schema_violations(interchange_format, document)
    if violations:
        return Err(
            HoldoutReleaseError(
                operation=operation,
                detail=(
                    f"{path} does not conform to {interchange_format}: "
                    + "; ".join(violations)
                ),
            )
        )

    try:
        return Ok(model.model_validate(document))
    except ValidationError as exc:
        return Err(
            HoldoutReleaseError(
                operation=operation, detail=f"{path} is not a valid {model.__name__}: {exc}"
            )
        )


def resolve_labels(
    labels: AssayLabelsFile,
    key: AssayKeyFile,
    packet: AssayPacketFile | None = None,
) -> Result[ReleaseLabels, HoldoutReleaseError]:
    """Join labels to held evaluations through the private key.

    Every refusal here is a whole-run refusal, because every one of them
    means the operator has the wrong files rather than one bad record: the
    digests disagree, a case appears twice, a label points at a case the key
    does not know, or an answer does not resolve to a label.
    """
    verified = _verify_digests(labels, key, packet)
    if isinstance(verified, Err):
        return verified

    key_by_case: dict[int, AssayKeyCase] = {}
    for key_case in key.cases:
        if key_case.case_id in key_by_case:
            return Err(
                HoldoutReleaseError(
                    operation="resolve_labels",
                    detail="key contains a duplicate case id",
                    case_id=key_case.case_id,
                )
            )
        key_by_case[key_case.case_id] = key_case

    seen_evaluations: dict[int, int] = {}
    for case_id, mapped in key_by_case.items():
        if mapped.evaluation_id in seen_evaluations:
            return Err(
                HoldoutReleaseError(
                    operation="resolve_labels",
                    detail=(
                        f"key maps evaluation {mapped.evaluation_id} to two cases "
                        f"({seen_evaluations[mapped.evaluation_id]} and {case_id})"
                    ),
                    evaluation_id=mapped.evaluation_id,
                )
            )
        seen_evaluations[mapped.evaluation_id] = case_id

    resolved: dict[int, ResolvedLabel] = {}
    seen_cases: set[int] = set()
    for case in labels.cases:
        if case.case_id in seen_cases:
            return Err(
                HoldoutReleaseError(
                    operation="resolve_labels",
                    detail="labels contain a duplicate case id",
                    case_id=case.case_id,
                )
            )
        seen_cases.add(case.case_id)
        matched = key_by_case.get(case.case_id)
        if matched is None:
            return Err(
                HoldoutReleaseError(
                    operation="resolve_labels",
                    detail="label refers to a case the key does not contain",
                    case_id=case.case_id,
                )
            )
        label = label_from_case(case)
        if isinstance(label, Err):
            return label
        resolved[matched.evaluation_id] = ResolvedLabel(
            case_id=case.case_id,
            evaluation_id=matched.evaluation_id,
            project_key=matched.project_key,
            label=label.value,
            action=action_for_label(label.value),
            needs_thread=case.needs_thread,
            label_provenance=LabelProvenance(
                format=labels.format,
                packet=labels.packet,
                packet_digest=labels.packet_digest,
                plan_digest=labels.plan_digest,
                reviewer=labels.reviewer,
                saved_at=labels.saved_at,
                case_id=case.case_id,
            ),
            key_provenance=KeyProvenance(
                format=key.format,
                name=key.name,
                digest=key.digest,
                plan_digest=key.plan_digest,
                sitting=key.sitting,
                case_id=case.case_id,
                evaluation_id=matched.evaluation_id,
                project_key=matched.project_key,
            ),
        )
    return Ok(ReleaseLabels(by_evaluation=resolved))


def load_release_labels(
    inputs: ReleaseInputs,
) -> Result[ReleaseLabels, HoldoutReleaseError]:
    """Read and resolve the supplied interchange files, if any."""
    if inputs.labels_path is None and inputs.key_path is None:
        return Ok(ReleaseLabels(by_evaluation={}))
    if inputs.labels_path is None or inputs.key_path is None:
        return Err(
            HoldoutReleaseError(
                operation="load_release_labels",
                detail="labels and key must be supplied together",
            )
        )
    labels = _read_interchange(
        inputs.labels_path, AssayLabelsFile, LABELS_FORMAT, operation="load_labels"
    )
    if isinstance(labels, Err):
        return labels
    key = _read_interchange(inputs.key_path, AssayKeyFile, KEY_FORMAT, operation="load_key")
    if isinstance(key, Err):
        return key
    packet: AssayPacketFile | None = None
    if inputs.packet_path is not None:
        read = _read_interchange(
            inputs.packet_path, AssayPacketFile, PACKET_FORMAT, operation="load_packet"
        )
        if isinstance(read, Err):
            return read
        packet = read.value
    return resolve_labels(labels.value, key.value, packet)


def released_label_record(
    holdout: Holdout, resolved: ResolvedLabel
) -> dict[str, JsonValue]:
    """The resolved-label record, as contracts/.../holdout-labels.v1 defines it."""
    return {
        "schema_version": HOLDOUT_LABELS_SCHEMA_VERSION,
        "holdout_id": holdout.id,
        "evaluation_id": resolved.evaluation_id,
        "project_key": resolved.project_key,
        "case_id": resolved.case_id,
        "label": resolved.label,
        "needs_thread": resolved.needs_thread,
        "released_action": resolved.action,
        "authority": "label",
        "packet": resolved.label_provenance.packet,
        "packet_digest": resolved.label_provenance.packet_digest,
        "plan_digest": resolved.label_provenance.plan_digest,
        "reviewer": resolved.label_provenance.reviewer,
        "saved_at": resolved.label_provenance.saved_at,
    }


# ---------------------------------------------------------------------------
# Releasing
# ---------------------------------------------------------------------------


class _CompletionRefused(Exception):
    """A refused completion, raised so the enclosing transaction rolls back."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ReleaseResolution:
    """What one hold releases as, and on whose authority."""

    authority: ReleaseAuthority
    action: ClassifierAction
    label: ResolvedLabel | None = None


@dataclass(frozen=True, slots=True)
class HoldoutReleaseOutcome:
    """What happened to one hold. Reported whether it succeeded or not."""

    holdout_id: int
    evaluation_id: int
    status: ReleaseStatus
    held_at: str
    age_seconds: float
    authority: ReleaseAuthority | None = None
    action: ClassifierAction | None = None
    label: HoldoutLabel | None = None
    case_id: int | None = None
    scan_id: int | None = None
    target_evaluation_id: int | None = None
    surface_status: str | None = None
    dossier_summary_id: str | None = None
    dossier_revision: str | None = None
    already_completed: bool = False
    #: Trace ids of complete phase runs an earlier attempt on this hold left
    #: unlinked. Reported whether or not this attempt succeeded, so the work a
    #: crash abandoned stays visible rather than being silently repeated.
    abandoned_traces: tuple[str, ...] = ()
    resumed_scan: bool = False
    error: str | None = None
    error_category: ReleaseErrorCategory | None = None

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "holdout_id": self.holdout_id,
            "evaluation_id": self.evaluation_id,
            "status": self.status,
            "held_at": self.held_at,
            "age_seconds": round(self.age_seconds, 3),
            "authority": self.authority,
            "action": self.action,
            "label": self.label,
            "case_id": self.case_id,
            "scan_id": self.scan_id,
            "target_evaluation_id": self.target_evaluation_id,
            "surface_status": self.surface_status,
            "dossier_summary_id": self.dossier_summary_id,
            "dossier_revision": self.dossier_revision,
            "already_completed": self.already_completed,
            "abandoned_traces": list(self.abandoned_traces),
            "resumed_scan": self.resumed_scan,
            "error": self.error,
            "error_category": self.error_category,
        }


@dataclass(frozen=True, slots=True)
class HoldoutReleaseReport:
    """Every attempt this run made, and what came of it."""

    attempted: int
    released: int
    failed: int
    skipped: int
    outcomes: tuple[HoldoutReleaseOutcome, ...]
    unmatched_label_cases: tuple[int, ...] = ()

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "attempted": self.attempted,
            "released": self.released,
            "failed": self.failed,
            "skipped": self.skipped,
            "unmatched_label_cases": list(self.unmatched_label_cases),
            "outcomes": [outcome.to_json() for outcome in self.outcomes],
        }


@dataclass(frozen=True, slots=True)
class ReleaseProject:
    """The hold's own project and route, as configured today.

    Resolved from the identity frozen on the hold, never from a fresh routing
    pass over the post: the route may have changed, the project may not.
    """

    route: KeywordRoute
    project: ProjectTarget


@dataclass(frozen=True, slots=True)
class ReleaseRuntime:
    """The current configuration one hold's generation runs against.

    The route, project and dossier are resolved now, not frozen: an old hold
    must respect the gates in force today. Its decision-time identity stays
    on the hold row, untouched.
    """

    scan_id: int
    message: Message
    route: KeywordRoute
    project: ProjectTarget
    dossiers: Mapping[str, DossierSummary]
    dossier_revision: str | None
    resumed_scan: bool = False


def _age_seconds(held_at: str) -> float:
    """Hold-to-release age. Unparseable timestamps report 0 rather than raise."""
    try:
        held = datetime.fromisoformat(held_at)
    except ValueError:
        return 0.0
    if held.tzinfo is None:
        held = held.replace(tzinfo=UTC)
    return (datetime.now(UTC) - held).total_seconds()


def resolve_release(
    state: StateManager, holdout: Holdout, labels: ReleaseLabels
) -> Result[ReleaseResolution, HoldoutReleaseError]:
    """Decide what this hold releases as: the label if graded, else the record.

    A label is only applied when its key maps to this exact held evaluation
    *and* the project it was written against matches the project frozen on
    the hold. A mismatch is refused, never quietly released on the recorded
    action instead — that would hide a wrong-file mistake behind a plausible
    outcome.
    """
    resolved = labels.by_evaluation.get(holdout.evaluation_id)
    if resolved is not None:
        frozen_project = holdout.frozen_input.project_key
        if resolved.project_key != frozen_project:
            return Err(
                HoldoutReleaseError(
                    operation="resolve_release",
                    detail=(
                        f"label project {resolved.project_key!r} does not match the "
                        f"frozen project {frozen_project!r}"
                    ),
                    holdout_id=holdout.id,
                    evaluation_id=holdout.evaluation_id,
                    case_id=resolved.case_id,
                )
            )
        return Ok(
            ReleaseResolution(authority="label", action=resolved.action, label=resolved)
        )

    recorded = state.holdouts.get_decision(holdout.evaluation_id)
    if recorded is None:
        return Err(
            HoldoutReleaseError(
                operation="resolve_release",
                detail="hold is ungraded and has no recorded classifier action",
                holdout_id=holdout.id,
                evaluation_id=holdout.evaluation_id,
            )
        )
    # No threshold is recomputed here. The action was decided under the
    # threshold in force at decision time and recorded then, precisely so an
    # ungraded release stays reproducible however the threshold has moved.
    return Ok(ReleaseResolution(authority="recorded_action", action=recorded.action))


def validate_label_targets(
    state: StateManager, labels: ReleaseLabels
) -> Result[None, HoldoutReleaseError]:
    """Check every label points at a real hold on its own project.

    Runs once, before the first hold is claimed. `resolve_release` makes the
    same check per record, but only when that record's turn comes — which
    would release the first forty holds and then discover on the forty-first
    that the key belongs to a different corpus. A wrong pair of files is an
    operator mistake about the whole run, so it is caught while the run has
    changed nothing.

    A label whose evaluation is held but already released is not an error: it
    is reported as an unmatched case at the end. Only a label pointing at an
    evaluation that was never held, or at a hold made for another project,
    refuses the run.
    """
    for evaluation_id, resolved in sorted(labels.by_evaluation.items()):
        held = state.holdouts.get_by_evaluation(evaluation_id)
        if held is None:
            return Err(
                HoldoutReleaseError(
                    operation="validate_label_targets",
                    detail=(
                        f"the key maps case {resolved.case_id} to evaluation "
                        f"{evaluation_id}, which was never held; these labels were "
                        "written for a different population"
                    ),
                    evaluation_id=evaluation_id,
                    case_id=resolved.case_id,
                )
            )
        if resolved.project_key != held.frozen_input.project_key:
            return Err(
                HoldoutReleaseError(
                    operation="validate_label_targets",
                    detail=(
                        f"case {resolved.case_id} is labelled for project "
                        f"{resolved.project_key!r} but hold {held.id} froze project "
                        f"{held.frozen_input.project_key!r}"
                    ),
                    holdout_id=held.id,
                    evaluation_id=evaluation_id,
                    case_id=resolved.case_id,
                )
            )
    return Ok(None)


def _resolve_route(
    registry: RuntimeRegistry, holdout: Holdout
) -> Result[KeywordRoute, HoldoutReleaseError]:
    """Find the hold's own project's route. Never another project's.

    The route recorded at decision time wins. When it has since been deleted,
    the highest-priority surviving route for the *same* project stands in —
    which is a route change, not a project change. With no route at all the
    hold stays pending until one is restored.
    """
    project_key = holdout.frozen_input.project_key
    if project_key is None:
        return Err(
            HoldoutReleaseError(
                operation="resolve_route",
                detail="hold has no frozen project to release against",
                category="validation",
                holdout_id=holdout.id,
            )
        )
    original = next(
        (
            route
            for route in registry.keywords
            if route.id == holdout.frozen_input.keyword_route_id
            and route.project_key == project_key
        ),
        None,
    )
    if original is not None:
        return Ok(original)
    same_project = sorted(
        (route for route in registry.keywords if route.project_key == project_key),
        key=lambda route: (-route.priority, route.id),
    )
    if not same_project:
        return Err(
            HoldoutReleaseError(
                operation="resolve_route",
                detail=(
                    f"no active route resolves to the frozen project {project_key!r}; "
                    "restore the route and retry"
                ),
                category="config",
                holdout_id=holdout.id,
            )
        )
    return Ok(same_project[0])


def validate_frozen_project(
    state: StateManager, holdout: Holdout
) -> Result[ReleaseProject | None, HoldoutReleaseError]:
    """Check the hold's own frozen project is still configured.

    Runs before *every* completion, a drop included. A drop writes no
    evaluation, but it still spends the hold: completing one against a project
    that has since been deleted records a graded outcome for a project nobody
    can account for, and the hold cannot be re-run afterwards to fix it. A
    configuration refusal leaves it pending, which is recoverable.

    `None` is the answer for a hold that froze no project at all — a decision
    made with `KEYWORD_PREFILTER=false` that resolved none. There is nothing
    to validate there, and nothing to validate it against; a *graded* release
    of such a hold is already refused upstream, because a label always names a
    project and no project can match an absent one.
    """
    if holdout.frozen_input.project_key is None:
        return Ok(None)
    registry = state.load_runtime_registry()
    route = _resolve_route(registry, holdout)
    if isinstance(route, Err):
        return route
    project = registry.projects.get(route.value.project_key)
    if project is None:
        return Err(
            HoldoutReleaseError(
                operation="validate_frozen_project",
                detail=(
                    f"project {route.value.project_key!r} is no longer active; "
                    "restore it and retry"
                ),
                category="config",
                holdout_id=holdout.id,
            )
        )
    return Ok(ReleaseProject(route=route.value, project=project))


def prepare_release_runtime(
    state: StateManager, holdout: Holdout
) -> Result[ReleaseRuntime, HoldoutReleaseError]:
    """Validate today's generation gates for one hold and open its release scan.

    Builds on the frozen-project check every path shares, and adds the gates
    only a post that will actually be drafted has to clear. Every refusal
    below leaves the hold retryable: the configuration is what is wrong, not
    the hold, and repairing it must let the same hold through however old it
    is.
    """
    message = state.load_post(holdout.post_id)
    if message is None:
        return Err(
            HoldoutReleaseError(
                operation="prepare_release",
                detail=f"post {holdout.post_id} not found",
                category="persistence",
                holdout_id=holdout.id,
            )
        )

    validated = validate_frozen_project(state, holdout)
    if isinstance(validated, Err):
        return validated
    if validated.value is None:
        return Err(
            HoldoutReleaseError(
                operation="prepare_release",
                detail=(
                    "hold froze no project, so there is no project to draft a reply "
                    "for; it can only be released as a drop"
                ),
                category="config",
                holdout_id=holdout.id,
            )
        )
    route, project = validated.value.route, validated.value.project

    if message.author_id.strip() and state.is_author_blocked(
        platform=message.platform, author_id=message.author_id
    ) is True:
        return Err(
            HoldoutReleaseError(
                operation="prepare_release",
                detail=(
                    f"author {message.platform}:{message.author_id} is blocked; "
                    "the hold stays pending rather than surfacing"
                ),
                category="config",
                holdout_id=holdout.id,
            )
        )

    dossiers, dossier_errors = load_project_dossiers(state.load_runtime_registry().projects)
    if dossier_errors:
        return Err(
            HoldoutReleaseError(
                operation="prepare_release",
                detail="; ".join(dossier_errors),
                category="config",
                holdout_id=holdout.id,
            )
        )
    if dossiers.get(project.key) is None:
        return Err(
            HoldoutReleaseError(
                operation="prepare_release",
                detail=f"no ready dossier for project {project.key!r}",
                category="config",
                holdout_id=holdout.id,
            )
        )

    dossier_revision: str | None = None
    if _config.SCOUT_DOSSIER_ROOT:
        try:
            dossier_revision = get_pinned_dossier_revision(Path(_config.SCOUT_DOSSIER_ROOT))
        except RuntimeError as exc:
            return Err(
                HoldoutReleaseError(
                    operation="prepare_release",
                    detail=str(exc),
                    category="config",
                    holdout_id=holdout.id,
                )
            )

    # An attempt that crashed between generating and persisting left its phase
    # runs complete, durable and unlinked under a release scan that was never
    # closed. Rejoining that scan keeps one hold's whole release history in one
    # place, so the abandoned traces stay attributable to the hold that paid
    # for them instead of accumulating as orphan scans nothing points at.
    # Linking only ever names this attempt's own contributor ids, so the older
    # rows stay unlinked and are not mistaken for evidence of this outcome.
    resumed_scan_id = state.holdouts.abandoned_release_scan(holdout.post_id)
    scan_id = resumed_scan_id or state.start_scan(
        environment=_config.SCOUT_ENVIRONMENT, run_kind="holdout_release"
    )
    if resumed_scan_id is not None:
        logger.info(
            "holdout %s resuming abandoned release scan %s", holdout.id, resumed_scan_id
        )
    return Ok(
        ReleaseRuntime(
            scan_id=scan_id,
            message=message,
            route=route,
            project=project,
            dossiers=dossiers,
            dossier_revision=dossier_revision,
            resumed_scan=resumed_scan_id is not None,
        )
    )


def _release_relevance(holdout: Holdout, resolution: ReleaseResolution) -> RelevancePhaseOutput:
    """The relevance authority a released draft runs under.

    Score 1.0 because the release decision is explicit: it bypasses the
    relevance threshold and nothing else. Every gate after it — critic,
    abstention, verifier, author rate, account blocks — runs unchanged.
    """
    project_key = holdout.frozen_input.project_key
    origin = (
        f"holdout {holdout.id} released on label {resolution.label.label!r}"
        if resolution.label is not None
        else f"holdout {holdout.id} released on its recorded {resolution.action!r} action"
    )
    return RelevancePhaseOutput(
        relevant=True,
        score=1.0,
        reason=origin,
        relevant_to=[project_key] if project_key else [],
    )


def _outcome(
    holdout: Holdout,
    status: ReleaseStatus,
    **fields: Any,
) -> HoldoutReleaseOutcome:
    return HoldoutReleaseOutcome(
        holdout_id=holdout.id,
        evaluation_id=holdout.evaluation_id,
        status=status,
        held_at=holdout.held_at,
        age_seconds=_age_seconds(holdout.held_at),
        **fields,
    )


def _fail(
    state: StateManager,
    holdout: Holdout,
    claim: HoldoutClaim,
    error: HoldoutReleaseError,
    *,
    resolution: ReleaseResolution | None = None,
    scan_id: int | None = None,
) -> HoldoutReleaseOutcome:
    """Return the hold to the queue and report why, without losing it."""
    logger.warning(
        "holdout %s release failed (%s): %s", holdout.id, error.category, error.detail
    )
    state.holdouts.fail_attempt(claim, detail=f"{error.operation}: {error.detail}")
    if scan_id is not None:
        with contextlib.suppress(Exception):
            state.fail_scan(
                scan_id,
                1,
                failure_post_id=holdout.post_id,
                error_kind=error.category,
                error_message=error.detail,
            )
    return _outcome(
        holdout,
        "failed",
        authority=resolution.authority if resolution else None,
        action=resolution.action if resolution else None,
        label=resolution.label.label if resolution and resolution.label else None,
        case_id=resolution.label.case_id if resolution and resolution.label else None,
        scan_id=scan_id,
        error=error.detail,
        error_category=error.category,
    )


def _completion_fields(resolution: ReleaseResolution) -> dict[str, Any]:
    """The label and key provenance one completion records."""
    label = resolution.label
    if label is None:
        return {}
    return {
        "label": label.label,
        "label_source": f"{label.label_provenance.packet}#case-{label.case_id}",
        "label_provenance": label.label_provenance,
        "key_provenance": label.key_provenance,
        "labelled_at": label.label_provenance.saved_at,
    }


async def release_one_holdout(
    *,
    state: StateManager,
    tracer: object,
    feedback: object,
    holdout: Holdout,
    labels: ReleaseLabels,
    owner: str,
) -> HoldoutReleaseOutcome:
    """Claim one hold and carry it to a terminal outcome.

    Ordering is what makes a crash safe. The claim is durable before any work
    starts. The model calls happen with no database transaction open. The
    target evaluation, its surface event and the release completion commit in
    one transaction, so a crash either leaves the hold claimed and retryable
    with nothing else written, or leaves it released with its target in place.
    A claim superseded while it was away cannot complete at all.

    Evidence an earlier attempt abandoned is read before this attempt starts
    and reported on whatever outcome it reaches, so a crash that cost real
    model calls is visible in the run that follows it instead of being
    silently written off.
    """
    abandoned = tuple(
        run.trace_id for run in state.holdouts.abandoned_release_evidence(holdout.post_id)
    )
    outcome = await _attempt_release(
        state=state,
        tracer=tracer,
        feedback=feedback,
        holdout=holdout,
        labels=labels,
        owner=owner,
    )
    return outcome if not abandoned else replace(outcome, abandoned_traces=abandoned)


async def _attempt_release(
    *,
    state: StateManager,
    tracer: object,
    feedback: object,
    holdout: Holdout,
    labels: ReleaseLabels,
    owner: str,
) -> HoldoutReleaseOutcome:
    """One release attempt, from claim to terminal outcome."""
    claimed = state.holdouts.claim(
        holdout.id, owner=owner, ttl_seconds=RELEASE_CLAIM_TTL_SECONDS
    )
    if isinstance(claimed, Err):
        # A hold that completed between the listing and the claim reads its
        # committed outcome back rather than being attempted a second time.
        current = state.holdouts.get(holdout.id)
        if current is not None and current.status == "released":
            return _outcome(
                holdout,
                "skipped",
                authority=current.release_authority,
                action=current.release_action,
                label=current.label,
                target_evaluation_id=current.target_evaluation_id,
                already_completed=True,
            )
        return _outcome(
            holdout,
            "skipped",
            error=claimed.error.detail,
            error_category="claim",
        )
    claim = claimed.value

    resolution = resolve_release(state, holdout, labels)
    if isinstance(resolution, Err):
        return _fail(state, holdout, claim, resolution.error)
    release = resolution.value

    # The frozen project is validated on every path, a drop included. A drop
    # generates nothing, but it still spends the hold: completing one against
    # a project that has since been deleted would record a graded outcome
    # nobody can account for, and the hold cannot be re-run to correct it.
    validated_project = validate_frozen_project(state, holdout)
    if isinstance(validated_project, Err):
        return _fail(state, holdout, claim, validated_project.error, resolution=release)

    if release.action == "drop":
        # Stops before generation. No draft, no target evaluation, no event.
        completed = state.holdouts.complete_release(
            claim,
            release_authority=release.authority,
            release_action="drop",
            **_completion_fields(release),
        )
        if isinstance(completed, Err):
            return _fail(
                state,
                holdout,
                claim,
                HoldoutReleaseError(
                    operation="complete_release",
                    detail=completed.error.detail,
                    category="persistence",
                    holdout_id=holdout.id,
                ),
                resolution=release,
            )
        return _outcome(
            holdout,
            "released",
            authority=release.authority,
            action="drop",
            label=release.label.label if release.label else None,
            case_id=release.label.case_id if release.label else None,
        )

    prepared = prepare_release_runtime(state, holdout)
    if isinstance(prepared, Err):
        return _fail(state, holdout, claim, prepared.error, resolution=release)
    runtime = prepared.value

    try:
        phase_runtime = build_response_phase_runtime(
            state=state,
            tracer=tracer,
            feedback=feedback,
            scan_id=runtime.scan_id,
            post_id=holdout.post_id,
            registry=state.load_runtime_registry(),
            route=runtime.route,
        )
    except Exception as exc:  # IO boundary: snapshot and prompt assembly
        return _fail(
            state,
            holdout,
            claim,
            HoldoutReleaseError(
                operation="build_response_phase_runtime",
                detail=str(exc) or type(exc).__name__,
                category="config",
                holdout_id=holdout.id,
            ),
            resolution=release,
            scan_id=runtime.scan_id,
        )

    # Model calls, with no transaction open. Each phase opens its own short
    # evidence transaction only after its trace is verified durable.
    phase_result = await draft_and_critic_step(
        {
            "input": RoutedMessage(message=runtime.message, keyword_route=runtime.route),
            "phase_configs": phase_runtime.phase_configs,
            "dossier_summaries": runtime.dossiers,
            "execution_context": phase_runtime.execution,
            "relevance_output": _release_relevance(holdout, release),
        }
    )
    if isinstance(phase_result, Err):
        return _fail(
            state,
            holdout,
            claim,
            HoldoutReleaseError(
                operation="generate",
                detail=getattr(phase_result.error, "detail", str(phase_result.error)),
                category="generation",
                holdout_id=holdout.id,
            ),
            resolution=release,
            scan_id=runtime.scan_id,
        )

    decision = classify_outcome(phase_result.value, runtime.message, runtime.dossiers)
    context = PersistenceContext(
        post_id=holdout.post_id,
        scan_id=runtime.scan_id,
        keyword_route_id=runtime.route.id,
        # Current identity, recorded on the target. The hold keeps its own,
        # decision-time identity untouched beside it.
        dossier_revision=runtime.dossier_revision,
        dossier_summary_id=runtime.project.dossier_summary_id,
        surfaced_at=runtime.message.created_at.isoformat(),
        allow_response_only_phase_runs=True,
    )

    try:
        with state.db.begin_immediate():
            try:
                target_evaluation_id = persist_outcome(state, decision, context)
                surface_status = decision.status
            except SurfaceRateLimitedError as rate_limited:
                # The gate-blocked evaluation is already written under the
                # same write lock that observed the cap. Complete the release
                # against it rather than losing the work to a retry.
                target_evaluation_id = rate_limited.persisted_evaluation_id
                surface_status = "gate_blocked"
            completed = state.holdouts.complete_release(
                claim,
                release_authority=release.authority,
                release_action=release.action,
                target_evaluation_id=target_evaluation_id,
                **_completion_fields(release),
            )
            if isinstance(completed, Err):
                raise _CompletionRefused(completed.error.detail)
    except _CompletionRefused as refused:
        return _fail(
            state,
            holdout,
            claim,
            HoldoutReleaseError(
                operation="complete_release",
                detail=refused.detail,
                category="persistence",
                holdout_id=holdout.id,
            ),
            resolution=release,
            scan_id=runtime.scan_id,
        )
    except Exception as exc:
        return _fail(
            state,
            holdout,
            claim,
            HoldoutReleaseError(
                operation="persist_release",
                detail=str(exc) or type(exc).__name__,
                category="persistence",
                holdout_id=holdout.id,
            ),
            resolution=release,
            scan_id=runtime.scan_id,
        )

    state.complete_scan(
        runtime.scan_id, messages_scanned=1, relevant_found=int(surface_status == "surfaced")
    )
    return _outcome(
        holdout,
        "released",
        authority=release.authority,
        action=release.action,
        label=release.label.label if release.label else None,
        case_id=release.label.case_id if release.label else None,
        scan_id=runtime.scan_id,
        target_evaluation_id=target_evaluation_id,
        surface_status=surface_status,
        dossier_summary_id=runtime.project.dossier_summary_id,
        dossier_revision=runtime.dossier_revision,
        resumed_scan=runtime.resumed_scan,
    )


async def release_pending_holdouts(
    *,
    state: StateManager,
    tracer: object,
    feedback: object,
    labels: ReleaseLabels,
    owner: str | None = None,
) -> Result[HoldoutReleaseReport, HoldoutReleaseError]:
    """Attempt every eligible hold, oldest first, and report each outcome.

    Refuses the whole run only before it starts, and only for a mistake about
    the whole run: labels that were written for a different population. Once
    the first hold is claimed there is no early exit and no age cutoff — one
    hold whose project was deleted must not strand the twenty behind it, and
    neither must one that fails in a way nothing here anticipated. Every
    failure is recorded on its own hold, which stays pending for a later run.
    """
    targets = validate_label_targets(state, labels)
    if isinstance(targets, Err):
        return targets

    worker = owner or f"holdout-release-{uuid.uuid4().hex[:12]}"
    eligible = state.holdouts.list_releasable()
    eligible_evaluations = {holdout.evaluation_id for holdout in eligible}

    outcomes: list[HoldoutReleaseOutcome] = []
    for holdout in eligible:
        try:
            outcome = await release_one_holdout(
                state=state,
                tracer=tracer,
                feedback=feedback,
                holdout=holdout,
                labels=labels,
                owner=worker,
            )
        except Exception as exc:
            # Deliberately broad, and deliberately not a re-raise. Whatever
            # went wrong on this hold, the ones behind it are unaffected and
            # are still owed an attempt. The hold keeps whatever claim it
            # took; its lease expiring is what returns it to the queue, so a
            # failure that escaped every handler above cannot lose it either.
            logger.exception("holdout %s release raised", holdout.id)
            outcome = _outcome(
                holdout,
                "failed",
                error=f"{type(exc).__name__}: {exc}",
                error_category="unexpected",
            )
        outcomes.append(outcome)

    unmatched = tuple(
        sorted(
            resolved.case_id
            for evaluation_id, resolved in labels.by_evaluation.items()
            if evaluation_id not in eligible_evaluations
        )
    )
    return Ok(
        HoldoutReleaseReport(
            attempted=len(outcomes),
            released=sum(1 for outcome in outcomes if outcome.status == "released"),
            failed=sum(1 for outcome in outcomes if outcome.status == "failed"),
            skipped=sum(1 for outcome in outcomes if outcome.status == "skipped"),
            outcomes=tuple(outcomes),
            unmatched_label_cases=unmatched,
        )
    )


__all__: Sequence[str] = (
    "AssayKeyCase",
    "AssayKeyFile",
    "AssayLabelCase",
    "AssayLabelsFile",
    "AssayPacketFile",
    "ASSAY_SCHEMA_FILES",
    "HoldoutReleaseError",
    "HoldoutReleaseOutcome",
    "HoldoutReleaseReport",
    "NO_EXCLUSION",
    "ReleaseInputs",
    "ReleaseLabels",
    "ReleaseProject",
    "ResolvedLabel",
    "action_for_label",
    "interchange_validator",
    "label_from_case",
    "load_release_labels",
    "prepare_release_runtime",
    "release_one_holdout",
    "release_pending_holdouts",
    "released_label_record",
    "resolve_labels",
    "resolve_release",
    "validate_frozen_project",
    "validate_label_targets",
)
