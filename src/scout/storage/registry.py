"""Registry aggregate: projects, their keyword routes, and prompt
templates — the runtime configuration that drives scan routing. Owns
`projects`, `project_keywords`, and `prompt_templates`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from scout.registry import KeywordRoute, ProjectTarget, RuntimeRegistry
from scout.storage.unit_of_work import UnitOfWork

SUPPORTED_KEYWORD_MATCH_TYPES = frozenset({"substring", "phrase", "exact", "regex"})


def _normalize_match_type(match_type: str | None) -> str:
    value = (match_type or "").strip().casefold()
    if value in SUPPORTED_KEYWORD_MATCH_TYPES:
        return value
    return "substring"


def _serialize_keyword_context(
    value: str | tuple[str, ...] | list[str] | None,
) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(list(value))


def _parse_keyword_context(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    text = value.strip()
    if not text:
        return ()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        items = []
        for item in parsed:
            if isinstance(item, str):
                stripped = item.strip()
                if stripped:
                    items.append(stripped)
        return tuple(items)
    return tuple(line.strip() for line in text.splitlines() if line.strip())


@dataclass(frozen=True, slots=True)
class ProjectRow:
    """One `projects` row."""

    key: str
    name: str
    description: str
    link: str
    active: bool
    created_at: str
    updated_at: str
    dossier_summary_id: str | None


@dataclass(frozen=True, slots=True)
class KeywordRow:
    """One `project_keywords` row."""

    id: int
    project_key: str
    keyword: str
    evaluate_prompt: str | None
    respond_prompt: str | None
    critique_prompt: str | None
    match_type: str
    intent: str | None
    positive_context: str | None
    negative_context: str | None
    notes: str | None
    active: bool
    priority: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class PromptTemplateRow:
    """One `prompt_templates` row."""

    name: str
    body: str
    kind: str
    active: bool
    created_at: str
    updated_at: str


class RegistryStore:
    """Owns projects, keyword routes, and prompt templates."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    @property
    def _conn(self) -> sqlite3.Connection:
        return self._uow.conn

    def list_projects(self, include_inactive: bool = False) -> list[ProjectRow]:
        """Return all projects, optionally including inactive ones."""
        if include_inactive:
            rows = self._conn.execute("SELECT * FROM projects ORDER BY key").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM projects WHERE active = 1 ORDER BY key"
            ).fetchall()
        return [
            ProjectRow(
                key=row["key"],
                name=row["name"],
                description=row["description"],
                link=row["link"],
                active=bool(row["active"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                dossier_summary_id=row["dossier_summary_id"],
            )
            for row in rows
        ]

    def upsert_project(
        self,
        key: str,
        name: str,
        description: str,
        link: str,
        active: bool = True,
        dossier_summary_id: str | None = None,
    ) -> None:
        """Insert or update a project row, stamping updated_at on every write."""
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            self._conn.execute(
                "INSERT INTO projects "
                "(key, name, description, link, active, dossier_summary_id, created_at, "
                "updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "name = excluded.name, "
                "description = excluded.description, "
                "link = excluded.link, "
                "active = excluded.active, "
                "dossier_summary_id = excluded.dossier_summary_id, "
                "updated_at = excluded.updated_at",
                (key, name, description, link, int(active), dossier_summary_id, now, now),
            )

    def set_project_active(self, key: str, active: bool) -> None:
        """Enable or disable a project by key."""
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            self._conn.execute(
                "UPDATE projects SET active = ?, updated_at = ? WHERE key = ?",
                (int(active), now, key),
            )

    def list_keywords(
        self,
        project_key: str | None = None,
        include_inactive: bool = False,
    ) -> list[KeywordRow]:
        """Return keyword rows ordered by priority ASC, length(keyword) DESC, id ASC."""
        clauses: list[str] = []
        params: list[object] = []
        if not include_inactive:
            clauses.append("active = 1")
        if project_key is not None:
            clauses.append("project_key = ?")
            params.append(project_key)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM project_keywords {where} "
            "ORDER BY priority ASC, length(keyword) DESC, id ASC",
            params,
        ).fetchall()
        return [
            KeywordRow(
                id=row["id"],
                project_key=row["project_key"],
                keyword=row["keyword"],
                evaluate_prompt=row["evaluate_prompt"],
                respond_prompt=row["respond_prompt"],
                critique_prompt=row["critique_prompt"],
                match_type=row["match_type"],
                intent=row["intent"],
                positive_context=row["positive_context"],
                negative_context=row["negative_context"],
                notes=row["notes"],
                active=bool(row["active"]),
                priority=row["priority"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def upsert_keyword(
        self,
        project_key: str,
        keyword: str,
        evaluate_prompt: str | None = None,
        respond_prompt: str | None = None,
        critique_prompt: str | None = None,
        priority: int = 100,
        active: bool = True,
        match_type: str | None = "substring",
        intent: str | None = None,
        positive_context: str | tuple[str, ...] | list[str] | None = None,
        negative_context: str | tuple[str, ...] | list[str] | None = None,
        notes: str | None = None,
    ) -> None:
        """Insert or update a keyword row, stamping updated_at on every write."""
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            self._conn.execute(
                "INSERT INTO project_keywords "
                "(project_key, keyword, evaluate_prompt, respond_prompt, critique_prompt, "
                "match_type, intent, positive_context, negative_context, notes, "
                "priority, active, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(project_key, keyword) DO UPDATE SET "
                "evaluate_prompt = excluded.evaluate_prompt, "
                "respond_prompt = excluded.respond_prompt, "
                "critique_prompt = excluded.critique_prompt, "
                "match_type = excluded.match_type, "
                "intent = excluded.intent, "
                "positive_context = excluded.positive_context, "
                "negative_context = excluded.negative_context, "
                "notes = excluded.notes, "
                "priority = excluded.priority, "
                "active = excluded.active, "
                "updated_at = excluded.updated_at",
                (
                    project_key,
                    keyword,
                    evaluate_prompt,
                    respond_prompt,
                    critique_prompt,
                    _normalize_match_type(match_type),
                    intent,
                    _serialize_keyword_context(positive_context),
                    _serialize_keyword_context(negative_context),
                    notes,
                    priority,
                    int(active),
                    now,
                    now,
                ),
            )

    def set_keyword_active(self, keyword_id: int, active: bool) -> None:
        """Enable or disable a keyword row by id."""
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            self._conn.execute(
                "UPDATE project_keywords SET active = ?, updated_at = ? WHERE id = ?",
                (int(active), now, keyword_id),
            )

    def list_prompt_templates(self, include_inactive: bool = False) -> list[PromptTemplateRow]:
        """Return prompt template rows, optionally including inactive ones."""
        if include_inactive:
            rows = self._conn.execute(
                "SELECT * FROM prompt_templates ORDER BY name"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM prompt_templates WHERE active = 1 ORDER BY name"
            ).fetchall()
        return [
            PromptTemplateRow(
                name=row["name"],
                body=row["body"],
                kind=row["kind"],
                active=bool(row["active"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def upsert_prompt_template(
        self,
        name: str,
        body: str,
        kind: str,
        active: bool = True,
    ) -> None:
        """Insert or update a prompt template, stamping updated_at on every write."""
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            self._conn.execute(
                "INSERT INTO prompt_templates (name, body, kind, active, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "body = excluded.body, "
                "kind = excluded.kind, "
                "active = excluded.active, "
                "updated_at = excluded.updated_at",
                (name, body, kind, int(active), now, now),
            )

    def set_prompt_template_active(self, name: str, active: bool) -> None:
        """Enable or disable a prompt template by name."""
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            self._conn.execute(
                "UPDATE prompt_templates SET active = ?, updated_at = ? WHERE name = ?",
                (int(active), now, name),
            )

    def load_runtime_registry(self) -> RuntimeRegistry:
        """Return active projects, keywords, and prompt templates as a RuntimeRegistry.

        Keywords are pre-sorted (priority ASC, length(keyword) DESC, id ASC) so
        cohort 2's matcher can use first-match-wins without re-sorting. Keywords
        whose owning project is inactive are excluded.
        """
        project_rows = self._conn.execute(
            "SELECT key, name, description, link, dossier_summary_id"
            " FROM projects WHERE active = 1 ORDER BY key"
        ).fetchall()
        projects: dict[str, ProjectTarget] = {
            row["key"]: ProjectTarget(
                key=row["key"],
                name=row["name"],
                description=row["description"],
                link=row["link"],
                dossier_summary_id=row["dossier_summary_id"],
            )
            for row in project_rows
        }

        keyword_rows = self._conn.execute(
            "SELECT pk.id, pk.project_key, pk.keyword, "
            "pk.evaluate_prompt, pk.respond_prompt, pk.critique_prompt, "
            "pk.match_type, pk.intent, pk.positive_context, pk.negative_context, "
            "pk.priority "
            "FROM project_keywords pk "
            "JOIN projects p ON p.key = pk.project_key "
            "WHERE pk.active = 1 AND p.active = 1 "
            "ORDER BY pk.priority ASC, length(pk.keyword) DESC, pk.id ASC"
        ).fetchall()
        keywords = tuple(
            KeywordRoute(
                id=row["id"],
                project_key=row["project_key"],
                keyword=row["keyword"],
                evaluate_prompt=row["evaluate_prompt"],
                respond_prompt=row["respond_prompt"],
                critique_prompt=row["critique_prompt"],
                priority=row["priority"],
                match_type=_normalize_match_type(row["match_type"]),
                intent=row["intent"],
                positive_context=_parse_keyword_context(row["positive_context"]),
                negative_context=_parse_keyword_context(row["negative_context"]),
            )
            for row in keyword_rows
        )

        template_rows = self._conn.execute(
            "SELECT name, body FROM prompt_templates WHERE active = 1 ORDER BY name"
        ).fetchall()
        prompt_templates: dict[str, str] = {
            row["name"]: row["body"] for row in template_rows
        }

        return RuntimeRegistry(
            projects=projects,
            keywords=keywords,
            prompt_templates=prompt_templates,
        )
