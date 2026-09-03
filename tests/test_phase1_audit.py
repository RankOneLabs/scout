from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

import scout.paa.audit.data as phase1_audit_data
import scout.paa.audit.replay as phase1_audit_replay
import scout.paa.audit.runner as phase1_audit
from scout.paa.audit.runner import (
    canonical_json,
    parse_utc,
    record_parent_change,
    render_markdown,
    run_audit,
)
from scout.scanning.schemas import QuestionSegment, StructuredDraftOutput
from scout.storage.state import StateManager, format_graded_at
from tests.conftest import resolve_dossier_from_disk


def _init_git_repo(path: Path) -> None:
    """Initialize a git repo and commit all current files.

    Author/committer identity and dates are fixed rather than left to
    wall-clock time, so the resulting commit SHA — used as a pinned
    dossier_revision throughout these tests — is reproducible across runs
    and machines instead of changing on every invocation.
    """
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    for key, value in (
        ("user.email", "test@test.com"),
        ("user.name", "Test"),
        ("commit.gpgsign", "false"),
    ):
        subprocess.run(["git", "config", key, value], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    pinned_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@test.com",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test.com",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    }
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=path,
        check=True,
        capture_output=True,
        env=pinned_env,
    )


def test_strict_audit_rejects_short_window(tmp_path: Path) -> None:
    db = tmp_path / "scout.db"
    with StateManager(str(db)):
        pass
    end = datetime.now(UTC)
    with pytest.raises(ValueError, match="at least 14 days"):
        run_audit(
            db,
            tmp_path,
            window_from=end - timedelta(days=13),
            window_to=end,
            strict=True,
        )


def test_parse_utc_requires_offset() -> None:
    with pytest.raises(ValueError, match="UTC offset"):
        parse_utc("2026-07-01T00:00:00")


def test_audit_is_read_only_and_reports_unmet_criteria(tmp_path: Path) -> None:
    db = tmp_path / "scout.db"
    with StateManager(str(db)):
        pass
    before = db.read_bytes()
    end = datetime.now(UTC)
    result = run_audit(
        db,
        tmp_path,
        window_from=end - timedelta(days=14),
        window_to=end,
        strict=True,
    )
    assert not result.passed
    assert not result.report["criteria"]["production_live_scans_present"]["passed"]
    assert result.report["schema_version"] == 4
    assert db.read_bytes() == before


