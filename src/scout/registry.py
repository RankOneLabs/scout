"""Registry dataclasses for the SQLite-backed project/keyword/prompt data layer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ProjectTarget:
    key: str
    name: str
    description: str
    link: str
    dossier_summary_id: str | None = None


@dataclass(frozen=True, slots=True)
class KeywordRoute:
    id: int
    project_key: str
    keyword: str
    evaluate_prompt: str | None
    respond_prompt: str | None
    critique_prompt: str | None
    priority: int
    match_type: str = "substring"
    intent: str | None = None
    positive_context: tuple[str, ...] = field(default_factory=tuple)
    negative_context: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class RuntimeRegistry:
    projects: Mapping[str, ProjectTarget]
    keywords: tuple[KeywordRoute, ...]
    prompt_templates: Mapping[str, str]
