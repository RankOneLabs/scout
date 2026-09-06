"""Atomic queue review: compare revisions → grade writer → immutable observation.

No fitting, replay, inference, or promotion is performed by this boundary.
The same Db transaction covers the grade revision and its queue attribution.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from pydantic import ValidationError

from scout.config import HUMAN_GRADE_SCHEMA_VERSION, GradeRecord
from scout.grading.artifacts import ArtifactDigest
from scout.grading.assistance_population import read_population
from scout.grading.assistance_scope import read_assistance_bundle
from scout.grading.assistance_store import supports_assistance
from scout.grading.assistance_types import RejectedInput, ReviewQueue
from scout.grading.review_types import ReviewDisposition, ReviewError, ReviewRequest
from scout.grading.service import grade_envelope_payload, validate_grade_envelope
from scout.grading.snapshots import RecordedPost
from scout.result import Err, Ok, Result
from scout.storage.grades import GradeValidationError
from scout.storage.migrations import grade_revision_comparison_shape
from scout.storage.state import StateManager


class ReviewConflict(ValueError):
    """Known precondition failure at the review IO boundary, safe to discard/reload."""


def review_source(
    state: StateManager, queue_digest: ArtifactDigest, evaluation_id: int
) -> Result[RejectedInput, ReviewError]:
    """Resolve frozen source bytes through the existing scoped integrity reader."""
    bundle = read_assistance_bundle(state.conn, (queue_digest,))
    if isinstance(bundle, Err):
        return Err(ReviewError(evaluation_id=evaluation_id, detail=bundle.error.detail, status=400))
    contents = {item.digest: item.content for item in bundle.value.artifacts}
    producers = [
        lineage
        for lineage in bundle.value.lineages
        if supports_assistance(lineage)
        and len(lineage.outputs) == 4
        and queue_digest == lineage.outputs[2]
    ]
    if not producers:
        return Err(
            ReviewError(
                evaluation_id=evaluation_id, detail="Unsupported queue producer", status=400
            )
        )
    queue = ReviewQueue.model_validate_json(contents[queue_digest])
    if not any(
        source.evaluation_id == evaluation_id for item in queue.items for source in item.sources
    ):
        return Err(
            ReviewError(
                evaluation_id=evaluation_id, detail="Evaluation is not in this queue", status=404
            )
        )
    population = read_population(queue.population_digest, contents)
    if isinstance(population, Err):
        return Err(
            ReviewError(evaluation_id=evaluation_id, detail=population.error.detail, status=400)
        )
    candidates = [item for item in population.value.items if item.evaluation.id == evaluation_id]
    if len(candidates) != 1:
        return Err(
            ReviewError(
                evaluation_id=evaluation_id, detail="Queue source is not unique", status=400
            )
        )
    source = candidates[0]
    if (
        source.post is None
        or source.context is None
        or source.evaluation.project_key != queue.project_key
    ):
        return Err(
            ReviewError(
                evaluation_id=evaluation_id,
                detail="Queue source lacks matching context",
                status=400,
            )
        )
    return Ok(source)


def current_revision(state: StateManager, evaluation_id: int) -> int | None:
    row = state.conn.execute(
        "SELECT r.id, r.grade_id, r.payload FROM grade_revisions r "
        "JOIN grades g ON g.id = r.grade_id "
        "WHERE g.evaluation_id = ? AND r.evaluation_id = ? ORDER BY r.revision DESC LIMIT 1",
        (evaluation_id, evaluation_id),
    ).fetchone()
    if row is None:
        if state.get_grade_id_for_evaluation(evaluation_id) is not None:
            raise ReviewConflict("Grade is missing its pinned revision; remediate the grade first")
        return None
    grade = state.get_grade_row_by_id(row["grade_id"])
    try:
        matches = grade is not None and json.loads(
            row["payload"]
        ) == grade_revision_comparison_shape(grade)
    except (ValueError, TypeError) as exc:
        raise ReviewConflict(
            "Grade revision payload is invalid; remediate the grade first"
        ) from exc
    if not matches:
        raise ReviewConflict("Grade differs from its pinned revision; remediate the grade first")
    return int(row["id"])


def save_review(
    state: StateManager, queue_digest: ArtifactDigest, evaluation_id: int, request: ReviewRequest
) -> Result[ReviewDisposition, ReviewError]:
    """Idempotent by action ID and exact semantic request; stale writers get 409."""
    try:
        with state.db.begin_immediate():
            return _save_review_in_transaction(state, queue_digest, evaluation_id, request)
    except ReviewConflict as exc:
        return Err(ReviewError(evaluation_id=evaluation_id, detail=str(exc), status=409))
    except GradeValidationError as exc:
        return Err(
            ReviewError(evaluation_id=evaluation_id, detail="; ".join(exc.errors), status=400)
        )
    except (ValueError, KeyError, IndexError, sqlite3.Error) as exc:
        return Err(
            ReviewError(
                evaluation_id=evaluation_id, detail=f"Cannot record review: {exc}", status=500
            )
        )


def _save_review_in_transaction(
    state: StateManager, queue_digest: ArtifactDigest, evaluation_id: int, request: ReviewRequest
) -> Result[ReviewDisposition, ReviewError]:
    def failure(detail: str) -> Err[ReviewError]:
        return Err(ReviewError(evaluation_id=evaluation_id, detail=detail, status=409))

    previous = state.conn.execute(
        "SELECT queue_digest, evaluation_id, request_json, disposition_json "
        "FROM review_dispositions WHERE action_id = ?",
        (request.action_id,),
    ).fetchone()
    if previous is not None:
        if (
            previous["queue_digest"] != queue_digest
            or previous["evaluation_id"] != evaluation_id
            or ReviewRequest.model_validate_json(previous["request_json"]) != request
        ):
            return failure("Action ID already used for a different request")
        return Ok(ReviewDisposition.model_validate_json(previous["disposition_json"]))

    source = review_source(state, queue_digest, evaluation_id)
    if isinstance(source, Err):
        return source
    recorded = source.value.evaluation
    live = state.get_evaluation(evaluation_id)
    if live is None:
        return failure("Evaluation no longer exists")
    post_row = state.conn.execute(
        "SELECT * FROM posts WHERE id = ?", (recorded.post_id,)
    ).fetchone()
    if post_row is None:
        return failure("Post context changed since selection; create a new queue")
    try:
        live_post = RecordedPost.model_validate(dict(post_row))
    except ValidationError:
        return failure(
            "Post context no longer matches the recorded schema; "
            "create a new queue after updating Scout"
        )
    if live_post != source.value.post:
        return failure("Post context changed since selection; create a new queue")
    # Queue grades must describe the same recorded input, not a rescore or a
    # changed project/dossier. Never attach a grade to a sibling evaluation.
    for key in type(recorded).model_fields:
        if live[key] != getattr(recorded, key):
            return failure(f"Evaluation {key} changed since selection")
    revision_id = current_revision(state, evaluation_id)
    if revision_id != request.expected_grade_revision_id:
        return failure("Grade changed; reload and review the latest revision")
    latest = state.conn.execute(
        "SELECT action_id FROM review_dispositions WHERE queue_digest = ? AND evaluation_id = ? "
        "ORDER BY sequence DESC LIMIT 1",
        (queue_digest, evaluation_id),
    ).fetchone()
    if (latest[0] if latest else None) != request.expected_action_id:
        return failure("Queue disposition changed; reload before saving")

    now = datetime.now(UTC)
    if request.action.kind == "grade":
        grade = GradeRecord(
            post_id=recorded.post_id,
            evaluation_id=evaluation_id,
            scan_id=live["scan_id"],
            source="web",
            graded_at=now,
            **request.action.grade.model_dump(),
        )
        state.save_grade(grade)
        revision_id = current_revision(state, evaluation_id)
        if revision_id is None:
            raise ValueError("Grade writer did not retain its revision")
    elif request.action.kind == "reconcile":
        grade_row = state.get_grade_for_evaluation(evaluation_id)
        if (
            revision_id is None
            or grade_row is None
            or grade_row.needs_regrade
            or grade_row.schema_version != HUMAN_GRADE_SCHEMA_VERSION
        ):
            return failure("No complete current grade to reconcile")
        if validate_grade_envelope(grade_envelope_payload(grade_row), live["posture"]):
            return failure("Current grade fails the grading contract")
    else:
        revision_id = None

    disposition = ReviewDisposition(
        action_id=request.action_id,
        queue_digest=queue_digest,
        evaluation_id=evaluation_id,
        action=request.action,
        recorded_at=now.isoformat(),
        timing=request.timing,
        pricing=request.pricing,
        grade_revision_id=revision_id,
    )
    state.conn.execute(
        "INSERT INTO review_dispositions(action_id, queue_digest, evaluation_id, "
        "grade_revision_id, "
        "request_json, disposition_json) VALUES (?, ?, ?, ?, ?, ?)",
        (
            request.action_id,
            queue_digest,
            evaluation_id,
            revision_id,
            json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
            json.dumps(disposition.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
        ),
    )
    return Ok(disposition)
