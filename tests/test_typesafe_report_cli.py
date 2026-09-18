import argparse
import sqlite3
from importlib.resources import files

from scout.cli.typesafe import run_typesafe
from scout.typesafe.reporting import FixtureReplayUnavailableError, TypesafeReport


def test_acceptance_fixture_is_packaged_with_scout() -> None:
    fixture = files("scout.typesafe").joinpath("acceptance-cases.json")
    assert fixture.is_file()
    assert '"gate_v1"' in fixture.read_text()


def _args(path, **overrides):
    values = {"db_path": str(path), "since": None, "scan_id": 7, "json": False}
    values.update(overrides)
    return argparse.Namespace(**values)


def test_report_missing_table_is_a_clean_noop(tmp_path, capsys) -> None:
    db_path = tmp_path / "old.db"
    sqlite3.connect(db_path).close()
    assert run_typesafe(_args(db_path)) == 0
    assert "predates schema v47" in capsys.readouterr().out


def test_report_explains_when_packaged_fixture_is_unavailable(
    tmp_path, capsys, monkeypatch
) -> None:
    db_path = tmp_path / "report.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE shadow_relevance_runs (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        "scout.storage.shadow_relevance.ShadowRelevanceStore.report_rows",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "scout.cli.typesafe.replay_acceptance_fixture",
        lambda: (_ for _ in ()).throw(FixtureReplayUnavailableError("fixture missing")),
    )

    assert run_typesafe(_args(db_path)) == 1
    assert "Cannot produce typesafe report: fixture missing" in capsys.readouterr().out


def test_report_lists_decisions_grade_summary_and_round_trips_json(tmp_path, capsys) -> None:
    db_path = tmp_path / "report.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE evaluations (id INTEGER PRIMARY KEY, relevant INTEGER, score REAL);
        CREATE TABLE grades (id INTEGER PRIMARY KEY, evaluation_id INTEGER);
        CREATE TABLE grade_revisions (
          id INTEGER PRIMARY KEY, grade_id INTEGER, revision INTEGER, payload TEXT);
        CREATE TABLE shadow_relevance_runs (
          id INTEGER PRIMARY KEY, evaluation_id INTEGER, scan_id INTEGER,
          created_at TEXT, status TEXT, eligible INTEGER, p_eligible REAL,
          uncertain INTEGER, decision_json TEXT, error_detail TEXT);
        INSERT INTO evaluations VALUES (1, 1, .82), (2, 0, .21);
        INSERT INTO grades VALUES (9, 1);
        INSERT INTO grade_revisions VALUES
          (10, 9, 1, '{"relevance_judgment":"correct"}');
        INSERT INTO shadow_relevance_runs VALUES
          (1, 1, 7, '2026-09-16T00:00:00Z', 'ok', 0, .4, 0,
           '{"reason":"old"}', NULL),
          (2, 1, 7, '2026-09-17T00:00:00Z', 'ok', 1, .91, 0,
           '{"reason":"threshold met"}', NULL),
          (3, 2, 7, '2026-09-17T00:01:00Z', 'ok', 1, .7, 1,
           '{"reason":"uncertain"}', NULL);
        """
    )
    conn.commit()
    conn.close()

    assert run_typesafe(_args(db_path, json=True)) == 0
    report = TypesafeReport.model_validate_json(capsys.readouterr().out)
    assert [item.evaluation_id for item in report.evaluations] == [1, 2]
    assert report.evaluations[0].model_dump() == {
        "evaluation_id": 1, "scan_id": 7, "shadow_status": "ok",
        "shadow_eligible": True, "shadow_p_eligible": .91,
        "shadow_uncertain": False, "shadow_reason": "threshold met",
        "llm_relevant": True, "llm_score": .82, "human_grade": "correct",
    }
    assert report.evaluations[1].human_grade is None
    assert report.summary.shadow_llm_agree == 1
    assert report.summary.shadow_llm_disagree == 1
    assert report.placeholder_fixture_replay.passed
    assert report.placeholder_fixture_replay.checked == 3

    assert run_typesafe(_args(db_path)) == 0
    text = capsys.readouterr().out
    assert "evaluation 1:" in text
    assert "human_grade=correct" in text
    assert "placeholder_fixture_replay=PASS" in text
