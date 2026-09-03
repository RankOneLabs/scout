"""Tests for scripts/audit_feedback_policy.py."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scout.config import GradeRecord, Message, RelevanceResult
from scout.grading.feedback import resolve_feedback_policy_config
from scout.storage.state import LATEST_SCHEMA_VERSION, StateManager
from scripts.audit_feedback_policy import (
    AuditError,
    build_manifest,
    open_readonly_connection,
    run_audit,
)


def _ro_uri(db_path: Path) -> str:
    return f"file:{db_path}?mode=ro"


def _seed_grade(
    state: StateManager,
    *,
    scan_id: int,
    platform_id: str,
    graded_at: datetime,
) -> int:
    msg = Message(
        platform="bluesky", platform_id=platform_id, channel_name="",
        channel_id="", author_name="a", author_id="u",
        content="hello", created_at=datetime.now(UTC),
    )
    post_id = state.save_post(msg, scan_id)
    result = RelevanceResult(
        message=msg, relevant=True, score=0.9, reason="r", relevant_to=("proj",),
    )
    eval_id = state.save_evaluation(
        result, post_id, scan_id, project_key="proj", posture="answer",
        surface_status="surfaced",
    )
    state.save_draft(post_id, eval_id, "proj", "draft text", scan_id, posture="answer")
    return state.save_grade(GradeRecord(
        post_id=post_id, evaluation_id=eval_id, scan_id=scan_id, source="cli",
        graded_at=graded_at, relevance_judgment="correct", action_judgment="accept",
        schema_version=3,
    ))


def _build_fixture_db(db_path: Path) -> int:
    """A grade entered recently against an evaluation from an old (now
    stale-relative-to-limit_scans=3) scan — the exact legacy bug scenario:
    a human judgment the legacy scan-scoped query cannot see."""
    state = StateManager(db_path=str(db_path))
    old_scan_id = state.start_scan()
    state.complete_scan(old_scan_id, 1, 1, status="complete")
    for _ in range(5):
        newer_scan_id = state.start_scan()
        state.complete_scan(newer_scan_id, 1, 1, status="complete")

    grade_id = _seed_grade(
        state, scan_id=old_scan_id, platform_id="old-scan-recent-grade",
        graded_at=datetime.now(UTC),
    )
    state.commit()
    state.close()
    return grade_id


def test_open_readonly_connection_rejects_uri_without_mode_ro(tmp_path: Path) -> None:
    db_path = tmp_path / "scout.db"
    _build_fixture_db(db_path)
    with pytest.raises(AuditError, match="mode=ro"):
        open_readonly_connection(str(db_path))


def test_open_readonly_connection_rejects_old_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "scout.db"
    _build_fixture_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version = 20")
    conn.commit()
    conn.close()
    with pytest.raises(AuditError, match=f"upgrade to {LATEST_SCHEMA_VERSION}"):
        open_readonly_connection(_ro_uri(db_path))


def test_reconciliation_finds_the_legacy_gap(tmp_path: Path) -> None:
    """The exact bug this policy fixes: a grade entered recently on an old
    scan is invisible to the legacy scan-scoped query but is correctly
    considered valid by evaluation-feedback/v1."""
    db_path = tmp_path / "scout.db"
    grade_id = _build_fixture_db(db_path)

    config = resolve_feedback_policy_config()
    manifest = run_audit(_ro_uri(db_path), now=datetime.now(UTC), config=config)

    assert manifest["new_considered_valid_count"] == 1
    assert manifest["legacy_prompt_eligible_count"] == 0
    assert manifest["dispositions"]["eligible"] == 1
    assert manifest["population_count"] == 1
    assert grade_id  # sanity: fixture actually produced a grade


def test_legacy_count_matches_scan_scoped_grades(tmp_path: Path) -> None:
    """When a grade is graded on one of the three most recently completed
    scans, both counts agree."""
    db_path = tmp_path / "scout.db"
    state = StateManager(db_path=str(db_path))
    scan_id = state.start_scan()
    _seed_grade(state, scan_id=scan_id, platform_id="p1", graded_at=datetime.now(UTC))
    state.complete_scan(scan_id, 1, 1, status="complete")
    state.commit()
    state.close()

    config = resolve_feedback_policy_config()
    manifest = run_audit(_ro_uri(db_path), now=datetime.now(UTC), config=config)
    assert manifest["new_considered_valid_count"] == 1
    assert manifest["legacy_prompt_eligible_count"] == 1


def test_manifest_digest_is_deterministic_for_same_inputs(tmp_path: Path) -> None:
    db_path = tmp_path / "scout.db"
    _build_fixture_db(db_path)
    config = resolve_feedback_policy_config()
    now = datetime(2026, 1, 1, tzinfo=UTC)

    conn_a = open_readonly_connection(_ro_uri(db_path))
    manifest_a = build_manifest(conn_a, db_target="a", now=now, config=config)
    conn_a.close()

    conn_b = open_readonly_connection(_ro_uri(db_path))
    manifest_b = build_manifest(conn_b, db_target="a", now=now, config=config)
    conn_b.close()

    assert manifest_a["digest"] == manifest_b["digest"]


def test_reads_run_inside_one_pinned_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: build_manifest issues two separate read statements
    (the population load and the legacy count) against an autocommit
    connection. Both must run inside one explicit transaction so they see
    the same snapshot, not two independently-committed reads that a
    concurrent writer could observe differently."""
    import scripts.audit_feedback_policy as audit_module

    db_path = tmp_path / "scout.db"
    _build_fixture_db(db_path)
    config = resolve_feedback_policy_config()

    real_load = audit_module.load_grade_population
    observed_in_transaction = []

    def spy_load(conn: sqlite3.Connection, *, as_of: datetime, config: object) -> object:
        observed_in_transaction.append(conn.in_transaction)
        return real_load(conn, as_of=as_of, config=config)  # type: ignore[arg-type]

    monkeypatch.setattr(audit_module, "load_grade_population", spy_load)

    conn = open_readonly_connection(_ro_uri(db_path))
    assert not conn.in_transaction
    build_manifest(conn, db_target="x", now=datetime.now(UTC), config=config)
    assert not conn.in_transaction, "transaction must be closed after build_manifest returns"
    assert observed_in_transaction == [True], (
        "load_grade_population must run inside an already-open transaction"
    )
    conn.close()


def test_readonly_connection_cannot_write(tmp_path: Path) -> None:
    db_path = tmp_path / "scout.db"
    _build_fixture_db(db_path)
    conn = open_readonly_connection(_ro_uri(db_path))
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM grades")
    conn.close()
