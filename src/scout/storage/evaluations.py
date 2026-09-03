"""Evaluation aggregate: relevance evaluations, phase-run trace identity,
model-comparison experiments, drafts, surfaced events, critiques, gate
blocks, and evaluation-feedback snapshots. Owns `evaluations`,
`evaluation_phase_runs`, `experiment_runs`, `evaluation_experiments`,
`trace_comparisons`, `draft_comments`, `surfaced_events`, `critiques`,
`gate_blocks`, `feedback_snapshots`, `feedback_snapshot_phases`, and
`feedback_snapshot_items`.

`list_evaluation_ids_with_reply_correction` reads `grades` (owned by
GradeStore) as a read-only filter; `get_draft_for_evaluation` is this
store's public read GradeStore uses to validate a grade's target
evaluation/draft without either store opening a second connection — see
`unit_of_work.py`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from scout.config import CritiqueLesson, RelevanceResult
from scout.grading.feedback import (
    FeedbackMode,
    PersistedFeedbackSnapshot,
    PhaseFeedbackBundle,
    aggregate_phase_evidence,
    classify_feedback_eligibility,
    load_committed_phase_bundle,
    load_grade_population,
    persist_feedback_snapshot,
    project_phase_evidence,
    render_feedback_sections,
    resolve_feedback_policy_config,
    select_phase_examples,
)
from scout.storage.unit_of_work import UnitOfWork
from scout.verifier import GateViolation

SURFACE_STATUSES: frozenset[str] = frozenset({
    "surfaced", "low_relevance", "abstained", "critic_rejected",
    "gate_blocked", "not_relevant", "drafting_failed",
})

# Canonical phase execution order. Ordinary model-scored evaluations use a
# prefix of the full sequence. Human-positive promotions intentionally skip
# a second relevance model call and use the response-only suffix.
PHASE_RUN_PHASE_ORDER: tuple[str, ...] = ("relevance", "reply_draft", "critic")
PHASE_RUN_NORMAL_SEQUENCES: frozenset[tuple[str, ...]] = frozenset(
    {
        ("relevance",),
        ("relevance", "reply_draft"),
        ("relevance", "reply_draft", "critic"),
    }
)
PHASE_RUN_RESPONSE_ONLY_SEQUENCES: frozenset[tuple[str, ...]] = frozenset(
    {("reply_draft",), ("reply_draft", "critic")}
)
PHASE_RUN_STATUSES: frozenset[str] = frozenset({"complete", "error", "cancelled"})

EXPERIMENT_STATUSES: frozenset[str] = frozenset({"queued", "running", "complete", "failed"})
EXPERIMENT_RUN_STATUSES: frozenset[str] = frozenset(
    {"queued", "running", "complete", "partial", "failed"}
)

# PAA author_rate producer version (paa/declarations.py resolves the
# author_rate evaluator against this). Lives beside the serialized
# rate-window decision in persist_surfaced_outcome below — the sole
# production seam that decides an author_rate outcome. Bump together with
# the PAA declaration reference whenever the cap policy or decision logic
# changes.
AUTHOR_RATE_EVALUATOR_VERSION = "1"


class SurfaceRateLimitedError(RuntimeError):
    """A candidate passed content gates but lost the serialized author-rate gate.

    Raised only after the losing candidate's gate-blocked evaluation and
    author_rate gate_blocks row have been committed durably under the same
    write lock that observed the cap — persisted_evaluation_id and
    gate_block_ids let callers confirm and log that durable outcome without
    querying by guesswork.
    """

    def __init__(
        self,
        author_id: str,
        count: int,
        cap: int,
        *,
        persisted_evaluation_id: int,
        gate_block_ids: tuple[int, ...],
    ) -> None:
        super().__init__(f"{count} surfaced events for {author_id} in the last 7 days (cap {cap})")
        self.author_id = author_id
        self.count = count
        self.cap = cap
        self.persisted_evaluation_id = persisted_evaluation_id
        self.gate_block_ids = gate_block_ids


class PhaseRunLinkageError(RuntimeError):
    """A supplied set of contributor evaluation_phase_runs ids could not be
    atomically linked to a new evaluation.

    Raised when the ids are empty, contain a duplicate, or when the
    prevalidation select or the affected-row count of the linking update
    does not match the supplied id count — cross-scan/post ids, an id
    already linked to another evaluation, a non-'complete' status, or an
    id that does not match the expected phase sequence all surface here.
    Raising inside the caller's evaluation-insert transaction rolls back
    both the new evaluation and every phase-run update attempted for it.
    """


class ExperimentCASError(RuntimeError):
    """A compare-and-swap lifecycle update on evaluation_experiments (or a
    validation check in insert_experiment_attempt) did not succeed.

    Raised by insert_experiment_attempt, cas_experiment_to_running,
    record_candidate_trace, complete_experiment_with_comparison, and
    fail_experiment whenever the guarding predicate for the transition
    does not match the current row — the attempt is not in the expected
    prior state (already transitioned, concurrently modified, does not
    exist, or — for insert_experiment_attempt — the supplied
    supersedes_experiment_id does not name that baseline case's actual
    latest attempt). Callers must never retry the same experiment attempt
    id after a CAS failure; a new attempt row (via insert_experiment_attempt)
    is the only way to attempt that baseline case again.
    """


@dataclass(frozen=True, slots=True)
class PhaseRun:
    """One `evaluation_phase_runs` row's stored identity and links."""

    id: int
    scan_id: int
    post_id: int
    evaluation_id: int | None
    snapshot_phase_id: int
    phase: str
    trace_id: str
    model: str
    status: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ExperimentRun:
    """One `experiment_runs` row."""

    id: int
    name: str
    status: str
    candidate_config: str
    created_at: str
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class Experiment:
    """One `evaluation_experiments` attempt row."""

    id: int
    experiment_run_id: int
    phase_run_id: int
    attempt_number: int
    supersedes_experiment_id: int | None
    status: str
    baseline_evidence: str
    candidate_trace_id: str | None
    candidate_llm_call_count: int | None
    candidate_cost: float | None
    error_detail: str | None
    created_at: str
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class TraceComparison:
    """The one `trace_comparisons` row for an experiment attempt."""

    id: int
    experiment_id: int
    trace_a_id: str
    trace_b_id: str
    jig_revision: str
    trace_diff: str
    domain_diff: str
    score_evidence: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class EvaluationRow:
    """One `evaluations` row, mirrored in full."""

    id: int
    post_id: int
    relevant: bool
    score: float
    reason: str | None
    relevant_to: str | None
    keyword_route_id: int | None
    scan_id: int | None
    created_at: str | None
    project_key: str | None
    posture: str | None
    surface_status: str
    failure_reason: str | None
    dossier_summary_id: str | None
    dossier_revision: str | None


