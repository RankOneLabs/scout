"""Typed repository for non-gating typesafe shadow relevance runs."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal

from scout.config import HUMAN_GRADE_SCHEMA_VERSION
from scout.storage.timestamps import parse_aware_utc
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


@dataclass(frozen=True, slots=True)
class ShadowReportRow:
    evaluation_id: int
    scan_id: int | None
    created_at: str
    status: Literal["ok", "error"]
    eligible: bool | None
    p_eligible: float | None
    uncertain: bool | None
    reason: str | None
    error_detail: str | None
    llm_relevant: bool
    llm_score: float
    human_grade: str | None


@dataclass(frozen=True, slots=True)
class ShadowFitRow:
    evaluation_id: int
    answers: dict[str, object]
    human_relevant: bool


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


_IDEMPOTENT_FIELDS = (
    "scan_id",
    "post_id",
    "backend",
    "model",
    "catalogue_id",
    "catalogue_version",
    "weight_set_version",
    "request_id",
    "state",
    "answers",
    "decision",
    "eligible",
    "p_eligible",
    "uncertain",
    "account_label",
    "account_confidence",
    "input_tokens",
    "output_tokens",
    "latency_ms",
    "status",
    "error_detail",
)


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
        persisted = _row(row)
        if any(getattr(persisted, field) != getattr(run, field) for field in _IDEMPOTENT_FIELDS):
            raise sqlite3.IntegrityError(
                "conflicting shadow relevance run for backend and request_id"
            )
        return persisted

    def backfill_evaluation_id(self, run_id: int, evaluation_id: int) -> bool:
        with self._uow.begin_immediate():
            cursor = self._uow.db.execute(
                "UPDATE shadow_relevance_runs SET evaluation_id = ? "
                "WHERE id = ? AND evaluation_id IS NULL "
                "AND EXISTS ("
                "SELECT 1 FROM evaluations "
                "WHERE evaluations.id = ? "
                "AND evaluations.scan_id = shadow_relevance_runs.scan_id "
                "AND evaluations.post_id = shadow_relevance_runs.post_id"
                ")",
                (evaluation_id, run_id, evaluation_id),
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

    @staticmethod
    def table_exists(conn: sqlite3.Connection) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                ("shadow_relevance_runs",),
            ).fetchone()
            is not None
        )

    @staticmethod
    def report_rows(
        conn: sqlite3.Connection, *, since: str | None = None, scan_id: int | None = None
    ) -> list[ShadowReportRow]:
        """Read the latest shadow run per evaluation for the operator report.

        SQL lives in the typed repository; the CLI consumes only named rows.
        A grade is exposed only when its latest immutable revision and current
        row are finalized under the current schema. An invalidating revision
        never revives an older label.
        """
        cutoff = None if since is None else parse_aware_utc(since)
        parameters: tuple[object, ...] = (
            HUMAN_GRADE_SCHEMA_VERSION,
            HUMAN_GRADE_SCHEMA_VERSION,
            HUMAN_GRADE_SCHEMA_VERSION,
        )
        rows = conn.execute(
            """
            SELECT sr.id AS shadow_run_id, sr.evaluation_id, sr.scan_id, sr.created_at, sr.status,
                   sr.eligible, sr.p_eligible, sr.uncertain,
                   json_extract(sr.decision_json, '$.reason') AS reason,
                   sr.error_detail, e.relevant AS llm_relevant, e.score AS llm_score,
                   CASE WHEN g.schema_version = ? AND g.needs_regrade = 0
                              AND gr.evaluation_id = e.id
                              AND gr.schema_version = ?
                              AND json_type(gr.payload, '$.schema_version') = 'integer'
                              AND json_extract(gr.payload, '$.schema_version') = ?
                              AND json_type(gr.payload, '$.needs_regrade') = 'integer'
                              AND json_extract(gr.payload, '$.needs_regrade') = 0
                        THEN json_extract(gr.payload, '$.relevance_judgment')
                        ELSE NULL END AS human_grade
              FROM shadow_relevance_runs sr
              JOIN evaluations e ON e.id = sr.evaluation_id
              LEFT JOIN grades g ON g.id = (
                   SELECT candidate.id FROM grades candidate
                    WHERE candidate.evaluation_id = e.id
                    ORDER BY candidate.id DESC LIMIT 1
              )
              LEFT JOIN grade_revisions gr ON gr.id = (
                   SELECT revision.id FROM grade_revisions revision
                    WHERE revision.grade_id = g.id
                    ORDER BY revision.revision DESC LIMIT 1
              )
             ORDER BY sr.created_at, sr.id
            """,
            parameters,
        ).fetchall()
        latest: dict[int, tuple[datetime, sqlite3.Row]] = {}
        for row in rows:
            instant = parse_aware_utc(row["created_at"])
            current = latest.get(row["evaluation_id"])
            if current is None or (instant, row["shadow_run_id"]) > (
                current[0],
                current[1]["shadow_run_id"],
            ):
                latest[row["evaluation_id"]] = (instant, row)
        selected = latest.values()
        if scan_id is not None:
            rows = [row for _instant, row in selected if row["scan_id"] == scan_id]
        else:
            assert cutoff is not None
            rows = [row for instant, row in selected if instant >= cutoff]
        rows = sorted(
            rows, key=lambda row: (parse_aware_utc(row["created_at"]), row["shadow_run_id"])
        )
        return [
            ShadowReportRow(
                evaluation_id=row["evaluation_id"],
                scan_id=row["scan_id"],
                created_at=row["created_at"],
                status=row["status"],
                eligible=None if row["eligible"] is None else bool(row["eligible"]),
                p_eligible=row["p_eligible"],
                uncertain=None if row["uncertain"] is None else bool(row["uncertain"]),
                reason=row["reason"],
                error_detail=row["error_detail"],
                llm_relevant=bool(row["llm_relevant"]),
                llm_score=row["llm_score"],
                human_grade=row["human_grade"],
            )
            for row in rows
        ]

    @staticmethod
    def fitting_rows(
        conn: sqlite3.Connection,
        *,
        catalogue_version: str,
        finalized_revisions: dict[int, int],
    ) -> list[ShadowFitRow]:
        """Read only pinned train IDs, joined to their exact finalized revisions."""
        if not finalized_revisions:
            return []
        selected = json.dumps(sorted(finalized_revisions.items()), separators=(",", ":"))
        rows = conn.execute(
            """
            WITH selected(evaluation_id, grade_revision_id) AS (
                SELECT CAST(json_extract(value, '$[0]') AS INTEGER),
                       CAST(json_extract(value, '$[1]') AS INTEGER)
                  FROM json_each(?)
            )
            SELECT selected.evaluation_id, sr.answers_json, e.relevant AS llm_relevant,
                   json_extract(gr.payload, '$.relevance_judgment') AS human_grade
              FROM selected
              JOIN evaluations e ON e.id = selected.evaluation_id
              JOIN grade_revisions gr
                ON gr.id = selected.grade_revision_id
               AND gr.evaluation_id = selected.evaluation_id
              JOIN shadow_relevance_runs sr ON sr.id = (
                   SELECT candidate.id
                     FROM shadow_relevance_runs candidate
                    WHERE candidate.evaluation_id = selected.evaluation_id
                      AND candidate.catalogue_version = ?
                      AND candidate.status = 'ok'
                      AND candidate.answers_json IS NOT NULL
                    ORDER BY candidate.created_at DESC, candidate.id DESC
                    LIMIT 1
              )
             ORDER BY selected.evaluation_id
            """,
            (selected, catalogue_version),
        ).fetchall()
        output: list[ShadowFitRow] = []
        for row in rows:
            grade = row["human_grade"]
            if grade == "correct":
                relevant = bool(row["llm_relevant"])
            elif grade == "false_positive":
                relevant = False
            elif grade == "false_negative":
                relevant = True
            else:
                continue
            output.append(
                ShadowFitRow(
                    evaluation_id=row["evaluation_id"],
                    answers=json.loads(row["answers_json"]),
                    human_relevant=relevant,
                )
            )
        return output
