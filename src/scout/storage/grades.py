"""Grade aggregate: human/migration grades, their immutable revision
history, usage overrides, human-positive promotions, and the durable home
of grade-edit reply text. Owns `grades`, `grade_revisions`,
`grade_usage_overrides`, `human_positive_promotions`, and
`reply_draft_revisions`.

A grade always targets an evaluation, so this store is constructed with an
`EvaluationStore` reference for read-only validation (resolving/verifying
the target evaluation, checking a draft exists before accepting
`edited_text`) — never for writes. Both stores share the same `UnitOfWork`
(see `unit_of_work.py`), so a validation read here and a write there
compose atomically with zero extra ceremony when the caller already holds
an outer transaction; there is no case in this module where a Grade write
and an Evaluation write must commit together — see
docs/transactions-and-scan-durability.md for the one real cross-aggregate
case (Evaluation + Grade, composed externally by
grading/promotion.py via `StateManager.db`).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from scout.config import HUMAN_GRADE_SCHEMA_VERSION, GradeRecord, GradingSignal
from scout.storage.evaluations import EvaluationRow, EvaluationStore
from scout.storage.migrations import GradeConvergenceStatus, grade_revision_comparison_shape
from scout.storage.unit_of_work import UnitOfWork

_GRADED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def format_graded_at(when: datetime) -> str:
    """Serialize an aware datetime to the canonical graded_at form:
    YYYY-MM-DDTHH:MM:SS.mmmZ in UTC, truncated to millisecond precision.

    This is the sole boundary rule for normal grade writes: it converts
    the caller-supplied instant to UTC rather than inventing timezone
    context, so it raises ValueError for naive datetimes instead of
    silently assuming one.
    """
    if when.tzinfo is None:
        raise ValueError("format_graded_at requires a timezone-aware datetime")
    as_utc = when.astimezone(UTC)
    millis = as_utc.microsecond // 1000
    return as_utc.strftime("%Y-%m-%dT%H:%M:%S") + f".{millis:03d}Z"


def parse_graded_at(value: str) -> datetime:
    """Parse a canonical graded_at string into an aware UTC datetime.

    Rejects any shape other than the exact YYYY-MM-DDTHH:MM:SS.mmmZ form
    — this is the strict counterpart to format_graded_at, for callers
    that must confirm a stored value is already canonical.
    """
    if not _GRADED_AT_RE.match(value):
        raise ValueError(f"graded_at {value!r} is not in canonical form")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def _parse_stored_graded_at(value: str) -> datetime:
    """Parse canonical timestamps while retaining legacy-row compatibility."""
    try:
        return parse_graded_at(value)
    except ValueError:
        return datetime.fromisoformat(value)


class GradeValidationError(Exception):
    """A grade failed validation at the StateManager.save_grade persistence
    boundary. Carries every aggregated validation message; raised before any
    INSERT or UPDATE against the grades table."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


class HumanPositivePromotionInProgressError(RuntimeError):
    """A non-stale draft promotion already owns this source evaluation."""


@dataclass(frozen=True, slots=True)
class GradeRow:
    """One `grades` row plus its resolved `edited_text`
    (`reply_draft_revisions.reply_text`, joined via `reply_revision_id` —
    `grades` carries no text column of its own for it)."""

    id: int
    evaluation_id: int | None
    post_id: int
    scan_id: int | None
    source: str
    graded_at: str
    relevance_judgment: str
    rejection_reason: str | None
    comment_quality: int | None
    comment_issue: str | None
    schema_version: int
    needs_regrade: bool
    action_judgment: str | None
    dimensions: str | None
    failure_note: str | None
    factual_offending_claim: str | None
    factual_disposition: str | None
    factual_contradicting_evidence: str | None
    context_missing_input: str | None
    posture_should_have_been: str | None
    implication_implied_claim: str | None
    implication_missing_support: str | None
    reply_revision_id: int | None
    edited_text: str | None


@dataclass(frozen=True, slots=True)
class GradeRevision:
    """One immutable `grade_revisions` row."""

    id: int
    grade_id: int
    evaluation_id: int | None
    revision: int
    schema_version: int
    source: str
    payload: str
    recorded_at: str


@dataclass(frozen=True, slots=True)
class GradeUsageOverride:
    """The current-state usage override for a grade id."""

    grade_id: int
    mode: str
    reason: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class HumanPositivePromotion:
    """One `human_positive_promotions` row."""

    source_evaluation_id: int
    source_grade_id: int
    scan_id: int | None
    target_evaluation_id: int | None
    status: str
    error_detail: str | None
    created_at: str
    updated_at: str
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class GradeableItem:
    """One row of the post/evaluation/draft/critique/grade review join
    (`get_gradeable_items`)."""

    post_id: int
    platform: str
    channel_name: str | None
    author_name: str | None
    content: str | None
    url: str | None
    created_at: str | None
    scan_id: int | None
    parent_lookup_status: str
    parent_id: str | None
    parent_author_name: str | None
    parent_text: str | None
    parent_url: str | None
    evaluation_id: int
    eval_scan_id: int | None
    score: float
    reason: str | None
    relevant: bool
    relevant_to: str | None
    project_key: str | None
    dossier_revision: str | None
    posture: str | None
    surface_status: str
    draft_id: int | None
    comment_text: str | None
    draft_project_key: str | None
    verdict: str | None
    critique_feedback: str | None
    relevance_judgment: str | None
    action_judgment: str | None
    dimensions: str | None
    failure_note: str | None
    graded_at: str | None
    grade_schema_version: int | None


def _row_to_grade_row(row: sqlite3.Row) -> GradeRow:
    return GradeRow(
        id=row["id"],
        evaluation_id=row["evaluation_id"],
        post_id=row["post_id"],
        scan_id=row["scan_id"],
        source=row["source"],
        graded_at=row["graded_at"],
        relevance_judgment=row["relevance_judgment"],
        rejection_reason=row["rejection_reason"],
        comment_quality=row["comment_quality"],
        comment_issue=row["comment_issue"],
        schema_version=row["schema_version"],
        needs_regrade=bool(row["needs_regrade"]),
        action_judgment=row["action_judgment"],
        dimensions=row["dimensions"],
        failure_note=row["failure_note"],
        factual_offending_claim=row["factual_offending_claim"],
        factual_disposition=row["factual_disposition"],
        factual_contradicting_evidence=row["factual_contradicting_evidence"],
        context_missing_input=row["context_missing_input"],
        posture_should_have_been=row["posture_should_have_been"],
        implication_implied_claim=row["implication_implied_claim"],
        implication_missing_support=row["implication_missing_support"],
        reply_revision_id=row["reply_revision_id"],
        edited_text=row["edited_text"],
    )


