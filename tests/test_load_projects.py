# load_projects and TOML-backed project loading were removed in cohort 2
# (scout_better_config/2). Projects are now loaded from the SQLite registry
# via StateManager.load_runtime_registry().
#
# This module now also covers scan_runner.load_project_dossiers and
# scan_runner.run_preflight: both read whatever projects the SQLite registry
# marks active, with no hardcoded project set. Fixed here because a fixed
# gateway/zk-extension set previously made a legitimate registry change fail
# startup (S-011.c).

from __future__ import annotations

import sqlite3
import subprocess
from datetime import date
from pathlib import Path

import pytest
import yaml

import scout.config as config
import scout.scanning.runner as scan_runner
from scout.dossiers.resolver import get_dossier_revision
from scout.registry import ProjectTarget
from scripts import check_dossiers
from tests.conftest import resolve_dossier_from_disk


def _details(report: dict[str, object]) -> dict[str, object]:
    d = report["details"]
    assert isinstance(d, dict)
    return d


def _errors(report: dict[str, object]) -> list[str]:
    e = report["errors"]
    assert isinstance(e, list)
    return e


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    for key, value in (
        ("user.email", "test@test.com"),
        ("user.name", "Test"),
        ("commit.gpgsign", "false"),
    ):
        subprocess.run(["git", "config", key, value], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)


def _write_summary(root: Path, project_key: str, summary_id: str) -> None:
    summaries = root / "summaries"
    summaries.mkdir(exist_ok=True)
    (summaries / f"{project_key}.yaml").write_text(yaml.safe_dump({
        "id": summary_id,
        "type": "summary",
        "dossier": {
            "project_key": project_key,
            "last_reviewed": date.today().isoformat(),
            "reviewer": {"id": "reviewer", "display_name": "Reviewer"},
            "evidence": [{
                "id": "evidence-1", "kind": "git-blob",
                "locator": "repository:summary.yaml", "immutable_ref": "a" * 40,
            }],
            "facts": [{
                "id": f"fact-{project_key}", "claim": "Fact.", "status": "planned",
                "safe_phrasings": ["Fact."], "evidence_ids": ["evidence-1"],
            }],
            "resources": [],
            "prohibitions": [],
            "known_gaps": [],
        },
    }))


def _make_dossier_root(tmp_path: Path, project_keys: list[str]) -> Path:
    """Build a git-backed dossier-source root with one summary per project key."""
    root = tmp_path / "dossier-source"
    (root / "summaries").mkdir(parents=True)
    index_entries = {}
    for key in project_keys:
        summary_id = f"{key}-dossier"
        _write_summary(root, key, summary_id)
        index_entries[summary_id] = {"type": "summary", "path": f"summaries/{key}.yaml"}
    (root / "index.yaml").write_text(yaml.safe_dump({"version": "1.0.0", "entries": index_entries}))
    _init_git_repo(root)
    return root


def _project(key: str, dossier_summary_id: str | None) -> ProjectTarget:
    return ProjectTarget(
        key=key, name=key, description="d", link="l", dossier_summary_id=dossier_summary_id
    )


@pytest.fixture(autouse=True)
def _low_min_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixtures here carry one fact each; drop the floor so readiness checks
    below exercise the registry-vs-project-set logic, not the (separately
    tested) entry-count gate."""
    monkeypatch.setattr(config, "SCOUT_DOSSIER_MIN_ENTRIES", 1)


@pytest.fixture(autouse=True)
def _stub_resolve_dossier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scan_runner, "resolve_dossier", resolve_dossier_from_disk)
    monkeypatch.setattr(check_dossiers, "resolve_dossier", resolve_dossier_from_disk)


@pytest.fixture(autouse=True)
def _resolve_at_the_throwaway_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve at the temp repo's HEAD rather than Scout's real pin.

    Each case here builds its own one-off dossier-source checkout, so the
    shipped pin necessarily names a commit that does not exist in it — the
    same "pinned revision is not present" error for every case, before the
    logic under test runs. Which revision production resolves at is covered
    in tests/test_dossier.py; what these cases exercise is the
    registry-vs-project-set behaviour downstream of it.
    """
    monkeypatch.setattr(scan_runner, "get_pinned_dossier_revision", get_dossier_revision)
    monkeypatch.setattr(check_dossiers, "get_pinned_dossier_revision", get_dossier_revision)


