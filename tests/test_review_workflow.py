from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from scout.cli import grading_api
from scout.config import GradeRecord
from scout.grading.artifacts import ArtifactDigest
from scout.grading.assistance_store import build_assistance_bundle
from scout.grading.assistance_types import AssistanceOutputs
from scout.grading.corpus_export import export_grading_corpus
from scout.grading.review_costs import read_review_costs
from scout.grading.review_store import current_revision, save_review
from scout.grading.review_types import ReviewRequest
from scout.result import Err, Ok
from tests.test_grading_assistance import request_data, runtime, state  # noqa: F401


@pytest.fixture
def queue(state, request_data, runtime):  # noqa: F811
    result = build_assistance_bundle(request_data, runtime)
    assert isinstance(result, Ok), result
    assert state.artifacts.import_bundle(result.value.bundle) == Ok(None)
    return ReviewFixture(result.value.lineage.outputs[2], result.value.outputs)


@dataclass(frozen=True)
class ReviewFixture:
    queue_digest: ArtifactDigest
    outputs: AssistanceOutputs


def action(**changes):
    body = {
        "action_id": str(uuid4()),
        "expected_grade_revision_id": None,
        "expected_action_id": None,
        "action": {
            "kind": "grade",
            "grade": {
                "relevance_judgment": "correct",
                "action_judgment": "accept",
            },
        },
        "timing": {"elapsed_ms": 1250, "method": "active-visible-idle60/v1"},
        "pricing": {"usd_per_hour": 36.0, "basis": "synthetic rate"},
    }
    body.update(changes)
    return ReviewRequest.model_validate_json(json.dumps(body))


def selected(queue):
    return queue.outputs.queue.items[0].sources[0].evaluation_id


def test_grade_disposition_pins_revision_and_retry_never_regrades(state, queue):  # noqa: F811
    request = action()
    evaluation_id = selected(queue)
    first = save_review(state, queue.queue_digest, evaluation_id, request)
    assert isinstance(first, Ok), first
    assert first.value.grade_revision_id == current_revision(state, evaluation_id)
    assert save_review(state, queue.queue_digest, evaluation_id, request) == first
    assert state.conn.execute("SELECT count(*) FROM review_dispositions").fetchone()[0] == 1
    assert state.conn.execute("SELECT count(*) FROM human_positive_promotions").fetchone()[0] == 0


def test_false_negative_saves_without_promotion(state, queue):  # noqa: F811
    result = save_review(
        state,
        queue.queue_digest,
        selected(queue),
        action(
            action={
                "kind": "grade",
                "grade": {
                    "relevance_judgment": "false_negative",
                    "action_judgment": "fail",
                    "dimensions": ["usefulness"],
                    "failure_note": "Synthetic missed relevance",
                },
            }
        ),
    )
    assert isinstance(result, Ok), result
    assert state.conn.execute("SELECT count(*) FROM human_positive_promotions").fetchone()[0] == 0


def test_invalid_grade_rolls_back_disposition_and_grade(state, queue):  # noqa: F811
    evaluation_id = selected(queue)
    result = save_review(
        state,
        queue.queue_digest,
        evaluation_id,
        action(
            action={
                "kind": "grade",
                "grade": {"relevance_judgment": "false_negative", "action_judgment": "accept"},
            }
        ),
    )
    assert isinstance(result, Err), result
    assert current_revision(state, evaluation_id) is None
    assert state.conn.execute("SELECT count(*) FROM review_dispositions").fetchone()[0] == 0


def test_failed_disposition_insert_rolls_back_successful_grade(state, queue):  # noqa: F811
    state.conn.execute(
        "CREATE TRIGGER simulate_failure BEFORE INSERT ON review_dispositions "
        "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
    )
    result = save_review(state, queue.queue_digest, selected(queue), action())
    assert isinstance(result, Err)
    assert current_revision(state, selected(queue)) is None


def test_only_exact_selected_evaluation_can_be_graded(state, queue):  # noqa: F811
    result = save_review(state, queue.queue_digest, 1, action())
    assert isinstance(result, Err) and result.error.status == 404


def test_skip_has_time_and_reason_but_no_grade(state, queue):  # noqa: F811
    result = save_review(
        state,
        queue.queue_digest,
        selected(queue),
        action(action={"kind": "skip", "reason": "Need domain context"}),
    )
    assert isinstance(result, Ok), result
    assert result.value.grade_revision_id is None
    assert result.value.timing.elapsed_ms == 1250
    assert current_revision(state, selected(queue)) is None


