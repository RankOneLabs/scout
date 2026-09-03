"""Tests for the grade corpus audit and remediation module.

Fixture layout (see build_fixture): one row per known corruption class,
two deliberately overlapping rows, and one clean row — built against a
real SQLite database (via StateManager) so joins, posture context,
source, status, and downstream-reader filtering are all exercised
faithfully rather than through mocked records.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scout.config import GradeRecord
from scout.storage.state import LATEST_SCHEMA_VERSION, StateManager, format_graded_at
from scripts.grade_corpus_audit import (
    HISTORICAL_ABSTAIN_CONFUSION,
    INVERTED_WEB_SEMANTICS,
    MANUFACTURED_CLI_DATA,
    NOOP_POSTURE_CORRECTION,
    VALIDATOR_FAILURE,
    AuditError,
    convergence_repair,
    get_recent_grading_signal_evaluation_ids,
    open_readonly_connection,
    remediate,
    render_convergence_markdown,
    render_markdown,
    run_audit,
    run_convergence_audit,
)

FIXTURE_DB = Path(__file__).parent / "fixtures" / "grade_corpus_audit.db"


def _insert_post(state: StateManager, msg_id: str) -> int:
    now = datetime.now(UTC).isoformat()
    cursor = state.conn.execute(
        "INSERT INTO posts (platform, platform_msg_id, content, created_at) "
        "VALUES ('discord', ?, 'content', ?)",
        (msg_id, now),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _insert_evaluation(
    state: StateManager,
    post_id: int,
    *,
    relevant: int,
    posture: str | None,
    surface_status: str,
) -> int:
    now = datetime.now(UTC).isoformat()
    cursor = state.conn.execute(
        "INSERT INTO evaluations (post_id, relevant, score, created_at, posture, surface_status) "
        "VALUES (?, ?, 1.0, ?, ?, ?)",
        (post_id, relevant, now, posture, surface_status),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _insert_grade(
    state: StateManager,
    *,
    post_id: int,
    evaluation_id: int,
    source: str,
    relevance_judgment: str,
    schema_version: int,
    action_judgment: str | None = None,
    dimensions: list[str] | None = None,
    failure_note: str | None = None,
    posture_should_have_been: str | None = None,
) -> int:
    dims_json = json.dumps(dimensions) if dimensions is not None else None
    cursor = state.conn.execute(
        "INSERT INTO grades (evaluation_id, post_id, source, graded_at, relevance_judgment, "
        "schema_version, needs_regrade, action_judgment, dimensions, failure_note, "
        "posture_should_have_been) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
        (
            evaluation_id,
            post_id,
            source,
            format_graded_at(datetime.now(UTC)),
            relevance_judgment,
            schema_version,
            action_judgment,
            dims_json,
            failure_note,
            posture_should_have_been,
        ),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def build_fixture(db_path: Path) -> dict[str, int]:
    """Build the grade-corpus-audit fixture database and return a label ->
    grade_id map for the rows relevant to each corruption class."""
    ids: dict[str, int] = {}
    with StateManager(str(db_path)) as state:
        # 1. clean row — schema-v2 valid pass, no defects.
        p1 = _insert_post(state, "clean-1")
        e1 = _insert_evaluation(state, p1, relevant=1, posture="answer", surface_status="surfaced")
        ids["clean"] = _insert_grade(
            state,
            post_id=p1,
            evaluation_id=e1,
            source="cli",
            relevance_judgment="correct",
            schema_version=2,
            action_judgment="accept",
        )

        # 2. validator_failure (isolated) — accept incompatible with false_positive.
        p2 = _insert_post(state, "validator-failure-1")
        e2 = _insert_evaluation(state, p2, relevant=1, posture="answer", surface_status="surfaced")
        ids["validator_failure"] = _insert_grade(
            state,
            post_id=p2,
            evaluation_id=e2,
            source="cli",
            relevance_judgment="false_positive",
            schema_version=2,
            action_judgment="accept",
        )

        # 3. inverted_web_semantics (isolated) — source=web joined to relevant=0.
        p3 = _insert_post(state, "inverted-web-1")
        e3 = _insert_evaluation(state, p3, relevant=0, posture=None, surface_status="not_relevant")
        ids["inverted_web_semantics"] = _insert_grade(
            state,
            post_id=p3,
            evaluation_id=e3,
            source="web",
            relevance_judgment="correct",
            schema_version=2,
            action_judgment="accept",
        )

        # 4. manufactured_cli_data (isolated) — placeholder failure_note, valid dims.
        p4 = _insert_post(state, "manufactured-cli-1")
        e4 = _insert_evaluation(state, p4, relevant=1, posture="answer", surface_status="surfaced")
        ids["manufactured_cli_data"] = _insert_grade(
            state,
            post_id=p4,
            evaluation_id=e4,
            source="cli",
            relevance_judgment="false_positive",
            schema_version=2,
            action_judgment="fail",
            dimensions=["tone"],
            failure_note="no-note-provided",
        )

        # 5. overlap: validator_failure + manufactured_cli_data — usefulness-only,
        #    no note, no causal detail (always structurally invalid too).
        p5 = _insert_post(state, "overlap-validator-manufactured-1")
        e5 = _insert_evaluation(state, p5, relevant=1, posture="answer", surface_status="surfaced")
        ids["overlap_validator_manufactured"] = _insert_grade(
            state,
            post_id=p5,
            evaluation_id=e5,
            source="cli",
            relevance_judgment="false_positive",
            schema_version=2,
            action_judgment="fail",
            dimensions=["usefulness"],
            failure_note=None,
        )

        # 6. noop_posture_correction (isolated) — legacy schema-v1 row so the
        #    live validator (schema-v2 only) doesn't also flag it.
        p6 = _insert_post(state, "noop-posture-1")
        e6 = _insert_evaluation(state, p6, relevant=1, posture="answer", surface_status="surfaced")
        ids["noop_posture_correction"] = _insert_grade(
            state,
            post_id=p6,
            evaluation_id=e6,
            source="web",
            relevance_judgment="correct",
            schema_version=1,
            dimensions=["posture"],
            posture_should_have_been="answer",
        )

        # 7. historical_abstain_confusion (isolated) — posture=abstain but
        #    surface_status=not_relevant instead of abstained.
        p7 = _insert_post(state, "abstain-confusion-1")
        e7 = _insert_evaluation(
            state, p7, relevant=1, posture="abstain", surface_status="not_relevant"
        )
        ids["historical_abstain_confusion"] = _insert_grade(
            state,
            post_id=p7,
            evaluation_id=e7,
            source="cli",
            relevance_judgment="correct",
            schema_version=2,
            action_judgment="accept",
        )

        # 8. overlap: inverted_web_semantics + historical_abstain_confusion.
        p8 = _insert_post(state, "overlap-web-abstain-1")
        e8 = _insert_evaluation(
            state, p8, relevant=0, posture="abstain", surface_status="not_relevant"
        )
        ids["overlap_web_abstain"] = _insert_grade(
            state,
            post_id=p8,
            evaluation_id=e8,
            source="web",
            relevance_judgment="correct",
            schema_version=2,
            action_judgment="accept",
        )

        state.commit()

    return ids


@pytest.fixture
def fixture_ids(tmp_path: Path) -> tuple[Path, dict[str, int]]:
    db_path = tmp_path / "scout.db"
    ids = build_fixture(db_path)
    return db_path, ids


def _ro_uri(db_path: Path) -> str:
    return f"file:{db_path}?mode=ro"


# --- Classification correctness ---


def test_isolated_validator_failure_row_is_classified_alone(
    fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, ids = fixture_ids
    manifest = run_audit(_ro_uri(db_path), now=datetime.now(UTC))
    assert ids["validator_failure"] in manifest["classes"][VALIDATOR_FAILURE]["grade_ids"]
    for other in (
        INVERTED_WEB_SEMANTICS,
        MANUFACTURED_CLI_DATA,
        NOOP_POSTURE_CORRECTION,
        HISTORICAL_ABSTAIN_CONFUSION,
    ):
        assert ids["validator_failure"] not in manifest["classes"][other]["grade_ids"]


def test_isolated_inverted_web_semantics_row(fixture_ids: tuple[Path, dict[str, int]]) -> None:
    db_path, ids = fixture_ids
    manifest = run_audit(_ro_uri(db_path), now=datetime.now(UTC))
    assert ids["inverted_web_semantics"] in manifest["classes"][INVERTED_WEB_SEMANTICS]["grade_ids"]
    assert ids["inverted_web_semantics"] not in manifest["classes"][VALIDATOR_FAILURE]["grade_ids"]


def test_isolated_manufactured_cli_data_row(fixture_ids: tuple[Path, dict[str, int]]) -> None:
    db_path, ids = fixture_ids
    manifest = run_audit(_ro_uri(db_path), now=datetime.now(UTC))
    assert ids["manufactured_cli_data"] in manifest["classes"][MANUFACTURED_CLI_DATA]["grade_ids"]
    assert ids["manufactured_cli_data"] not in manifest["classes"][VALIDATOR_FAILURE]["grade_ids"]


def test_isolated_noop_posture_correction_row(fixture_ids: tuple[Path, dict[str, int]]) -> None:
    db_path, ids = fixture_ids
    manifest = run_audit(_ro_uri(db_path), now=datetime.now(UTC))
    assert (
        ids["noop_posture_correction"] in manifest["classes"][NOOP_POSTURE_CORRECTION]["grade_ids"]
    )
    assert ids["noop_posture_correction"] not in manifest["classes"][VALIDATOR_FAILURE]["grade_ids"]


def test_isolated_historical_abstain_confusion_row(
    fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, ids = fixture_ids
    manifest = run_audit(_ro_uri(db_path), now=datetime.now(UTC))
    assert (
        ids["historical_abstain_confusion"]
        in manifest["classes"][HISTORICAL_ABSTAIN_CONFUSION]["grade_ids"]
    )
    assert (
        ids["historical_abstain_confusion"]
        not in manifest["classes"][VALIDATOR_FAILURE]["grade_ids"]
    )


def test_overlap_row_hits_both_validator_and_manufactured(
    fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, ids = fixture_ids
    manifest = run_audit(_ro_uri(db_path), now=datetime.now(UTC))
    gid = ids["overlap_validator_manufactured"]
    assert gid in manifest["classes"][VALIDATOR_FAILURE]["grade_ids"]
    assert gid in manifest["classes"][MANUFACTURED_CLI_DATA]["grade_ids"]
    assert gid in manifest["intersections"][f"{VALIDATOR_FAILURE}__{MANUFACTURED_CLI_DATA}"]


def test_overlap_row_hits_both_inverted_web_and_abstain_confusion(
    fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, ids = fixture_ids
    manifest = run_audit(_ro_uri(db_path), now=datetime.now(UTC))
    gid = ids["overlap_web_abstain"]
    assert gid in manifest["classes"][INVERTED_WEB_SEMANTICS]["grade_ids"]
    assert gid in manifest["classes"][HISTORICAL_ABSTAIN_CONFUSION]["grade_ids"]
    assert (
        gid
        in manifest["intersections"][f"{INVERTED_WEB_SEMANTICS}__{HISTORICAL_ABSTAIN_CONFUSION}"]
    )


def test_clean_row_is_classified_into_no_class(fixture_ids: tuple[Path, dict[str, int]]) -> None:
    db_path, ids = fixture_ids
    manifest = run_audit(_ro_uri(db_path), now=datetime.now(UTC))
    assert ids["clean"] not in manifest["affected_grade_ids"]


def test_affected_total_is_deduplicated(fixture_ids: tuple[Path, dict[str, int]]) -> None:
    db_path, ids = fixture_ids
    manifest = run_audit(_ro_uri(db_path), now=datetime.now(UTC))
    assert manifest["affected_total"] == len(set(manifest["affected_grade_ids"]))
    # 7 of the 8 non-clean rows are affected; none double counted.
    assert manifest["affected_total"] == 7
    assert manifest["total_eligible_grades"] == 8


def test_class_dispositions_are_flag_and_manual_regrade_under_threshold(
    fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, _ids = fixture_ids
    manifest = run_audit(_ro_uri(db_path), now=datetime.now(UTC))
    for entry in manifest["classes"].values():
        if entry["count"] == 0:
            continue
        assert entry["disposition"] == "flag_and_manual_regrade"


# --- Read-only enforcement ---


def test_open_readonly_connection_rejects_uri_without_mode_ro(tmp_path: Path) -> None:
    db_path = tmp_path / "scout.db"
    with StateManager(str(db_path)):
        pass
    with pytest.raises(AuditError, match="mode=ro"):
        open_readonly_connection(str(db_path))


def test_audit_cannot_write_to_the_database(fixture_ids: tuple[Path, dict[str, int]]) -> None:
    db_path, _ids = fixture_ids
    before = db_path.read_bytes()
    run_audit(_ro_uri(db_path), now=datetime.now(UTC))
    assert db_path.read_bytes() == before

    conn = open_readonly_connection(_ro_uri(db_path))
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE grades SET needs_regrade = 1")
    finally:
        conn.close()


def test_render_markdown_is_pure_rendering_of_manifest(
    fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, _ids = fixture_ids
    manifest = run_audit(_ro_uri(db_path), now=datetime.now(UTC))
    rendered = render_markdown(manifest)
    assert manifest["digest"] in rendered
    assert str(manifest["affected_total"]) in rendered


# --- Remediation ---


def test_remediate_dry_run_changes_nothing(fixture_ids: tuple[Path, dict[str, int]]) -> None:
    db_path, _ids = fixture_ids
    manifest = run_audit(_ro_uri(db_path), now=datetime.now(UTC))
    before = db_path.read_bytes()

    result = remediate(str(db_path), manifest, apply=False, now=datetime.now(UTC))

    assert result["dry_run"] is True
    assert result["would_flag_count"] == manifest["affected_total"]
    assert db_path.read_bytes() == before


def test_remediate_apply_flags_exact_deduplicated_ids(
    fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, ids = fixture_ids
    manifest = run_audit(_ro_uri(db_path), now=datetime.now(UTC))

    result = remediate(str(db_path), manifest, apply=True, now=datetime.now(UTC))

    assert result["dry_run"] is False
    assert sorted(result["flagged_grade_ids"]) == manifest["affected_grade_ids"]

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    flagged = {
        r["id"] for r in conn.execute("SELECT id FROM grades WHERE needs_regrade = 1").fetchall()
    }
    conn.close()
    assert flagged == set(manifest["affected_grade_ids"])
    assert ids["clean"] not in flagged


def test_remediate_aborts_without_mutation_when_manifest_has_drifted(
    fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, _ids = fixture_ids
    manifest = run_audit(_ro_uri(db_path), now=datetime.now(UTC))
    stale_manifest = dict(manifest)
    stale_manifest["digest"] = "not-a-real-digest"
    before = db_path.read_bytes()

    with pytest.raises(AuditError, match="drifted"):
        remediate(str(db_path), stale_manifest, apply=True, now=datetime.now(UTC))

    assert db_path.read_bytes() == before


def test_remediate_never_implicitly_migrates_an_old_audited_database(
    fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, _ids = fixture_ids
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version=20")
    conn.commit()
    conn.close()
    manifest = run_audit(_ro_uri(db_path), now=datetime.now(UTC))
    before = db_path.read_bytes()

    with pytest.raises(AuditError, match=f"upgrade to {LATEST_SCHEMA_VERSION}, then rerun"):
        remediate(str(db_path), manifest, apply=True, now=datetime.now(UTC))

    assert db_path.read_bytes() == before


@pytest.mark.parametrize("invalid_version", [None, "not-a-version"])
def test_remediate_rejects_malformed_manifest_schema_version(
    fixture_ids: tuple[Path, dict[str, int]], invalid_version: object
) -> None:
    db_path, _ids = fixture_ids
    manifest = run_audit(_ro_uri(db_path), now=datetime.now(UTC))
    manifest["db_schema_version"] = invalid_version
    before = db_path.read_bytes()

    with pytest.raises(AuditError, match="db_schema_version must be an integer"):
        remediate(str(db_path), manifest, apply=True, now=datetime.now(UTC))

    assert db_path.read_bytes() == before


def test_remediate_replacement_manifest_restores_a_row(
    fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, ids = fixture_ids
    manifest = run_audit(_ro_uri(db_path), now=datetime.now(UTC))
    grade_id = ids["validator_failure"]

    replacement_manifest = {
        str(grade_id): {
            "source": "cli",
            "relevance_judgment": "false_positive",
            "action_judgment": "fail",
            "dimensions": ["tone"],
            "failure_note": "operator-reviewed: relevance was actually wrong",
        }
    }

    result = remediate(
        str(db_path),
        manifest,
        apply=True,
        replacement_manifest=replacement_manifest,
        now=datetime.now(UTC),
    )

    assert result["replacements_applied"] == 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT needs_regrade, failure_note FROM grades WHERE id = ?", (grade_id,)
    ).fetchone()
    conn.close()
    assert row["needs_regrade"] == 0
    assert row["failure_note"] == "operator-reviewed: relevance was actually wrong"


def test_remediate_rejects_replacement_outside_audited_affected_set(
    fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, ids = fixture_ids
    manifest = run_audit(_ro_uri(db_path), now=datetime.now(UTC))
    clean_grade_id = ids["clean"]
    assert clean_grade_id not in manifest["affected_grade_ids"]

    replacement_manifest = {
        str(clean_grade_id): {
            "source": "cli",
            "relevance_judgment": "correct",
            "action_judgment": "accept",
        }
    }

    with pytest.raises(AuditError, match="not in the audited affected set"):
        remediate(
            str(db_path),
            manifest,
            apply=True,
            replacement_manifest=replacement_manifest,
            now=datetime.now(UTC),
        )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT relevance_judgment FROM grades WHERE id = ?", (clean_grade_id,)
    ).fetchone()
    conn.close()
    assert row["relevance_judgment"] == "correct"


def test_remediate_replacement_failing_validation_raises_audit_error(
    fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, ids = fixture_ids
    manifest = run_audit(_ro_uri(db_path), now=datetime.now(UTC))
    grade_id = ids["validator_failure"]

    # accept is incompatible with false_positive — save_grade raises GradeValidationError.
    replacement_manifest = {
        str(grade_id): {
            "source": "cli",
            "relevance_judgment": "false_positive",
            "action_judgment": "accept",
        }
    }

    with pytest.raises(AuditError, match="failed validation"):
        remediate(
            str(db_path),
            manifest,
            apply=True,
            replacement_manifest=replacement_manifest,
            now=datetime.now(UTC),
        )

    conn = sqlite3.connect(str(db_path))
    flagged_count = conn.execute(
        "SELECT COUNT(*) FROM grades WHERE needs_regrade = 1"
    ).fetchone()[0]
    conn.close()
    assert flagged_count == 0, "failed replacement must roll back flagging too"


def test_remediate_replacement_batch_is_all_or_nothing(
    fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, ids = fixture_ids
    manifest = run_audit(_ro_uri(db_path), now=datetime.now(UTC))
    first_grade_id = ids["validator_failure"]
    second_grade_id = ids["inverted_web_semantics"]

    replacement_manifest = {
        str(first_grade_id): {
            "source": "cli",
            "relevance_judgment": "false_positive",
            "action_judgment": "fail",
            "dimensions": ["tone"],
            "failure_note": "reviewed first replacement",
        },
        str(second_grade_id): {
            "source": "cli",
            "relevance_judgment": "false_positive",
            "action_judgment": "accept",
        },
    }

    with pytest.raises(AuditError, match=f"replacement for grade id {second_grade_id}"):
        remediate(
            str(db_path),
            manifest,
            apply=True,
            replacement_manifest=replacement_manifest,
            now=datetime.now(UTC),
        )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    first = conn.execute(
        "SELECT needs_regrade, failure_note FROM grades WHERE id = ?",
        (first_grade_id,),
    ).fetchone()
    flagged_count = conn.execute(
        "SELECT COUNT(*) FROM grades WHERE needs_regrade = 1"
    ).fetchone()[0]
    conn.close()
    assert first["failure_note"] != "reviewed first replacement"
    assert first["needs_regrade"] == 0
    assert flagged_count == 0


def test_remediate_dry_run_does_not_leave_the_database_locked(
    fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, _ids = fixture_ids
    manifest = run_audit(_ro_uri(db_path), now=datetime.now(UTC))

    remediate(str(db_path), manifest, apply=False, now=datetime.now(UTC))

    # If the dry-run path leaked its connection, this exclusive write would
    # hang or fail against a still-open, still-transacting connection.
    conn = sqlite3.connect(str(db_path), timeout=1)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE grades SET needs_regrade = needs_regrade")
    conn.commit()
    conn.close()


def test_post_remediation_downstream_readers_expose_zero_known_bad_rows(
    fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, _ids = fixture_ids
    manifest = run_audit(_ro_uri(db_path), now=datetime.now(UTC))

    result = remediate(str(db_path), manifest, apply=True, now=datetime.now(UTC))

    assert result["signal_reachable_count"] == 0
    assert result["export_reachable_count"] == 0

    with StateManager(str(db_path)) as state:
        signal_eval_ids = get_recent_grading_signal_evaluation_ids(state.conn)
        export_eval_ids = set()
        for case in state.export_eval_cases():
            evaluation = case["evaluation"]
            assert isinstance(evaluation, dict)
            export_eval_ids.add(evaluation["evaluation_id"])
    known_bad_eval_ids = set(manifest["affected_evaluation_ids"])
    assert not (known_bad_eval_ids & signal_eval_ids)
    assert not (known_bad_eval_ids & export_eval_ids)


def test_committed_fixture_db_matches_builder(tmp_path: Path) -> None:
    """The committed tests/fixtures/grade_corpus_audit.db should classify
    identically to a freshly built fixture, proving it isn't stale."""
    if not FIXTURE_DB.exists():
        pytest.skip("fixture db not yet generated")
    fresh_path = tmp_path / "fresh.db"
    fresh_ids = build_fixture(fresh_path)

    committed_copy = tmp_path / "committed.db"
    shutil.copyfile(FIXTURE_DB, committed_copy)

    fresh_manifest = run_audit(_ro_uri(fresh_path), now=datetime.now(UTC))
    committed_manifest = run_audit(_ro_uri(committed_copy), now=datetime.now(UTC))

    assert committed_manifest["total_eligible_grades"] == fresh_manifest["total_eligible_grades"]
    assert committed_manifest["affected_total"] == fresh_manifest["affected_total"]
    for class_id, entry in fresh_manifest["classes"].items():
        assert committed_manifest["classes"][class_id]["count"] == entry["count"]
    assert fresh_ids  # sanity: builder produced rows


