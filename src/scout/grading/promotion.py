"""Promote a human-confirmed model-negative case into Scout's response flow."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import scout.config as _config
from scout.config import GradeRecord
from scout.dossiers.resolver import get_pinned_dossier_revision
from scout.grading.feedback import FeedbackMode, legacy_feedback_bundle
from scout.result import Err
from scout.scanning.agent import (
    PhaseRunIdentity,
    ScoutExecutionContext,
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
from scout.scanning.schemas import RelevancePhaseOutput
from scout.storage.state import StateManager

PromotionErrorCategory = Literal["validation", "config", "generation", "persistence"]


class NegativeCasePromotionError(RuntimeError):
    def __init__(self, category: PromotionErrorCategory, detail: str) -> None:
        super().__init__(detail)
        self.category = category
        self.detail = detail


@dataclass(frozen=True, slots=True)
class NegativeCasePromotionResult:
    source_grade_id: int
    scan_id: int
    target_evaluation_id: int
    surface_status: str
    already_completed: bool = False


async def promote_negative_case(
    *,
    state: StateManager,
    tracer: object,
    feedback: object,
    source_evaluation_id: int,
    grade: GradeRecord,
) -> NegativeCasePromotionResult:
    """Run reply_draft + critic after a human false-negative judgment.

    The relevance phase is deliberately not re-run. Its authority is the
    source grade; the generated response is persisted under a distinct target
    evaluation so it can receive an ordinary draft grade later.
    """
    claim = state.begin_human_positive_promotion(grade)
    if claim["status"] == "completed":
        target = state.get_evaluation(int(claim["target_evaluation_id"]))
        if target is None:
            raise NegativeCasePromotionError(
                "persistence", "completed promotion has no target evaluation"
            )
        return NegativeCasePromotionResult(
            source_grade_id=int(claim["source_grade_id"]),
            scan_id=int(claim["scan_id"]),
            target_evaluation_id=int(claim["target_evaluation_id"]),
            surface_status=str(target["surface_status"]),
            already_completed=True,
        )

    scan_id: int | None = None
    try:
        source = state.get_evaluation(source_evaluation_id)
        if source is None:
            raise NegativeCasePromotionError(
                "validation", f"evaluation {source_evaluation_id} not found"
            )
        message = state.load_post(int(source["post_id"]))
        if message is None:
            raise NegativeCasePromotionError(
                "persistence", f"post {source['post_id']} not found"
            )

        scan_id = state.start_scan(
            environment=_config.SCOUT_ENVIRONMENT,
            run_kind="human_positive",
        )
        state.attach_human_positive_promotion_scan(source_evaluation_id, scan_id)
    except NegativeCasePromotionError as exc:
        state.fail_human_positive_promotion(
            source_evaluation_id, error_detail=exc.detail
        )
        raise
    except Exception as exc:
        detail = str(exc) or type(exc).__name__
        state.fail_human_positive_promotion(
            source_evaluation_id, error_detail=detail
        )
        if scan_id is not None:
            with contextlib.suppress(Exception):
                state.fail_scan(
                    scan_id,
                    1,
                    failure_post_id=grade.post_id,
                    error_kind="persistence",
                    error_message=detail,
                )
        raise NegativeCasePromotionError("persistence", detail) from exc

    assert scan_id is not None

    try:
        registry = state.load_runtime_registry()
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
            raise NegativeCasePromotionError(
                "config",
                "the case no longer resolves to an active project route; "
                "update the route before retrying",
            )

        dossiers, dossier_errors = load_project_dossiers(registry.projects)
        if dossier_errors:
            raise NegativeCasePromotionError("config", "; ".join(dossier_errors))
        dossier = dossiers.get(route.project_key)
        if dossier is None:
            raise NegativeCasePromotionError(
                "config", f"no ready dossier for project {route.project_key!r}"
            )

        dossier_revision: str | None = None
        if _config.SCOUT_DOSSIER_ROOT:
            try:
                dossier_revision = get_pinned_dossier_revision(
                    Path(_config.SCOUT_DOSSIER_ROOT)
                )
            except RuntimeError as exc:
                raise NegativeCasePromotionError("config", str(exc)) from exc

        feedback_mode: FeedbackMode = (
            "active" if _config.FEEDBACK_PROMPT_ENABLED else "shadow"
        )
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
                snapshot_phase_id=phase_by_name["relevance"].snapshot_phase_id,
                model=_config.RELEVANCE_MODEL,
            ),
            "reply_draft": PhaseRunIdentity(
                snapshot_phase_id=phase_by_name["reply_draft"].snapshot_phase_id,
                model=_config.REPLY_DRAFT_MODEL,
            ),
            "critic": PhaseRunIdentity(
                snapshot_phase_id=phase_by_name["critic"].snapshot_phase_id,
                model=_config.CRITIC_MODEL,
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
        relevance = RelevancePhaseOutput(
            relevant=True,
            score=1.0,
            reason=(
                f"Human reviewer marked model-negative evaluation "
                f"#{source_evaluation_id} relevant. {grade.failure_note or ''}"
            ).strip(),
            relevant_to=[route.project_key],
        )
        phase_result = await draft_and_critic_step(
            {
                "input": RoutedMessage(message=message, keyword_route=route),
                "phase_configs": phase_configs,
                "dossier_summaries": dossiers,
                "execution_context": execution,
                "relevance_output": relevance,
            }
        )
        if isinstance(phase_result, Err):
            detail = getattr(phase_result.error, "detail", str(phase_result.error))
            raise NegativeCasePromotionError("generation", detail)

        decision = classify_outcome(phase_result.value, message, dossiers)
        context = PersistenceContext(
            post_id=int(source["post_id"]),
            scan_id=scan_id,
            keyword_route_id=route.id,
            dossier_revision=dossier_revision,
            dossier_summary_id=registry.projects[route.project_key].dossier_summary_id,
            surfaced_at=message.created_at.isoformat(),
            allow_response_only_phase_runs=True,
        )
        with state.db.begin_immediate():
            target_evaluation_id = persist_outcome(state, decision, context)
            state.complete_human_positive_promotion(
                source_evaluation_id,
                scan_id=scan_id,
                target_evaluation_id=target_evaluation_id,
            )
        state.complete_scan(
            scan_id,
            messages_scanned=1,
            relevant_found=int(decision.status == "surfaced"),
            advance_watermark=False,
        )
        return NegativeCasePromotionResult(
            source_grade_id=int(claim["source_grade_id"]),
            scan_id=scan_id,
            target_evaluation_id=target_evaluation_id,
            surface_status=decision.status,
        )
    except NegativeCasePromotionError as exc:
        state.fail_human_positive_promotion(
            source_evaluation_id, error_detail=exc.detail
        )
        with contextlib.suppress(Exception):
            state.fail_scan(
                scan_id,
                1,
                failure_post_id=int(source["post_id"]),
                error_kind=exc.category,
                error_message=exc.detail,
            )
        raise
    except Exception as exc:
        detail = str(exc) or type(exc).__name__
        state.fail_human_positive_promotion(
            source_evaluation_id, error_detail=detail
        )
        with contextlib.suppress(Exception):
            state.fail_scan(
                scan_id,
                1,
                failure_post_id=int(source["post_id"]),
                error_kind="persistence",
                error_message=detail,
            )
        raise NegativeCasePromotionError("persistence", detail) from exc
