"""Convert neutral StateManager grade exports into Jig cases + causal scores.

Storage owns the database-shaped export. This module owns the evaluation-
harness representation so importing ``state_manager`` does not pull Jig or
``scout.evals.phase1`` into every production process.

This is a pure, read-only projection: it accepts mappings and constructs
Jig objects only. It never receives a FeedbackLoop and never calls
``store_result``/``score`` — Scout's own tables remain the sole mutable
authority over grade data, and Jig is strictly an analysis projection
target here, not a second write path.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from jig import EvalCase, Score, ScoreSource

from scout.config import FAILURE_DIMENSIONS
from scout.evals.phase1.models import render_case_sentinel
from scout.grading.service import validate_grade_envelope

# Canonical failure dimensions, derived from config.FAILURE_DIMENSIONS (itself
# sourced from web/grading_schema.json's failureDimension enum) so the grading
# contract has exactly one dimension authority. Every projected grade emits
# exactly one HUMAN score per dimension: 0.0 when the dimension is listed as
# failed, 1.0 otherwise. A fixed vector distinguishes a human pass from
# missing data and is directly consumable by sweeps, regression detection,
# and calibration — unlike emitting scores only for failed dimensions, which
# would leave "passed" and "never graded" indistinguishable.
if len(FAILURE_DIMENSIONS) != 7 or len(set(FAILURE_DIMENSIONS)) != 7:
    raise RuntimeError(
        "scout.config.FAILURE_DIMENSIONS must contain exactly seven unique values, "
        f"got {FAILURE_DIMENSIONS!r}"
    )
CANONICAL_FAILURE_DIMENSIONS: tuple[str, ...] = FAILURE_DIMENSIONS

# Identifies the shape of the Jig result metadata this module emits. Bump
# when the metadata contract below changes so consumers can distinguish
# rebuild vintages.
PROJECTION_VERSION = "scout-finalized-grades-v1"

# Materialized when a false-negative correction lacks the project, dossier,
# or posture identity needed to know what a counterfactual replay would
# have produced. This is the sole rejection-free resolution for missing
# counterfactual identity — see project_exported_grade.
REPLAY_UNREADY_MISSING_COUNTERFACTUAL_IDENTITY = "missing_counterfactual_identity"


class ExportedGradeRejected(ValueError):
    """Raised when an exported grade record cannot become an EvalCase."""


@dataclass(frozen=True, slots=True)
class ScoutJigProjection:
    """One exported grade, projected into the Jig object families a runner
    or analysis tool consumes.

    ``eval_case`` is the same rendered shape the Phase 1 corpus loader
    produces (Jig runners use ``EvalCase`` identity). ``result_content`` and
    ``result_metadata`` are what an effectful caller passes to
    ``FeedbackLoop.store_result`` — the actual Scout output (or a canonical
    no-draft terminal payload) and its full causal/replay metadata.
    ``scores`` is the fixed seven-dimension HUMAN score vector carrying causal
    and dossier identity in each score's ``metadata`` (downstream analysis
    reads ``Score.metadata`` rather than parsing rendered input).
    """

    eval_case: EvalCase
    result_content: str
    result_metadata: dict[str, Any]
    scores: tuple[Score, ...]


def _validate_and_build_contract_grade(
    evaluation: Mapping[str, Any], grade: Mapping[str, Any], evaluation_id: Any
) -> dict[str, Any]:
    # Export rows also carry bookkeeping fields outside the shared contract.
    # Strip those before validation and store only this validated shape in
    # EvalCase.metadata so validation and consumption cannot diverge.
    contract_grade = {
        "relevance_judgment": grade.get("relevance_judgment"),
        "action_judgment": grade.get("action_judgment"),
        "dimensions": grade.get("dimensions"),
        "failure_note": grade.get("failure_note"),
        "factual_offending_claim": grade.get("factual_offending_claim"),
        "factual_disposition": grade.get("factual_disposition"),
        "factual_contradicting_evidence": grade.get("factual_contradicting_evidence"),
        "context_missing_input": grade.get("context_missing_input"),
        "posture_should_have_been": grade.get("posture_should_have_been"),
        "implication_implied_claim": grade.get("implication_implied_claim"),
        "implication_missing_support": grade.get("implication_missing_support"),
        "edited_text": grade.get("edited_text"),
    }
    contract_errors = validate_grade_envelope(contract_grade, evaluation.get("posture"))
    if contract_errors:
        raise ExportedGradeRejected(
            f"exported record {evaluation_id} has an invalid grade: {contract_errors}"
        )
    return contract_grade


def _build_causal_scores(
    *,
    case_id: str,
    evaluation: Mapping[str, Any],
    grade: Mapping[str, Any],
    source: Mapping[str, Any],
    contract_grade: Mapping[str, Any],
) -> tuple[Score, ...]:
    """Build the fixed seven-dimension HUMAN score vector for one exported grade.

    Every score carries the same common causal metadata (joinable back to
    the source evaluation and pinned dossier) plus dimension-specific
    detail already owned by the grading contract — never invented values;
    absent detail stays None.
    """
    failed_dimensions = set(contract_grade.get("dimensions") or [])
    common_metadata: dict[str, Any] = {
        "case_id": case_id,
        "evaluation_id": evaluation.get("evaluation_id"),
        "post_id": source.get("post_id"),
        "scan_id": evaluation.get("scan_id"),
        "graded_at": grade.get("graded_at"),
        "grade_source": grade.get("grade_source"),
        "relevance_judgment": contract_grade.get("relevance_judgment"),
        "action_judgment": contract_grade.get("action_judgment"),
        "failure_note": contract_grade.get("failure_note"),
        "project_key": evaluation.get("project_key"),
        "dossier_revision": evaluation.get("dossier_revision"),
        "dossier_summary_id": evaluation.get("dossier_summary_id"),
    }

    dimension_extra: dict[str, dict[str, Any]] = {
        "factual_support": {
            "offending_claim": contract_grade.get("factual_offending_claim"),
            "disposition": contract_grade.get("factual_disposition"),
            "contradicting_evidence": contract_grade.get("factual_contradicting_evidence"),
        },
        "contextual_understanding": {
            "missing_input": contract_grade.get("context_missing_input"),
        },
        "posture": {
            "observed_posture": evaluation.get("posture"),
            "should_have_been": contract_grade.get("posture_should_have_been"),
        },
        "unsupported_implication": {
            "implied_claim": contract_grade.get("implication_implied_claim"),
            "missing_support": contract_grade.get("implication_missing_support"),
        },
    }

    scores = []
    for dimension in CANONICAL_FAILURE_DIMENSIONS:
        metadata = dict(common_metadata)
        metadata.update(dimension_extra.get(dimension, {}))
        scores.append(
            Score(
                dimension=dimension,
                value=0.0 if dimension in failed_dimensions else 1.0,
                source=ScoreSource.HUMAN,
                metadata=metadata,
            )
        )
    return tuple(scores)


def project_exported_grade(record: Mapping[str, Any]) -> ScoutJigProjection:
    """Convert one ``StateManager.export_eval_cases`` record into a
    :class:`ScoutJigProjection` — the rendered ``EvalCase``, the Jig result
    content/metadata, and a causal HUMAN score vector.

    Desired relevance is derived from the stored relevance judgment. A
    posture correction overrides the observed posture, and the resulting
    terminal status is not_relevant, abstained, or surfaced.

    A false-negative correction is the one case where the desired outcome
    can call for a surfaced response the original (not-relevant) execution
    never resolved dossier grounding for. When the project, dossier, or
    posture identity needed to know what that counterfactual replay would
    have produced is missing, this is not treated as malformed input: it is
    materialized as a canonical replay-unready record (observed_outcome
    forced to not_relevant, expectation fields nulled, replay_ready=False)
    rather than rejected or backfilled with invented identity.
    """
    evaluation = record.get("evaluation") or {}
    grade = record.get("grade") or {}
    source = record.get("source") or {}

    evaluation_id = evaluation.get("evaluation_id")
    if evaluation_id is None:
        raise ExportedGradeRejected("exported record is missing evaluation_id")

    relevance_judgment = grade.get("relevance_judgment")
    observed_posture = evaluation.get("posture")
    observed_outcome = evaluation.get("surface_status")

    contract_grade = _validate_and_build_contract_grade(evaluation, grade, evaluation_id)

    if relevance_judgment == "false_positive":
        desired_relevant = False
    elif relevance_judgment == "false_negative":
        desired_relevant = True
    else:
        desired_relevant = bool(evaluation.get("source_relevant"))

    desired_posture = grade.get("posture_should_have_been") or observed_posture

    project_key = evaluation.get("project_key")
    dossier_revision = evaluation.get("dossier_revision")
    dossier_summary_id = evaluation.get("dossier_summary_id")

    replay_ready = True
    replay_unready_reason: str | None = None

    if relevance_judgment == "false_negative" and not (
        desired_posture and project_key and dossier_revision and dossier_summary_id
    ):
        replay_ready = False
        replay_unready_reason = REPLAY_UNREADY_MISSING_COUNTERFACTUAL_IDENTITY
        observed_outcome = "not_relevant"
        expected_outcome = None
        desired_relevant = True
        desired_posture = None
        terminal_status = None
        project_key = None
        dossier_revision = None
        dossier_summary_id = None
    else:
        if not desired_relevant:
            terminal_status = "not_relevant"
        elif desired_posture == "abstain":
            terminal_status = "abstained"
        else:
            terminal_status = "surfaced"
        # expected_outcome mirrors expected_terminal_status: the two are
        # named separately so result metadata pairs observed_outcome with
        # an expected_outcome of its own, without callers having to reach
        # back into the rendered EvalCase.expected JSON.
        expected_outcome = terminal_status

    case_id = f"export-evaluation-{evaluation_id}"
    lines = [
        render_case_sentinel(case_id),
        f"platform: {source.get('platform', '')}",
        f"channel: {source.get('channel', '')}",
        f"content: {source.get('content', '')}",
    ]
    parent = source.get("parent")
    if parent:
        lines.append(f"parent_author: {parent.get('author_name', '')}")
        lines.append(f"parent_text: {parent.get('text', '')}")

    expected = {
        "expected_source_relevant": desired_relevant,
        "expected_posture": desired_posture,
        "expected_terminal_status": terminal_status,
        "replay_ready": replay_ready,
        "replay_unready_reason": replay_unready_reason,
    }

    eval_case = EvalCase(
        input="\n".join(lines),
        expected=json.dumps(expected, sort_keys=True),
        context={
            "case_id": case_id,
            "evaluation_id": evaluation_id,
            "project_key": project_key,
            "dossier_revision": dossier_revision,
            "dossier_summary_id": dossier_summary_id,
        },
        metadata={
            "case_id": case_id,
            "case_kind": "scout_response",
            "grade": contract_grade,
        },
    )

    draft = evaluation.get("draft")
    if draft is not None:
        result_content = draft
    else:
        # Canonical, deterministic no-draft terminal payload — never
        # EvalCase.expected, which represents what should have happened,
        # not what Scout actually produced.
        result_content = json.dumps(
            {"draft": None, "outcome": observed_outcome, "terminal": True},
            sort_keys=True,
            separators=(",", ":"),
        )

    result_metadata: dict[str, Any] = {
        "projection_version": PROJECTION_VERSION,
        "case_id": case_id,
        "evaluation_id": evaluation_id,
        "post_id": source.get("post_id"),
        "scan_id": evaluation.get("scan_id"),
        "project_key": project_key,
        "dossier_revision": dossier_revision,
        "dossier_summary_id": dossier_summary_id,
        "observed_outcome": observed_outcome,
        "expected_outcome": expected_outcome,
        "expected_source_relevant": desired_relevant,
        "expected_posture": desired_posture,
        "expected_terminal_status": terminal_status,
        "replay_ready": replay_ready,
        "replay_unready_reason": replay_unready_reason,
        "grade": contract_grade,
    }

    scores = _build_causal_scores(
        case_id=case_id,
        evaluation=evaluation,
        grade=grade,
        source=source,
        contract_grade=contract_grade,
    )
    return ScoutJigProjection(
        eval_case=eval_case,
        result_content=result_content,
        result_metadata=result_metadata,
        scores=scores,
    )


def exported_grade_to_eval_case(record: Mapping[str, Any]) -> EvalCase:
    """Compatibility wrapper over :func:`project_exported_grade`.

    Retained for existing callers that only want the rendered ``EvalCase``
    shape the Phase 1 corpus loader produces.
    """
    return project_exported_grade(record).eval_case


def project_exported_grades(records: Sequence[Mapping[str, Any]]) -> list[ScoutJigProjection]:
    """Batch helper: project every record, in order."""
    return [project_exported_grade(record) for record in records]


__all__ = [
    "CANONICAL_FAILURE_DIMENSIONS",
    "PROJECTION_VERSION",
    "REPLAY_UNREADY_MISSING_COUNTERFACTUAL_IDENTITY",
    "ExportedGradeRejected",
    "ScoutJigProjection",
    "exported_grade_to_eval_case",
    "project_exported_grade",
    "project_exported_grades",
]