@dataclass(frozen=True, slots=True)
class DraftComment:
    """One `draft_comments` row."""

    id: int
    post_id: int
    evaluation_id: int
    project_key: str | None
    comment_text: str | None
    created_at: str | None
    scan_id: int | None
    posture: str | None
    structured_output: str | None
    dossier_summary_id: str | None
    dossier_revision: str | None


def _row_to_phase_run(row: sqlite3.Row) -> PhaseRun:
    return PhaseRun(
        id=row["id"],
        scan_id=row["scan_id"],
        post_id=row["post_id"],
        evaluation_id=row["evaluation_id"],
        snapshot_phase_id=row["snapshot_phase_id"],
        phase=row["phase"],
        trace_id=row["trace_id"],
        model=row["model"],
        status=row["status"],
        created_at=row["created_at"],
    )


def _row_to_experiment(row: sqlite3.Row) -> Experiment:
    return Experiment(
        id=row["id"],
        experiment_run_id=row["experiment_run_id"],
        phase_run_id=row["phase_run_id"],
        attempt_number=row["attempt_number"],
        supersedes_experiment_id=row["supersedes_experiment_id"],
        status=row["status"],
        baseline_evidence=row["baseline_evidence"],
        candidate_trace_id=row["candidate_trace_id"],
        candidate_llm_call_count=row["candidate_llm_call_count"],
        candidate_cost=row["candidate_cost"],
        error_detail=row["error_detail"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


def _row_to_evaluation(row: sqlite3.Row) -> EvaluationRow:
    return EvaluationRow(
        id=row["id"],
        post_id=row["post_id"],
        relevant=bool(row["relevant"]),
        score=row["score"],
        reason=row["reason"],
        relevant_to=row["relevant_to"],
        keyword_route_id=row["keyword_route_id"],
        scan_id=row["scan_id"],
        created_at=row["created_at"],
        project_key=row["project_key"],
        posture=row["posture"],
        surface_status=row["surface_status"],
        failure_reason=row["failure_reason"],
        dossier_summary_id=row["dossier_summary_id"],
        dossier_revision=row["dossier_revision"],
    )


def _row_to_draft_comment(row: sqlite3.Row) -> DraftComment:
    return DraftComment(
        id=row["id"],
        post_id=row["post_id"],
        evaluation_id=row["evaluation_id"],
        project_key=row["project_key"],
        comment_text=row["comment_text"],
        created_at=row["created_at"],
        scan_id=row["scan_id"],
        posture=row["posture"],
        structured_output=row["structured_output"],
        dossier_summary_id=row["dossier_summary_id"],
        dossier_revision=row["dossier_revision"],
    )


class EvaluationStore:
    """Owns relevance evaluations, phase-run trace identity, experiments,
    drafts, surfaced events, critiques, gate blocks, and feedback snapshots."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    @property
    def _conn(self) -> sqlite3.Connection:
        return self._uow.conn

    def insert_phase_run(
        self,
        *,
        scan_id: int,
        post_id: int,
        snapshot_phase_id: int,
        phase: str,
        trace_id: str,
        model: str,
        status: str,
    ) -> int:
        """Durably record one phase attempt's trace identity, unlinked to
        any evaluation.

        Callers (pipeline._run_phase) must only call this after the
        phase's AGENT_RUN trace has been finalized, flushed, and read back
        from the configured trace store as a verified AGENT_RUN root — this
        method itself does not touch Jig or the trace store. Opens its own
        short transaction; must never be called while a model call is in
        flight or with a Db transaction already held open across inference.
        """
        if phase not in PHASE_RUN_PHASE_ORDER:
            raise ValueError(f"unknown phase run phase: {phase!r}")
        if status not in PHASE_RUN_STATUSES:
            raise ValueError(f"unknown phase run status: {status!r}")
        now = datetime.now(UTC).isoformat()
        with self._uow.begin_immediate():
            cursor = self._conn.execute(
                "INSERT INTO evaluation_phase_runs "
                "(scan_id, post_id, evaluation_id, snapshot_phase_id, phase, "
                "trace_id, model, status, created_at) "
                "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)",
                (scan_id, post_id, snapshot_phase_id, phase, trace_id, model, status, now),
            )
            phase_run_id = cursor.lastrowid
        assert phase_run_id is not None
        return phase_run_id

    def get_phase_run(self, phase_run_id: int) -> PhaseRun | None:
        """Return one evaluation_phase_runs row's stored identity and
        links, or None if it does not exist.

        Never returns prompt content or grade evidence — those remain in
        the Jig trace store and feedback_snapshot_phases respectively; this
        is stored-key identity only.
        """
        row = self._conn.execute(
            "SELECT id, scan_id, post_id, evaluation_id, snapshot_phase_id, "
            "phase, trace_id, model, status, created_at "
            "FROM evaluation_phase_runs WHERE id = ?",
            (phase_run_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_phase_run(row)

    def list_complete_reply_draft_phase_runs(self) -> list[PhaseRun]:
        """Every complete reply_draft evaluation_phase_runs row's stored
        identity, ascending id order — the shared read behind every batch
        selector except an explicit --phase-run-id list, which resolves
        each id individually via get_phase_run instead."""
        rows = self._conn.execute(
            "SELECT id, scan_id, post_id, evaluation_id, snapshot_phase_id, "
            "phase, trace_id, model, status, created_at "
            "FROM evaluation_phase_runs "
            "WHERE phase = 'reply_draft' AND status = 'complete' "
            "ORDER BY id",
        ).fetchall()
        return [_row_to_phase_run(row) for row in rows]

    def list_evaluation_ids_with_reply_correction(self) -> set[int]:
        """Every evaluation_id with a grade whose reply_revision_id is set
        (a recorded correction) — used to filter the --graded-with-
        corrections batch selector. Does not itself verify the correction
        text is non-blank or that the revision belongs to this evaluation's
        own draft; resolve_reply_correction_oracle re-verifies the full
        chain per case before any spend."""
        rows = self._conn.execute(
            "SELECT evaluation_id FROM grades "
            "WHERE evaluation_id IS NOT NULL AND reply_revision_id IS NOT NULL",
        ).fetchall()
        return {row["evaluation_id"] for row in rows}

    def create_experiment_run(self, *, name: str, candidate_config: str) -> int:
        """Insert a new, clean 'queued' experiment_runs row and return its id.

        `candidate_config` must already be the fully serialized, validated
        v2 candidate-only JSON document (phase/model/system_prompt/
        system_prompt_sha256/grader_attached) — this method does not
        construct or validate its contents beyond the database's own
        JSON-validity trigger. Raises ValueError for a blank `name` before
        ever touching the database. Every child attempt under this run is
        added separately via insert_experiment_attempt.
        """
        if not name or not name.strip():
            raise ValueError("experiment_runs.name must not be blank")
        now = datetime.now(UTC).isoformat()
        with self._uow.begin_immediate():
            cursor = self._conn.execute(
                "INSERT INTO experiment_runs (name, status, candidate_config, created_at) "
                "VALUES (?, 'queued', ?, ?)",
                (name, candidate_config, now),
            )
            experiment_run_id = cursor.lastrowid
        assert experiment_run_id is not None
        return experiment_run_id

    def get_experiment_run(self, experiment_run_id: int) -> ExperimentRun | None:
        """Return one experiment_runs row, or None if it does not exist."""
        row = self._conn.execute(
            "SELECT id, name, status, candidate_config, created_at, completed_at "
            "FROM experiment_runs WHERE id = ?",
            (experiment_run_id,),
        ).fetchone()
        if row is None:
            return None
        return ExperimentRun(
            id=row["id"],
            name=row["name"],
            status=row["status"],
            candidate_config=row["candidate_config"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
        )

    def complete_experiment_run_without_attempts(self, experiment_run_id: int) -> None:
        """Mark a newly-created run complete when every authorized pair was skipped.

        A run with no child attempts cannot reach a terminal projection through
        ``_recompute_experiment_run_status``. This explicit transition is only
        valid while the run is still queued and has no children, so it cannot
        hide an in-flight or previously attempted case.
        """
        now = datetime.now(UTC).isoformat()
        with self._uow.begin_immediate():
            cursor = self._conn.execute(
                "UPDATE experiment_runs SET status = 'complete', completed_at = ? "
                "WHERE id = ? AND status = 'queued' AND NOT EXISTS ("
                "SELECT 1 FROM evaluation_experiments WHERE experiment_run_id = ?)",
                (now, experiment_run_id, experiment_run_id),
            )
            if cursor.rowcount != 1:
                raise ExperimentCASError(
                    f"expected experiment_run {experiment_run_id} to be queued with no "
                    "attempts, could not complete it"
                )

    def _recompute_experiment_run_status(self, experiment_run_id: int) -> None:
        """Recompute experiment_runs.status as the deterministic projection
        of every linked baseline case's latest attempt.

        Must run inside the caller's active transaction, immediately after
        the child mutation that may have changed the projection — this is
        what makes the parent a transactionally consistent view of its
        children rather than an independently-CASed value. A baseline case
        (phase_run_id) contributes its highest-attempt_number row's status
        only; earlier superseded attempts never affect the projection.
        queued only when every case is still queued; complete/failed only
        when every case's latest attempt agrees; partial when every case is
        terminal but they disagree; running otherwise (at least one case
        still queued or running while another has already gone terminal,
        or at least one case is actively running). completed_at tracks the
        current projection: set to now whenever it lands on a terminal
        status, cleared back to NULL whenever a retry (see
        insert_experiment_attempt) reopens the run to 'running' — unlike a
        child attempt's own status, this projection is deliberately not
        forward-only (experiment_runs_immutable_identity only protects
        name/candidate_config/created_at).
        """
        assert self._uow.in_transaction, (
            "_recompute_experiment_run_status requires an active Db transaction context"
        )
        rows = self._conn.execute(
            "SELECT phase_run_id, attempt_number, status FROM evaluation_experiments "
            "WHERE experiment_run_id = ? ORDER BY phase_run_id, attempt_number",
            (experiment_run_id,),
        ).fetchall()
        latest_status_by_case: dict[int, str] = {}
        for row in rows:
            latest_status_by_case[row["phase_run_id"]] = row["status"]
        statuses = list(latest_status_by_case.values())
        if not statuses:
            return

        terminal = {"complete", "failed"}
        if all(s == "queued" for s in statuses):
            new_status = "queued"
        elif all(s in terminal for s in statuses):
            if all(s == "complete" for s in statuses):
                new_status = "complete"
            elif all(s == "failed" for s in statuses):
                new_status = "failed"
            else:
                new_status = "partial"
        else:
            new_status = "running"

        is_terminal = new_status in ("complete", "partial", "failed")
        completed_at = datetime.now(UTC).isoformat() if is_terminal else None
        self._conn.execute(
            "UPDATE experiment_runs SET status = ?, completed_at = ? WHERE id = ?",
            (new_status, completed_at, experiment_run_id),
        )

    def insert_experiment_attempt(
        self,
        *,
        experiment_run_id: int,
        phase_run_id: int,
        baseline_evidence: str,
        supersedes_experiment_id: int | None = None,
    ) -> int:
        """Insert a new, clean 'queued' evaluation_experiments attempt for
        one baseline case (phase_run_id) under `experiment_run_id`, and
        return its id.

        A baseline case's first attempt under a run passes
        `supersedes_experiment_id=None` and is rejected if that case
        already has an attempt under this run; a retry passes the id of
        that case's current latest attempt under this run and is rejected
        otherwise — closing both silent re-attempts of an already-tried
        case and retries that don't chain from the actual latest attempt.
        attempt_number is allocated as 1 plus the case's existing attempt
        count. A retry is accepted even when the parent run has already
        reached a terminal projected status (complete/partial/failed) —
        that is the main reason retries exist: fixing the one failed
        baseline case in an otherwise-successful batch run without
        reprocessing every other case. Accepting it here legitimately
        reopens the run back to 'running' (and clears completed_at) via
        the recompute below; see experiment_runs_immutable_identity,
        which deliberately leaves status/completed_at unrestricted so this
        reopening is not itself a trigger violation. `baseline_evidence`
        must already be the fully serialized, validated v2 JSON document;
        this method does not construct or validate its contents beyond the
        database's own JSON-validity trigger. Recomputes the parent's
        projected status in the same transaction as the insert.
        """
        now = datetime.now(UTC).isoformat()
        with self._uow.begin_immediate():
            run_row = self._conn.execute(
                "SELECT id FROM experiment_runs WHERE id = ?", (experiment_run_id,)
            ).fetchone()
            if run_row is None:
                raise ExperimentCASError(f"no experiment_runs row with id={experiment_run_id}")
            latest = self._conn.execute(
                "SELECT id, attempt_number, status FROM evaluation_experiments "
                "WHERE experiment_run_id = ? AND phase_run_id = ? "
                "ORDER BY attempt_number DESC LIMIT 1",
                (experiment_run_id, phase_run_id),
            ).fetchone()
            if supersedes_experiment_id is None:
                if latest is not None:
                    raise ExperimentCASError(
                        f"phase_run_id={phase_run_id} already has an attempt under "
                        f"experiment_run {experiment_run_id}; pass supersedes_experiment_id "
                        "to retry it"
                    )
                next_attempt = 1
            else:
                if latest is None or latest["id"] != supersedes_experiment_id:
                    raise ExperimentCASError(
                        f"supersedes_experiment_id={supersedes_experiment_id} is not the "
                        f"latest attempt for (experiment_run_id={experiment_run_id}, "
                        f"phase_run_id={phase_run_id})"
                    )
                if latest["status"] not in ("complete", "failed"):
                    raise ExperimentCASError(
                        f"attempt {supersedes_experiment_id} is still {latest['status']!r}; "
                        "a retry requires the attempt it supersedes to already be terminal"
                    )
                next_attempt = latest["attempt_number"] + 1
            cursor = self._conn.execute(
                "INSERT INTO evaluation_experiments "
                "(experiment_run_id, phase_run_id, attempt_number, supersedes_experiment_id, "
                "status, baseline_evidence, created_at) "
                "VALUES (?, ?, ?, ?, 'queued', ?, ?)",
                (
                    experiment_run_id,
                    phase_run_id,
                    next_attempt,
                    supersedes_experiment_id,
                    baseline_evidence,
                    now,
                ),
            )
            experiment_id = cursor.lastrowid
            self._recompute_experiment_run_status(experiment_run_id)
        assert experiment_id is not None
        return experiment_id

    def get_experiment(self, experiment_id: int) -> Experiment | None:
        """Return one evaluation_experiments attempt row, or None if it
        does not exist."""
        row = self._conn.execute(
            "SELECT id, experiment_run_id, phase_run_id, attempt_number, "
            "supersedes_experiment_id, status, baseline_evidence, candidate_trace_id, "
            "candidate_llm_call_count, candidate_cost, error_detail, created_at, completed_at "
            "FROM evaluation_experiments WHERE id = ?",
            (experiment_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_experiment(row)

    def list_experiment_attempts(self, experiment_run_id: int) -> list[Experiment]:
        """Return every attempt under `experiment_run_id`, oldest first,
        ordered by (phase_run_id, attempt_number) so retry chains stay
        contiguous."""
        rows = self._conn.execute(
            "SELECT id, experiment_run_id, phase_run_id, attempt_number, "
            "supersedes_experiment_id, status, baseline_evidence, candidate_trace_id, "
            "candidate_llm_call_count, candidate_cost, error_detail, created_at, completed_at "
            "FROM evaluation_experiments WHERE experiment_run_id = ? "
            "ORDER BY phase_run_id, attempt_number",
            (experiment_run_id,),
        ).fetchall()
        return [_row_to_experiment(row) for row in rows]

    def get_trace_comparison(self, experiment_id: int) -> TraceComparison | None:
        """Return the one trace_comparisons row for `experiment_id`, or
        None if the experiment has no comparison yet (queued, running, or
        failed before a comparison was persisted)."""
        row = self._conn.execute(
            "SELECT id, experiment_id, trace_a_id, trace_b_id, jig_revision, "
            "trace_diff, domain_diff, score_evidence, created_at "
            "FROM trace_comparisons WHERE experiment_id = ?",
            (experiment_id,),
        ).fetchone()
        if row is None:
            return None
        return TraceComparison(
            id=row["id"],
            experiment_id=row["experiment_id"],
            trace_a_id=row["trace_a_id"],
            trace_b_id=row["trace_b_id"],
            jig_revision=row["jig_revision"],
            trace_diff=row["trace_diff"],
            domain_diff=row["domain_diff"],
            score_evidence=row["score_evidence"],
            created_at=row["created_at"],
        )

    def cas_experiment_to_running(self, experiment_id: int) -> None:
        """Compare-and-swap one experiment attempt from 'queued' to
        'running', and recompute its parent run's projected status.

        Raises ExperimentCASError if the affected-row count is not
        exactly 1 — the experiment does not exist or is not 'queued'.
        """
        with self._uow.begin_immediate():
            cursor = self._conn.execute(
                "UPDATE evaluation_experiments SET status = 'running' "
                "WHERE id = ? AND status = 'queued'",
                (experiment_id,),
            )
            if cursor.rowcount != 1:
                raise ExperimentCASError(
                    f"expected experiment {experiment_id} to be 'queued', "
                    f"could not CAS to 'running'"
                )
            experiment_run_id = self._conn.execute(
                "SELECT experiment_run_id FROM evaluation_experiments WHERE id = ?",
                (experiment_id,),
            ).fetchone()["experiment_run_id"]
            self._recompute_experiment_run_status(experiment_run_id)

    def record_candidate_trace(
        self,
        experiment_id: int,
        *,
        candidate_trace_id: str,
        candidate_llm_call_count: int,
        candidate_cost: float | None,
    ) -> None:
        """Persist the generated candidate AGENT_RUN trace identity and its
        measured attempt count/cost onto a 'running' experiment attempt,
        before any comparison is constructed.

        Callers must only call this after the candidate trace has been
        finalized, flushed, and verified as an AGENT_RUN root — mirroring
        insert_phase_run's evidence contract. Raises ExperimentCASError if
        the affected-row count is not exactly 1 — the experiment is not
        'running' or already has a candidate_trace_id recorded (this method
        is not idempotent-retryable; a distinct trace identity must never
        silently overwrite one already persisted). Never changes status, so
        the parent run's projected status is never affected and is not
        recomputed here.
        """
        with self._uow.begin_immediate():
            cursor = self._conn.execute(
                "UPDATE evaluation_experiments SET candidate_trace_id = ?, "
                "candidate_llm_call_count = ?, candidate_cost = ? "
                "WHERE id = ? AND status = 'running' AND candidate_trace_id IS NULL",
                (candidate_trace_id, candidate_llm_call_count, candidate_cost, experiment_id),
            )
            if cursor.rowcount != 1:
                raise ExperimentCASError(
                    f"expected experiment {experiment_id} to be 'running' with no "
                    f"candidate_trace_id yet, could not record candidate trace"
                )

    def complete_experiment_with_comparison(
        self,
        experiment_id: int,
        *,
        jig_revision: str,
        trace_diff: str,
        domain_diff: str,
        score_evidence: str | None = None,
    ) -> None:
        """Insert the experiment attempt's sole trace_comparisons row and
        CAS the attempt to 'complete' with completed_at, then recompute its
        parent run's projected status, all in one transaction.

        `score_evidence` is the versioned grader/assembler identity plus
        correction hash/revision, both distances, and their delta — pass
        None for an ungraded (relevance/critic, or grader-ineligible)
        comparison. Requires the experiment to already be 'running' with a
        candidate_trace_id recorded (via record_candidate_trace). Raises
        ExperimentCASError if the affected-row count on the completing
        UPDATE is not exactly 1 — rolling back the comparison insert too,
        so a 'complete' experiment always implies exactly one durable,
        valid trace_comparisons row and vice versa.
        """
        try:
            trace_diff_doc = json.loads(trace_diff)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("trace_diff must be valid JSON with trace identities") from exc
        if not isinstance(trace_diff_doc, dict):
            raise ValueError("trace_diff must be a JSON object with trace identities")
        trace_a_id = trace_diff_doc.get("trace_a_id")
        trace_b_id = trace_diff_doc.get("trace_b_id")
        if not isinstance(trace_a_id, str) or not trace_a_id:
            raise ValueError("trace_diff.trace_a_id must be a non-empty string")
        if not isinstance(trace_b_id, str) or not trace_b_id:
            raise ValueError("trace_diff.trace_b_id must be a non-empty string")

        now = datetime.now(UTC).isoformat()
        with self._uow.begin_immediate():
            experiment = self._conn.execute(
                "SELECT e.status, e.candidate_trace_id, e.experiment_run_id, "
                "pr.trace_id AS baseline_trace_id "
                "FROM evaluation_experiments e "
                "JOIN evaluation_phase_runs pr ON pr.id = e.phase_run_id "
                "WHERE e.id = ?",
                (experiment_id,),
            ).fetchone()
            if (
                experiment is None
                or experiment["status"] != "running"
                or experiment["candidate_trace_id"] is None
            ):
                raise ExperimentCASError(
                    f"expected experiment {experiment_id} to be 'running' with a "
                    f"candidate_trace_id, could not CAS to 'complete'"
                )
            self._conn.execute(
                "INSERT INTO trace_comparisons "
                "(experiment_id, trace_a_id, trace_b_id, jig_revision, "
                "trace_diff, domain_diff, score_evidence, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    experiment_id,
                    trace_a_id,
                    trace_b_id,
                    jig_revision,
                    trace_diff,
                    domain_diff,
                    score_evidence,
                    now,
                ),
            )
            cursor = self._conn.execute(
                "UPDATE evaluation_experiments SET status = 'complete', completed_at = ? "
                "WHERE id = ? AND status = 'running' AND candidate_trace_id IS NOT NULL",
                (now, experiment_id),
            )
            if cursor.rowcount != 1:
                raise ExperimentCASError(
                    f"expected experiment {experiment_id} to be 'running' with a "
                    f"candidate_trace_id, could not CAS to 'complete'"
                )
            self._recompute_experiment_run_status(experiment["experiment_run_id"])

    def fail_experiment(self, experiment_id: int, *, error_detail: str) -> None:
        """Compare-and-swap one 'running' experiment attempt to 'failed',
        recording a sanitized, operator-safe error_detail, then recompute
        its parent run's projected status.

        Never invents a candidate identity — whatever candidate_trace_id/
        candidate_llm_call_count/candidate_cost were already recorded (or
        left NULL) are retained unchanged. Raises ExperimentCASError if the
        affected-row count is not exactly 1 — the experiment is not
        'running'.
        """
        now = datetime.now(UTC).isoformat()
        with self._uow.begin_immediate():
            cursor = self._conn.execute(
                "UPDATE evaluation_experiments SET status = 'failed', "
                "error_detail = ?, completed_at = ? "
                "WHERE id = ? AND status = 'running'",
                (error_detail, now, experiment_id),
            )
            if cursor.rowcount != 1:
                raise ExperimentCASError(
                    f"expected experiment {experiment_id} to be 'running', "
                    f"could not CAS to 'failed'"
                )
            experiment_run_id = self._conn.execute(
                "SELECT experiment_run_id FROM evaluation_experiments WHERE id = ?",
                (experiment_id,),
            ).fetchone()["experiment_run_id"]
            self._recompute_experiment_run_status(experiment_run_id)

    def _link_phase_run_contributors(
        self,
        *,
        evaluation_id: int,
        scan_id: int,
        post_id: int,
        contributor_phase_run_ids: Sequence[int],
        allow_response_only: bool = False,
    ) -> None:
        """Link the ordered contributor phase-run ids to `evaluation_id`.

        Requires an already-open Db transaction from the caller (the same
        transaction that inserted the evaluation) so a mismatch rolls both
        back together. Rejects empty or duplicate ids outright. Prevalidates
        every id belongs to `scan_id`/`post_id`, is status='complete', is
        still unlinked, and matches the canonical phase sequence at its
        position before issuing the linking UPDATE, then verifies the
        UPDATE's affected-row count equals the supplied id count — closing
        cross-scan, cross-post, double-link, and partial-link races.
        """
        assert self._uow.in_transaction, (
            "_link_phase_run_contributors requires an active Db transaction context"
        )
        ids = list(contributor_phase_run_ids)
        if not ids:
            raise PhaseRunLinkageError(
                "contributor_phase_run_ids must not be empty"
            )
        if len(ids) != len(set(ids)):
            raise PhaseRunLinkageError(
                "contributor_phase_run_ids must not contain duplicates"
            )
        placeholders = ",".join("?" for _ in ids)
        rows = self._conn.execute(
            f"SELECT id, phase FROM evaluation_phase_runs "
            f"WHERE id IN ({placeholders}) AND scan_id = ? AND post_id = ? "
            f"AND status = 'complete' AND evaluation_id IS NULL",
            (*ids, scan_id, post_id),
        ).fetchall()
        phase_by_id = {row["id"]: row["phase"] for row in rows}
        if len(phase_by_id) != len(ids):
            raise PhaseRunLinkageError(
                f"expected {len(ids)} unlinked complete phase runs for "
                f"scan_id={scan_id} post_id={post_id}, found {len(phase_by_id)}"
            )
        actual_sequence = tuple(phase_by_id[phase_run_id] for phase_run_id in ids)
        allowed_sequences = PHASE_RUN_NORMAL_SEQUENCES
        if allow_response_only:
            allowed_sequences = allowed_sequences | PHASE_RUN_RESPONSE_ONLY_SEQUENCES
        if actual_sequence not in allowed_sequences:
            raise PhaseRunLinkageError(
                f"phase runs have unsupported sequence {actual_sequence!r}"
            )
        cursor = self._conn.execute(
            f"UPDATE evaluation_phase_runs SET evaluation_id = ? "
            f"WHERE id IN ({placeholders}) AND scan_id = ? AND post_id = ? "
            f"AND status = 'complete' AND evaluation_id IS NULL",
            (evaluation_id, *ids, scan_id, post_id),
        )
        if cursor.rowcount != len(ids):
            raise PhaseRunLinkageError(
                f"expected to link {len(ids)} phase runs to evaluation "
                f"{evaluation_id}, linked {cursor.rowcount}"
            )

    def save_evaluation(
        self,
        result: RelevanceResult,
        post_id: int,
        scan_id: int,
        keyword_route_id: int | None = None,
        project_key: str | None = None,
        posture: str | None = None,
        surface_status: str = "not_relevant",
        failure_reason: str | None = None,
        dossier_revision: str | None = None,
        dossier_summary_id: str | None = None,
    ) -> int:
        """Save a terminal relevance evaluation outside a surfaced unit."""
        if surface_status not in SURFACE_STATUSES:
            raise ValueError(f"unknown surface status: {surface_status}")
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            cursor = self._conn.execute(
                "INSERT INTO evaluations "
                "(post_id, relevant, score, reason, relevant_to, keyword_route_id, "
                "scan_id, created_at, project_key, posture, surface_status, failure_reason, "
                "dossier_revision, dossier_summary_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    post_id,
                    int(result.relevant),
                    result.score,
                    result.reason,
                    json.dumps(result.relevant_to),
                    keyword_route_id,
                    scan_id,
                    now,
                    project_key,
                    posture,
                    surface_status,
                    failure_reason,
                    dossier_revision,
                    dossier_summary_id,
                ),
            )
            evaluation_id = cursor.lastrowid
        assert evaluation_id is not None
        return evaluation_id

    def persist_terminal_outcome(
        self,
        result: RelevanceResult,
        post_id: int,
        scan_id: int,
        *,
        surface_status: str,
        contributor_phase_run_ids: Sequence[int],
        keyword_route_id: int | None = None,
        project_key: str | None = None,
        posture: str | None = None,
        failure_reason: str | None = None,
        dossier_revision: str | None = None,
        dossier_summary_id: str | None = None,
        critique: tuple[str, str] | None = None,
        gate_violations: Iterable[object] | None = None,
        allow_response_only_phase_runs: bool = False,
    ) -> int:
        """Persist a no-draft terminal outcome and its durable evidence.

        This deliberately never creates a draft or surfaced event.  It keeps
        all non-surfaced paths visible without inventing an approved reply.

        `contributor_phase_run_ids` must be the ordered, deduplicated ids of
        every phase actually executed successfully before this terminal
        decision (see pipeline.score_and_draft_step) — linked to the new
        evaluation in the same transaction as its insert, so there is no
        externally visible state where the evaluation exists without its
        contributors linked.

        Runs as its own complete workflow boundary: a root call flushes any
        caller-left implicit transaction (e.g. from ``save_post``) and opens
        a fresh BEGIN IMMEDIATE, so each post's terminal outcome becomes
        durable as soon as this call returns rather than staying pinned to
        whatever the caller eventually commits. A caller that wants this
        composed atomically with other writes should open its own outer
        ``self.db`` context first — this call then joins it via savepoint.
        """
        if surface_status == "surfaced":
            raise ValueError("use persist_surfaced_outcome for surfaced results")
        with self._uow.begin_immediate():
            evaluation_id = self.save_evaluation(
                result, post_id, scan_id, keyword_route_id, project_key, posture,
                surface_status, failure_reason, dossier_revision, dossier_summary_id,
            )
            self._link_phase_run_contributors(
                evaluation_id=evaluation_id,
                scan_id=scan_id,
                post_id=post_id,
                contributor_phase_run_ids=contributor_phase_run_ids,
                allow_response_only=allow_response_only_phase_runs,
            )
            if critique is not None:
                self.save_critique(None, critique[0], critique[1], scan_id, evaluation_id)
            if gate_violations:
                context = f"{result.message.platform}:{result.message.platform_id}"
                self._save_gate_violations(
                    gate_violations, project_key, dossier_summary_id, dossier_revision,
                    scan_id, post_id, evaluation_id, context,
                )
            return evaluation_id

    def persist_surfaced_outcome(
        self,
        result: RelevanceResult,
        post_id: int,
        scan_id: int,
        *,
        project_key: str,
        author_id: str,
        platform: str,
        comment_text: str,
        structured_output: str | None,
        contributor_phase_run_ids: Sequence[int],
        keyword_route_id: int | None = None,
        posture: str | None = None,
        critique: tuple[str, str] | None = None,
        dossier_revision: str | None = None,
        dossier_summary_id: str | None = None,
        surfaced_at: str | None = None,
        fail_at: str | None = None,
        allow_response_only_phase_runs: bool = False,
    ) -> tuple[int, int, int]:
        """Atomically write exactly one surfaced evaluation, draft, and event.

        The immediate transaction serializes the rate-window read with the
        event insert.  When the serialized count is at or above the cap, this
        instead writes a durable gate_blocked evaluation and one author_rate
        gate_blocks row under the same lock, commits that losing outcome, and
        raises SurfaceRateLimitedError — the exception signals control flow
        to the caller while the terminal outcome it names is already durable.
        Any other exception rolls the entire unit back; callers may then
        persist a separate terminal gate-blocked evaluation if desired.
        ``fail_at`` is an intentionally narrow test seam for rollback proof.

        `contributor_phase_run_ids` is linked to whichever evaluation this
        call commits — the happy surfaced path or the losing author-rate
        gate-blocked path — in the same transaction as that evaluation's
        insert, exactly as persist_terminal_outcome does.
        """
        from datetime import timedelta

        from scout.config import SCOUT_AUTHOR_WEEKLY_CAP

        if not result.relevant:
            raise ValueError("only relevant results can be surfaced")

        # Built but not raised inside the `with` block below: raising there
        # would exit begin_immediate() via its exception path, which rolls
        # back rather than commits — wrong for a rate-limit outcome that
        # must land durably. Constructing it here and raising only after
        # the block exits normally also keeps this call safe to nest (a
        # mid-block commit() would otherwise commit an enclosing caller's
        # transaction too, and Db's exception handling would then try to
        # release/rollback a savepoint that no longer exists).
        rate_limited: SurfaceRateLimitedError | None = None
        outcome: tuple[int, int, int] | None = None
        with self._uow.begin_immediate():
            cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
            count = int(self._conn.execute(
                "SELECT COUNT(*) FROM surfaced_events WHERE author_id = ? AND created_at >= ?",
                (author_id, cutoff),
            ).fetchone()[0])
            context = f"{result.message.platform}:{result.message.platform_id}"
            if count >= SCOUT_AUTHOR_WEEKLY_CAP:
                evaluation_id = self.save_evaluation(
                    result, post_id, scan_id, keyword_route_id, project_key, posture,
                    "gate_blocked", None, dossier_revision, dossier_summary_id,
                )
                self._link_phase_run_contributors(
                    evaluation_id=evaluation_id,
                    scan_id=scan_id,
                    post_id=post_id,
                    contributor_phase_run_ids=contributor_phase_run_ids,
                    allow_response_only=allow_response_only_phase_runs,
                )
                gate_block_ids = self._save_gate_violations(
                    [GateViolation(
                        reason_code="author_rate",
                        offending_text=(
                            f"{count} events in last 7 days (cap {SCOUT_AUTHOR_WEEKLY_CAP})"
                        ),
                        segment_index=None,
                    )],
                    project_key, dossier_summary_id, dossier_revision,
                    scan_id, post_id, evaluation_id, context,
                )
                rate_limited = SurfaceRateLimitedError(
                    author_id, count, SCOUT_AUTHOR_WEEKLY_CAP,
                    persisted_evaluation_id=evaluation_id,
                    gate_block_ids=tuple(gate_block_ids),
                )
            else:
                evaluation_id = self.save_evaluation(
                    result, post_id, scan_id, keyword_route_id, project_key, posture,
                    "surfaced", None, dossier_revision, dossier_summary_id,
                )
                self._link_phase_run_contributors(
                    evaluation_id=evaluation_id,
                    scan_id=scan_id,
                    post_id=post_id,
                    contributor_phase_run_ids=contributor_phase_run_ids,
                    allow_response_only=allow_response_only_phase_runs,
                )
                if fail_at == "evaluation":
                    raise sqlite3.IntegrityError("injected evaluation failure")
                draft_id = self.save_draft(
                    post_id, evaluation_id, project_key, comment_text, scan_id, posture,
                    structured_output, dossier_revision, dossier_summary_id,
                )
                if fail_at == "draft":
                    raise sqlite3.IntegrityError("injected draft failure")
                if critique is not None:
                    self.save_critique(
                        draft_id, critique[0], critique[1], scan_id, evaluation_id
                    )
                if fail_at == "critique":
                    raise sqlite3.IntegrityError("injected critique failure")
                event_id = self.save_surfaced_event(
                    post_id, evaluation_id, draft_id, scan_id, project_key, author_id,
                    platform, surfaced_at,
                )
                if fail_at == "event":
                    raise sqlite3.IntegrityError("injected event failure")
                outcome = (evaluation_id, draft_id, event_id)

        # The exception signals control flow to the caller — the terminal
        # outcome it names is already durable, committed above.
        if rate_limited is not None:
            raise rate_limited
        assert outcome is not None
        return outcome

    def _save_gate_violations(
        self,
        violations: Iterable[object],
        project_key: str | None,
        dossier_summary_id: str | None,
        dossier_revision: str | None,
        scan_id: int,
        post_id: int,
        evaluation_id: int,
        context: str | None = None,
    ) -> list[int]:
        """Shared multi-step SQL helper: requires an already-open Db
        context from the caller and never commits on its own."""
        assert self._uow.in_transaction, (
            "_save_gate_violations requires an active Db transaction context"
        )
        now = datetime.now(UTC).isoformat()
        ids: list[int] = []
        for violation in violations:
            record = cast(Any, violation)
            cursor = self._conn.execute(
                "INSERT INTO gate_blocks (reason_code, offending_text, segment_index, "
                "project_key, dossier_summary_id, dossier_revision, scan_id, post_id, "
                "evaluation_id, context, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.reason_code,
                    getattr(record, "offending_text", None),
                    getattr(record, "segment_index", None),
                    project_key,
                    dossier_summary_id,
                    dossier_revision,
                    scan_id,
                    post_id,
                    evaluation_id,
                    context,
                    now,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("gate_blocks INSERT did not return a row id")
            ids.append(cursor.lastrowid)
        return ids

    def get_latest_evaluation_id(self, post_id: int, scan_id: int) -> int | None:
        """Return the most recent evaluation id for a post within a scan, or None."""
        row = self._conn.execute(
            "SELECT id FROM evaluations WHERE post_id = ? AND scan_id = ? ORDER BY id DESC LIMIT 1",
            (post_id, scan_id),
        ).fetchone()
        if row is None:
            return None
        return int(row["id"])

    def save_draft(
        self,
        post_id: int,
        evaluation_id: int,
        project_key: str,
        comment_text: str,
        scan_id: int,
        posture: str | None = None,
        structured_output: str | None = None,
        dossier_revision: str | None = None,
        dossier_summary_id: str | None = None,
    ) -> int:
        """Save a draft engagement comment."""
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            cursor = self._conn.execute(
                "INSERT INTO draft_comments "
                "(post_id, evaluation_id, project_key, comment_text, created_at, scan_id, "
                "posture, structured_output, dossier_revision, dossier_summary_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    post_id,
                    evaluation_id,
                    project_key,
                    comment_text,
                    now,
                    scan_id,
                    posture,
                    structured_output,
                    dossier_revision,
                    dossier_summary_id,
                ),
            )
            draft_id = cursor.lastrowid
        assert draft_id is not None
        return draft_id

    def save_surfaced_event(
        self,
        post_id: int,
        evaluation_id: int,
        draft_id: int,
        scan_id: int,
        project_key: str,
        author_id: str,
        platform: str,
        surfaced_at: str | None = None,
    ) -> int:
        """Record a draft that passed all gates and was surfaced to the operator.

        surfaced_at should be a stable per-post timestamp (e.g. the post's
        created_at) so that the UNIQUE(platform, author_id, surfaced_at)
        constraint deduplicates retries correctly.  Defaults to the current
        time when not provided.
        """
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            cursor = self._conn.execute(
                "INSERT INTO surfaced_events "
                "(platform, author_id, surfaced_at, post_id, evaluation_id, draft_id, "
                "project_key, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    platform,
                    author_id,
                    surfaced_at or now,
                    post_id,
                    evaluation_id,
                    draft_id,
                    project_key,
                    now,
                ),
            )
            event_id = cursor.lastrowid
        assert event_id is not None
        return event_id

    def save_critique(
        self,
        draft_id: int | None,
        verdict: str,
        feedback: str,
        scan_id: int,
        evaluation_id: int | None = None,
    ) -> int:
        """Save a critique verdict for a draft comment."""
        now = datetime.now(UTC).isoformat()
        with self._uow.begin():
            if evaluation_id is None:
                if draft_id is None:
                    raise ValueError("critique requires a draft_id or evaluation_id")
                row = self._conn.execute(
                    "SELECT evaluation_id FROM draft_comments WHERE id = ?", (draft_id,)
                ).fetchone()
                if row is None or row["evaluation_id"] is None:
                    raise ValueError(f"draft #{draft_id} has no evaluation")
                evaluation_id = int(row["evaluation_id"])
            cursor = self._conn.execute(
                "INSERT INTO critiques (draft_id, evaluation_id, verdict, feedback, "
                "created_at, scan_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (draft_id, evaluation_id, verdict, feedback, now, scan_id),
            )
            critique_id = cursor.lastrowid
        assert critique_id is not None
        return critique_id

    def get_recent_critique_feedback(self, limit: int = 10) -> list[CritiqueLesson]:
        """Pull recent revise/reject critiques as lessons for the generator."""
        rows = self._conn.execute(
            "SELECT c.verdict, c.feedback, d.comment_text "
            "FROM critiques c "
            "JOIN draft_comments d ON c.draft_id = d.id "
            "WHERE c.verdict IN ('revise', 'reject') "
            "ORDER BY c.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            CritiqueLesson(
                verdict=row["verdict"],
                feedback=row["feedback"] or "",
                comment_text=row["comment_text"] or "",
            )
            for row in rows
        ]

    def get_evaluation(self, evaluation_id: int) -> EvaluationRow | None:
        """Load an evaluation row by id, or None if not found."""
        row = self._conn.execute(
            "SELECT * FROM evaluations WHERE id = ?",
            (evaluation_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_evaluation(row)

    def get_draft_for_evaluation(self, evaluation_id: int) -> DraftComment | None:
        """Load the (at most one) draft_comments row for an evaluation.

        The grade domain's target-evaluation validation (a grade may only
        carry edited_text when its evaluation actually has a draft) reads
        this instead of querying draft_comments directly, so GradeStore
        never issues SQL against a table it does not own.
        """
        row = self._conn.execute(
            "SELECT * FROM draft_comments WHERE evaluation_id = ? LIMIT 1",
            (evaluation_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_draft_comment(row)

    def record_feedback_snapshot(
        self, scan_id: int, *, mode: FeedbackMode
    ) -> PersistedFeedbackSnapshot:
        """Build and persist the evaluation-feedback/v1 snapshot for
        `scan_id`.

        Runs the whole selection -> classification -> phase projection ->
        aggregation -> example selection -> rendering -> persistence
        pipeline (grading/feedback.py) inside one `begin_immediate()`
        transaction, so every read and the final insert see one stable
        database snapshot. Must be called immediately after start_scan and
        before any model call: on any failure the transaction rolls back
        (Db.begin_immediate()'s normal exception handling) and the
        exception propagates for the caller to fail the scan through the
        existing terminal scan-failure path.

        `mode` must be resolved once by the caller (from
        FEEDBACK_PROMPT_ENABLED) before this call, not re-read here — the
        stored snapshot's mode always matches the mode the scan's prompts
        actually used. In shadow mode the snapshot is for id/count/hash
        logging only; in active mode the caller loads the committed phase
        bundle separately via `load_committed_feedback_bundle` after this
        transaction commits.
        """
        config = resolve_feedback_policy_config()
        as_of = datetime.now(UTC)
        with self._uow.begin_immediate():
            population = load_grade_population(self._conn, as_of=as_of, config=config)
            eligibility = classify_feedback_eligibility(population, config=config)
            projections = project_phase_evidence(eligibility)
            aggregates = aggregate_phase_evidence(projections, config=config)
            examples = select_phase_examples(projections, config=config)
            sections = render_feedback_sections(aggregates, examples, config=config)
            return persist_feedback_snapshot(
                self._conn,
                scan_id=scan_id,
                mode=mode,
                as_of=as_of,
                config=config,
                population=population,
                eligibility=eligibility,
                projections=projections,
                examples=examples,
                sections=sections,
                recorded_at=datetime.now(UTC),
            )

    def load_committed_feedback_bundle(
        self, snapshot_id: int, *, expected_mode: FeedbackMode
    ) -> PhaseFeedbackBundle:
        """Load the three committed `feedback_snapshot_phases` rows for
        `snapshot_id` and build a phase-keyed bundle of stored
        `rendered_text`.

        Must be called after `record_feedback_snapshot`'s transaction has
        committed, so this reads exactly what is durable — never the
        in-memory `RenderedFeedbackSection` values from that call. Raises
        `FeedbackBundleIntegrityError` on a missing phase, hash mismatch,
        mode mismatch, or unresolvable snapshot id; there is no legacy
        fallback for evaluation-feedback/v1 active mode.
        """
        return load_committed_phase_bundle(
            self._conn, snapshot_id=snapshot_id, expected_mode=expected_mode
        )

    def count_relevant_evaluations(self) -> int:
        """Total relevant evaluations across all scans — one leg of ScanStats."""
        return int(
            self._conn.execute(
                "SELECT COUNT(*) FROM evaluations WHERE relevant = 1"
            ).fetchone()[0]
        )

    def count_drafts(self) -> int:
        """Total draft_comments rows ever recorded — one leg of ScanStats."""
        return int(self._conn.execute("SELECT COUNT(*) FROM draft_comments").fetchone()[0])
