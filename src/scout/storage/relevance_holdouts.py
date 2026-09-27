"""Typed persistence for held relevance decisions and their lifecycle."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal

from scout.config import RelevanceResult
from scout.registry import RuntimeRegistry
from scout.relevance.models import RelevanceAction
from scout.storage.evaluations import EvaluationStore
from scout.storage.unit_of_work import UnitOfWork

HoldoutStatus = Literal["pending", "claimed", "released", "failed"]


@dataclass(frozen=True, slots=True)
class RelevanceHoldout:
    id: int
    evaluation_id: int
    production_action: RelevanceAction
    held: bool
    batch_id: str | None
    exported_at: str | None
    dossier_revision: str | None
    registry_state: dict[str, object]
    status: HoldoutStatus
    claim_token: str | None
    claim_fence: int
    claim_owner: str | None
    claim_expires_at: str | None
    release_action: RelevanceAction | None
    target_evaluation_id: int | None
    released_at: str | None
    attempts: int
    last_error: str | None
    created_at: str


def _registry_state(registry: RuntimeRegistry) -> dict[str, object]:
    return {
        "projects": {key: asdict(project) for key, project in sorted(registry.projects.items())},
        "keywords": [asdict(route) for route in registry.keywords],
        "prompt_templates": dict(sorted(registry.prompt_templates.items())),
    }


def _row(row: sqlite3.Row) -> RelevanceHoldout:
    data = dict(row)
    data["held"] = bool(data["held"])
    data["registry_state"] = json.loads(data["registry_state"])
    return RelevanceHoldout(**data)


def load_held_evaluation_ids(conn: sqlite3.Connection) -> frozenset[int]:
    """Read held evaluation identities for consumers with a pinned connection."""
    return frozenset(
        int(row["evaluation_id"])
        for row in conn.execute(
            "SELECT evaluation_id FROM relevance_holdouts WHERE held = 1"
        ).fetchall()
    )


class RelevanceHoldoutStore:
    def __init__(self, uow: UnitOfWork, *, evaluations: EvaluationStore) -> None:
        self._uow = uow
        self._evaluations = evaluations

    def record_holdout(
        self,
        *,
        evaluation_id: int,
        production_action: RelevanceAction,
        registry: RuntimeRegistry,
        dossier_revision: str | None,
    ) -> RelevanceHoldout:
        """Insert the held marker. The caller may compose this in a larger transaction."""
        registry_json = json.dumps(_registry_state(registry), sort_keys=True, separators=(",", ":"))
        created_at = datetime.now(UTC).isoformat()
        with self._uow.begin():
            cursor = self._uow.conn.execute(
                "INSERT INTO relevance_holdouts "
                "(evaluation_id, production_action, held, dossier_revision, "
                "registry_state, created_at) VALUES (?, ?, 1, ?, ?, ?)",
                (
                    evaluation_id,
                    production_action,
                    dossier_revision,
                    registry_json,
                    created_at,
                ),
            )
            holdout_id = cursor.lastrowid
        assert holdout_id is not None
        stored = self.get(holdout_id)
        assert stored is not None
        return stored

    def persist_held_evaluation(
        self,
        result: RelevanceResult,
        post_id: int,
        scan_id: int,
        *,
        production_action: RelevanceAction,
        registry: RuntimeRegistry,
        contributor_phase_run_ids: tuple[int, ...],
        keyword_route_id: int | None,
        project_key: str | None,
        dossier_revision: str | None,
        dossier_summary_id: str | None,
    ) -> tuple[int, RelevanceHoldout]:
        """Atomically write the evaluation and the marker that withholds its action."""
        with self._uow.begin_immediate():
            evaluation_id = self._evaluations.persist_terminal_outcome(
                result,
                post_id,
                scan_id,
                surface_status="not_relevant",
                contributor_phase_run_ids=contributor_phase_run_ids,
                keyword_route_id=keyword_route_id,
                project_key=project_key,
                dossier_revision=dossier_revision,
                dossier_summary_id=dossier_summary_id,
            )
            holdout = self.record_holdout(
                evaluation_id=evaluation_id,
                production_action=production_action,
                registry=registry,
                dossier_revision=dossier_revision,
            )
        return evaluation_id, holdout

    def get(self, holdout_id: int) -> RelevanceHoldout | None:
        row = self._uow.conn.execute(
            "SELECT * FROM relevance_holdouts WHERE id = ?", (holdout_id,)
        ).fetchone()
        return None if row is None else _row(row)

    def get_for_evaluation(self, evaluation_id: int) -> RelevanceHoldout | None:
        row = self._uow.conn.execute(
            "SELECT * FROM relevance_holdouts WHERE evaluation_id = ?",
            (evaluation_id,),
        ).fetchone()
        return None if row is None else _row(row)

    def rows_for_export(self, *, batch_id: str | None) -> list[sqlite3.Row]:
        """Load held rows and their population fields under the caller's snapshot."""
        predicate = "h.batch_id IS NULL" if batch_id is None else "h.batch_id = ?"
        parameters: tuple[object, ...] = () if batch_id is None else (batch_id,)
        return self._uow.conn.execute(
            "SELECT h.id, h.evaluation_id, h.production_action, h.batch_id, "
            "h.exported_at, e.project_key, e.score AS production_score, "
            "e.relevant AS production_decision, p.platform, "
            "p.channel_name AS channel, p.url, p.content AS text, "
            "p.parent_author_name, p.parent_text, p.author_name "
            "FROM relevance_holdouts h "
            "JOIN evaluations e ON e.id = h.evaluation_id "
            "JOIN posts p ON p.id = e.post_id "
            f"WHERE h.held = 1 AND {predicate} ORDER BY e.project_key, e.id",
            parameters,
        ).fetchall()

    def assign_export_batch(
        self,
        holdout_ids: tuple[int, ...],
        *,
        batch_id: str,
        exported_at: str,
    ) -> None:
        if not holdout_ids:
            raise ValueError("cannot assign an empty holdout batch")
        placeholders = ",".join("?" for _ in holdout_ids)
        cursor = self._uow.conn.execute(
            f"UPDATE relevance_holdouts SET batch_id = ?, exported_at = ? "
            f"WHERE id IN ({placeholders}) AND batch_id IS NULL AND held = 1",
            (batch_id, exported_at, *holdout_ids),
        )
        if cursor.rowcount != len(holdout_ids):
            raise RuntimeError("holdout export batch changed while it was being written")


__all__ = [
    "HoldoutStatus",
    "RelevanceAction",
    "RelevanceHoldout",
    "RelevanceHoldoutStore",
    "load_held_evaluation_ids",
]