class TestLoadProjectDossiers:
    def test_empty_registry_is_valid_readiness(self) -> None:
        summaries, errors = scan_runner.load_project_dossiers({})
        assert summaries == {}
        assert errors == []

    def test_missing_dossier_root_is_a_single_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "SCOUT_DOSSIER_ROOT", "")
        summaries, errors = scan_runner.load_project_dossiers(
            {"anything": _project("anything", "x")}
        )
        assert summaries == {}
        assert any("SCOUT_DOSSIER_ROOT" in e for e in errors)

    def test_arbitrary_project_names_are_not_restricted_to_gateway_and_zk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _make_dossier_root(tmp_path, ["agent-evals", "agent-ops"])
        monkeypatch.setattr(config, "SCOUT_DOSSIER_ROOT", str(root))
        projects = {
            "agent-evals": _project("agent-evals", "agent-evals-dossier"),
            "agent-ops": _project("agent-ops", "agent-ops-dossier"),
        }
        summaries, errors = scan_runner.load_project_dossiers(projects)
        assert errors == []
        assert set(summaries) == {"agent-evals", "agent-ops"}

    def test_single_active_project_is_not_rejected_for_missing_a_peer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A registry with only one active project must not be rejected for
        not also containing some other specific project — activation is
        registry-controlled, not code-controlled."""
        root = _make_dossier_root(tmp_path, ["solo"])
        monkeypatch.setattr(config, "SCOUT_DOSSIER_ROOT", str(root))
        summaries, errors = scan_runner.load_project_dossiers(
            {"solo": _project("solo", "solo-dossier")}
        )
        assert errors == []
        assert set(summaries) == {"solo"}

    def test_each_project_is_checked_independently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two failing projects both surface their own error — one failure
        must not hide the other (S-008/S-011 fail-closed requirement)."""
        root = _make_dossier_root(tmp_path, ["ready"])
        monkeypatch.setattr(config, "SCOUT_DOSSIER_ROOT", str(root))
        projects = {
            "ready": _project("ready", "ready-dossier"),
            "missing-a": _project("missing-a", None),
            "missing-b": _project("missing-b", None),
        }
        summaries, errors = scan_runner.load_project_dossiers(projects)
        assert set(summaries) == {"ready"}
        assert len(errors) == 2
        assert any("missing-a" in e for e in errors)
        assert any("missing-b" in e for e in errors)


