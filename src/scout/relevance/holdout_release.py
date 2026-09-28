"""Apply a blind-grading answer key to exported relevance holdouts."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import socket
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jig import FeedbackLoop, TracingLogger

import scout.config as _config
from scout.dossiers.resolver import DossierSummary, get_pinned_dossier_revision
from scout.grading.feedback import FeedbackMode, legacy_feedback_bundle
from scout.registry import KeywordRoute, RuntimeRegistry
from scout.relevance.models import RelevanceAction
from scout.result import Err
from scout.scanning.agent import (
    PhaseRunIdentity,
    ScoutExecutionContext,
    ScoutPhaseConfigs,
    build_scout_phase_configs,
    resolve_mode_for_message,
)
from scout.scanning.pipeline import draft_and_critic_step
from scout.scanning.prefilter import RoutedMessage, keyword_prefilter
from scout.scanning.runner import (
    PersistenceContext,
    classify_outcome,
    load_project_dossiers,
    persist_outcome,
)
from scout.scanning.schemas import RelevancePhaseOutput, ReplyCandidate
from scout.storage.relevance_holdouts import RelevanceHoldout, registry_state
from scout.storage.state import StateManager, SurfaceRateLimitedError

logger = logging.getLogger(__name__)


class HoldoutReleaseError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AnswerKeyEntry:
    evaluation_id: int
    exclusion: str | bool | None
    needs_thread: bool | None
    substance: str | bool | None


@dataclass(frozen=True, slots=True)
class HoldoutReleaseResult:
    batch_id: str
    released: int
    already_released: int
    action_counts: Mapping[RelevanceAction, int]


@dataclass(frozen=True, slots=True)
class HoldoutReleaseProvenance:
    source_holdout_id: int
    source_evaluation_id: int
    hold_to_release_seconds: float
    release_action: RelevanceAction
    held_dossier_revision: str | None
    release_dossier_revision: str | None
    dossier_revision_changed: bool
    held_registry_sha256: str
    release_registry_sha256: str
    registry_changed: bool


@dataclass(frozen=True, slots=True)
class _ResponseFlow:
    registry: RuntimeRegistry
    route: KeywordRoute
    dossiers: Mapping[str, DossierSummary]
    dossier_revision: str | None
    phase_configs: ScoutPhaseConfigs
    execution: ScoutExecutionContext


def _label(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "present", "exclude", "excluded", "1"}:
            return True
        if normalized in {"false", "no", "none", "absent", "0"}:
            return False
    return None


def _answer_rows(document: object) -> Sequence[object]:
    if isinstance(document, list):
        return document
    if isinstance(document, dict):
        for key in ("labels", "answers", "cases"):
            candidate = document.get(key)
            if isinstance(candidate, list):
                return candidate
        return [
            {"evaluation_id": evaluation_id, "labels": labels}
            for evaluation_id, labels in document.items()
        ]
    raise ValueError("answer key must be a JSON list or object")


def load_answer_key(path: Path) -> tuple[AnswerKeyEntry, ...]:
    """Load the small stable portion of an Assay answer key used for routing."""
    document: object = json.loads(path.read_text(encoding="utf-8"))
    entries: list[AnswerKeyEntry] = []
    seen: set[int] = set()
    for raw in _answer_rows(document):
        if not isinstance(raw, dict):
            raise ValueError("answer key entries must be objects")
        identity = raw.get("evaluation_id", raw.get("case_id", raw.get("id")))
        if identity is None:
            raise ValueError("answer-key evaluation id is required")
        try:
            evaluation_id = int(identity)
        except (TypeError, ValueError):
            raise ValueError(f"invalid answer-key evaluation id: {identity!r}") from None
        if evaluation_id in seen:
            raise ValueError(f"duplicate answer-key evaluation {evaluation_id}")
        seen.add(evaluation_id)
        labels = raw.get("labels", raw)
        if not isinstance(labels, dict):
            raise ValueError(f"labels for evaluation {evaluation_id} must be an object")
        exclusion_value = labels.get("exclusion")
        if exclusion_value is None:
            exclusion_value = any(
                _label(value) is True
                for key, value in labels.items()
                if str(key).startswith("excl_")
            ) or None
        entries.append(
            AnswerKeyEntry(
                evaluation_id=evaluation_id,
                exclusion=(
                    exclusion_value
                    if isinstance(exclusion_value, str)
                    else _label(exclusion_value)
                ),
                needs_thread=_label(labels.get("needs_thread")),
                substance=(
                    labels.get("substance")
                    if isinstance(labels.get("substance"), str)
                    else _label(labels.get("substance"))
                ),
            )
        )
    return tuple(entries)


def action_from_labels(entry: AnswerKeyEntry, fallback: RelevanceAction) -> RelevanceAction:
    """Apply the reviewed precedence: exclusion, thread need, then substance."""
    exclusion = entry.exclusion
    if exclusion is True or (
        isinstance(exclusion, str) and exclusion.strip().lower() not in {"", "none"}
    ):
        return "drop"
    if entry.needs_thread is True:
        return "review"
    substance = entry.substance
    if substance is True or (
        isinstance(substance, str) and substance.strip().lower() == "in_post"
    ):
        return "respond"
    if isinstance(substance, str) and substance.strip().lower() == "pointer":
        return "review"
    if substance is False or (
        isinstance(substance, str) and substance.strip().lower() == "none"
    ):
        return "drop"
    logger.warning(
        "answer key for evaluation %s is indeterminate; falling back to recorded action %s",
        entry.evaluation_id,
        fallback,
    )
    return fallback


def _revision() -> str | None:
    if not _config.SCOUT_DOSSIER_ROOT:
        return None
    return get_pinned_dossier_revision(Path(_config.SCOUT_DOSSIER_ROOT))


def _canonical_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _provenance(
    holdout: RelevanceHoldout,
    *,
    action: RelevanceAction,
    release_registry: Mapping[str, object],
    release_dossier_revision: str | None,
    released_at: datetime,
) -> HoldoutReleaseProvenance:
    created_at = datetime.fromisoformat(holdout.created_at)
    age = max(0.0, (released_at - created_at).total_seconds())
    held_registry_hash = _canonical_hash(holdout.registry_state)
    release_registry_hash = _canonical_hash(release_registry)
    return HoldoutReleaseProvenance(
        source_holdout_id=holdout.id,
        source_evaluation_id=holdout.evaluation_id,
        hold_to_release_seconds=age,
        release_action=action,
        held_dossier_revision=holdout.dossier_revision,
        release_dossier_revision=release_dossier_revision,
        dossier_revision_changed=holdout.dossier_revision != release_dossier_revision,
        held_registry_sha256=held_registry_hash,
        release_registry_sha256=release_registry_hash,
        registry_changed=held_registry_hash != release_registry_hash,
    )


def _human_reason(
    holdout: RelevanceHoldout, action: RelevanceAction, project_key: str | None
) -> str:
    destination = f" for project {project_key!r}" if project_key else ""
    return (
        f"Blind-grading answer key released held evaluation #{holdout.evaluation_id}"
        f"{destination} with action {action}."
    )


def _route_for_source(
    source: Mapping[str, Any], message: Any, registry: RuntimeRegistry
) -> KeywordRoute:
    route = next(
        (
            candidate
            for candidate in registry.keywords
            if candidate.id == source["keyword_route_id"]
        ),
        None,
    )
    if route is None:
        rerouted = keyword_prefilter([message], registry.keywords)
        route = rerouted[0].keyword_route if rerouted else None
    if route is None or route.project_key not in registry.projects:
        raise HoldoutReleaseError(
            "the held post no longer resolves to an active project route"
        )
    return route


def _prepare_response_flow(
    *,
    state: StateManager,
    tracer: TracingLogger,
    feedback: FeedbackLoop,
    source: Mapping[str, Any],
    message: Any,
    scan_id: int,
) -> _ResponseFlow:
    registry = state.load_runtime_registry()
    route = _route_for_source(source, message, registry)
    dossiers, dossier_errors = load_project_dossiers(registry.projects)
    if dossier_errors:
        raise HoldoutReleaseError("; ".join(dossier_errors))
    if route.project_key not in dossiers:
        raise HoldoutReleaseError(f"no ready dossier for project {route.project_key!r}")
    dossier_revision = _revision()
    feedback_mode: FeedbackMode = "active" if _config.FEEDBACK_PROMPT_ENABLED else "shadow"
    snapshot = state.record_feedback_snapshot(scan_id, mode=feedback_mode)
    if feedback_mode == "active":
        feedback_bundle = state.load_committed_feedback_bundle(
            snapshot.snapshot_id, expected_mode="active"
        )
    else:
        from scout.grading.service import format_grading_signals

        feedback_bundle = legacy_feedback_bundle(
            format_grading_signals(state.get_recent_grading_signals(limit_scans=3))
        )
    phase_by_name = {phase.phase: phase for phase in snapshot.phases}
    identities = {
        "relevance": PhaseRunIdentity(
            phase_by_name["relevance"].snapshot_phase_id, _config.RELEVANCE_MODEL
        ),
        "reply_draft": PhaseRunIdentity(
            phase_by_name["reply_draft"].snapshot_phase_id, _config.REPLY_DRAFT_MODEL
        ),
        "critic": PhaseRunIdentity(
            phase_by_name["critic"].snapshot_phase_id, _config.CRITIC_MODEL
        ),
    }
    mode = resolve_mode_for_message(_config.MODES["lead_gen"], route)
    phase_configs = build_scout_phase_configs(
        relevance_model=_config.RELEVANCE_MODEL,
        reply_draft_model=_config.REPLY_DRAFT_MODEL,
        critic_model=_config.CRITIC_MODEL,
        mode_cfg=mode,
        projects=registry.projects,
        templates=registry.prompt_templates,
        tracer=tracer,
        feedback=feedback,
        lessons=state.get_recent_critique_feedback(limit=10) or None,
        feedback_bundle=feedback_bundle,
    )
    execution = ScoutExecutionContext(
        state=state,
        scan_id=scan_id,
        post_id=int(source["post_id"]),
        relevance=identities["relevance"],
        reply_draft=identities["reply_draft"],
        critic=identities["critic"],
    )
    return _ResponseFlow(
        registry, route, dossiers, dossier_revision, phase_configs, execution
    )


async def _release_one(
    *,
    state: StateManager,
    tracer: TracingLogger,
    feedback: FeedbackLoop,
    holdout: RelevanceHoldout,
    action: RelevanceAction,
    owner: str,
) -> bool:
    claim = state.relevance_holdouts.claim_release(holdout.id, owner=owner)
    if claim.status == "released":
        if claim.release_action != action:
            raise HoldoutReleaseError(
                f"evaluation {claim.evaluation_id} was already released as "
                f"{claim.release_action}, not {action}"
            )
        return False
    assert claim.claim_token is not None
    scan_id: int | None = None
    source: Mapping[str, Any] | None = None
    try:
        source_row = state.get_evaluation(claim.evaluation_id)
        if source_row is None:
            raise HoldoutReleaseError(f"evaluation {claim.evaluation_id} not found")
        source = dict(source_row)
        message = state.load_post(int(source["post_id"]))
        if message is None:
            raise HoldoutReleaseError(f"post {source['post_id']} not found")
        scan_id = state.start_scan(
            environment=_config.SCOUT_ENVIRONMENT, run_kind="holdout_release"
        )
        state.relevance_holdouts.attach_release_scan(
            claim.id, claim_token=claim.claim_token, scan_id=scan_id
        )

        keyword_route_id: int | None
        if action in ("respond", "review"):
            flow = _prepare_response_flow(
                state=state,
                tracer=tracer,
                feedback=feedback,
                source=source,
                message=message,
                scan_id=scan_id,
            )
            release_registry = registry_state(flow.registry)
            released_at = datetime.now(UTC)
            provenance = _provenance(
                claim,
                action=action,
                release_registry=release_registry,
                release_dossier_revision=flow.dossier_revision,
                released_at=released_at,
            )
            relevance = RelevancePhaseOutput(
                relevant=True,
                score=1.0,
                reason=_human_reason(claim, action, flow.route.project_key),
                relevant_to=[flow.route.project_key],
            )
            phase_result = await draft_and_critic_step(
                {
                    "input": RoutedMessage(message=message, keyword_route=flow.route),
                    "phase_configs": flow.phase_configs,
                    "dossier_summaries": flow.dossiers,
                    "execution_context": flow.execution,
                    "relevance_output": relevance,
                    "relevance_action": action,
                }
            )
            if isinstance(phase_result, Err):
                detail = getattr(phase_result.error, "detail", str(phase_result.error))
                raise HoldoutReleaseError(detail)
            decision = classify_outcome(phase_result.value, message, flow.dossiers)
            keyword_route_id = flow.route.id
            dossier_revision = flow.dossier_revision
            dossier_summary_id = flow.registry.projects[
                flow.route.project_key
            ].dossier_summary_id
        else:
            registry = state.load_runtime_registry()
            release_registry = registry_state(registry)
            dossier_revision = _revision()
            released_at = datetime.now(UTC)
            provenance = _provenance(
                claim,
                action=action,
                release_registry=release_registry,
                release_dossier_revision=dossier_revision,
                released_at=released_at,
            )
            project_key = str(source["project_key"]) if source["project_key"] else None
            candidate = ReplyCandidate(
                relevant=False,
                score=0.0,
                reason=_human_reason(claim, action, project_key),
                relevant_to=[] if project_key is None else [project_key],
                project_key=project_key,
                relevance_classifier="human",
                relevance_action="drop",
            )
            decision = classify_outcome(candidate, message, {})
            current_route = next(
                (
                    candidate_route
                    for candidate_route in registry.keywords
                    if candidate_route.id == source["keyword_route_id"]
                ),
                None,
            )
            keyword_route_id = None if current_route is None else current_route.id
            dossier_summary_id = (
                registry.projects[project_key].dossier_summary_id
                if project_key is not None and project_key in registry.projects
                else None
            )

        context = PersistenceContext(
            post_id=int(source["post_id"]),
            scan_id=scan_id,
            keyword_route_id=keyword_route_id,
            dossier_revision=dossier_revision,
            dossier_summary_id=dossier_summary_id,
            surfaced_at=message.created_at.isoformat(),
            allow_response_only_phase_runs=True,
        )
        surfaced = decision.status == "surfaced"
        with state.db.begin_immediate():
            if action == "drop":
                target_evaluation_id = state.save_evaluation(
                    decision.evaluation,
                    context.post_id,
                    context.scan_id,
                    keyword_route_id=context.keyword_route_id,
                    project_key=decision.project_key,
                    posture=decision.posture,
                    surface_status=decision.status,
                    failure_reason=decision.terminal_reason,
                    dossier_revision=context.dossier_revision,
                    dossier_summary_id=context.dossier_summary_id,
                    relevance_classifier="human",
                    relevance_action=action,
                )
            else:
                try:
                    target_evaluation_id = persist_outcome(state, decision, context)
                except SurfaceRateLimitedError as rate_limited:
                    # The author-rate gate wrote its gate_blocked evaluation
                    # inside this transaction; release onto that outcome, as
                    # the scan runner does, rather than rolling it back.
                    target_evaluation_id = rate_limited.persisted_evaluation_id
                    surfaced = False
            state.relevance_holdouts.complete_release(
                claim.id,
                claim_token=claim.claim_token,
                action=action,
                target_evaluation_id=target_evaluation_id,
                released_at=released_at.isoformat(),
                provenance=asdict(provenance),
            )
        state.complete_scan(
            scan_id, messages_scanned=1, relevant_found=int(surfaced)
        )
        return True
    except Exception as exc:
        detail = str(exc) or type(exc).__name__
        state.relevance_holdouts.fail_release(
            claim.id, claim_token=claim.claim_token, error_detail=detail
        )
        if scan_id is not None:
            with contextlib.suppress(Exception):
                state.fail_scan(
                    scan_id,
                    1,
                    failure_post_id=(None if source is None else int(source["post_id"])),
                    error_kind="persistence",
                    error_message=detail,
                )
        if isinstance(exc, HoldoutReleaseError):
            raise
        raise HoldoutReleaseError(detail) from exc


async def release_holdout_batch(
    *,
    state: StateManager,
    tracer: TracingLogger,
    feedback: FeedbackLoop,
    batch_id: str,
    answers: Sequence[AnswerKeyEntry],
    owner: str | None = None,
) -> HoldoutReleaseResult:
    """Validate a complete key first, then release each row through a fenced claim."""
    holdouts = state.relevance_holdouts.list_batch(batch_id)
    if not holdouts:
        raise HoldoutReleaseError(f"batch {batch_id!r} not found")
    by_evaluation = {row.evaluation_id: row for row in holdouts}
    seen_evaluations: set[int] = set()
    duplicate_evaluations: set[int] = set()
    for answer in answers:
        if answer.evaluation_id in seen_evaluations:
            duplicate_evaluations.add(answer.evaluation_id)
        seen_evaluations.add(answer.evaluation_id)
    if duplicate_evaluations:
        raise HoldoutReleaseError(
            "answer key repeats batch evaluations: "
            f"{sorted(duplicate_evaluations)}"
        )
    answer_by_evaluation = {answer.evaluation_id: answer for answer in answers}
    outside = sorted(set(answer_by_evaluation) - set(by_evaluation))
    if outside:
        raise HoldoutReleaseError(
            f"answer key names evaluations outside batch {batch_id!r}: {outside}"
        )
    missing = sorted(set(by_evaluation) - set(answer_by_evaluation))
    if missing:
        raise HoldoutReleaseError(f"answer key omits batch evaluations: {missing}")

    actions = {
        evaluation_id: action_from_labels(
            answer_by_evaluation[evaluation_id], row.production_action
        )
        for evaluation_id, row in by_evaluation.items()
    }
    action_counts: dict[RelevanceAction, int] = {"respond": 0, "review": 0, "drop": 0}
    for action in actions.values():
        action_counts[action] += 1

    release_owner = owner or f"{socket.gethostname()}:{os.getpid()}"
    released = 0
    already_released = 0
    for holdout in holdouts:
        changed = await _release_one(
            state=state,
            tracer=tracer,
            feedback=feedback,
            holdout=holdout,
            action=actions[holdout.evaluation_id],
            owner=release_owner,
        )
        released += int(changed)
        already_released += int(not changed)
    return HoldoutReleaseResult(batch_id, released, already_released, action_counts)


__all__ = [
    "AnswerKeyEntry",
    "HoldoutReleaseError",
    "HoldoutReleaseResult",
    "action_from_labels",
    "load_answer_key",
    "release_holdout_batch",
]
