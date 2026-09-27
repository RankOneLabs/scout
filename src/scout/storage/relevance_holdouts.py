"""Typed persistence for held relevance decisions and their lifecycle."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
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
    release_provenance: dict[str, object] | None = None


def registry_state(registry: RuntimeRegistry) -> dict[str, object]:
    return {
        "projects": {key: asdict(project) for key, project in sorted(registry.projects.items())},
        "keywords": [asdict(route) for route in registry.keywords],
        "prompt_templates": dict(sorted(registry.prompt_templates.items())),
    }


def _row(row: sqlite3.Row) -> RelevanceHoldout:
    data = dict(row)
    data["held"] = bool(data["held"])
    registry_document = json.loads(data["registry_state"])
    release_provenance = registry_document.pop("_holdout_release_provenance", None)
    data["registry_state"] = registry_document
    data["release_provenance"] = (
        release_provenance if isinstance(release_provenance, dict) else None
    )
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
        registry_json = json.dumps(registry_state(registry), sort_keys=True, separators=(",", ":"))
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

    def list_batch(self, batch_id: str) -> tuple[RelevanceHoldout, ...]:
        rows = self._uow.conn.execute(
            "SELECT * FROM relevance_holdouts WHERE batch_id = ? ORDER BY id",
            (batch_id,),
        ).fetchall()
        return tuple(_row(row) for row in rows)

    def claim_release(
        self,
        holdout_id: int,
        *,
        owner: str,
        now: datetime | None = None,
        lease_seconds: int = 600,
    ) -> RelevanceHoldout:
        """Claim one exported holdout; failed and expired claims are retryable."""
        claimed_at = now or datetime.now(UTC)
        expires_at = claimed_at + timedelta(seconds=lease_seconds)
        token = str(uuid.uuid4())
        with self._uow.begin_immediate():
            existing = self._uow.conn.execute(
                "SELECT * FROM relevance_holdouts WHERE id = ?", (holdout_id,)
            ).fetchone()
            if existing is None:
                raise ValueError(f"holdout {holdout_id} not found")
            if existing["batch_id"] is None:
                raise ValueError(f"holdout {holdout_id} has not been exported")
            if existing["status"] == "released":
                return _row(existing)
            if existing["status"] == "claimed" and existing["claim_expires_at"]:
                try:
                    current_expiry = datetime.fromisoformat(existing["claim_expires_at"])
                except (TypeError, ValueError):
                    current_expiry = claimed_at
                if current_expiry > claimed_at:
                    raise RuntimeError(f"holdout {holdout_id} is already claimed")
            cursor = self._uow.conn.execute(
                "UPDATE relevance_holdouts SET status = 'claimed', claim_token = ?, "
                "claim_fence = claim_fence + 1, claim_owner = ?, claim_expires_at = ?, "
                "attempts = attempts + 1, last_error = NULL WHERE id = ? "
                "AND status IN ('pending', 'failed', 'claimed')",
                (token, owner, expires_at.isoformat(), holdout_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"holdout {holdout_id} could not be claimed")
            claimed = self._uow.conn.execute(
                "SELECT * FROM relevance_holdouts WHERE id = ?", (holdout_id,)
            ).fetchone()
        assert claimed is not None
        return _row(claimed)

    def attach_release_scan(self, holdout_id: int, *, claim_token: str, scan_id: int) -> None:
        with self._uow.begin_immediate():
            cursor = self._uow.conn.execute(
                "UPDATE relevance_holdouts SET claim_owner = ? WHERE id = ? "
                "AND status = 'claimed' AND claim_token = ?",
                (f"scan:{scan_id}", holdout_id, claim_token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"holdout {holdout_id} lost its release claim")

    def complete_release(
        self,
        holdout_id: int,
        *,
        claim_token: str,
        action: RelevanceAction,
        target_evaluation_id: int,
        released_at: str,
        provenance: Mapping[str, object],
    ) -> None:
        """Fence completion to the current claim for atomic outcome composition."""
        with self._uow.begin_immediate():
            existing = self._uow.conn.execute(
                "SELECT registry_state FROM relevance_holdouts WHERE id = ? "
                "AND status = 'claimed' AND claim_token = ?",
                (holdout_id, claim_token),
            ).fetchone()
            if existing is None:
                raise RuntimeError(f"holdout {holdout_id} lost its release claim")
            registry_document = json.loads(existing["registry_state"])
            registry_document["_holdout_release_provenance"] = dict(provenance)
            cursor = self._uow.conn.execute(
                "UPDATE relevance_holdouts SET status = 'released', release_action = ?, "
                "target_evaluation_id = ?, released_at = ?, claim_token = NULL, "
                "claim_owner = NULL, claim_expires_at = NULL, last_error = NULL, "
                "registry_state = ? "
                "WHERE id = ? AND status = 'claimed' AND claim_token = ?",
                (
                    action,
                    target_evaluation_id,
                    released_at,
                    json.dumps(registry_document, sort_keys=True, separators=(",", ":")),
                    holdout_id,
                    claim_token,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"holdout {holdout_id} lost its release claim")

    def fail_release(self, holdout_id: int, *, claim_token: str, error_detail: str) -> None:
        detail = " ".join(error_detail.split())[:2000] or "holdout release failed"
        with self._uow.begin_immediate():
            self._uow.conn.execute(
                "UPDATE relevance_holdouts SET status = 'failed', last_error = ?, "
                "claim_token = NULL, claim_owner = NULL, claim_expires_at = NULL "
                "WHERE id = ? AND status = 'claimed' AND claim_token = ?",
                (detail, holdout_id, claim_token),
            )


__all__ = [
    "HoldoutStatus",
    "RelevanceAction",
    "RelevanceHoldout",
    "RelevanceHoldoutStore",
    "load_held_evaluation_ids",
    "registry_state",
]