class TestRunPreflight:
    @pytest.mark.parametrize(("models", "expected_ok", "families"), [
        (("openrouter/google/gemini-2.5-flash", "openrouter/openai/gpt-5-mini",
          "openrouter/anthropic/claude-sonnet-4.6"), True, ["claude", "gemini", "gpt"]),
        (("openrouter/moonshotai/kimi-k2", "openrouter/qwen/qwen3-32b",
          "openrouter/anthropic/claude-sonnet-4.6"), True, ["claude", "kimi", "qwen"]),
        (("claude-sonnet-4-6", "openrouter/anthropic/claude-sonnet-4.6",
          "openrouter/anthropic/claude-opus-4.6"), False, ["claude"]),
        (("gpt-5-mini", "claude-sonnet-4-6", "dispatch/sonnet"),
         False, ["claude", "gpt"]),
    ])
    def test_model_identity_gate_is_read_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        models: tuple[str, str, str], expected_ok: bool, families: list[str],
    ) -> None:
        db_path = tmp_path / "scout.db"
        self._seed_db(db_path, [])
        before = db_path.read_bytes()
        for setting, model in zip(
            ("RELEVANCE_MODEL", "REPLY_DRAFT_MODEL", "CRITIC_MODEL"), models, strict=True,
        ):
            monkeypatch.setattr(scan_runner, setting, model)
        report = scan_runner.run_preflight(str(db_path), str(tmp_path / "dossier-source"))
        assert report["ok"] is expected_ok
        assert _details(report)["model_families"] == families
        assert db_path.read_bytes() == before

    @pytest.fixture(autouse=True)
    def _independent_model_families(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """run_preflight also gates on model-family independence; give it two
        distinct families so these tests isolate the registry/readiness
        behavior under test."""
        monkeypatch.setattr(scan_runner, "RELEVANCE_MODEL", "claude-x")
        monkeypatch.setattr(scan_runner, "REPLY_DRAFT_MODEL", "claude-x")
        monkeypatch.setattr(scan_runner, "CRITIC_MODEL", "gpt-x")

    def _seed_db(
        self, db_path: Path, projects: list[ProjectTarget], user_version: int = 18
    ) -> None:
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            """
            CREATE TABLE projects (
                key TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                link TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, dossier_summary_id TEXT
            );
            """
        )
        for p in projects:
            conn.execute(
                "INSERT INTO projects (key, name, description, link, active, "
                "created_at, updated_at, dossier_summary_id) "
                "VALUES (?, ?, ?, ?, 1, 'now', 'now', ?)",
                (p.key, p.name, p.description, p.link, p.dossier_summary_id),
            )
        conn.execute(f"PRAGMA user_version = {user_version}")
        conn.commit()
        conn.close()

    def test_empty_active_registry_is_ok(self, tmp_path: Path) -> None:
        db_path = tmp_path / "scout.db"
        self._seed_db(db_path, [])
        report = scan_runner.run_preflight(str(db_path), str(tmp_path / "dossier-source"))
        assert report["ok"] is True
        details = _details(report)
        assert details["active_projects"] == []
        assert "required_projects" not in details

    def test_reports_sorted_active_projects(self, tmp_path: Path) -> None:
        root = _make_dossier_root(tmp_path, ["zeta", "alpha"])
        db_path = tmp_path / "scout.db"
        self._seed_db(db_path, [
            _project("zeta", "zeta-dossier"),
            _project("alpha", "alpha-dossier"),
        ])
        report = scan_runner.run_preflight(str(db_path), str(root))
        details = _details(report)
        assert details["active_projects"] == ["alpha", "zeta"]
        assert report["ok"] is True
        assert details["dossier_revision"]

    def test_stale_database_version_is_reported(self, tmp_path: Path) -> None:
        db_path = tmp_path / "scout.db"
        self._seed_db(db_path, [], user_version=13)
        report = scan_runner.run_preflight(str(db_path), str(tmp_path / "dossier-source"))
        assert report["ok"] is False
        errors = _errors(report)
        assert any("user_version=13" in e for e in errors)
        assert any("18" in e for e in errors)

    def test_one_failing_project_does_not_hide_others(self, tmp_path: Path) -> None:
        root = _make_dossier_root(tmp_path, ["ready"])
        db_path = tmp_path / "scout.db"
        self._seed_db(db_path, [
            _project("ready", "ready-dossier"),
            _project("broken", None),
        ])
        report = scan_runner.run_preflight(str(db_path), str(root))
        assert report["ok"] is False
        assert any("broken" in e for e in _errors(report))
        assert _details(report)["active_projects"] == ["broken", "ready"]


class TestCheckDossiersScript:
    def _seed_db(self, db_path: Path, projects: list[ProjectTarget]) -> None:
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            """
            CREATE TABLE projects (
                key TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                link TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, dossier_summary_id TEXT
            );
            """
        )
        for p in projects:
            conn.execute(
                "INSERT INTO projects (key, name, description, link, active, "
                "created_at, updated_at, dossier_summary_id) "
                "VALUES (?, ?, ?, ?, 1, 'now', 'now', ?)",
                (p.key, p.name, p.description, p.link, p.dossier_summary_id),
            )
        conn.commit()
        conn.close()

    def test_no_active_projects_prints_zero_of_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = tmp_path / "scout.db"
        self._seed_db(db_path, [])
        exit_code = check_dossiers.check(
            str(db_path), str(tmp_path / "dossier-source"), min_entries=1, max_age_days=90
        )
        assert exit_code == 0
        assert "0/0 dossiers ready." in capsys.readouterr().out

    def test_arbitrary_project_names_are_checked(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = _make_dossier_root(tmp_path, ["agent-evals", "agent-ops"])
        db_path = tmp_path / "scout.db"
        self._seed_db(db_path, [
            _project("agent-evals", "agent-evals-dossier"),
            _project("agent-ops", "agent-ops-dossier"),
        ])
        exit_code = check_dossiers.check(
            str(db_path), str(root), min_entries=1, max_age_days=90
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "OK      agent-evals" in out
        assert "OK      agent-ops" in out
        assert "2/2 dossiers ready." in out
