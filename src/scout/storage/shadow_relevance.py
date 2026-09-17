"""Typed repository for non-gating typesafe shadow relevance runs."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal

from scout.storage.unit_of_work import UnitOfWork


@dataclass(frozen=True, slots=True)
class ShadowRunWrite:
    scan_id: int
    post_id: int
    backend: str
    model: str
    catalogue_id: str
    catalogue_version: str
    request_id: str
    state: dict[str, object]
    status: Literal["ok", "error"]
    answers: dict[str, object] | None = None
    decision: dict[str, object] | None = None
    evaluation_id: int | None = None
    weight_set_version: str | None = None
    eligible: bool | None = None
    p_eligible: float | None = None
    uncertain: bool | None = None
    account_label: str | None = None
    account_confidence: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    error_detail: str | None = None
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class ShadowRun:
    id: int
    scan_id: int
    post_id: int
    evaluation_id: int | None
    backend: str
    model: str
    catalogue_id: str
    catalogue_version: str
    weight_set_version: str | None
    request_id: str
    state: dict[str, object]
    answers: dict[str, object] | None
    decision: dict[str, object] | None
    eligible: bool | None
    p_eligible: float | None
    uncertain: bool | None
    account_label: str | None
    account_confidence: float | None
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None
    status: Literal["ok", "error"]
    error_detail: str | None
    created_at: str


def _json(value: dict[str, object] | None) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, separators=(",", ":"))


def _row(row: sqlite3.Row) -> ShadowRun:
    data = dict(row)
    data["state"] = json.loads(data.pop("state_json"))
    answers_json = data.pop("answers_json")
    decision_json = data.pop("decision_json")
    data["answers"] = None if answers_json is None else json.loads(answers_json)
    data["decision"] = None if decision_json is None else json.loads(decision_json)
    for name in ("eligible", "uncertain"):
        data[name] = None if data[name] is None else bool(data[name])
    return ShadowRun(**data)


class ShadowRelevanceStore:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    def record_shadow_run(self, run: ShadowRunWrite) -> ShadowRun:
        values = asdict(run)
        created_at = values.pop("created_at") or datetime.now(UTC).isoformat()
        state_json = _json(values.pop("state"))
        answers_json = _json(values.pop("answers"))
        decision_json = _json(values.pop("decision"))
        columns = [*values, "state_json", "answers_json", "decision_json", "created_at"]
        params = [*values.values(), state_json, answers_json, decision_json, created_at]
        placeholders = ", ".join("?" for _ in columns)
        with self._uow.begin_immediate():
            self._uow.db.execute(
                f"INSERT INTO shadow_relevance_runs ({', '.join(columns)}) "  # noqa: S608
                f"VALUES ({placeholders}) ON CONFLICT(backend, request_id) DO NOTHING",
                params,
            )
            row = self._uow.db.execute(
                "SELECT * FROM shadow_relevance_runs WHERE backend = ? AND request_id = ?",
                (run.backend, run.request_id),
            ).fetchone()
        assert row is not None
        return _row(row)

    def backfill_evaluation_id(self, run_id: int, evaluation_id: int) -> bool:
        with self._uow.begin_immediate():
            cursor = self._uow.db.execute(
                "UPDATE shadow_relevance_runs SET evaluation_id = ? "
                "WHERE id = ? AND evaluation_id IS NULL",
                (evaluation_id, run_id),
            )
        return cursor.rowcount == 1

    def list_runs_for_scan(self, scan_id: int) -> list[ShadowRun]:
        rows = self._uow.db.execute(
            "SELECT * FROM shadow_relevance_runs WHERE scan_id = ? ORDER BY id", (scan_id,)
        ).fetchall()
        return [_row(row) for row in rows]

    def list_runs_since(self, created_at: str) -> list[ShadowRun]:
        rows = self._uow.db.execute(
            "SELECT * FROM shadow_relevance_runs WHERE created_at >= ? ORDER BY created_at, id",
            (created_at,),
        ).fetchall()
        return [_row(row) for row in rows]