def test_skip_concurrent_save_requires_latest_action(state, queue):  # noqa: F811
    first = save_review(
        state,
        queue.queue_digest,
        selected(queue),
        action(action={"kind": "skip", "reason": "Unsure"}),
    )
    assert isinstance(first, Ok)
    stale = save_review(state, queue.queue_digest, selected(queue), action())
    assert isinstance(stale, Err) and stale.error.status == 409
    next_action = action(expected_action_id=first.value.action_id)
    assert isinstance(save_review(state, queue.queue_digest, selected(queue), next_action), Ok)


def test_external_grade_conflict_then_reconcile_without_time(state, queue):  # noqa: F811
    evaluation_id = selected(queue)
    state.save_grade(
        GradeRecord(
            post_id=evaluation_id,
            evaluation_id=evaluation_id,
            scan_id=None,
            source="cli",
            graded_at=datetime.now(UTC),
            relevance_judgment="correct",
            action_judgment="accept",
        )
    )
    stale = save_review(state, queue.queue_digest, evaluation_id, action())
    assert isinstance(stale, Err) and stale.error.status == 409
    request = action(
        expected_grade_revision_id=current_revision(state, evaluation_id),
        action={"kind": "reconcile"},
        timing={"elapsed_ms": None, "method": None},
        pricing=None,
    )
    reconciled = save_review(state, queue.queue_digest, evaluation_id, request)
    assert isinstance(reconciled, Ok), reconciled
    assert reconciled.value.timing.elapsed_ms is None


def test_action_id_reuse_with_changed_timing_is_conflict(state, queue):  # noqa: F811
    request = action()
    assert isinstance(save_review(state, queue.queue_digest, selected(queue), request), Ok)
    changed = action(
        action_id=request.action_id,
        timing={"elapsed_ms": 2000, "method": "active-visible-idle60/v1"},
    )
    result = save_review(state, queue.queue_digest, selected(queue), changed)
    assert isinstance(result, Err) and result.error.status == 409


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM review_dispositions",
        "UPDATE review_dispositions SET evaluation_id = 1",
        "INSERT OR REPLACE INTO review_dispositions SELECT * FROM review_dispositions",
    ],
)
def test_dispositions_are_immutable(state, queue, sql):  # noqa: F811
    assert isinstance(save_review(state, queue.queue_digest, selected(queue), action()), Ok)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        state.conn.execute(sql)
    state.conn.rollback()


@pytest.mark.parametrize(
    "timing",
    [
        {"elapsed_ms": -1, "method": "active-visible-idle60/v1"},
        {"elapsed_ms": None, "method": "active-visible-idle60/v1"},
        {"elapsed_ms": True, "method": "active-visible-idle60/v1"},
    ],
)
def test_invalid_timing_rejected(timing):
    with pytest.raises(ValueError):
        action(timing=timing)


def test_sidecar_saves_exact_queue_action_and_retries(state, queue, monkeypatch):  # noqa: F811
    monkeypatch.setattr(grading_api, "DB_PATH", state.db_path)
    path = f"/review-queues/{queue.queue_digest}/evaluations/{selected(queue)}/actions"
    with TestClient(grading_api.app, headers={"Host": "localhost"}) as client:
        request = action().model_dump(mode="json")
        first = client.post(path, json=request)
        assert first.status_code == 200, first.text
        assert client.post(path, json=request).json() == first.json()


def test_review_costs_project_distinct_operating_components(state, queue):  # noqa: F811
    request = action()
    assert isinstance(save_review(state, queue.queue_digest, selected(queue), request), Ok)
    assert isinstance(save_review(state, queue.queue_digest, selected(queue), request), Ok)
    report = read_review_costs(state.conn, queue.queue_digest)
    assert isinstance(report, Ok), report
    assert len(report.value.lines) == 1
    line = report.value.lines[0]
    assert line.component == {
        "kind": "corpus_building_human_review",
        "quantity": 1250,
        "unit": "ms",
        "price": {"currency": "USD", "amount": 0.0125, "basis": "synthetic rate"},
    }
    assert request.action_id in line.source_references[0]


def test_corpus_export_preserves_review_actions_and_pinned_revisions(state, queue, tmp_path):  # noqa: F811
    request = action()
    saved = save_review(state, queue.queue_digest, selected(queue), request)
    assert isinstance(saved, Ok)
    destination = tmp_path / "preservation.db"
    export_grading_corpus(state.db_path, destination)
    with sqlite3.connect(destination) as restored:
        row = restored.execute("SELECT disposition_json FROM review_dispositions").fetchone()
        assert json.loads(row[0]) == saved.value.model_dump(mode="json")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            restored.execute("DELETE FROM review_dispositions")


def test_source_change_requires_new_queue(state, queue):  # noqa: F811
    evaluation_id = selected(queue)
    with state.db.transaction():
        state.conn.execute("UPDATE posts SET content = 'changed' WHERE id = ?", (evaluation_id,))
    result = save_review(state, queue.queue_digest, evaluation_id, action())
    assert isinstance(result, Err) and result.error.status == 409
