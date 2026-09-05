"""Tests for schema v6 registry tables and StateManager registry methods."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator

import pytest

from scout.registry import KeywordRoute, ProjectTarget, RuntimeRegistry
from scout.storage.state import LATEST_SCHEMA_VERSION, StateManager


def _sm() -> Generator[StateManager, None, None]:
    state = StateManager(db_path=":memory:")
    yield state
    # Mirror how production callers use StateManager: issue writes, then
    # commit once. Db.close() refuses to close over an unmanaged pending
    # transaction (e.g. left behind by a test that exercised a failed
    # write and never explicitly committed or rolled back).
    state.commit()
    state.close()


sm = pytest.fixture(_sm)


class TestSchemaVersion:
    def test_latest_schema_version_is_38(self) -> None:
        assert LATEST_SCHEMA_VERSION == 38

    def test_fresh_db_stamped_at_latest_version(self, sm: StateManager) -> None:
        version = int(sm.conn.execute("PRAGMA user_version").fetchone()[0])
        assert version == LATEST_SCHEMA_VERSION

    # The full SCHEMA-vs-legacy-upgrade convergence check
    # (columns, types, defaults, foreign keys, indexes, CHECK constraints,
    # and PRAGMA user_version) lives in
    # tests/test_state_manager.py::TestSchemaConvergence, where the
    # oldest-supported legacy fixture (LEGACY_SCHEMA) and MIGRATIONS are
    # already available.

    def test_v5_tables_exist(self, sm: StateManager) -> None:
        tables = {
            row["name"]
            for row in sm.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "projects" in tables
        assert "project_keywords" in tables
        assert "prompt_templates" in tables

    def test_v5_indexes_exist(self, sm: StateManager) -> None:
        indexes = {
            row["name"]
            for row in sm.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "project_keywords_active_idx" in indexes
        assert "project_keywords_project_idx" in indexes

    def test_project_keywords_have_metadata_columns(self, sm: StateManager) -> None:
        cols = {
            row["name"]
            for row in sm.conn.execute("PRAGMA table_info(project_keywords)").fetchall()
        }
        assert {
            "match_type",
            "intent",
            "positive_context",
            "negative_context",
            "notes",
        }.issubset(cols)

    def test_migrate_to_5_idempotent(self, sm: StateManager) -> None:
        from scout.storage.migrations import _migrate_to_5

        _migrate_to_5(sm.conn)
        tables = {
            row["name"]
            for row in sm.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "projects" in tables
        assert "project_keywords" in tables
        assert "prompt_templates" in tables


class TestProjectCRUD:
    def test_upsert_and_list_project(self, sm: StateManager) -> None:
        sm.upsert_project(
            key="gateway", name="Gateway", description="Bot detection", link="https://example.com"
        )
        sm.commit()
        rows = sm.list_projects()
        assert len(rows) == 1
        assert rows[0]["key"] == "gateway"
        assert rows[0]["name"] == "Gateway"

    def test_upsert_is_idempotent(self, sm: StateManager) -> None:
        sm.upsert_project(key="gw", name="Old", description="d", link="l")
        sm.upsert_project(key="gw", name="New", description="d2", link="l2")
        sm.commit()
        rows = sm.list_projects()
        assert len(rows) == 1
        assert rows[0]["name"] == "New"

    def test_upsert_stamps_timestamps(self, sm: StateManager) -> None:
        sm.upsert_project(key="gw", name="GW", description="d", link="l")
        sm.commit()
        row = sm.list_projects()[0]
        assert row["created_at"] is not None
        assert row["updated_at"] is not None

    def test_set_project_active_false(self, sm: StateManager) -> None:
        sm.upsert_project(key="gw", name="GW", description="d", link="l")
        sm.commit()
        sm.set_project_active("gw", False)
        sm.commit()
        assert sm.list_projects() == []
        assert len(sm.list_projects(include_inactive=True)) == 1

    def test_list_projects_include_inactive(self, sm: StateManager) -> None:
        sm.upsert_project(key="a", name="A", description="d", link="l", active=False)
        sm.commit()
        assert sm.list_projects() == []
        assert len(sm.list_projects(include_inactive=True)) == 1


class TestKeywordCRUD:
    def _setup_project(self, sm: StateManager) -> None:
        sm.upsert_project(key="gw", name="GW", description="d", link="l")
        sm.commit()

    def test_upsert_and_list_keyword(self, sm: StateManager) -> None:
        self._setup_project(sm)
        sm.upsert_keyword(project_key="gw", keyword="bot detection")
        sm.commit()
        rows = sm.list_keywords()
        assert len(rows) == 1
        assert rows[0]["keyword"] == "bot detection"

    def test_upsert_keyword_idempotent(self, sm: StateManager) -> None:
        self._setup_project(sm)
        sm.upsert_keyword(project_key="gw", keyword="bots", priority=50)
        sm.upsert_keyword(project_key="gw", keyword="bots", priority=10)
        sm.commit()
        rows = sm.list_keywords(include_inactive=True)
        assert len(rows) == 1
        assert rows[0]["priority"] == 10

    def test_upsert_keyword_with_prompts(self, sm: StateManager) -> None:
        self._setup_project(sm)
        sm.upsert_keyword(
            project_key="gw",
            keyword="fraud",
            evaluate_prompt="eval text",
            respond_prompt="respond text",
            critique_prompt="critique text",
        )
        sm.commit()
        rows = sm.list_keywords()
        assert rows[0]["evaluate_prompt"] == "eval text"
        assert rows[0]["respond_prompt"] == "respond text"
        assert rows[0]["critique_prompt"] == "critique text"

    def test_upsert_keyword_with_metadata(self, sm: StateManager) -> None:
        self._setup_project(sm)
        sm.upsert_keyword(
            project_key="gw",
            keyword="abuse",
            match_type="Regex",
            intent="reduce spam",
            positive_context=("trusted user", "clear threat"),
            negative_context="never\nblank",
            notes="internal only",
        )
        sm.commit()
        row = sm.list_keywords()[0]
        assert row["match_type"] == "regex"
        assert row["intent"] == "reduce spam"
        assert row["positive_context"] == '["trusted user", "clear threat"]'
        assert row["negative_context"] == "never\nblank"
        assert row["notes"] == "internal only"

    def test_upsert_keyword_with_unknown_match_type_defaults_to_substring(
        self, sm: StateManager
    ) -> None:
        self._setup_project(sm)
        sm.upsert_keyword(project_key="gw", keyword="abuse", match_type="prefix")
        sm.commit()
        row = sm.list_keywords()[0]
        assert row["match_type"] == "substring"

    def test_set_keyword_active_false(self, sm: StateManager) -> None:
        self._setup_project(sm)
        sm.upsert_keyword(project_key="gw", keyword="bots")
        sm.commit()
        kw_id = sm.list_keywords()[0]["id"]
        sm.set_keyword_active(kw_id, False)
        sm.commit()
        assert sm.list_keywords() == []
        assert len(sm.list_keywords(include_inactive=True)) == 1

    def test_list_keywords_by_project(self, sm: StateManager) -> None:
        sm.upsert_project(key="a", name="A", description="d", link="l")
        sm.upsert_project(key="b", name="B", description="d", link="l")
        sm.commit()
        sm.upsert_keyword(project_key="a", keyword="alpha")
        sm.upsert_keyword(project_key="b", keyword="beta")
        sm.commit()
        a_rows = sm.list_keywords(project_key="a")
        assert len(a_rows) == 1
        assert a_rows[0]["keyword"] == "alpha"

    def test_list_keywords_sort_order(self, sm: StateManager) -> None:
        self._setup_project(sm)
        sm.upsert_keyword(project_key="gw", keyword="ab", priority=50)
        sm.upsert_keyword(project_key="gw", keyword="longer_kw", priority=50)
        sm.upsert_keyword(project_key="gw", keyword="z", priority=10)
        sm.commit()
        rows = sm.list_keywords()
        # priority 10 first, then priority 50 sorted by length DESC
        assert rows[0]["keyword"] == "z"
        assert rows[1]["keyword"] == "longer_kw"
        assert rows[2]["keyword"] == "ab"


class TestPromptTemplateCRUD:
    def test_upsert_and_list(self, sm: StateManager) -> None:
        sm.upsert_prompt_template(name="lead_gen", body="You are...", kind="evaluate")
        sm.commit()
        rows = sm.list_prompt_templates()
        assert len(rows) == 1
        assert rows[0]["name"] == "lead_gen"
        assert rows[0]["kind"] == "evaluate"

    def test_upsert_idempotent(self, sm: StateManager) -> None:
        sm.upsert_prompt_template(name="tmpl", body="v1", kind="shared")
        sm.upsert_prompt_template(name="tmpl", body="v2", kind="custom")
        sm.commit()
        rows = sm.list_prompt_templates()
        assert len(rows) == 1
        assert rows[0]["body"] == "v2"
        assert rows[0]["kind"] == "custom"

    def test_kind_check_constraint(self, sm: StateManager) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            sm.upsert_prompt_template(name="bad", body="x", kind="invalid_kind")
            sm.commit()

    def test_set_template_active_false(self, sm: StateManager) -> None:
        sm.upsert_prompt_template(name="tmpl", body="x", kind="shared")
        sm.commit()
        sm.set_prompt_template_active("tmpl", False)
        sm.commit()
        assert sm.list_prompt_templates() == []
        assert len(sm.list_prompt_templates(include_inactive=True)) == 1

    def test_list_include_inactive(self, sm: StateManager) -> None:
        sm.upsert_prompt_template(name="t", body="b", kind="custom", active=False)
        sm.commit()
        assert sm.list_prompt_templates() == []
        rows = sm.list_prompt_templates(include_inactive=True)
        assert len(rows) == 1


class TestLoadRuntimeRegistry:
    def _populate(self, sm: StateManager) -> None:
        sm.upsert_project(key="gw", name="Gateway", description="Bot det", link="https://gw.com")
        sm.upsert_project(
            key="off", name="Offline", description="Inactive", link="https://off.com", active=False
        )
        sm.commit()
        sm.upsert_keyword(project_key="gw", keyword="bot detection", priority=50)
        sm.upsert_keyword(project_key="gw", keyword="fraud", priority=10)
        sm.upsert_keyword(project_key="off", keyword="offline kw")
        sm.commit()
        sm.upsert_prompt_template(name="lead_gen", body="Eval prompt", kind="evaluate")
        sm.upsert_prompt_template(name="hidden", body="Nope", kind="custom", active=False)
        sm.commit()

    def test_returns_runtime_registry(self, sm: StateManager) -> None:
        self._populate(sm)
        reg = sm.load_runtime_registry()
        assert isinstance(reg, RuntimeRegistry)

    def test_only_active_projects(self, sm: StateManager) -> None:
        self._populate(sm)
        reg = sm.load_runtime_registry()
        assert "gw" in reg.projects
        assert "off" not in reg.projects

    def test_projects_are_project_targets(self, sm: StateManager) -> None:
        self._populate(sm)
        reg = sm.load_runtime_registry()
        pt = reg.projects["gw"]
        assert isinstance(pt, ProjectTarget)
        assert pt.key == "gw"
        assert pt.name == "Gateway"
        assert pt.link == "https://gw.com"

    def test_keywords_exclude_inactive_project(self, sm: StateManager) -> None:
        self._populate(sm)
        reg = sm.load_runtime_registry()
        project_keys = {kw.project_key for kw in reg.keywords}
        assert "off" not in project_keys

    def test_keywords_sorted_by_priority_then_length(self, sm: StateManager) -> None:
        self._populate(sm)
        reg = sm.load_runtime_registry()
        gw_kws = [kw for kw in reg.keywords if kw.project_key == "gw"]
        # priority 10 (fraud) before priority 50 (bot detection)
        assert gw_kws[0].keyword == "fraud"
        assert gw_kws[1].keyword == "bot detection"

    def test_keywords_are_keyword_routes(self, sm: StateManager) -> None:
        self._populate(sm)
        reg = sm.load_runtime_registry()
        assert all(isinstance(kw, KeywordRoute) for kw in reg.keywords)

    def test_keywords_is_tuple(self, sm: StateManager) -> None:
        self._populate(sm)
        reg = sm.load_runtime_registry()
        assert isinstance(reg.keywords, tuple)

    def test_only_active_prompt_templates(self, sm: StateManager) -> None:
        self._populate(sm)
        reg = sm.load_runtime_registry()
        assert "lead_gen" in reg.prompt_templates
        assert "hidden" not in reg.prompt_templates

    def test_prompt_templates_are_body_strings(self, sm: StateManager) -> None:
        self._populate(sm)
        reg = sm.load_runtime_registry()
        assert reg.prompt_templates["lead_gen"] == "Eval prompt"

    def test_empty_registry_on_fresh_db(self, sm: StateManager) -> None:
        reg = sm.load_runtime_registry()
        assert reg.projects == {}
        assert reg.keywords == ()
        assert reg.prompt_templates == {}

    def test_inactive_keyword_excluded(self, sm: StateManager) -> None:
        sm.upsert_project(key="p", name="P", description="d", link="l")
        sm.commit()
        sm.upsert_keyword(project_key="p", keyword="active_kw")
        sm.upsert_keyword(project_key="p", keyword="dead_kw")
        sm.commit()
        dead_id = sm.list_keywords(project_key="p")[-1]["id"]
        sm.set_keyword_active(dead_id, False)
        sm.commit()
        reg = sm.load_runtime_registry()
        kw_names = {kw.keyword for kw in reg.keywords}
        assert "active_kw" in kw_names
        assert "dead_kw" not in kw_names

    def test_keywords_parse_runtime_metadata(self, sm: StateManager) -> None:
        sm.upsert_project(key="p", name="P", description="d", link="l")
        sm.commit()
        now = "2026-06-08T00:00:00+00:00"
        sm.conn.execute(
            """
            INSERT INTO project_keywords (
                project_key, keyword, evaluate_prompt, respond_prompt,
                critique_prompt, match_type, intent, positive_context,
                negative_context, notes, active, priority, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "p",
                "meta",
                None,
                None,
                None,
                "",
                "reduce abuse",
                '["trusted", "recently active"]',
                "avoid spam\navoid harassment",
                "internal note",
                1,
                10,
                now,
                now,
            ),
        )
        sm.commit()
        reg = sm.load_runtime_registry()
        kw = reg.keywords[0]
        assert kw.match_type == "substring"
        assert kw.intent == "reduce abuse"
        assert kw.positive_context == ("trusted", "recently active")
        assert kw.negative_context == ("avoid spam", "avoid harassment")
        assert not hasattr(kw, "notes")