# --------------------------------------------------------------------------
# Revision convergence audit + repair
# --------------------------------------------------------------------------


def build_convergence_fixture(db_path: Path) -> dict[str, int]:
    """One converged row (normal save_grade), one missing_revision row (a
    current grade with zero grade_revisions rows), and one
    divergent_revision row (a current grade mutated directly, without
    appending a revision, after its one honest revision was written)."""
    ids: dict[str, int] = {}
    with StateManager(str(db_path)) as state:
        p1 = _insert_post(state, "convergence-clean-1")
        e1 = _insert_evaluation(state, p1, relevant=1, posture="answer", surface_status="surfaced")
        ids["converged"] = state.save_grade(
            GradeRecord(
                post_id=p1, evaluation_id=e1, source="cli",
                graded_at=datetime.now(UTC), relevance_judgment="correct",
                action_judgment="accept", schema_version=3,
            )
        )

        p2 = _insert_post(state, "convergence-missing-1")
        e2 = _insert_evaluation(state, p2, relevant=1, posture="answer", surface_status="surfaced")
        ids["missing_revision"] = _insert_grade(
            state, post_id=p2, evaluation_id=e2, source="cli",
            relevance_judgment="correct", schema_version=2, action_judgment="accept",
        )

        p3 = _insert_post(state, "convergence-divergent-1")
        e3 = _insert_evaluation(state, p3, relevant=1, posture="answer", surface_status="surfaced")
        divergent_id = state.save_grade(
            GradeRecord(
                post_id=p3, evaluation_id=e3, source="cli",
                graded_at=datetime.now(UTC), relevance_judgment="correct",
                action_judgment="accept", schema_version=3,
            )
        )
        state.conn.execute(
            "UPDATE grades SET relevance_judgment = 'false_positive', "
            "action_judgment = 'fail', dimensions = '[\"tone\"]', "
            "failure_note = 'drift' WHERE id = ?",
            (divergent_id,),
        )
        ids["divergent_revision"] = divergent_id

        state.commit()

    return ids


