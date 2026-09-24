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
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

import scout.config as _config
from scout.config import Message
from scout.dossiers.resolver import DossierSummary, get_pinned_dossier_revision
from scout.grading.promotion import build_response_phase_runtime
from scout.registry import KeywordRoute, ProjectTarget, RuntimeRegistry
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

#: `exclusion` values that mean no exclusion fired. Compared case-folded and
#: stripped. Anything else is the name of the exclusion that fired, whatever
#: it is called — the catalogue, not this module, decides which exist.
NO_EXCLUSION_VALUES: frozenset[str] = frozenset({"", "none"})

#: `substance` values that resolve to a label once no exclusion fired.
SUBSTANCE_LABELS: dict[str, HoldoutLabel] = {
    "in_post": "in_post",
    "pointer": "pointer",
    "none": "none",
}

#: How long one worker holds a claim before another may take it over.
RELEASE_CLAIM_TTL_SECONDS = 900

ReleaseErrorCategory = Literal[
    "validation", "config", "generation", "persistence", "claim"
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
    """The private answer key, which is what a label actually joins through."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    format: Literal["assay.label-packet-key/v2"]
    name: str
    sitting: str
    digest: str
    plan_digest: str
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

    Precedence, first match wins: an exclusion that fired, then `in_post`,
    then `pointer`, then `none`. `needs_thread` is recorded but does not enter
    the mapping — the four labels are the whole action vocabulary.

    A missing substance with no exclusion is an incomplete answer, not a
    `none`: the reviewer was asked what the post carries and did not say.
    """
    if case.exclusion.strip().casefold() not in NO_EXCLUSION_VALUES:
        return Ok("exclusion")
    substance = None if case.substance is None else case.substance.strip().casefold()
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


def _read_model[T: BaseModel](
    path: Path, model: type[T], *, operation: str
) -> Result[T, HoldoutReleaseError]:
    """Parse one interchange file at the IO boundary."""
    try:
        return Ok(model.model_validate_json(path.read_bytes()))
    except OSError as exc:
        return Err(
            HoldoutReleaseError(operation=operation, detail=f"cannot read {path}: {exc}")
        )
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
    labels = _read_model(inputs.labels_path, AssayLabelsFile, operation="load_labels")
    if isinstance(labels, Err):
        return labels
    key = _read_model(inputs.key_path, AssayKeyFile, operation="load_key")
    if isinstance(key, Err):
        return key
    packet: AssayPacketFile | None = None
    if inputs.packet_path is not None:
        read = _read_model(inputs.packet_path, AssayPacketFile, operation="load_packet")
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


def prepare_release_runtime(
    state: StateManager, holdout: Holdout
) -> Result[ReleaseRuntime, HoldoutReleaseError]:
    """Validate today's gates for one hold and open its release scan.

    Every refusal below leaves the hold retryable: the configuration is what
    is wrong, not the hold, and repairing it must let the same hold through
    however old it is.
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

    registry = state.load_runtime_registry()
    route = _resolve_route(registry, holdout)
    if isinstance(route, Err):
        return route
    project = registry.projects.get(route.value.project_key)
    if project is None:
        return Err(
            HoldoutReleaseError(
                operation="prepare_release",
                detail=(
                    f"project {route.value.project_key!r} is no longer active; "
                    "restore it and retry"
                ),
                category="config",
                holdout_id=holdout.id,
            )
        )

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

    dossiers, dossier_errors = load_project_dossiers(registry.projects)
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

    scan_id = state.start_scan(
        environment=_config.SCOUT_ENVIRONMENT, run_kind="holdout_release"
    )
    return Ok(
        ReleaseRuntime(
            scan_id=scan_id,
            message=message,
            route=route.value,
            project=project,
            dossiers=dossiers,
            dossier_revision=dossier_revision,
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
    """
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
    )


async def release_pending_holdouts(
    *,
    state: StateManager,
    tracer: object,
    feedback: object,
    labels: ReleaseLabels,
    owner: str | None = None,
) -> HoldoutReleaseReport:
    """Attempt every eligible hold, oldest first, and report each outcome.

    No age cutoff and no early exit: one hold whose project was deleted must
    not strand the twenty behind it. Every failure is recorded on its own
    hold, which stays pending for a later run.
    """
    worker = owner or f"holdout-release-{uuid.uuid4().hex[:12]}"
    eligible = state.holdouts.list_releasable()
    eligible_evaluations = {holdout.evaluation_id for holdout in eligible}

    outcomes: list[HoldoutReleaseOutcome] = []
    for holdout in eligible:
        outcomes.append(
            await release_one_holdout(
                state=state,
                tracer=tracer,
                feedback=feedback,
                holdout=holdout,
                labels=labels,
                owner=worker,
            )
        )

    unmatched = tuple(
        sorted(
            resolved.case_id
            for evaluation_id, resolved in labels.by_evaluation.items()
            if evaluation_id not in eligible_evaluations
        )
    )
    return HoldoutReleaseReport(
        attempted=len(outcomes),
        released=sum(1 for outcome in outcomes if outcome.status == "released"),
        failed=sum(1 for outcome in outcomes if outcome.status == "failed"),
        skipped=sum(1 for outcome in outcomes if outcome.status == "skipped"),
        outcomes=tuple(outcomes),
        unmatched_label_cases=unmatched,
    )


__all__: Sequence[str] = (
    "AssayKeyCase",
    "AssayKeyFile",
    "AssayLabelCase",
    "AssayLabelsFile",
    "AssayPacketFile",
    "HoldoutReleaseError",
    "HoldoutReleaseOutcome",
    "HoldoutReleaseReport",
    "NO_EXCLUSION_VALUES",
    "ReleaseInputs",
    "ReleaseLabels",
    "ResolvedLabel",
    "action_for_label",
    "label_from_case",
    "load_release_labels",
    "prepare_release_runtime",
    "release_one_holdout",
    "release_pending_holdouts",
    "released_label_record",
    "resolve_labels",
    "resolve_release",
)
