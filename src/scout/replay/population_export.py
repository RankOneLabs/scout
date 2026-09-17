"""Stable projections used by ``scout replay export-population``."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

from scout.config import HUMAN_GRADE_SCHEMA_VERSION
from scout.grading.relevance_targets import (
    derive_relevance_target,
    project_relevance_target_source,
)
from scout.grading.snapshots import FrozenGradeInput
from scout.result import Err
from scout.scanning.author_class import handle_from_post_url


class PopulationExportRecord(BaseModel):
    """Closed, deterministic JSON-lines contract for one evaluated post."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluation_id: int
    platform: str
    channel: str | None
    url: str | None
    text: str | None
    parent_author_name: str | None
    parent_text: str | None
    author_name: str | None
    author_handle: str | None
    snapshot_id: int | None
    human_label: bool | None
    production_score: float
    production_decision: bool


def record_from_frozen_input(item: FrozenGradeInput) -> PopulationExportRecord:
    """Project one eligible frozen grade input without performing I/O."""
    if item.post is None or item.evaluation is None:
        raise ValueError("population export requires a recorded post and evaluation")
    target = derive_relevance_target(project_relevance_target_source(item.grade))
    if isinstance(target, Err):
        raise ValueError(f"population export grade has no valid human label: {target.error.reason}")
    if target.value.evaluation_id != item.evaluation.id:
        raise ValueError("population export grade and evaluation identities differ")
    if item.evaluation.relevant not in (0, 1):
        raise ValueError("population export decision must be boolean")
    snapshot_id = max(
        (exposure.snapshot_id for exposure in item.exposures),
        default=None,
    )
    post = item.post
    return PopulationExportRecord(
        evaluation_id=item.evaluation.id,
        platform=post.platform,
        channel=post.channel_name,
        url=post.url,
        text=post.content,
        parent_author_name=post.parent_author_name,
        parent_text=post.parent_text,
        author_name=post.author_name,
        author_handle=handle_from_post_url(post.platform, post.url),
        snapshot_id=snapshot_id,
        human_label=target.value.is_relevant,
        production_score=item.evaluation.score,
        production_decision=bool(item.evaluation.relevant),
    )


def records_from_frozen_inputs(
    items: Iterable[FrozenGradeInput],
) -> tuple[PopulationExportRecord, ...]:
    """Project and order a frozen population by evaluation identity."""
    return tuple(sorted(map(record_from_frozen_input, items), key=lambda row: row.evaluation_id))


def load_live_population(
    conn: sqlite3.Connection,
    project_key: str,
) -> tuple[PopulationExportRecord, ...]:
    """Read all stored project evaluations with their posts and finalized labels."""
    rows = conn.execute(
        "SELECT e.id AS evaluation_id, p.platform, p.channel_name AS channel, "
        "p.url, p.content AS text, p.parent_author_name, p.parent_text, p.author_name, "
        "e.score AS production_score, e.relevant AS production_decision, "
        "CASE WHEN g.schema_version = ? AND g.needs_regrade = 0 THEN "
        "CASE g.relevance_judgment "
        "WHEN 'correct' THEN e.relevant "
        "WHEN 'false_positive' THEN 0 "
        "WHEN 'false_negative' THEN 1 END END AS human_label "
        "FROM evaluations e JOIN posts p ON p.id = e.post_id "
        "LEFT JOIN grades g ON g.evaluation_id = e.id "
        "WHERE e.project_key = ? ORDER BY e.id",
        (HUMAN_GRADE_SCHEMA_VERSION, project_key),
    ).fetchall()
    return tuple(
        PopulationExportRecord(
            evaluation_id=row["evaluation_id"],
            platform=row["platform"],
            channel=row["channel"],
            url=row["url"],
            text=row["text"],
            parent_author_name=row["parent_author_name"],
            parent_text=row["parent_text"],
            author_name=row["author_name"],
            author_handle=handle_from_post_url(row["platform"], row["url"]),
            snapshot_id=None,
            human_label=(None if row["human_label"] is None else bool(row["human_label"])),
            production_score=row["production_score"],
            production_decision=bool(row["production_decision"]),
        )
        for row in rows
    )


def render_population_jsonl(records: Iterable[PopulationExportRecord]) -> bytes:
    """Render canonical UTF-8 JSON lines in evaluation order."""
    ordered = sorted(records, key=lambda row: row.evaluation_id)
    return b"".join(record.model_dump_json().encode("utf-8") + b"\n" for record in ordered)


__all__ = [
    "PopulationExportRecord",
    "load_live_population",
    "record_from_frozen_input",
    "records_from_frozen_inputs",
    "render_population_jsonl",
]