@pytest.fixture
def convergence_fixture_ids(tmp_path: Path) -> tuple[Path, dict[str, int]]:
    db_path = tmp_path / "scout.db"
    ids = build_convergence_fixture(db_path)
    return db_path, ids


def test_convergence_audit_classifies_each_row_correctly(
    convergence_fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, ids = convergence_fixture_ids
    manifest = run_convergence_audit(_ro_uri(db_path), now=datetime.now(UTC))

    assert manifest["total_grades"] == 3
    assert ids["converged"] in manifest["converged_grade_ids"]
    assert ids["missing_revision"] in manifest["missing_revision_grade_ids"]
    assert ids["divergent_revision"] in manifest["divergent_revision_grade_ids"]
    assert manifest["converged_count"] == 1
    assert manifest["missing_revision_count"] == 1
    assert manifest["divergent_revision_count"] == 1


def test_convergence_audit_reports_field_level_mismatches_for_divergent(
    convergence_fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, ids = convergence_fixture_ids
    manifest = run_convergence_audit(_ro_uri(db_path), now=datetime.now(UTC))
    mismatched_fields = manifest["mismatches"][str(ids["divergent_revision"])]
    assert "relevance_judgment" in mismatched_fields
    assert "dimensions" in mismatched_fields
    assert "failure_note" in mismatched_fields
    # The converged row has no entry at all.
    assert str(ids["converged"]) not in manifest["mismatches"]


def test_convergence_audit_recognizes_v3_edited_text_and_reply_revision_id(
    tmp_path: Path,
) -> None:
    """edited_text lives in reply_draft_revisions, not a grades column, so
    the convergence check must resolve it via reply_revision_id — an
    edit-bearing save_grade write must converge out of the box, not
    misreport as divergent purely because the correction text isn't a
    grades column the naive `SELECT * FROM grades` would see."""
    db_path = tmp_path / "scout.db"
    with StateManager(str(db_path)) as state:
        scan_id = state.start_scan()
        post_id = _insert_post(state, "convergence-edit-1")
        eval_id = _insert_evaluation(
            state, post_id, relevant=1, posture="answer", surface_status="surfaced"
        )
        state.save_draft(
            post_id=post_id, evaluation_id=eval_id, project_key="gateway",
            comment_text="original reply", scan_id=scan_id,
        )
        grade_id = state.save_grade(
            GradeRecord(
                post_id=post_id, evaluation_id=eval_id, source="cli",
                graded_at=datetime.now(UTC), relevance_judgment="false_positive",
                action_judgment="fail", schema_version=3,
                dimensions=["tone"], failure_note="too casual",
                edited_text="a corrected reply",
            )
        )
        state.commit()

    manifest = run_convergence_audit(_ro_uri(db_path), now=datetime.now(UTC))
    assert grade_id in manifest["converged_grade_ids"]
    assert grade_id not in manifest["divergent_revision_grade_ids"]
    assert str(grade_id) not in manifest["mismatches"]


def test_convergence_audit_reports_edited_text_mismatch_on_drift(
    tmp_path: Path,
) -> None:
    """A grades.reply_revision_id pointer moved without a matching
    grade_revisions append — the same drift shape divergent_revision
    already detects for plain columns — must surface edited_text (not just
    reply_revision_id) as a mismatched field."""
    db_path = tmp_path / "scout.db"
    with StateManager(str(db_path)) as state:
        scan_id = state.start_scan()
        post_id = _insert_post(state, "convergence-edit-drift-1")
        eval_id = _insert_evaluation(
            state, post_id, relevant=1, posture="answer", surface_status="surfaced"
        )
        draft_id = state.save_draft(
            post_id=post_id, evaluation_id=eval_id, project_key="gateway",
            comment_text="original reply", scan_id=scan_id,
        )
        grade_id = state.save_grade(
            GradeRecord(
                post_id=post_id, evaluation_id=eval_id, source="cli",
                graded_at=datetime.now(UTC), relevance_judgment="false_positive",
                action_judgment="fail", schema_version=3,
                dimensions=["tone"], failure_note="too casual",
                edited_text="v1 text",
            )
        )
        now = datetime.now(UTC).isoformat()
        new_rev_id = state.conn.execute(
            "INSERT INTO reply_draft_revisions "
            "(draft_comment_id, version, parent_revision_id, reply_text, source, created_at) "
            "VALUES (?, 2, (SELECT reply_revision_id FROM grades WHERE id = ?), "
            "'drifted text', 'migration', ?)",
            (draft_id, grade_id, now),
        ).lastrowid
        state.conn.execute(
            "UPDATE grades SET reply_revision_id = ? WHERE id = ?", (new_rev_id, grade_id)
        )
        state.commit()

    manifest = run_convergence_audit(_ro_uri(db_path), now=datetime.now(UTC))
    assert grade_id in manifest["divergent_revision_grade_ids"]
    mismatched = manifest["mismatches"][str(grade_id)]
    assert "edited_text" in mismatched
    assert "reply_revision_id" in mismatched


def test_convergence_audit_cannot_write_to_the_database(
    convergence_fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, _ids = convergence_fixture_ids
    before = db_path.read_bytes()
    run_convergence_audit(_ro_uri(db_path), now=datetime.now(UTC))
    assert db_path.read_bytes() == before


def test_render_convergence_markdown_is_pure_rendering_of_manifest(
    convergence_fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, _ids = convergence_fixture_ids
    manifest = run_convergence_audit(_ro_uri(db_path), now=datetime.now(UTC))
    rendered = render_convergence_markdown(manifest)
    assert manifest["digest"] in rendered
    assert str(manifest["converged_count"]) in rendered


def test_convergence_repair_dry_run_changes_nothing(
    convergence_fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, ids = convergence_fixture_ids
    manifest = run_convergence_audit(_ro_uri(db_path), now=datetime.now(UTC))
    before = db_path.read_bytes()

    result = convergence_repair(str(db_path), manifest, apply=False)

    assert result["dry_run"] is True
    assert sorted(result["would_repair_grade_ids"]) == sorted(
        [ids["missing_revision"], ids["divergent_revision"]]
    )
    assert result["already_converged_count"] == 0
    assert db_path.read_bytes() == before


def test_convergence_repair_apply_repairs_exact_ids(
    convergence_fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, ids = convergence_fixture_ids
    manifest = run_convergence_audit(_ro_uri(db_path), now=datetime.now(UTC))

    result = convergence_repair(str(db_path), manifest, apply=True)

    assert result["dry_run"] is False
    assert sorted(result["repaired_grade_ids"]) == sorted(
        [ids["missing_revision"], ids["divergent_revision"]]
    )
    assert result["already_converged_count"] == 0

    post_manifest = run_convergence_audit(_ro_uri(db_path), now=datetime.now(UTC))
    assert post_manifest["missing_revision_count"] == 0
    assert post_manifest["divergent_revision_count"] == 0
    assert post_manifest["converged_count"] == 3


def test_convergence_repair_never_writes_to_grades_table(
    convergence_fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, ids = convergence_fixture_ids
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    before = {
        row["id"]: dict(row)
        for row in conn.execute("SELECT * FROM grades").fetchall()
    }
    conn.close()

    manifest = run_convergence_audit(_ro_uri(db_path), now=datetime.now(UTC))
    convergence_repair(str(db_path), manifest, apply=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    after = {
        row["id"]: dict(row)
        for row in conn.execute("SELECT * FROM grades").fetchall()
    }
    conn.close()
    assert after == before
    assert set(ids.values()) <= set(before)


def test_convergence_repair_never_updates_or_deletes_existing_revisions(
    convergence_fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, ids = convergence_fixture_ids
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    original_revision = dict(
        conn.execute(
            "SELECT * FROM grade_revisions WHERE grade_id = ? AND revision = 1",
            (ids["divergent_revision"],),
        ).fetchone()
    )
    conn.close()

    manifest = run_convergence_audit(_ro_uri(db_path), now=datetime.now(UTC))
    convergence_repair(str(db_path), manifest, apply=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    after_revision = dict(
        conn.execute(
            "SELECT * FROM grade_revisions WHERE grade_id = ? AND revision = 1",
            (ids["divergent_revision"],),
        ).fetchone()
    )
    revision_count = conn.execute(
        "SELECT COUNT(*) FROM grade_revisions WHERE grade_id = ?",
        (ids["divergent_revision"],),
    ).fetchone()[0]
    conn.close()
    assert after_revision == original_revision
    assert revision_count == 2  # exactly one new revision appended, none removed


def test_convergence_repair_is_idempotent_on_rerun_with_same_manifest(
    convergence_fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, ids = convergence_fixture_ids
    manifest = run_convergence_audit(_ro_uri(db_path), now=datetime.now(UTC))

    first = convergence_repair(str(db_path), manifest, apply=True)
    assert first["repaired_count"] == 2

    # Rerun with the *same*, now-stale manifest — must not error, and must
    # not append a second migration revision to any already-repaired grade.
    second = convergence_repair(str(db_path), manifest, apply=True)
    assert second["repaired_count"] == 0
    assert sorted(second["already_converged_grade_ids"]) == sorted(
        [ids["missing_revision"], ids["divergent_revision"]]
    )

    conn = sqlite3.connect(str(db_path))
    counts = {
        gid: conn.execute(
            "SELECT COUNT(*) FROM grade_revisions WHERE grade_id = ?", (gid,)
        ).fetchone()[0]
        for gid in ids.values()
    }
    conn.close()
    assert counts[ids["missing_revision"]] == 1
    assert counts[ids["divergent_revision"]] == 2
    assert counts[ids["converged"]] == 1


def test_convergence_repair_rejects_malformed_manifest_schema_version(
    convergence_fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, _ids = convergence_fixture_ids
    manifest = run_convergence_audit(_ro_uri(db_path), now=datetime.now(UTC))
    manifest["db_schema_version"] = "not-a-version"
    before = db_path.read_bytes()

    with pytest.raises(AuditError, match="db_schema_version must be an integer"):
        convergence_repair(str(db_path), manifest, apply=True)

    assert db_path.read_bytes() == before


def test_convergence_repair_never_implicitly_migrates_an_old_audited_database(
    convergence_fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, _ids = convergence_fixture_ids
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version=20")
    conn.commit()
    conn.close()
    manifest = run_convergence_audit(_ro_uri(db_path), now=datetime.now(UTC))
    before = db_path.read_bytes()

    with pytest.raises(AuditError, match=f"upgrade to {LATEST_SCHEMA_VERSION}, then rerun"):
        convergence_repair(str(db_path), manifest, apply=True)

    assert db_path.read_bytes() == before


def test_convergence_repair_rejects_schema_version_drift_from_manifest(
    convergence_fixture_ids: tuple[Path, dict[str, int]],
) -> None:
    db_path, _ids = convergence_fixture_ids
    manifest = run_convergence_audit(_ro_uri(db_path), now=datetime.now(UTC))
    stale_manifest = dict(manifest)
    stale_manifest["db_schema_version"] = manifest["db_schema_version"] + 1
    before = db_path.read_bytes()

    with pytest.raises(AuditError, match="schema version has drifted"):
        convergence_repair(str(db_path), stale_manifest, apply=True)

    assert db_path.read_bytes() == before