def _row_to_human_positive_promotion(row: sqlite3.Row) -> HumanPositivePromotion:
    return HumanPositivePromotion(
        source_evaluation_id=row["source_evaluation_id"],
        source_grade_id=row["source_grade_id"],
        scan_id=row["scan_id"],
        target_evaluation_id=row["target_evaluation_id"],
        status=row["status"],
        error_detail=row["error_detail"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
    )


class GradeStore:
    """Owns grades, grade revisions, usage overrides, and human-positive
    promotions."""

    # grades carries no text column of its own for edited_text — only the
    # reply_revision_id pointer — so every reader that builds a
    # grade_revisions comparison payload (grade_revision_comparison_shape)
    # must resolve it via this join, or a v3 grade with a real correction
    # would compare as edited_text=None against its own recorded revision.
    # No trailing space here — callers append their own clause with an
    # explicit leading space, so the join stays syntactically correct
    # regardless of how either string is edited later.
    _GRADE_WITH_EDITED_TEXT_SELECT = (
        "SELECT g.*, rdr.reply_text AS edited_text FROM grades g "
        "LEFT JOIN reply_draft_revisions rdr ON rdr.id = g.reply_revision_id"
    )

    def __init__(self, uow: UnitOfWork, *, evaluations: EvaluationStore) -> None:
        self._uow = uow
        self._evaluations = evaluations

    @property
    def _conn(self) -> sqlite3.Connection:
        return self._uow.conn

    def _resolve_grade_evaluation_id(self, grade: GradeRecord) -> int | None:
        if grade.evaluation_id is not None:
            return grade.evaluation_id
        if grade.scan_id is None:
            return None
        return self._evaluations.get_latest_evaluation_id(grade.post_id, grade.scan_id)

    def _resolve_and_verify_grade_evaluation(self, grade: GradeRecord) -> EvaluationRow:
        """Resolve the evaluation a grade targets and verify identity.

        Resolves from grade.evaluation_id, or from the existing post_id plus
        scan_id lookup when evaluation_id is absent. Requires a real
        evaluation and verifies its post and scan identity match the grade,
        so the caller-supplied posture cannot silently diverge from the
        stored evaluation posture that validate_grade requires.
        """
        evaluation_id = self._resolve_grade_evaluation_id(grade)
        if evaluation_id is None:
            raise GradeValidationError(
                [
                    "no evaluation found for grade "
                    f"(post_id={grade.post_id}, scan_id={grade.scan_id})"
                ]
            )
        evaluation = self._evaluations.get_evaluation(evaluation_id)
        if evaluation is None:
            raise GradeValidationError([f"evaluation {evaluation_id} not found"])
        if evaluation.post_id != grade.post_id:
            raise GradeValidationError(
                [
                    f"grade post_id {grade.post_id} does not match evaluation "
                    f"{evaluation_id}'s post_id {evaluation.post_id}"
                ]
            )
        if grade.scan_id is not None and evaluation.scan_id != grade.scan_id:
            raise GradeValidationError(
                [
                    f"grade scan_id {grade.scan_id} does not match evaluation "
                    f"{evaluation_id}'s scan_id {evaluation.scan_id}"
                ]
            )
        return evaluation

    @staticmethod
    def _grade_write_params(
        grade: GradeRecord, evaluation_id: int | None, reply_revision_id: int | None
    ) -> tuple[object, ...]:
        dims_json = json.dumps(grade.dimensions) if grade.dimensions is not None else None
        return (
            evaluation_id,
            grade.post_id,
            grade.scan_id,
            grade.source,
            format_graded_at(grade.graded_at),
            grade.relevance_judgment,
            grade.action_judgment,
            grade.schema_version,
            int(grade.needs_regrade),
            dims_json,
            grade.failure_note,
            grade.factual_offending_claim,
            grade.factual_disposition,
            grade.factual_contradicting_evidence,
            grade.context_missing_input,
            grade.posture_should_have_been,
            grade.implication_implied_claim,
            grade.implication_missing_support,
            reply_revision_id,
        )

    def _resolve_grade_reply_revision_id(
        self, grade: GradeRecord, evaluation_id: int
    ) -> int | None:
        """Create the next reply_draft_revisions row for grade.edited_text
        and return its id — edited_text's durable home, since grades
        itself carries only this pointer and no text column of its own.

        When edited_text is absent (a no-edit save), preserves whatever
        reply_revision_id the evaluation's current grade already carries
        instead of returning None outright: a later no-edit regrade must
        not sever the visible link to the latest correction. An initial
        no-edit grade — no existing row for this evaluation yet — has
        nothing to preserve and stays unlinked.

        Must run inside the caller's active transaction. The evaluation's
        draft_comments row is assumed to exist when edited_text is
        present: save_grade's persistence-time check
        (_validated_grade_evaluation_id) already rejected edited_text
        without one before this is ever reached.
        """
        assert self._uow.in_transaction, (
            "_resolve_grade_reply_revision_id requires an active Db transaction context"
        )
        if grade.edited_text is None:
            existing = self._conn.execute(
                "SELECT reply_revision_id FROM grades WHERE evaluation_id = ?",
                (evaluation_id,),
            ).fetchone()
            if existing is None or existing["reply_revision_id"] is None:
                return None
            return int(existing["reply_revision_id"])
        draft = self._evaluations.get_draft_for_evaluation(evaluation_id)
        assert draft is not None, (
            "edited_text requires a draft_comments row for this evaluation — "
            "already checked by save_grade's persistence-time validation"
        )
        draft_comment_id = draft.id
        parent_row = self._conn.execute(
            "SELECT id FROM reply_draft_revisions WHERE draft_comment_id = ? "
            "ORDER BY version DESC LIMIT 1",
            (draft_comment_id,),
        ).fetchone()
        parent_revision_id = int(parent_row["id"]) if parent_row is not None else None
        next_version = self._conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM reply_draft_revisions "
            "WHERE draft_comment_id = ?",
            (draft_comment_id,),
        ).fetchone()[0]
        cursor = self._conn.execute(
            "INSERT INTO reply_draft_revisions "
            "(draft_comment_id, version, parent_revision_id, reply_text, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                draft_comment_id,
                next_version,
                parent_revision_id,
                grade.edited_text,
                grade.source,
                datetime.now(UTC).isoformat(),
            ),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def _insert_grade_revision(
        self, grade_id: int, evaluation_id: int | None, source: str
    ) -> int:
        """Append the next immutable revision for `grade_id` in the
        caller's existing transaction, from the just-written grades row.

        Reads the grades row fresh (post-upsert) so the payload is the
        authoritative saved state, not the caller's pre-write GradeRecord.
        Revision numbers are allocated as COALESCE(MAX(revision), 0) + 1
        inside this same connection and transaction — callers must already
        hold the BEGIN IMMEDIATE (or a savepoint nested under one) that
        serializes concurrent writers, so no two callers can ever compute
        the same next revision for a given grade_id.
        """
        assert self._uow.in_transaction, (
            "_insert_grade_revision requires an active Db transaction context"
        )
        row = self._conn.execute(
            self._GRADE_WITH_EDITED_TEXT_SELECT + " WHERE g.id = ?", (grade_id,)
        ).fetchone()
        assert row is not None
        payload = json.dumps(grade_revision_comparison_shape(row))
        next_revision = self._conn.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM grade_revisions WHERE grade_id = ?",
            (grade_id,),
        ).fetchone()[0]
        recorded_at = datetime.now(UTC).isoformat()
        self._conn.execute(
            "INSERT INTO grade_revisions "
            "(grade_id, evaluation_id, revision, schema_version, source, payload, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                grade_id,
                evaluation_id,
                next_revision,
                int(row["schema_version"]),
                source,
                payload,
                recorded_at,
            ),
        )
        return int(next_revision)

    def _upsert_resolved_grade_in_transaction(
        self, grade: GradeRecord, evaluation_id: int
    ) -> int:
        """Write one validated, evaluation-scoped grade, plus its complete
        grade_revisions entry, in the caller's existing transaction without
        committing it."""
        assert self._uow.in_transaction, (
            "_upsert_resolved_grade_in_transaction requires an active Db transaction context"
        )

        reply_revision_id = self._resolve_grade_reply_revision_id(grade, evaluation_id)
        params = self._grade_write_params(grade, evaluation_id, reply_revision_id)
        self._conn.execute(
            "UPDATE grades SET evaluation_id = ? "
            "WHERE id = ("
            "  SELECT id FROM grades WHERE evaluation_id IS NULL "
            "  AND post_id = ? AND (scan_id = ? OR (scan_id IS NULL AND ? IS NULL))"
            "  ORDER BY graded_at DESC, id DESC"
            "  LIMIT 1"
            ") "
            "AND NOT EXISTS (SELECT 1 FROM grades WHERE evaluation_id = ?)",
            (evaluation_id, grade.post_id, grade.scan_id, grade.scan_id, evaluation_id),
        )
        self._conn.execute(
            "INSERT INTO grades "
            "(evaluation_id, post_id, scan_id, source, graded_at, relevance_judgment, "
            "action_judgment, schema_version, needs_regrade, dimensions, failure_note, "
            "factual_offending_claim, factual_disposition, factual_contradicting_evidence, "
            "context_missing_input, posture_should_have_been, "
            "implication_implied_claim, implication_missing_support, reply_revision_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(evaluation_id) WHERE evaluation_id IS NOT NULL DO UPDATE SET "
            "post_id = excluded.post_id, scan_id = excluded.scan_id, "
            "source = excluded.source, graded_at = excluded.graded_at, "
            "relevance_judgment = excluded.relevance_judgment, "
            "action_judgment = excluded.action_judgment, "
            "schema_version = excluded.schema_version, "
            "needs_regrade = excluded.needs_regrade, "
            "dimensions = excluded.dimensions, failure_note = excluded.failure_note, "
            "factual_offending_claim = excluded.factual_offending_claim, "
            "factual_disposition = excluded.factual_disposition, "
            "factual_contradicting_evidence = excluded.factual_contradicting_evidence, "
            "context_missing_input = excluded.context_missing_input, "
            "posture_should_have_been = excluded.posture_should_have_been, "
            "implication_implied_claim = excluded.implication_implied_claim, "
            "implication_missing_support = excluded.implication_missing_support, "
            "reply_revision_id = excluded.reply_revision_id",
            params,
        )
        row = self._conn.execute(
            "SELECT id FROM grades WHERE evaluation_id = ?",
            (evaluation_id,),
        ).fetchone()
        assert row is not None
        grade_id = int(row["id"])
        self._insert_grade_revision(grade_id, evaluation_id, grade.source)
        return grade_id

    def _write_grade(self, grade: GradeRecord, evaluation_id: int | None) -> int:
        """Insert or update the grades row for the given (already-resolved,
        already-validated) evaluation_id. Never call directly from CLI, web,
        or sidecar request paths — use save_grade or save_grade_for_migration.

        When evaluation_id is resolved, persistence runs inside one BEGIN
        IMMEDIATE transaction: a conditional UPDATE first adopts a matching
        legacy evaluation_id IS NULL row — only when no row already owns the
        evaluation — then an INSERT with ON CONFLICT(evaluation_id) DO
        UPDATE replaces the complete payload. This removes the
        SELECT-then-write race between simultaneous CLI and web grading of
        the same evaluation: the grades_evaluation_id_unique partial index
        is the concurrency arbiter, not application control flow, and the
        adopted or conflicting row's id is preserved.

        evaluation_id=None only reaches this method through
        save_grade_for_migration when no evaluation could be resolved at
        all; that lane has no unique-index arbiter to race on, so it keeps
        the prior select-then-write shape.
        """
        if evaluation_id is None:
            # No evaluation to attach a reply revision to, and migration
            # writes never carry edited_text — reply_revision_id is always
            # NULL here.
            params = self._grade_write_params(grade, evaluation_id, None)
            with self._uow.begin():
                existing = self._conn.execute(
                    "SELECT id FROM grades WHERE evaluation_id IS NULL "
                    "AND post_id = ? AND (scan_id = ? OR (scan_id IS NULL AND ? IS NULL))",
                    (grade.post_id, grade.scan_id, grade.scan_id),
                ).fetchone()
                if existing is None:
                    cursor = self._conn.execute(
                        "INSERT INTO grades "
                        "(evaluation_id, post_id, scan_id, source, graded_at, relevance_judgment, "
                        "action_judgment, schema_version, needs_regrade, dimensions, failure_note, "
                        "factual_offending_claim, factual_disposition, "
                        "factual_contradicting_evidence, context_missing_input, "
                        "posture_should_have_been, implication_implied_claim, "
                        "implication_missing_support, reply_revision_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        params,
                    )
                    assert cursor.lastrowid is not None
                    grade_id = cursor.lastrowid
                    self._insert_grade_revision(grade_id, evaluation_id, grade.source)
                    return grade_id

                self._conn.execute(
                    "UPDATE grades SET evaluation_id = ?, post_id = ?, scan_id = ?, "
                    "source = ?, graded_at = ?, relevance_judgment = ?, "
                    "action_judgment = ?, schema_version = ?, needs_regrade = ?, "
                    "dimensions = ?, failure_note = ?, "
                    "factual_offending_claim = ?, factual_disposition = ?, "
                    "factual_contradicting_evidence = ?, context_missing_input = ?, "
                    "posture_should_have_been = ?, "
                    "implication_implied_claim = ?, implication_missing_support = ?, "
                    "reply_revision_id = ? "
                    "WHERE id = ?",
                    (*params, existing["id"]),
                )
                grade_id = int(existing["id"])
                self._insert_grade_revision(grade_id, evaluation_id, grade.source)
                return grade_id

        with self._uow.begin_immediate():
            return self._upsert_resolved_grade_in_transaction(grade, evaluation_id)

    def _validated_grade_evaluation_id(self, grade: GradeRecord) -> int:
        """Validate one ordinary schema-v3 grade and return its evaluation ID."""
        # Imported lazily: grading.py imports StateManager, so a top-level
        # import here would create a circular import.
        from scout.grading.service import grade_envelope_payload, validate_grade_envelope

        evaluation = self._resolve_and_verify_grade_evaluation(grade)
        evaluation_id = evaluation.id
        evaluation_posture = evaluation.posture

        errors: list[str] = []
        if grade.schema_version != HUMAN_GRADE_SCHEMA_VERSION:
            errors.append(
                "save_grade requires schema_version="
                f"{HUMAN_GRADE_SCHEMA_VERSION}, got {grade.schema_version}"
            )
        if grade.action_judgment is None:
            errors.append("save_grade requires a non-null action_judgment")
        if grade.source not in ("cli", "web"):
            errors.append(
                "save_grade requires source to be 'cli' or 'web', got "
                f"'{grade.source}' — use save_grade_for_migration for migration writes"
            )
        if grade.edited_text is not None:
            has_draft = self._evaluations.get_draft_for_evaluation(evaluation_id)
            if has_draft is None:
                errors.append(
                    "save_grade requires an existing draft_comments row for "
                    "this evaluation to accept edited_text — the shared "
                    "contract alone cannot see draft existence"
                )
        if errors:
            raise GradeValidationError(errors)
        assert grade.action_judgment is not None

        errors = validate_grade_envelope(grade_envelope_payload(grade), evaluation_posture)
        if errors:
            raise GradeValidationError(errors)

        return evaluation_id

    def save_grade(self, grade: GradeRecord) -> int:
        """Validate and save a schema-v3 grade for an evaluation. Returns row ID.

        This is the mandatory validated entry point for all ordinary
        schema-v3 Python writes: it resolves and verifies the target
        evaluation, loads its stored posture, runs the full causal rule set
        against the assembled grade, and raises GradeValidationError before
        executing any INSERT or UPDATE when validation fails. Historical
        imports that cannot satisfy the current contract must use the
        explicitly named migration lane instead; this method never accepts a
        validation bypass.
        """
        evaluation_id = self._validated_grade_evaluation_id(grade)
        return self._write_grade(grade, evaluation_id)

    def get_human_positive_promotion(
        self, source_evaluation_id: int
    ) -> HumanPositivePromotion | None:
        row = self._conn.execute(
            "SELECT * FROM human_positive_promotions WHERE source_evaluation_id = ?",
            (source_evaluation_id,),
        ).fetchone()
        return _row_to_human_positive_promotion(row) if row is not None else None

    def begin_human_positive_promotion(self, grade: GradeRecord) -> HumanPositivePromotion:
        """Persist the source false-negative grade and claim its draft workflow.

        Completed workflows are idempotent. A recent running claim rejects a
        concurrent duplicate; failed or stale claims may be retried. The grade
        is committed before inference so recall reflects the human decision
        even when draft generation later fails.
        """
        if grade.evaluation_id is None:
            raise ValueError("human-positive promotion requires evaluation_id")
        if grade.relevance_judgment != "false_negative":
            raise ValueError("human-positive promotion requires false_negative grade")
        evaluation = self._evaluations.get_evaluation(grade.evaluation_id)
        if evaluation is None:
            raise ValueError(f"evaluation {grade.evaluation_id} not found")
        if evaluation.relevant:
            raise ValueError("only model-negative evaluations can be promoted")

        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        stale_before = now_dt - timedelta(minutes=10)
        with self._uow.begin_immediate():
            existing = self._conn.execute(
                "SELECT * FROM human_positive_promotions "
                "WHERE source_evaluation_id = ?",
                (grade.evaluation_id,),
            ).fetchone()
            if existing is not None and existing["status"] == "completed":
                return _row_to_human_positive_promotion(existing)
            if existing is not None and existing["status"] == "running":
                try:
                    updated_at = datetime.fromisoformat(existing["updated_at"])
                except (TypeError, ValueError):
                    updated_at = stale_before
                if updated_at > stale_before:
                    raise HumanPositivePromotionInProgressError(
                        f"draft generation is already in progress for evaluation "
                        f"{grade.evaluation_id}"
                    )

            grade_id = self.save_grade(grade)
            if existing is None:
                self._conn.execute(
                    "INSERT INTO human_positive_promotions "
                    "(source_evaluation_id, source_grade_id, status, created_at, updated_at) "
                    "VALUES (?, ?, 'running', ?, ?)",
                    (grade.evaluation_id, grade_id, now, now),
                )
            else:
                self._conn.execute(
                    "UPDATE human_positive_promotions SET source_grade_id = ?, "
                    "scan_id = NULL, target_evaluation_id = NULL, status = 'running', "
                    "error_detail = NULL, updated_at = ?, completed_at = NULL "
                    "WHERE source_evaluation_id = ?",
                    (grade_id, now, grade.evaluation_id),
                )
            claimed = self._conn.execute(
                "SELECT * FROM human_positive_promotions "
                "WHERE source_evaluation_id = ?",
                (grade.evaluation_id,),
            ).fetchone()
        assert claimed is not None
        return _row_to_human_positive_promotion(claimed)

    def attach_human_positive_promotion_scan(
        self, source_evaluation_id: int, scan_id: int
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._uow.begin_immediate():
            cursor = self._conn.execute(
                "UPDATE human_positive_promotions SET scan_id = ?, updated_at = ? "
                "WHERE source_evaluation_id = ? AND status = 'running'",
                (scan_id, now, source_evaluation_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"promotion for evaluation {source_evaluation_id} is not running"
                )

    def complete_human_positive_promotion(
        self,
        source_evaluation_id: int,
        *,
        scan_id: int,
        target_evaluation_id: int,
    ) -> None:
        """CAS a running promotion to completed.

        Callers may wrap this and target-outcome persistence in one outer
        transaction so the target evaluation and workflow completion become
        visible atomically.
        """
        now = datetime.now(UTC).isoformat()
        with self._uow.begin_immediate():
            cursor = self._conn.execute(
                "UPDATE human_positive_promotions SET target_evaluation_id = ?, "
                "status = 'completed', error_detail = NULL, updated_at = ?, "
                "completed_at = ? WHERE source_evaluation_id = ? "
                "AND scan_id = ? AND status = 'running'",
                (target_evaluation_id, now, now, source_evaluation_id, scan_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"promotion for evaluation {source_evaluation_id} could not complete"
                )

    def fail_human_positive_promotion(
        self, source_evaluation_id: int, *, error_detail: str
    ) -> None:
        now = datetime.now(UTC).isoformat()
        detail = " ".join(error_detail.split())[:2000] or "draft generation failed"
        with self._uow.begin_immediate():
            self._conn.execute(
                "UPDATE human_positive_promotions SET status = 'failed', "
                "error_detail = ?, updated_at = ?, completed_at = NULL "
                "WHERE source_evaluation_id = ? AND status = 'running'",
                (detail, now, source_evaluation_id),
            )

    def save_grade_for_remediation(
        self, grade: GradeRecord, *, remediation_reason: str
    ) -> int:
        """Validate and upsert a replacement inside the caller's transaction.

        The grade-corpus remediation path calls this from inside its own
        ``begin_immediate()`` unit so flag updates and all reviewed
        replacements commit or roll back together — this method opens its
        own ``Db.transaction()``, which joins that outer unit via
        savepoint rather than starting a second root. It applies the same
        validation as ``save_grade`` and never bypasses the shared
        contract.
        """
        if not remediation_reason or not remediation_reason.strip():
            raise ValueError("remediation_reason is required")
        with self._uow.begin():
            evaluation_id = self._validated_grade_evaluation_id(grade)
            return self._upsert_resolved_grade_in_transaction(grade, evaluation_id)

    def mark_grade_needs_regrade_for_remediation(
        self, grade_id: int, *, remediation_reason: str
    ) -> bool:
        """Mark one current grade for regrade and append its audit revision.

        This is the only remediation lane for mutating ``needs_regrade``.
        It joins the corpus tool's outer ``BEGIN IMMEDIATE`` through a
        savepoint, so the current row and its complete immutable revision
        commit or roll back together. Returns ``False`` when the grade was
        already flagged and therefore no save boundary occurred.
        """
        if not remediation_reason or not remediation_reason.strip():
            raise ValueError("remediation_reason is required")
        with self._uow.begin():
            row = self._conn.execute(
                "SELECT evaluation_id, needs_regrade FROM grades WHERE id = ?",
                (grade_id,),
            ).fetchone()
            if row is None:
                raise GradeValidationError([f"grade {grade_id} not found"])
            if bool(row["needs_regrade"]):
                return False
            self._conn.execute(
                "UPDATE grades SET needs_regrade = 1 WHERE id = ?", (grade_id,)
            )
            self._insert_grade_revision(
                grade_id, row["evaluation_id"], "migration"
            )
            return True

    def converge_grade_revision_for_remediation(
        self, grade_id: int, *, remediation_reason: str
    ) -> GradeConvergenceStatus:
        """Re-read the current grades row and its latest revision under
        this transaction, then append exactly one canonical revision
        (source="migration") only when history has drifted from the
        current state.

        This is the only remediation lane for restoring grade_revisions
        convergence. grade_revisions is append-only and immutable (see
        the grade_revisions_no_update / grade_revisions_no_delete
        triggers), so "repair" can only ever mean adding one more honest
        revision — it never rewrites or deletes existing history, and it
        never fabricates an evaluation_id for a current row that has
        none. It joins the caller's outer ``BEGIN IMMEDIATE`` through a
        savepoint, exactly like ``mark_grade_needs_regrade_for_remediation``,
        so the check and the conditional append are evaluated against the
        same locked snapshot — no other writer can interleave a change
        between them. Idempotent: once a grade converges, a rerun finds
        the freshly-appended revision already matches the current row and
        writes nothing.
        """
        if not remediation_reason or not remediation_reason.strip():
            raise ValueError("remediation_reason is required")
        with self._uow.begin():
            row = self._conn.execute(
                self._GRADE_WITH_EDITED_TEXT_SELECT + " WHERE g.id = ?", (grade_id,)
            ).fetchone()
            if row is None:
                raise GradeValidationError([f"grade {grade_id} not found"])
            latest = self._conn.execute(
                "SELECT payload FROM grade_revisions WHERE grade_id = ? "
                "ORDER BY revision DESC LIMIT 1",
                (grade_id,),
            ).fetchone()

            current_shape = grade_revision_comparison_shape(row)
            if latest is None:
                status: GradeConvergenceStatus = "missing_revision"
            elif json.loads(latest["payload"]) == current_shape:
                status = "converged"
            else:
                status = "divergent_revision"

            if status == "converged":
                return status
            self._insert_grade_revision(grade_id, row["evaluation_id"], "migration")
            return status

    def save_grade_for_migration(self, grade: GradeRecord, *, migration_reason: str) -> int:
        """Persist a grade without schema-v3 causal validation, for
        historical import or non-contract backfill only.

        Never called by CLI, web, or sidecar request paths — save_grade is
        the mandatory entry point for ordinary writes. This lane exists so
        backfill and repair scripts have a named, auditable path instead of
        a boolean skip_validation escape hatch on save_grade itself.
        """
        if not migration_reason or not migration_reason.strip():
            raise ValueError("migration_reason is required for save_grade_for_migration")
        evaluation_id = self._resolve_grade_evaluation_id(grade)
        if evaluation_id is not None:
            evaluation = self._evaluations.get_evaluation(evaluation_id)
            if evaluation is not None:
                if evaluation.post_id != grade.post_id:
                    raise ValueError(
                        f"grade post_id {grade.post_id} does not match evaluation "
                        f"{evaluation_id}'s post_id {evaluation.post_id}"
                    )
                if grade.scan_id is not None and evaluation.scan_id != grade.scan_id:
                    raise ValueError(
                        f"grade scan_id {grade.scan_id} does not match evaluation "
                        f"{evaluation_id}'s scan_id {evaluation.scan_id}"
                    )
        return self._write_grade(grade, evaluation_id)

    def get_grade(self, post_id: int) -> GradeRecord | None:
        """Load a grade by post ID, or None if ungraded."""
        row = self._conn.execute(
            "SELECT * FROM grades WHERE post_id = ? "
            "ORDER BY graded_at DESC, id DESC LIMIT 1",
            (post_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_grade(row)

    def get_grade_for_evaluation(self, evaluation_id: int) -> GradeRecord | None:
        """Load a grade by evaluation ID, or None if ungraded."""
        row = self._conn.execute(
            "SELECT * FROM grades WHERE evaluation_id = ?",
            (evaluation_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_grade(row)

    def get_grade_row_by_id(self, grade_id: int) -> GradeRow | None:
        """Load a grade row by its own id, including resolved edited text.

        Retains the row ``id`` that ``_row_to_grade``/``GradeRecord``
        deliberately drop.  ``edited_text`` lives in
        ``reply_draft_revisions``, so response-producing callers need the
        same projection as other grade readers or a successful save appears
        to erase the correction in the web client.
        """
        row = self._conn.execute(
            self._GRADE_WITH_EDITED_TEXT_SELECT + " WHERE g.id = ?",
            (grade_id,),
        ).fetchone()
        return _row_to_grade_row(row) if row is not None else None

    def get_grade_id_for_evaluation(self, evaluation_id: int) -> int | None:
        """Resolve the current grades row id for an evaluation, or None if
        ungraded. `GradeRecord`/`get_grade_for_evaluation` deliberately
        drop the row id — this is the lookup grade_id-keyed callers (e.g.
        the usage-override endpoint) need instead."""
        row = self._conn.execute(
            "SELECT id FROM grades WHERE evaluation_id = ?",
            (evaluation_id,),
        ).fetchone()
        return int(row["id"]) if row is not None else None

    def get_grade_revisions(self, grade_id: int) -> list[GradeRevision]:
        """Ordered oldest-to-newest revision history for a grade id.
        Returns typed rows without mutating payloads — callers that need the
        parsed envelope decode `payload` themselves."""
        rows = self._conn.execute(
            "SELECT * FROM grade_revisions WHERE grade_id = ? ORDER BY revision ASC",
            (grade_id,),
        ).fetchall()
        return [
            GradeRevision(
                id=row["id"],
                grade_id=row["grade_id"],
                evaluation_id=row["evaluation_id"],
                revision=row["revision"],
                schema_version=row["schema_version"],
                source=row["source"],
                payload=row["payload"],
                recorded_at=row["recorded_at"],
            )
            for row in rows
        ]

    def get_grade_revision_count(self, grade_id: int) -> int:
        """Return the number of revisions recorded for a grade id."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM grade_revisions WHERE grade_id = ?",
            (grade_id,),
        ).fetchone()
        return int(row[0])

    def save_grade_usage_override(
        self, grade_id: int, *, mode: str, reason: str | None
    ) -> GradeUsageOverride:
        """Upsert the current-state usage override for a grade id.

        mode='auto' stores reason=NULL — an explicit auto row is
        distinguishable from "no override row" only by its updated_at, but
        both read as auto, so this still records useful latest-update
        metadata. mode='exclude' requires a non-blank reason. There is no
        force-include value: this method can only ever suppress a grade,
        never override the schema/contract/linkage/needs-regrade gates
        upstream of it.
        """
        if mode not in ("auto", "exclude"):
            raise GradeValidationError(
                [f"usage override mode must be 'auto' or 'exclude', got {mode!r}"]
            )
        if mode == "exclude":
            if not isinstance(reason, str) or not reason.strip():
                raise GradeValidationError(
                    [
                        "usage override reason is required and must be a "
                        "non-blank string when mode is 'exclude'"
                    ]
                )
            reason = reason.strip()
        else:
            reason = None

        now = datetime.now(UTC).isoformat()
        with self._uow.begin_immediate():
            exists = self._conn.execute(
                "SELECT id FROM grades WHERE id = ?", (grade_id,)
            ).fetchone()
            if exists is None:
                raise GradeValidationError([f"grade {grade_id} not found"])
            self._conn.execute(
                "INSERT INTO grade_usage_overrides (grade_id, mode, reason, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(grade_id) DO UPDATE SET "
                "mode = excluded.mode, reason = excluded.reason, updated_at = excluded.updated_at",
                (grade_id, mode, reason, now),
            )
            row = self._conn.execute(
                "SELECT grade_id, mode, reason, updated_at FROM grade_usage_overrides "
                "WHERE grade_id = ?",
                (grade_id,),
            ).fetchone()
            assert row is not None
            return GradeUsageOverride(
                grade_id=row["grade_id"],
                mode=row["mode"],
                reason=row["reason"],
                updated_at=row["updated_at"],
            )

    def get_grades_by_scan(self, scan_id: int) -> list[GradeRecord]:
        """Load all grades for evaluations in a given scan."""
        rows = self._conn.execute(
            "SELECT g.* FROM grades g "
            "JOIN evaluations e ON e.id = g.evaluation_id "
            "WHERE e.scan_id = ? "
            "ORDER BY g.id",
            (scan_id,),
        ).fetchall()
        return [self._row_to_grade(row) for row in rows]

    def get_grading_progress(self, scan_id: int) -> tuple[int, int]:
        """Return (graded_count, total_evaluations) for a scan.

        Only grades at HUMAN_GRADE_SCHEMA_VERSION with needs_regrade=0 count
        as graded. Both numerator and denominator are scoped to evaluations
        in the scan.
        """
        row = self._conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM evaluations e "
            "JOIN grades g ON g.evaluation_id = e.id "
            "WHERE e.scan_id = ? "
            f"AND g.schema_version = {HUMAN_GRADE_SCHEMA_VERSION} "
            "AND g.needs_regrade = 0) AS graded, "
            "(SELECT COUNT(*) FROM evaluations WHERE scan_id = ?) AS total",
            (scan_id, scan_id),
        ).fetchone()
        return (int(row["graded"]), int(row["total"]))

    def get_gradeable_items(self, scan_id: int) -> list[GradeableItem]:
        """Load all evaluations with their posts, drafts, and grades for review.

        Includes all evaluation types: relevant drafts, abstentions, gate blocks,
        critic rejections, and irrelevant no-post outcomes. Selects by evaluation
        scan_id so rescore evaluations are surfaced correctly.
        """
        rows = self._conn.execute(
            "SELECT p.id AS post_id, p.platform, p.channel_name, p.author_name, "
            "p.content, p.url, p.created_at, p.scan_id, "
            "p.parent_lookup_status, p.parent_id, p.parent_author_name, "
            "p.parent_text, p.parent_url, "
            "e.id AS evaluation_id, e.scan_id AS eval_scan_id, "
            "e.score, e.reason, e.relevant, e.relevant_to, "
            "e.project_key, e.dossier_revision, e.posture, e.surface_status, "
            "d.id AS draft_id, d.comment_text, d.project_key AS draft_project_key, "
            "c.verdict, c.feedback AS critique_feedback, "
            "g.relevance_judgment, g.action_judgment, "
            "g.dimensions, g.failure_note, g.graded_at, "
            "g.schema_version AS grade_schema_version "
            "FROM evaluations e "
            "JOIN posts p ON p.id = e.post_id "
            "LEFT JOIN draft_comments d ON d.evaluation_id = e.id "
            "LEFT JOIN critiques c ON c.draft_id = d.id "
            "LEFT JOIN grades g ON g.evaluation_id = e.id "
            "WHERE e.scan_id = ? "
            "ORDER BY e.id",
            (scan_id,),
        ).fetchall()
        return [
            GradeableItem(
                post_id=row["post_id"],
                platform=row["platform"],
                channel_name=row["channel_name"],
                author_name=row["author_name"],
                content=row["content"],
                url=row["url"],
                created_at=row["created_at"],
                scan_id=row["scan_id"],
                parent_lookup_status=row["parent_lookup_status"],
                parent_id=row["parent_id"],
                parent_author_name=row["parent_author_name"],
                parent_text=row["parent_text"],
                parent_url=row["parent_url"],
                evaluation_id=row["evaluation_id"],
                eval_scan_id=row["eval_scan_id"],
                score=row["score"],
                reason=row["reason"],
                relevant=bool(row["relevant"]),
                relevant_to=row["relevant_to"],
                project_key=row["project_key"],
                dossier_revision=row["dossier_revision"],
                posture=row["posture"],
                surface_status=row["surface_status"],
                draft_id=row["draft_id"],
                comment_text=row["comment_text"],
                draft_project_key=row["draft_project_key"],
                verdict=row["verdict"],
                critique_feedback=row["critique_feedback"],
                relevance_judgment=row["relevance_judgment"],
                action_judgment=row["action_judgment"],
                dimensions=row["dimensions"],
                failure_note=row["failure_note"],
                graded_at=row["graded_at"],
                grade_schema_version=row["grade_schema_version"],
            )
            for row in rows
        ]

    def get_recent_grading_signals(self, limit_scans: int = 3) -> GradingSignal:
        """Aggregate v2 grading signal from recent scans for prompt injection."""
        rows = self._conn.execute(
            "SELECT g.relevance_judgment, g.action_judgment, g.dimensions, "
            "g.failure_note, g.factual_disposition, g.posture_should_have_been "
            "FROM grades g "
            "JOIN evaluations e ON e.id = g.evaluation_id "
            f"WHERE g.schema_version = {HUMAN_GRADE_SCHEMA_VERSION} AND g.needs_regrade = 0 "
            "AND e.scan_id IN ("
            "  SELECT id FROM scans WHERE completed_at IS NOT NULL "
            "  ORDER BY id DESC LIMIT ?"
            ") ORDER BY g.graded_at DESC",
            (limit_scans,),
        ).fetchall()

        pass_count = 0
        fp_count = 0
        fn_count = 0
        fail_count = 0
        dim_counts: dict[str, int] = {}
        posture_corrections = 0
        factual_unsupported = 0
        factual_contradicted = 0
        causal_examples: list[str] = []

        for r in rows:
            rel = r["relevance_judgment"]
            action = r["action_judgment"]
            if action == "accept":
                pass_count += 1
            else:
                fail_count += 1
            if rel == "false_positive":
                fp_count += 1
            elif rel == "false_negative":
                fn_count += 1

            if r["dimensions"]:
                try:
                    dims = json.loads(r["dimensions"])
                    for d in (dims if isinstance(dims, list) else []):
                        dim_counts[d] = dim_counts.get(d, 0) + 1
                except (json.JSONDecodeError, TypeError):
                    pass

            if r["posture_should_have_been"]:
                posture_corrections += 1
            if r["factual_disposition"] == "unsupported":
                factual_unsupported += 1
            elif r["factual_disposition"] == "contradicted":
                factual_contradicted += 1

            if r["failure_note"] and len(causal_examples) < 5:
                causal_examples.append(str(r["failure_note"])[:120])

        dim_sorted = tuple(
            sorted(dim_counts.items(), key=lambda x: x[1], reverse=True)
        )

        return GradingSignal(
            total_graded=len(rows),
            pass_count=pass_count,
            false_positive_count=fp_count,
            false_negative_count=fn_count,
            fail_count=fail_count,
            dimension_counts=dim_sorted,
            posture_correction_count=posture_corrections,
            factual_unsupported_count=factual_unsupported,
            factual_contradicted_count=factual_contradicted,
            recent_causal_examples=tuple(causal_examples),
        )

    def export_eval_cases(
        self,
        since: datetime | None = None,
        scan_id: int | None = None,
    ) -> list[dict[str, object]]:
        """Export complete current-schema grades as eval cases for regression testing.

        Exports one record per complete HUMAN_GRADE_SCHEMA_VERSION grade
        (needs_regrade=0), including all evaluations (relevant, irrelevant,
        abstentions, failures). Drafts are left-joined so records without
        drafts are still exported.

        When scan_id is provided, cases are restricted to evaluations from that
        scan. Without scan_id, all complete current-schema grades are exported.
        """
        params: list[object] = []

        if scan_id is not None:
            eval_join = (
                "JOIN evaluations e "
                "  ON e.id = g.evaluation_id AND e.scan_id = ? "
            )
            params.append(scan_id)
        else:
            eval_join = "JOIN evaluations e ON e.id = g.evaluation_id "

        query = (
            "SELECT p.id AS post_id, p.content, p.platform, p.channel_name, "
            "p.author_name, p.url, "
            "p.parent_id, p.parent_author_id, p.parent_author_name, "
            "p.parent_text, p.parent_url, p.parent_lookup_status, "
            "e.id AS evaluation_id, e.scan_id AS eval_scan_id, "
            "e.created_at AS eval_created_at, "
            "e.score, e.reason, e.relevant_to, "
            "e.project_key, e.dossier_revision, e.dossier_summary_id, "
            "e.posture, e.surface_status, e.relevant AS source_relevant, "
            "d.id AS draft_id, d.comment_text, d.project_key AS draft_project_key, "
            "g.relevance_judgment, g.action_judgment, g.dimensions, "
            "g.failure_note, g.factual_offending_claim, g.factual_disposition, "
            "g.factual_contradicting_evidence, g.context_missing_input, "
            "g.posture_should_have_been, g.implication_implied_claim, "
            "g.implication_missing_support, g.graded_at, g.source AS grade_source "
            "FROM grades g "
            "JOIN posts p ON p.id = g.post_id "
            + eval_join +
            "LEFT JOIN draft_comments d ON d.evaluation_id = e.id "
            f"WHERE g.schema_version = {HUMAN_GRADE_SCHEMA_VERSION} AND g.needs_regrade = 0 "
        )

        if since is not None:
            query += "AND g.graded_at >= ? "
            params.append(format_graded_at(since))

        query += "ORDER BY g.graded_at DESC"
        rows = self._conn.execute(query, params).fetchall()

        results: list[dict[str, object]] = []
        for row in rows:
            dims: list[str] | None = None
            if row["dimensions"]:
                try:
                    dims = json.loads(row["dimensions"])
                except (json.JSONDecodeError, TypeError):
                    dims = None

            parent: dict[str, object] | None = None
            if row["parent_lookup_status"] == "resolved" and row["parent_id"]:
                parent = {
                    "id": row["parent_id"],
                    "author_id": row["parent_author_id"],
                    "author_name": row["parent_author_name"],
                    "text": row["parent_text"],
                    "url": row["parent_url"],
                }

            results.append({
                "source": {
                    "post_id": row["post_id"],
                    "platform": row["platform"],
                    "channel": row["channel_name"],
                    "author": row["author_name"],
                    "content": row["content"],
                    "url": row["url"],
                    "parent": parent,
                },
                "evaluation": {
                    "evaluation_id": row["evaluation_id"],
                    "scan_id": row["eval_scan_id"],
                    "project_key": row["project_key"],
                    "dossier_revision": row["dossier_revision"],
                    "dossier_summary_id": row["dossier_summary_id"],
                    "posture": row["posture"],
                    "surface_status": row["surface_status"],
                    "source_relevant": bool(row["source_relevant"]),
                    "score": row["score"],
                    "draft": row["comment_text"],
                    "eval_created_at": row["eval_created_at"],
                },
                "grade": {
                    "relevance_judgment": row["relevance_judgment"],
                    "action_judgment": row["action_judgment"],
                    "dimensions": dims,
                    "failure_note": row["failure_note"],
                    "factual_offending_claim": row["factual_offending_claim"],
                    "factual_disposition": row["factual_disposition"],
                    "factual_contradicting_evidence": row["factual_contradicting_evidence"],
                    "context_missing_input": row["context_missing_input"],
                    "posture_should_have_been": row["posture_should_have_been"],
                    "implication_implied_claim": row["implication_implied_claim"],
                    "implication_missing_support": row["implication_missing_support"],
                    "graded_at": row["graded_at"],
                    "grade_source": row["grade_source"],
                },
            })
        return results

    def _row_to_grade(self, row: sqlite3.Row) -> GradeRecord:
        """Convert a DB row to a GradeRecord (handles both v1 and v2 rows)."""
        keys = row.keys()
        dims_raw = row["dimensions"] if "dimensions" in keys else None
        dims: list[str] | None = None
        if dims_raw:
            try:
                parsed = json.loads(dims_raw)
                dims = parsed if isinstance(parsed, list) else None
            except (json.JSONDecodeError, TypeError):
                dims = None

        return GradeRecord(
            post_id=row["post_id"],
            scan_id=row["scan_id"],
            source=row["source"],
            graded_at=_parse_stored_graded_at(row["graded_at"]),
            relevance_judgment=row["relevance_judgment"],
            evaluation_id=row["evaluation_id"],
            schema_version=(
                int(row["schema_version"])
                if "schema_version" in keys and row["schema_version"] is not None
                else 1
            ),
            needs_regrade=(
                bool(row["needs_regrade"])
                if "needs_regrade" in keys and row["needs_regrade"] is not None
                else False
            ),
            action_judgment=row["action_judgment"] if "action_judgment" in keys else None,
            dimensions=dims,
            failure_note=row["failure_note"] if "failure_note" in keys else None,
            factual_offending_claim=(
                row["factual_offending_claim"] if "factual_offending_claim" in keys else None
            ),
            factual_disposition=(
                row["factual_disposition"] if "factual_disposition" in keys else None
            ),
            factual_contradicting_evidence=(
                row["factual_contradicting_evidence"]
                if "factual_contradicting_evidence" in keys
                else None
            ),
            context_missing_input=(
                row["context_missing_input"] if "context_missing_input" in keys else None
            ),
            posture_should_have_been=(
                row["posture_should_have_been"]
                if "posture_should_have_been" in keys
                else None
            ),
            implication_implied_claim=(
                row["implication_implied_claim"]
                if "implication_implied_claim" in keys
                else None
            ),
            implication_missing_support=(
                row["implication_missing_support"]
                if "implication_missing_support" in keys
                else None
            ),
            edited_text=self._resolve_reply_revision_text(
                row["reply_revision_id"] if "reply_revision_id" in keys else None
            ),
        )

    def _resolve_reply_revision_text(self, reply_revision_id: int | None) -> str | None:
        """Read back edited_text from its durable home: reply_revision_id
        points at the reply_draft_revisions row that carries the actual
        corrected text (grades has no text column of its own for it)."""
        if reply_revision_id is None:
            return None
        row = self._conn.execute(
            "SELECT reply_text FROM reply_draft_revisions WHERE id = ?",
            (reply_revision_id,),
        ).fetchone()
        return row["reply_text"] if row is not None else None