def _tree_fingerprint(root: Path, *, exclude_dirs: frozenset[str] = frozenset({".git"})) -> str:
    """Deterministic digest of every file's relative path and content under
    root, skipping directories in exclude_dirs — git's own bookkeeping is
    not part of the tree the audit contract promises never to modify."""
    digest = hashlib.sha256()
    paths = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and not set(path.relative_to(root).parts) & exclude_dirs
    )
    for path in paths:
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_audit_does_not_modify_dossier_or_corpus_trees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_audit must never write to the dossier checkout it resolves
    against or the eval corpus it lints — both are evidence the audit
    reads, never owns."""
    monkeypatch.setattr(phase1_audit_replay, "resolve_dossier", resolve_dossier_from_disk)

    fixture_root = Path(__file__).parent / "fixtures" / "dossier_source"
    content_repo = tmp_path / "dossier-source"
    shutil.copytree(fixture_root, content_repo)
    _init_git_repo(content_repo)
    revision = subprocess.run(
        ["git", "-C", str(content_repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    project_key = "gateway"
    summary_id = "gateway-dossier"
    db = tmp_path / "scout.db"
    now = datetime.now(UTC)
    with StateManager(str(db)) as state:
        timestamp = now.isoformat()
        state.conn.execute(
            "INSERT INTO projects "
            "(key, name, description, link, active, created_at, updated_at) "
            "VALUES (?, 'Gateway', 'Gateway project', 'https://example.com/gateway', "
            "1, ?, ?)",
            (project_key, timestamp, timestamp),
        )
        scan_id = state.start_scan(environment="production", run_kind="live")
        post_cursor = state.conn.execute(
            "INSERT INTO posts "
            "(platform, platform_msg_id, author_id, content, created_at, scan_id) "
            "VALUES ('discord', 'tree-immutability', 'author-1', 'hello', ?, ?)",
            (timestamp, scan_id),
        )
        state.conn.execute(
            "INSERT INTO evaluations "
            "(post_id, relevant, score, reason, relevant_to, scan_id, created_at, "
            "project_key, surface_status, dossier_summary_id, dossier_revision) "
            "VALUES (?, 1, 1.0, 'relevant', '[]', ?, ?, ?, 'not_relevant', ?, ?)",
            (post_cursor.lastrowid, scan_id, timestamp, project_key, summary_id, revision),
        )
        state.commit()

    corpus_dir = Path(__file__).parent.parent / "src" / "scout" / "evals" / "phase1"
    dossier_before = _tree_fingerprint(content_repo)
    corpus_before = _tree_fingerprint(corpus_dir)

    run_audit(
        db,
        content_repo,
        window_from=now - timedelta(days=14),
        window_to=now + timedelta(minutes=1),
    )

    assert _tree_fingerprint(content_repo) == dossier_before
    assert _tree_fingerprint(corpus_dir) == corpus_before


def test_load_snapshot_observes_one_consistent_database_state_despite_concurrent_writes(
    tmp_path: Path,
) -> None:
    """load_snapshot's queries all run inside open_read_only_connection's
    one explicit deferred transaction, so a writer that commits new rows
    after the snapshot's read transaction has started must not appear in
    it — proving the snapshot is one consistent view, not a sequence of
    independently-visible autocommit reads."""
    db = tmp_path / "scout.db"
    with StateManager(str(db)) as state:
        state.start_scan(environment="production", run_kind="live")
        state.commit()
    now = datetime.now(UTC)

    with phase1_audit_data.open_read_only_connection(db) as conn:
        # Pin the read transaction's snapshot before the concurrent write.
        pinned_scans = phase1_audit_data._rows(conn, "SELECT * FROM scans")
        assert len(pinned_scans) == 1

        with StateManager(str(db)) as writer:
            writer.start_scan(environment="production", run_kind="live")
            writer.commit()

        snapshot = phase1_audit_data.load_snapshot(
            conn, now - timedelta(days=14), now + timedelta(minutes=1)
        )

    assert len(snapshot.scans) == 1
    assert {s["id"] for s in snapshot.scans} == {s["id"] for s in pinned_scans}

    with closing(sqlite3.connect(db)) as verify_conn:
        total_scans = verify_conn.execute("SELECT count(*) FROM scans").fetchone()[0]
    assert total_scans == 2


def test_audit_uses_active_registry_projects_instead_of_fixed_project_set(
    tmp_path: Path,
) -> None:
    db = tmp_path / "scout.db"
    now = datetime.now(UTC)
    with StateManager(str(db)) as state:
        timestamp = now.isoformat()
        state.conn.execute(
            "INSERT INTO projects "
            "(key, name, description, link, active, created_at, updated_at) "
            "VALUES ('agent-ops', 'Agent Ops', 'Operations', 'https://example.com/ops', "
            "1, ?, ?)",
            (timestamp, timestamp),
        )
        state.conn.execute(
            "INSERT INTO projects "
            "(key, name, description, link, active, created_at, updated_at) "
            "VALUES ('gateway', 'Gateway', 'Legacy', 'https://example.com/gateway', "
            "0, ?, ?)",
            (timestamp, timestamp),
        )
        scan_id = state.start_scan(environment="production", run_kind="live")
        post_cursor = state.conn.execute(
            "INSERT INTO posts "
            "(platform, platform_msg_id, author_id, content, created_at, scan_id) "
            "VALUES ('discord', 'registry-audit', 'author', 'message', ?, ?)",
            (timestamp, scan_id),
        )
        assert post_cursor.lastrowid is not None
        state.conn.execute(
            "INSERT INTO evaluations "
            "(post_id, relevant, score, reason, relevant_to, scan_id, created_at, "
            "project_key, surface_status) "
            "VALUES (?, 0, 0.0, 'not relevant', '[]', ?, ?, 'agent-ops', 'not_relevant')",
            (post_cursor.lastrowid, scan_id, timestamp),
        )
        state.commit()

    result = run_audit(
        db,
        tmp_path,
        window_from=now - timedelta(days=14),
        window_to=now + timedelta(minutes=1),
    )

    assert result.report["registry"]["active_projects"] == ["agent-ops"]
    criterion = result.report["criteria"]["active_project_set_exact"]
    assert criterion["passed"] is True
    assert criterion["findings"] == []


def test_audit_proves_dossier_readiness_approval_and_replay_through_real_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A production/live evaluation with a surfaced, approved draft pinned to
    the upstream export (tests/fixtures/dossier_source) must prove
    historical_dossier_readiness, external_approval_exact_revision, and
    historical_content_replay all pass with nonzero denominators — traversing
    the same database joins, immutable git reads, dossier resolution,
    approval matching, and replay verifier production uses.

    resolve_dossier now retrieves the pinned dossier-source schema via git show;
    tests/fixtures/dossier_source deliberately carries no schema copy (see its
    README), so dossier resolution here is routed through
    resolve_dossier_from_disk, which applies only Scout's own identity/
    readiness/cross-reference layer (dossier._build_resolution). Full
    schema+semantic conformance is proven separately, against the real
    pinned dossier-source checkout, by tests/test_dossier_conformance.py."""
    monkeypatch.setattr(phase1_audit_replay, "resolve_dossier", resolve_dossier_from_disk)
    fixture_root = Path(__file__).parent / "fixtures" / "dossier_source"
    content_repo = tmp_path / "dossier-source"
    shutil.copytree(fixture_root, content_repo)
    _init_git_repo(content_repo)
    revision = subprocess.run(
        ["git", "-C", str(content_repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    index = yaml.safe_load((content_repo / "index.yaml").read_text())
    for entry in index["entries"].values():
        assert set(entry) == {"path", "type"}

    project_key = "gateway"
    summary_id = "gateway-dossier"
    question_text = "Which authentication flow does your team already use?"
    structured = StructuredDraftOutput(
        posture="ask",
        segments=[QuestionSegment(type="question", text=question_text)],
        claims=[],
        resources_used=[],
    )

    db = tmp_path / "scout.db"
    now = datetime.now(UTC)
    with StateManager(str(db)) as state:
        timestamp = now.isoformat()
        state.conn.execute(
            "INSERT INTO projects "
            "(key, name, description, link, active, created_at, updated_at) "
            "VALUES (?, 'Gateway', 'Gateway project', 'https://example.com/gateway', "
            "1, ?, ?)",
            (project_key, timestamp, timestamp),
        )
        scan_id = state.start_scan(environment="production", run_kind="live")
        post_cursor = state.conn.execute(
            "INSERT INTO posts "
            "(platform, platform_msg_id, author_id, content, created_at, scan_id) "
            "VALUES ('discord', 'audit-success-post', 'author-1', "
            "'What auth flow does your gateway use?', ?, ?)",
            (timestamp, scan_id),
        )
        post_id = post_cursor.lastrowid
        eval_cursor = state.conn.execute(
            "INSERT INTO evaluations "
            "(post_id, relevant, score, reason, relevant_to, scan_id, created_at, "
            "project_key, posture, surface_status, dossier_summary_id, dossier_revision) "
            "VALUES (?, 1, 1.0, 'relevant', '[]', ?, ?, ?, 'ask', 'surfaced', ?, ?)",
            (post_id, scan_id, timestamp, project_key, summary_id, revision),
        )
        evaluation_id = eval_cursor.lastrowid
        draft_cursor = state.conn.execute(
            "INSERT INTO draft_comments "
            "(post_id, evaluation_id, project_key, comment_text, created_at, scan_id, "
            "posture, structured_output, dossier_summary_id, dossier_revision) "
            "VALUES (?, ?, ?, ?, ?, ?, 'ask', ?, ?, ?)",
            (
                post_id,
                evaluation_id,
                project_key,
                question_text,
                timestamp,
                scan_id,
                structured.model_dump_json(),
                summary_id,
                revision,
            ),
        )
        draft_id = draft_cursor.lastrowid
        state.conn.execute(
            "INSERT INTO surfaced_events "
            "(platform, author_id, surfaced_at, post_id, evaluation_id, draft_id, "
            "project_key, created_at) "
            "VALUES ('discord', 'author-1', ?, ?, ?, ?, ?, ?)",
            (timestamp, post_id, evaluation_id, draft_id, project_key, timestamp),
        )
        state.conn.execute(
            "INSERT INTO grades "
            "(evaluation_id, post_id, scan_id, source, graded_at, relevance_judgment, "
            "schema_version, needs_regrade, action_judgment) "
            "VALUES (?, ?, ?, 'cli', ?, 'correct', 2, 0, 'accept')",
            (evaluation_id, post_id, scan_id, format_graded_at(now)),
        )
        state.commit()

    result = run_audit(
        db,
        content_repo,
        window_from=now - timedelta(days=14),
        window_to=now + timedelta(minutes=1),
        approval_references={
            project_key: {
                "revision": revision,
                "reference": "https://example.com/approvals/gateway-001",
            }
        },
    )

    readiness = result.report["criteria"]["historical_dossier_readiness"]
    approval = result.report["criteria"]["external_approval_exact_revision"]
    replay = result.report["criteria"]["historical_content_replay"]

    assert readiness["passed"] is True
    assert readiness["denominator"] > 0
    assert readiness["findings"] == []
    assert approval["passed"] is True
    assert approval["denominator"] > 0
    assert approval["findings"] == []
    assert replay["passed"] is True
    assert replay["denominator"] > 0
    assert replay["findings"] == []

    dossier_row = result.report["dossiers"][0]
    assert dossier_row["summary_id"] == summary_id
    assert dossier_row["path"] == "summaries/gateway-dossier.yaml"
    assert dossier_row["gap_count"] > 0

    replay_row = result.report["historical_replay"][0]
    assert replay_row["project_key"] == project_key


def test_parent_change_requires_resolved_graded_bluesky(tmp_path: Path) -> None:
    db = tmp_path / "scout.db"
    with StateManager(str(db)) as state:
        state.conn.execute(
            "INSERT INTO posts(platform, platform_msg_id, content, parent_lookup_status) "
            "VALUES ('discord', 'm1', 'hello', 'not_applicable')"
        )
        state.conn.execute(
            "INSERT INTO evaluations(post_id, relevant, score, created_at, posture, "
            "surface_status) "
            "VALUES (1, 1, 1.0, ?, 'answer', 'not_relevant')",
            (datetime.now(UTC).isoformat(),),
        )
        state.commit()
    with pytest.raises(ValueError, match="resolved parent"):
        record_parent_change(
            db,
            evaluation_id=1,
            without_parent_relevance="relevant",
            without_parent_posture="ask",
            assessor="operator",
            explanation="Parent supplies the missing referent.",
        )


def test_parent_change_upserts_attributable_annotation(tmp_path: Path) -> None:
    db = tmp_path / "scout.db"
    with StateManager(str(db)) as state:
        state.conn.execute(
            "INSERT INTO posts(platform, platform_msg_id, content, "
            "parent_lookup_status, parent_id) "
            "VALUES ('bluesky', 'm1', 'hello', 'resolved', 'parent')"
        )
        state.conn.execute(
            "INSERT INTO evaluations(post_id, relevant, score, created_at, posture, "
            "surface_status) "
            "VALUES (1, 1, 1.0, ?, 'answer', 'not_relevant')",
            (datetime.now(UTC).isoformat(),),
        )
        state.conn.execute(
            """INSERT INTO grades(evaluation_id, post_id, source, graded_at,
                   relevance_judgment, schema_version, needs_regrade, action_judgment)
               VALUES (1, 1, 'cli', ?, 'correct', 2, 0, 'accept')""",
            (format_graded_at(datetime.now(UTC)),),
        )
        state.commit()
    record_parent_change(
        db,
        evaluation_id=1,
        without_parent_relevance="not_relevant",
        without_parent_posture="abstain",
        assessor="operator",
        explanation="Without the parent this is ambiguous.",
    )
    record_parent_change(
        db,
        evaluation_id=1,
        without_parent_relevance="relevant",
        without_parent_posture="ask",
        assessor="reviewer",
        explanation="Second review.",
    )
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT count(*) FROM parent_context_assessments").fetchone()[0] == 1
        assert (
            conn.execute("SELECT assessor FROM parent_context_assessments").fetchone()[0]
            == "reviewer"
        )


# --- Representative Phase 1 audit: committed canonical-report fixture ---
#
# REPRESENTATIVE_WINDOW_* and every timestamp seeded below are fixed
# wall-clock-independent constants (never datetime.now()) so the audit
# report captured here is byte-for-byte reproducible across runs, machines,
# and time. SCOUT_DOSSIER_MAX_AGE_DAYS is patched out for the same reason:
# dossier freshness is otherwise checked against date.today(), which would
# make the fixture's dossier-readiness criteria flip as real time passes.

REPRESENTATIVE_WINDOW_FROM = datetime(2026, 1, 5, tzinfo=UTC)
REPRESENTATIVE_WINDOW_TO = datetime(2026, 1, 20, tzinfo=UTC)


def _build_representative_audit_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> phase1_audit.AuditResult:
    """Seed a database and dossier checkout that exercises every Phase 1
    audit criterion with real, non-trivial evidence: a production/live scan
    alongside an excluded one, a surfaced/approved/replayed gateway draft,
    an attributable deterministic gate block, and a graded Bluesky reply
    whose parent context changes the outcome. The real evals/phase1 corpus
    pins dossier revisions from the actual dossier-source project history that
    this synthetic checkout deliberately does not carry, so corpus linting
    is expected to fail deterministically.

    Used both to capture the committed canonical-report fixtures and to
    prove this behavior survives the module decomposition.
    """
    monkeypatch.setattr(phase1_audit_replay, "resolve_dossier", resolve_dossier_from_disk)
    monkeypatch.setattr(phase1_audit_replay, "SCOUT_DOSSIER_MAX_AGE_DAYS", None)

    fixture_root = Path(__file__).parent / "fixtures" / "dossier_source"
    content_repo = tmp_path / "dossier-source"
    shutil.copytree(fixture_root, content_repo)
    _init_git_repo(content_repo)
    revision = subprocess.run(
        ["git", "-C", str(content_repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    project_key = "gateway"
    summary_id = "gateway-dossier"
    question_text = "Which authentication flow does your team already use?"
    structured = StructuredDraftOutput(
        posture="ask",
        segments=[QuestionSegment(type="question", text=question_text)],
        claims=[],
        resources_used=[],
    )

    db = tmp_path / "scout.db"
    with StateManager(str(db)) as state:
        state.conn.execute(
            "INSERT INTO projects "
            "(key, name, description, link, active, created_at, updated_at) "
            "VALUES (?, 'Gateway', 'Gateway project', 'https://example.com/gateway', "
            "1, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (project_key,),
        )
        scan_q = state.conn.execute(
            "INSERT INTO scans (started_at, environment, run_kind) "
            "VALUES ('2026-01-10T12:00:00+00:00', 'production', 'live')"
        ).lastrowid
        state.conn.execute(
            "INSERT INTO scans (started_at, environment, run_kind) "
            "VALUES ('2026-01-11T09:00:00+00:00', 'development', 'live')"
        )

        # --- Eval A: surfaced, approved, and replayed gateway draft ---
        post_a = state.conn.execute(
            "INSERT INTO posts "
            "(platform, platform_msg_id, author_id, content, created_at, scan_id) "
            "VALUES ('discord', 'audit-a', 'author-1', 'What auth flow does your gateway use?', "
            "'2026-01-10T12:05:00+00:00', ?)",
            (scan_q,),
        ).lastrowid
        eval_a = state.conn.execute(
            "INSERT INTO evaluations "
            "(post_id, relevant, score, reason, relevant_to, scan_id, created_at, "
            "project_key, posture, surface_status, dossier_summary_id, dossier_revision) "
            "VALUES (?, 1, 1.0, 'relevant', '[]', ?, '2026-01-10T12:05:00+00:00', ?, 'ask', "
            "'surfaced', ?, ?)",
            (post_a, scan_q, project_key, summary_id, revision),
        ).lastrowid
        draft_a = state.conn.execute(
            "INSERT INTO draft_comments "
            "(post_id, evaluation_id, project_key, comment_text, created_at, scan_id, "
            "posture, structured_output, dossier_summary_id, dossier_revision) "
            "VALUES (?, ?, ?, ?, '2026-01-10T12:05:00+00:00', ?, 'ask', ?, ?, ?)",
            (
                post_a,
                eval_a,
                project_key,
                question_text,
                scan_q,
                structured.model_dump_json(),
                summary_id,
                revision,
            ),
        ).lastrowid
        state.conn.execute(
            "INSERT INTO surfaced_events "
            "(platform, author_id, surfaced_at, post_id, evaluation_id, draft_id, "
            "project_key, created_at) "
            "VALUES ('discord', 'author-1', '2026-01-10T12:06:00+00:00', ?, ?, ?, ?, "
            "'2026-01-10T12:06:00+00:00')",
            (post_a, eval_a, draft_a, project_key),
        )
        state.conn.execute(
            "INSERT INTO grades "
            "(evaluation_id, post_id, scan_id, source, graded_at, relevance_judgment, "
            "schema_version, needs_regrade, action_judgment) "
            "VALUES (?, ?, ?, 'cli', ?, 'correct', 2, 0, 'accept')",
            (eval_a, post_a, scan_q, format_graded_at(datetime(2026, 1, 10, 12, 10, tzinfo=UTC))),
        )

        # --- Eval B: attributable deterministic gate block ---
        post_b = state.conn.execute(
            "INSERT INTO posts "
            "(platform, platform_msg_id, author_id, content, created_at, scan_id) "
            "VALUES ('discord', 'audit-b', 'author-2', 'Does the gateway ship yet?', "
            "'2026-01-10T13:00:00+00:00', ?)",
            (scan_q,),
        ).lastrowid
        eval_b = state.conn.execute(
            "INSERT INTO evaluations "
            "(post_id, relevant, score, reason, relevant_to, scan_id, created_at, "
            "project_key, posture, surface_status, dossier_summary_id, dossier_revision) "
            "VALUES (?, 1, 1.0, 'relevant', '[]', ?, '2026-01-10T13:00:00+00:00', ?, 'answer', "
            "'gate_blocked', ?, ?)",
            (post_b, scan_q, project_key, summary_id, revision),
        ).lastrowid
        state.conn.execute(
            "INSERT INTO gate_blocks "
            "(reason_code, offending_text, project_key, dossier_summary_id, dossier_revision, "
            "scan_id, post_id, evaluation_id, created_at) "
            "VALUES ('fact_ids', 'fact-gateway-is-shipped', ?, ?, ?, ?, ?, ?, "
            "'2026-01-10T13:00:00+00:00')",
            (project_key, summary_id, revision, scan_q, post_b, eval_b),
        )
        state.conn.execute(
            "INSERT INTO grades "
            "(evaluation_id, post_id, scan_id, source, graded_at, relevance_judgment, "
            "schema_version, needs_regrade, action_judgment) "
            "VALUES (?, ?, ?, 'cli', ?, 'correct', 2, 0, 'accept')",
            (eval_b, post_b, scan_q, format_graded_at(datetime(2026, 1, 10, 13, 5, tzinfo=UTC))),
        )

        # --- Eval C: graded Bluesky reply whose parent context changes the outcome ---
        post_c = state.conn.execute(
            "INSERT INTO posts "
            "(platform, platform_msg_id, author_id, content, created_at, scan_id, "
            "parent_lookup_status, parent_id) "
            "VALUES ('bluesky', 'audit-c', 'author-3', 'Yeah I guess.', "
            "'2026-01-10T14:00:00+00:00', ?, 'resolved', 'parent-c-1')",
            (scan_q,),
        ).lastrowid
        eval_c = state.conn.execute(
            "INSERT INTO evaluations "
            "(post_id, relevant, score, reason, relevant_to, scan_id, created_at, "
            "project_key, posture, surface_status, dossier_summary_id, dossier_revision) "
            "VALUES (?, 1, 1.0, 'relevant', '[]', ?, '2026-01-10T14:00:00+00:00', ?, 'abstain', "
            "'not_relevant', ?, ?)",
            (post_c, scan_q, project_key, summary_id, revision),
        ).lastrowid
        state.conn.execute(
            "INSERT INTO grades "
            "(evaluation_id, post_id, scan_id, source, graded_at, relevance_judgment, "
            "schema_version, needs_regrade, action_judgment) "
            "VALUES (?, ?, ?, 'cli', ?, 'correct', 2, 0, 'accept')",
            (eval_c, post_c, scan_q, format_graded_at(datetime(2026, 1, 10, 14, 5, tzinfo=UTC))),
        )
        state.conn.execute(
            "INSERT INTO parent_context_assessments "
            "(evaluation_id, assessor, assessed_at, without_parent_relevance, "
            "without_parent_posture, explanation) "
            "VALUES (?, 'operator', '2026-01-10T14:10:00+00:00', 'not_relevant', 'ask', "
            "'Without the parent this reads differently.')",
            (eval_c,),
        )

        state.commit()

    return run_audit(
        db,
        content_repo,
        window_from=REPRESENTATIVE_WINDOW_FROM,
        window_to=REPRESENTATIVE_WINDOW_TO,
        strict=True,
        approval_references={
            project_key: {
                "revision": revision,
                "reference": "https://example.com/approvals/gateway-001",
            }
        },
    )


def test_audit_renders_representative_report_deterministically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _build_representative_audit_result(tmp_path / "first", monkeypatch)
    repeated = _build_representative_audit_result(tmp_path / "second", monkeypatch)

    json_output = canonical_json(result.report)
    markdown_output = render_markdown(result.report)
    assert json_output == canonical_json(repeated.report)
    assert markdown_output == render_markdown(repeated.report)
    assert '"criteria"' in json_output
    assert "# Scout Phase 1 exit evidence" in markdown_output

    criteria = result.report["criteria"]
    assert criteria["window_at_least_14_days"]["passed"] is True
    assert criteria["production_live_scans_present"]["passed"] is True
    assert criteria["active_project_set_exact"]["passed"] is True
    assert criteria["complete_schema_v2_grades"]["passed"] is True
    assert criteria["complete_schema_v2_grades"]["denominator"] == 3
    assert criteria["attributable_deterministic_gate_block"]["passed"] is True
    assert criteria["graded_bluesky_parent_context_change"]["passed"] is True
    assert criteria["surfaced_pairing_and_rate"]["passed"] is True
    assert criteria["historical_dossier_readiness"]["passed"] is True
    assert criteria["external_approval_exact_revision"]["passed"] is True
    assert criteria["historical_content_replay"]["passed"] is True
    # This synthetic checkout never carries the real dossier-source history the
    # corpus's cases pin, so every case's dossier resolution fails
    # deterministically — proving the audit surfaces real lint failures
    # instead of silently treating them as ok.
    assert criteria["evaluation_corpus"]["passed"] is False
    assert criteria["evaluation_corpus"]["numerator"] == 42
    assert all(
        "revision is not a resolvable full commit" in finding["reason"]
        for finding in criteria["evaluation_corpus"]["findings"]
    )


def test_complete_schema_v2_grades_detects_missing_and_incomplete_grades(
    tmp_path: Path,
) -> None:
    db = tmp_path / "scout.db"
    with StateManager(str(db)) as state:
        state.conn.execute(
            "INSERT INTO projects (key, name, description, link, active, created_at, updated_at) "
            "VALUES ('gateway', 'Gateway', 'Gateway project', 'https://example.com/gateway', "
            "1, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
        scan_id = state.conn.execute(
            "INSERT INTO scans (started_at, environment, run_kind) "
            "VALUES ('2026-01-10T12:00:00+00:00', 'production', 'live')"
        ).lastrowid
        ungraded_post = state.conn.execute(
            "INSERT INTO posts "
            "(platform, platform_msg_id, author_id, content, created_at, scan_id) "
            "VALUES ('discord', 'ungraded', 'author-1', 'hello', '2026-01-10T12:05:00+00:00', ?)",
            (scan_id,),
        ).lastrowid
        state.conn.execute(
            "INSERT INTO evaluations "
            "(post_id, relevant, score, relevant_to, scan_id, created_at, project_key, "
            "surface_status) "
            "VALUES (?, 1, 1.0, '[]', ?, '2026-01-10T12:05:00+00:00', 'gateway', 'not_relevant')",
            (ungraded_post, scan_id),
        )
        incomplete_post = state.conn.execute(
            "INSERT INTO posts "
            "(platform, platform_msg_id, author_id, content, created_at, scan_id) "
            "VALUES ('discord', 'incomplete', 'author-2', 'hello again', "
            "'2026-01-10T12:06:00+00:00', ?)",
            (scan_id,),
        ).lastrowid
        incomplete_eval = state.conn.execute(
            "INSERT INTO evaluations "
            "(post_id, relevant, score, relevant_to, scan_id, created_at, project_key, "
            "surface_status) "
            "VALUES (?, 1, 1.0, '[]', ?, '2026-01-10T12:06:00+00:00', 'gateway', 'not_relevant')",
            (incomplete_post, scan_id),
        ).lastrowid
        # action_judgment='fail' requires dimensions + failure_note to count
        # as complete; this grade supplies neither.
        state.conn.execute(
            "INSERT INTO grades "
            "(evaluation_id, post_id, scan_id, source, graded_at, relevance_judgment, "
            "schema_version, needs_regrade, action_judgment) "
            "VALUES (?, ?, ?, 'cli', ?, 'correct', 2, 0, 'fail')",
            (
                incomplete_eval,
                incomplete_post,
                scan_id,
                format_graded_at(datetime(2026, 1, 10, 12, 10, tzinfo=UTC)),
            ),
        )
        state.commit()

    result = run_audit(
        db,
        tmp_path,
        window_from=REPRESENTATIVE_WINDOW_FROM,
        window_to=REPRESENTATIVE_WINDOW_TO,
    )

    criterion = result.report["criteria"]["complete_schema_v2_grades"]
    assert criterion["passed"] is False
    assert criterion["denominator"] == 2
    assert criterion["numerator"] == 0
    reasons = {finding["reason"] for finding in criterion["findings"]}
    assert "expected exactly one grade, found 0" in reasons
    assert "grade is missing required schema-v2 causal fields" in reasons


def test_attributable_deterministic_gate_block_detects_non_deterministic_block(
    tmp_path: Path,
) -> None:
    db = tmp_path / "scout.db"
    with StateManager(str(db)) as state:
        scan_id = state.conn.execute(
            "INSERT INTO scans (started_at, environment, run_kind) "
            "VALUES ('2026-01-10T12:00:00+00:00', 'production', 'live')"
        ).lastrowid
        post_id = state.conn.execute(
            "INSERT INTO posts "
            "(platform, platform_msg_id, author_id, content, created_at, scan_id) "
            "VALUES ('discord', 'blocked', 'author-1', 'hello', '2026-01-10T12:05:00+00:00', ?)",
            (scan_id,),
        ).lastrowid
        eval_id = state.conn.execute(
            "INSERT INTO evaluations "
            "(post_id, relevant, score, relevant_to, scan_id, created_at, surface_status) "
            "VALUES (?, 1, 1.0, '[]', ?, '2026-01-10T12:05:00+00:00', 'gate_blocked')",
            (post_id, scan_id),
        ).lastrowid
        state.conn.execute(
            "INSERT INTO gate_blocks "
            "(reason_code, offending_text, scan_id, post_id, evaluation_id, created_at) "
            "VALUES ('llm_judgment', 'offending text', ?, ?, ?, '2026-01-10T12:05:00+00:00')",
            (scan_id, post_id, eval_id),
        )
        state.commit()

    result = run_audit(
        db,
        tmp_path,
        window_from=REPRESENTATIVE_WINDOW_FROM,
        window_to=REPRESENTATIVE_WINDOW_TO,
    )

    criterion = result.report["criteria"]["attributable_deterministic_gate_block"]
    assert criterion["passed"] is False
    assert criterion["numerator"] == 0
    assert any(
        "gate code is not deterministic" in finding["reason"]
        for finding in criterion["findings"]
    )


def test_surfaced_pairing_and_rate_detects_missing_draft_and_event(
    tmp_path: Path,
) -> None:
    db = tmp_path / "scout.db"
    with StateManager(str(db)) as state:
        scan_id = state.conn.execute(
            "INSERT INTO scans (started_at, environment, run_kind) "
            "VALUES ('2026-01-10T12:00:00+00:00', 'production', 'live')"
        ).lastrowid
        post_id = state.conn.execute(
            "INSERT INTO posts "
            "(platform, platform_msg_id, author_id, content, created_at, scan_id) "
            "VALUES ('discord', 'surfaced-orphan', 'author-1', 'hello', "
            "'2026-01-10T12:05:00+00:00', ?)",
            (scan_id,),
        ).lastrowid
        state.conn.execute(
            "INSERT INTO evaluations "
            "(post_id, relevant, score, relevant_to, scan_id, created_at, surface_status) "
            "VALUES (?, 1, 1.0, '[]', ?, '2026-01-10T12:05:00+00:00', 'surfaced')",
            (post_id, scan_id),
        )
        state.commit()

    result = run_audit(
        db,
        tmp_path,
        window_from=REPRESENTATIVE_WINDOW_FROM,
        window_to=REPRESENTATIVE_WINDOW_TO,
    )

    criterion = result.report["criteria"]["surfaced_pairing_and_rate"]
    assert criterion["passed"] is False
    assert any(
        "requires one draft and one event" in finding["reason"] for finding in criterion["findings"]
    )


def test_graded_bluesky_parent_context_change_detects_no_qualifying_change(
    tmp_path: Path,
) -> None:
    """A graded, resolved-parent Bluesky reply whose counterfactual posture
    and relevance both match the real outcome proves nothing — the
    criterion must fail rather than credit a same-outcome assessment."""
    db = tmp_path / "scout.db"
    with StateManager(str(db)) as state:
        scan_id = state.conn.execute(
            "INSERT INTO scans (started_at, environment, run_kind) "
            "VALUES ('2026-01-10T12:00:00+00:00', 'production', 'live')"
        ).lastrowid
        post_id = state.conn.execute(
            "INSERT INTO posts "
            "(platform, platform_msg_id, author_id, content, created_at, scan_id, "
            "parent_lookup_status, parent_id) "
            "VALUES ('bluesky', 'no-change', 'author-1', 'Sure, sounds good.', "
            "'2026-01-10T12:05:00+00:00', ?, 'resolved', 'parent-1')",
            (scan_id,),
        ).lastrowid
        eval_id = state.conn.execute(
            "INSERT INTO evaluations "
            "(post_id, relevant, score, relevant_to, scan_id, created_at, posture, "
            "surface_status) "
            "VALUES (?, 1, 1.0, '[]', ?, '2026-01-10T12:05:00+00:00', 'answer', 'not_relevant')",
            (post_id, scan_id),
        ).lastrowid
        state.conn.execute(
            "INSERT INTO grades "
            "(evaluation_id, post_id, scan_id, source, graded_at, relevance_judgment, "
            "schema_version, needs_regrade, action_judgment) "
            "VALUES (?, ?, ?, 'cli', ?, 'correct', 2, 0, 'accept')",
            (
                eval_id,
                post_id,
                scan_id,
                format_graded_at(datetime(2026, 1, 10, 12, 10, tzinfo=UTC)),
            ),
        )
        state.conn.execute(
            "INSERT INTO parent_context_assessments "
            "(evaluation_id, assessor, assessed_at, without_parent_relevance, "
            "without_parent_posture, explanation) "
            "VALUES (?, 'operator', '2026-01-10T12:15:00+00:00', 'relevant', 'answer', "
            "'Same outcome without the parent.')",
            (eval_id,),
        )
        state.commit()

    result = run_audit(
        db,
        tmp_path,
        window_from=REPRESENTATIVE_WINDOW_FROM,
        window_to=REPRESENTATIVE_WINDOW_TO,
    )

    criterion = result.report["criteria"]["graded_bluesky_parent_context_change"]
    assert criterion["passed"] is False
    assert criterion["numerator"] == 0
    assert any(
        "no graded attributable Bluesky reply proves parent context changed" in finding["reason"]
        for finding in criterion["findings"]
    )
