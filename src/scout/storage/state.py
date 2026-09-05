"""SQLite-backed state persistence for scout.

StateManager is a backward-compatible facade: every retained-path method
signature is unchanged, but the implementation now delegates to five
aggregate stores (ScanStore, PostStore, EvaluationStore, GradeStore,
RegistryStore), each constructed with the same UnitOfWork/Db this class
owns — see unit_of_work.py. The append-only autonomy_events surface
remains defined directly on this class.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import asdict
from datetime import datetime
from types import TracebackType
from typing import Any, cast

# These used to be TYPE_CHECKING-only imports from paa_declarations and
# paa_events, deferred because paa_registry imports this module's evaluator
# version constants and a runtime import would have closed the cycle. Taken
# from paa_runtime they are ordinary imports: it depends on nothing in
# Scout, so there is no cycle to break.
from paa_runtime.events import (
    CURRENT_EVENT_SCHEMA,
    AutonomyEventType,
    AutonomyPosition,
)

from scout.config import (
    CritiqueLesson,
    GradeRecord,
    GradingSignal,
    Message,
    RelevanceResult,
    ScanStats,
)
from scout.grading.feedback import FeedbackMode, PersistedFeedbackSnapshot, PhaseFeedbackBundle
from scout.registry import RuntimeRegistry
from scout.storage.artifacts import ArtifactStore
from scout.storage.db import Db
from scout.storage.evaluations import (
    AUTHOR_RATE_EVALUATOR_VERSION as AUTHOR_RATE_EVALUATOR_VERSION,
)
from scout.storage.evaluations import PHASE_RUN_PHASE_ORDER as PHASE_RUN_PHASE_ORDER
from scout.storage.evaluations import EvaluationStore
from scout.storage.evaluations import ExperimentCASError as ExperimentCASError
from scout.storage.evaluations import PhaseRunLinkageError as PhaseRunLinkageError
from scout.storage.evaluations import SurfaceRateLimitedError as SurfaceRateLimitedError
from scout.storage.grades import GradeStore
from scout.storage.grades import GradeValidationError as GradeValidationError
from scout.storage.grades import (
    HumanPositivePromotionInProgressError as HumanPositivePromotionInProgressError,
)
from scout.storage.grades import format_graded_at as format_graded_at
from scout.storage.grades import parse_graded_at as parse_graded_at
from scout.storage.migrations import MIGRATIONS as MIGRATIONS
from scout.storage.migrations import AutonomyEventsNotEmptyError as AutonomyEventsNotEmptyError
from scout.storage.migrations import GradeConvergenceStatus as GradeConvergenceStatus
from scout.storage.migrations import (
    grade_revision_comparison_shape as grade_revision_comparison_shape,
)
from scout.storage.posts import PostStore
from scout.storage.registry import RegistryStore
from scout.storage.scans import ScanStatus as ScanStatus
from scout.storage.scans import ScanStore
from scout.storage.schema import LATEST_SCHEMA_VERSION as LATEST_SCHEMA_VERSION
from scout.storage.schema import SCHEMA as SCHEMA
from scout.storage.unit_of_work import UnitOfWork

logger = logging.getLogger(__name__)


class UnsupportedSchemaVersionError(RuntimeError):
    """Raised when a database's PRAGMA user_version is newer than this
    build's LATEST_SCHEMA_VERSION — opening it would risk silently
    misreading a shape this code doesn't understand yet."""


class NonContiguousMigrationPathError(RuntimeError):
    """Raised when the ordered MIGRATIONS path from a database's recorded
    version to LATEST_SCHEMA_VERSION has a gap. This is a programming
    error in the MIGRATIONS table itself, not a property of any one
    database."""


def _has_application_objects(conn: sqlite3.Connection) -> bool:
    """True if `sqlite_schema` has any object that isn't SQLite-internal.

    Used to distinguish a genuinely empty database (safe to bootstrap with
    SCHEMA) from a legacy database that happens to carry user_version=0 —
    the latter has application tables already and must upgrade through
    MIGRATIONS instead.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone()
    return row is not None


def _pending_migrations(current: int) -> list[int]:
    """Return the ordered migrations to apply to reach LATEST_SCHEMA_VERSION
    from `current`, or raise if no contiguous path exists.

    version 0 (the historical "no PRAGMA user_version ever stamped" legacy
    sentinel) has no corresponding MIGRATIONS[1] entry by design — it maps
    directly onto MIGRATIONS[2], which upgrades the original v0/v1 shape.
    Any other gap in the pending sequence is a genuine defect.
    """
    pending = sorted(v for v in MIGRATIONS if v > current)
    if not pending:
        if current < LATEST_SCHEMA_VERSION:
            raise NonContiguousMigrationPathError(
                f"no migrations registered above version {current}, but "
                f"LATEST_SCHEMA_VERSION is {LATEST_SCHEMA_VERSION}"
            )
        return pending
    for offset, version in enumerate(pending):
        if version != pending[0] + offset:
            raise NonContiguousMigrationPathError(
                f"migration path from version {current} is missing version "
                f"{pending[0] + offset}"
            )
    if pending[-1] != LATEST_SCHEMA_VERSION:
        raise NonContiguousMigrationPathError(
            f"migration path from version {current} stops at {pending[-1]}, "
            f"short of LATEST_SCHEMA_VERSION={LATEST_SCHEMA_VERSION}"
        )
    return pending


class StateManager:
    """Manages scan state and results in SQLite.

    Retained-path reads/writes delegate to five aggregate stores — scans,
    posts, evaluations, grades, registry — each constructed with the same
    UnitOfWork so a write spanning more than one store (the sole
    production case: grading/promotion.py composing an evaluation
    write with a grade write inside one ``self.db.begin_immediate()``
    block) stays atomic on one connection. The append-only
    autonomy_events methods remain defined here directly.
    """

    def __init__(
        self, db_path: str, *, init_schema: bool = True, allow_create: bool = True
    ) -> None:
        """Open a connection to `db_path`.

        Set `init_schema=False` for per-request connections once a caller
        (e.g. the FastAPI lifespan bootstrap) has already run the DDL and
        migrations once for this process. Every connection — regardless of
        `init_schema` — still gets its own foreign_keys/journal_mode/
        synchronous PRAGMAs applied, since those are per-connection, not
        persisted in the database file (journal_mode aside).
        """
        self.db_path = db_path
        if init_schema:
            # Keep FK enforcement OFF while migrations run: SQLite 3.26+
            # auto-rewrites FK references in dependent tables when
            # ALTER TABLE ... RENAME fires, and intermediate migration
            # states can otherwise trip FK checks that the final shape
            # never would. Re-enabled once the database is fully upgraded.
            self._db = Db(db_path, foreign_keys=False, allow_create=allow_create)
            self._init_schema()
            self._db.set_foreign_keys(True)
            self._db.commit()
        else:
            self._db = Db(db_path, foreign_keys=True, allow_create=allow_create)

        self._uow = UnitOfWork(self._db)
        self._scans = ScanStore(self._uow)
        self._posts = PostStore(self._uow)
        self._evaluations = EvaluationStore(self._uow)
        self._grades = GradeStore(self._uow, evaluations=self._evaluations)
        self._registry = RegistryStore(self._uow)
        self._artifacts = ArtifactStore(self._uow)

    @property
    def db(self) -> Db:
        """Read-only access to the owned Db instance. The sole public
        surface for transaction mechanics: `self.db.transaction()`,
        `self.db.begin_immediate()`, and `self.db.read_transaction()`."""
        return self._db

    @property
    def conn(self) -> sqlite3.Connection:
        """Read-only access to the owned sqlite3.Connection, for existing
        query consumers and migration call signatures. Production code must
        not issue transaction-control SQL (BEGIN/COMMIT/ROLLBACK/SAVEPOINT)
        through this property — use `self.db.transaction()` /
        `self.db.begin_immediate()` / `self.db.read_transaction()` instead."""
        return self._db.conn

    @property
    def scans(self) -> ScanStore:
        """The Scan aggregate store — scan lifecycle, fetch failures, and
        the author blocklist."""
        return self._scans

    @property
    def posts(self) -> PostStore:
        """The Post aggregate store — durable record of every fetched message."""
        return self._posts

    @property
    def evaluations(self) -> EvaluationStore:
        """The Evaluation aggregate store — relevance evaluations, phase
        runs, experiments, drafts, surfaced events, critiques, and
        feedback snapshots."""
        return self._evaluations

    @property
    def grades(self) -> GradeStore:
        """The Grade aggregate store — grades, revisions, usage
        overrides, and human-positive promotions."""
        return self._grades

    @property
    def artifacts(self) -> ArtifactStore:
        """Exact retained analysis bytes and immutable producer lineage."""
        return self._artifacts

    @property
    def registry(self) -> RegistryStore:
        """The Registry aggregate store — projects, keyword routes, and
        prompt templates."""
        return self._registry

    def _init_schema(self) -> None:
        current = int(self.db.execute("PRAGMA user_version").fetchone()[0])

        if current == 0 and not _has_application_objects(self.db.conn):
            # Genuinely empty database: bootstrap the exact latest schema
            # in one shot. No migration runs against a fresh database.
            self.db.executescript(SCHEMA)
            return

        if current > LATEST_SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(
                f"database at {self.db_path!r} has user_version={current}, "
                f"newer than this build's LATEST_SCHEMA_VERSION="
                f"{LATEST_SCHEMA_VERSION}"
            )

        pending = _pending_migrations(current)
        if not pending:
            return
        with self.db.transaction():
            for version in pending:
                MIGRATIONS[version](self.db.conn)
                self.db.execute(f"PRAGMA user_version = {version}")

    def __enter__(self) -> StateManager:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.db.commit()
        else:
            self.db.rollback()
        self.close()

    def commit(self) -> None:
        """Compatibility delegate: production writes already commit inside
        their own Db context. Calling this after such a write is a no-op."""
        self.db.commit()

    # --- Scan (delegates to ScanStore) ---

    def get_last_scan_timestamp(self) -> datetime | None:
        return self._scans.get_last_scan_timestamp()

    def get_latest_completed_scan_id(self) -> int | None:
        return self._scans.get_latest_completed_scan_id()

    def has_seen_message(self, platform: str, platform_msg_id: str) -> bool:
        return self._posts.has_seen_message(platform, platform_msg_id)

    def start_scan(
        self,
        fetch_started_at: datetime | None = None,
        *,
        environment: str = "development",
        run_kind: str = "live",
    ) -> int:
        return self._scans.start_scan(
            fetch_started_at, environment=environment, run_kind=run_kind
        )

    def complete_scan(
        self,
        scan_id: int,
        messages_scanned: int,
        relevant_found: int,
        status: ScanStatus = "complete",
        safe_watermark_at: datetime | None = None,
        overflow_count: int = 0,
        advance_watermark: bool = True,
    ) -> None:
        self._scans.complete_scan(
            scan_id,
            messages_scanned,
            relevant_found,
            status,
            safe_watermark_at,
            overflow_count,
            advance_watermark,
        )

    def save_fetch_failure(
        self,
        scan_id: int,
        platform: str,
        kind: str,
        message: str,
        context: str | None = None,
        http_status: int | None = None,
        retry_after: str | None = None,
        retryable: bool = True,
    ) -> int:
        return self._scans.save_fetch_failure(
            scan_id, platform, kind, message, context, http_status, retry_after, retryable
        )

    def fail_scan(
        self,
        scan_id: int,
        messages_scanned: int,
        *,
        failure_post_id: int | None,
        error_kind: str,
        error_message: str,
    ) -> None:
        self._scans.fail_scan(
            scan_id,
            messages_scanned,
            failure_post_id=failure_post_id,
            error_kind=error_kind,
            error_message=error_message,
        )

    def get_scan_fetch_failures(self, scan_id: int) -> list[dict[str, object]]:
        return [
            {
                "platform": f.platform,
                "context": f.context,
                "kind": f.kind,
                "message": f.message,
                "http_status": f.http_status,
                "retry_after": f.retry_after,
                "retryable": f.retryable,
            }
            for f in self._scans.get_scan_fetch_failures(scan_id)
        ]

    def block_author(
        self,
        *,
        platform: str,
        author_id: str,
        author_name: str | None = None,
        reason: str | None = None,
    ) -> int:
        return self._scans.block_author(
            platform=platform, author_id=author_id, author_name=author_name, reason=reason
        )

    def unblock_author(self, *, platform: str, author_id: str) -> bool:
        return self._scans.unblock_author(platform=platform, author_id=author_id)

    def get_blocked_author_keys(self) -> frozenset[tuple[str, str]]:
        return self._scans.get_blocked_author_keys()

    def is_author_blocked(self, *, platform: str, author_id: str) -> bool:
        return self._scans.is_author_blocked(platform=platform, author_id=author_id)

    def get_scan_stats(self) -> ScanStats:
        """Get aggregate stats across all scans.

        Composes independent counts from ScanStore, PostStore, and
        EvaluationStore — a read-only cross-aggregate query, not a write,
        so it needs no UnitOfWork coordination beyond the shared connection
        every store already reads through.
        """
        return ScanStats(
            total_scans=self._scans.count_scans(),
            total_posts=self._posts.count_posts(),
            total_relevant=self._evaluations.count_relevant_evaluations(),
            total_drafts=self._evaluations.count_drafts(),
        )

    # --- Post (delegates to PostStore) ---

    def save_post(self, msg: Message, scan_id: int) -> int:
        return self._posts.save_post(msg, scan_id)

    def load_posts(self, scan_id: int | None = None) -> list[Message]:
        return self._posts.load_posts(scan_id)

    def load_post(self, post_id: int) -> Message | None:
        return self._posts.load_post(post_id)

    def load_unevaluated_posts(self, scan_id: int | None = None) -> list[Message]:
        return self._posts.load_unevaluated_posts(scan_id)

    # --- Evaluation (delegates to EvaluationStore) ---

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
        return self._evaluations.insert_phase_run(
            scan_id=scan_id,
            post_id=post_id,
            snapshot_phase_id=snapshot_phase_id,
            phase=phase,
            trace_id=trace_id,
            model=model,
            status=status,
        )

    def get_phase_run(self, phase_run_id: int) -> dict[str, Any] | None:
        run = self._evaluations.get_phase_run(phase_run_id)
        return asdict(run) if run is not None else None

    def list_complete_reply_draft_phase_runs(self) -> list[dict[str, Any]]:
        return [
            asdict(r)
            for r in self._evaluations.list_complete_reply_draft_phase_runs()
        ]

    def list_evaluation_ids_with_reply_correction(self) -> set[int]:
        return self._evaluations.list_evaluation_ids_with_reply_correction()

    def create_experiment_run(self, *, name: str, candidate_config: str) -> int:
        return self._evaluations.create_experiment_run(
            name=name, candidate_config=candidate_config
        )

    def get_experiment_run(self, experiment_run_id: int) -> dict[str, Any] | None:
        run = self._evaluations.get_experiment_run(experiment_run_id)
        return asdict(run) if run is not None else None

    def complete_experiment_run_without_attempts(self, experiment_run_id: int) -> None:
        self._evaluations.complete_experiment_run_without_attempts(experiment_run_id)

    def insert_experiment_attempt(
        self,
        *,
        experiment_run_id: int,
        phase_run_id: int,
        baseline_evidence: str,
        supersedes_experiment_id: int | None = None,
    ) -> int:
        return self._evaluations.insert_experiment_attempt(
            experiment_run_id=experiment_run_id,
            phase_run_id=phase_run_id,
            baseline_evidence=baseline_evidence,
            supersedes_experiment_id=supersedes_experiment_id,
        )

    def get_experiment(self, experiment_id: int) -> dict[str, Any] | None:
        experiment = self._evaluations.get_experiment(experiment_id)
        return asdict(experiment) if experiment is not None else None

    def list_experiment_attempts(self, experiment_run_id: int) -> list[dict[str, Any]]:
        return [
            asdict(e)
            for e in self._evaluations.list_experiment_attempts(experiment_run_id)
        ]

    def get_trace_comparison(self, experiment_id: int) -> dict[str, Any] | None:
        comparison = self._evaluations.get_trace_comparison(experiment_id)
        return asdict(comparison) if comparison is not None else None

    def cas_experiment_to_running(self, experiment_id: int) -> None:
        self._evaluations.cas_experiment_to_running(experiment_id)

    def record_candidate_trace(
        self,
        experiment_id: int,
        *,
        candidate_trace_id: str,
        candidate_llm_call_count: int,
        candidate_cost: float | None,
    ) -> None:
        self._evaluations.record_candidate_trace(
            experiment_id,
            candidate_trace_id=candidate_trace_id,
            candidate_llm_call_count=candidate_llm_call_count,
            candidate_cost=candidate_cost,
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
        self._evaluations.complete_experiment_with_comparison(
            experiment_id,
            jig_revision=jig_revision,
            trace_diff=trace_diff,
            domain_diff=domain_diff,
            score_evidence=score_evidence,
        )

    def fail_experiment(self, experiment_id: int, *, error_detail: str) -> None:
        self._evaluations.fail_experiment(experiment_id, error_detail=error_detail)

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
        return self._evaluations.save_evaluation(
            result, post_id, scan_id, keyword_route_id, project_key, posture,
            surface_status, failure_reason, dossier_revision, dossier_summary_id,
        )

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
        return self._evaluations.persist_terminal_outcome(
            result,
            post_id,
            scan_id,
            surface_status=surface_status,
            contributor_phase_run_ids=contributor_phase_run_ids,
            keyword_route_id=keyword_route_id,
            project_key=project_key,
            posture=posture,
            failure_reason=failure_reason,
            dossier_revision=dossier_revision,
            dossier_summary_id=dossier_summary_id,
            critique=critique,
            gate_violations=gate_violations,
            allow_response_only_phase_runs=allow_response_only_phase_runs,
        )

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
        return self._evaluations.persist_surfaced_outcome(
            result,
            post_id,
            scan_id,
            project_key=project_key,
            author_id=author_id,
            platform=platform,
            comment_text=comment_text,
            structured_output=structured_output,
            contributor_phase_run_ids=contributor_phase_run_ids,
            keyword_route_id=keyword_route_id,
            posture=posture,
            critique=critique,
            dossier_revision=dossier_revision,
            dossier_summary_id=dossier_summary_id,
            surfaced_at=surfaced_at,
            fail_at=fail_at,
            allow_response_only_phase_runs=allow_response_only_phase_runs,
        )

    def get_latest_evaluation_id(self, post_id: int, scan_id: int) -> int | None:
        return self._evaluations.get_latest_evaluation_id(post_id, scan_id)

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
        return self._evaluations.save_draft(
            post_id, evaluation_id, project_key, comment_text, scan_id, posture,
            structured_output, dossier_revision, dossier_summary_id,
        )

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
        return self._evaluations.save_surfaced_event(
            post_id, evaluation_id, draft_id, scan_id, project_key, author_id,
            platform, surfaced_at,
        )

    def save_critique(
        self,
        draft_id: int | None,
        verdict: str,
        feedback: str,
        scan_id: int,
        evaluation_id: int | None = None,
    ) -> int:
        return self._evaluations.save_critique(
            draft_id, verdict, feedback, scan_id, evaluation_id
        )

    def get_recent_critique_feedback(self, limit: int = 10) -> list[CritiqueLesson]:
        return self._evaluations.get_recent_critique_feedback(limit)

    def get_evaluation(self, evaluation_id: int) -> sqlite3.Row | None:
        """Load an evaluation row by id, or None if not found."""
        row = self._evaluations.get_evaluation(evaluation_id)
        if row is None:
            return None
        return cast(sqlite3.Row, asdict(row))

    def record_feedback_snapshot(
        self, scan_id: int, *, mode: FeedbackMode
    ) -> PersistedFeedbackSnapshot:
        return self._evaluations.record_feedback_snapshot(scan_id, mode=mode)

    def load_committed_feedback_bundle(
        self, snapshot_id: int, *, expected_mode: FeedbackMode
    ) -> PhaseFeedbackBundle:
        return self._evaluations.load_committed_feedback_bundle(
            snapshot_id, expected_mode=expected_mode
        )

    # --- Grading (delegates to GradeStore) ---

    def save_grade(self, grade: GradeRecord) -> int:
        return self._grades.save_grade(grade)

    def get_human_positive_promotion(
        self, source_evaluation_id: int
    ) -> dict[str, Any] | None:
        promotion = self._grades.get_human_positive_promotion(source_evaluation_id)
        return asdict(promotion) if promotion is not None else None

    def begin_human_positive_promotion(self, grade: GradeRecord) -> dict[str, Any]:
        return asdict(self._grades.begin_human_positive_promotion(grade))

    def attach_human_positive_promotion_scan(
        self, source_evaluation_id: int, scan_id: int
    ) -> None:
        self._grades.attach_human_positive_promotion_scan(source_evaluation_id, scan_id)

    def complete_human_positive_promotion(
        self,
        source_evaluation_id: int,
        *,
        scan_id: int,
        target_evaluation_id: int,
    ) -> None:
        self._grades.complete_human_positive_promotion(
            source_evaluation_id,
            scan_id=scan_id,
            target_evaluation_id=target_evaluation_id,
        )

    def fail_human_positive_promotion(
        self, source_evaluation_id: int, *, error_detail: str
    ) -> None:
        self._grades.fail_human_positive_promotion(
            source_evaluation_id, error_detail=error_detail
        )

    def save_grade_for_remediation(
        self, grade: GradeRecord, *, remediation_reason: str
    ) -> int:
        return self._grades.save_grade_for_remediation(
            grade, remediation_reason=remediation_reason
        )

    def mark_grade_needs_regrade_for_remediation(
        self, grade_id: int, *, remediation_reason: str
    ) -> bool:
        return self._grades.mark_grade_needs_regrade_for_remediation(
            grade_id, remediation_reason=remediation_reason
        )

    def converge_grade_revision_for_remediation(
        self, grade_id: int, *, remediation_reason: str
    ) -> GradeConvergenceStatus:
        return self._grades.converge_grade_revision_for_remediation(
            grade_id, remediation_reason=remediation_reason
        )

    def save_grade_for_migration(self, grade: GradeRecord, *, migration_reason: str) -> int:
        return self._grades.save_grade_for_migration(grade, migration_reason=migration_reason)

    def get_grade(self, post_id: int) -> GradeRecord | None:
        return self._grades.get_grade(post_id)

    def get_grade_for_evaluation(self, evaluation_id: int) -> GradeRecord | None:
        return self._grades.get_grade_for_evaluation(evaluation_id)

    def get_grade_row_by_id(self, grade_id: int) -> sqlite3.Row | None:
        row = self._grades.get_grade_row_by_id(grade_id)
        if row is None:
            return None
        data = asdict(row)
        # Legacy row shape: needs_regrade stays the stored 0/1, not GradeRow's
        # bool. grading_api_sidecar serializes this dict straight into its
        # grade responses, and web/types/schema.ts declares the field a number.
        data["needs_regrade"] = int(row.needs_regrade)
        return cast(sqlite3.Row, data)

    def get_grade_id_for_evaluation(self, evaluation_id: int) -> int | None:
        return self._grades.get_grade_id_for_evaluation(evaluation_id)

    def get_grade_revisions(self, grade_id: int) -> list[sqlite3.Row]:
        return cast(
            list[sqlite3.Row],
            [asdict(r) for r in self._grades.get_grade_revisions(grade_id)],
        )

    def get_grade_revision_count(self, grade_id: int) -> int:
        return self._grades.get_grade_revision_count(grade_id)

    def save_grade_usage_override(
        self, grade_id: int, *, mode: str, reason: str | None
    ) -> sqlite3.Row:
        return cast(
            sqlite3.Row,
            asdict(
                self._grades.save_grade_usage_override(grade_id, mode=mode, reason=reason)
            ),
        )

    def get_grades_by_scan(self, scan_id: int) -> list[GradeRecord]:
        return self._grades.get_grades_by_scan(scan_id)

    def get_grading_progress(self, scan_id: int) -> tuple[int, int]:
        return self._grades.get_grading_progress(scan_id)

    def get_gradeable_items(self, scan_id: int) -> list[dict[str, object]]:
        return [asdict(item) for item in self._grades.get_gradeable_items(scan_id)]

    def get_recent_grading_signals(self, limit_scans: int = 3) -> GradingSignal:
        return self._grades.get_recent_grading_signals(limit_scans)

    def export_eval_cases(
        self,
        since: datetime | None = None,
        scan_id: int | None = None,
    ) -> list[dict[str, object]]:
        return self._grades.export_eval_cases(since, scan_id)


    # --- Runtime registry (delegates to RegistryStore) ---

    def list_projects(self, include_inactive: bool = False) -> list[sqlite3.Row]:
        return cast(
            list[sqlite3.Row],
            [asdict(p) for p in self._registry.list_projects(include_inactive)],
        )

    def upsert_project(
        self,
        key: str,
        name: str,
        description: str,
        link: str,
        active: bool = True,
        dossier_summary_id: str | None = None,
    ) -> None:
        self._registry.upsert_project(
            key, name, description, link, active, dossier_summary_id
        )

    def set_project_active(self, key: str, active: bool) -> None:
        self._registry.set_project_active(key, active)

    def list_keywords(
        self,
        project_key: str | None = None,
        include_inactive: bool = False,
    ) -> list[sqlite3.Row]:
        return cast(
            list[sqlite3.Row],
            [
                asdict(k)
                for k in self._registry.list_keywords(project_key, include_inactive)
            ],
        )

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
        self._registry.upsert_keyword(
            project_key,
            keyword,
            evaluate_prompt,
            respond_prompt,
            critique_prompt,
            priority,
            active,
            match_type,
            intent,
            positive_context,
            negative_context,
            notes,
        )

    def set_keyword_active(self, keyword_id: int, active: bool) -> None:
        self._registry.set_keyword_active(keyword_id, active)

    def list_prompt_templates(self, include_inactive: bool = False) -> list[sqlite3.Row]:
        return cast(
            list[sqlite3.Row],
            [
                asdict(t)
                for t in self._registry.list_prompt_templates(include_inactive)
            ],
        )

    def upsert_prompt_template(
        self,
        name: str,
        body: str,
        kind: str,
        active: bool = True,
    ) -> None:
        self._registry.upsert_prompt_template(name, body, kind, active)

    def set_prompt_template_active(self, name: str, active: bool) -> None:
        self._registry.set_prompt_template_active(name, active)

    def load_runtime_registry(self) -> RuntimeRegistry:
        return self._registry.load_runtime_registry()

    # --- PAA autonomy_events: append-only, insert + read only. ---
    #
    # autonomy_events has no update or delete path anywhere in this class —
    # its BEFORE UPDATE/DELETE triggers (migration 23) are the storage-level
    # backstop, but the Python surface is deliberately narrower still: only
    # one insert method and a handful of read queries. paa_service.py is
    # the sole caller, and owns the transaction boundary (self.db.
    # begin_immediate() / self.db.transaction()) around each insert.

    def insert_autonomy_event(
        self,
        *,
        event_id: str,
        motion_id: str,
        task: str,
        declaration_version: int,
        scope: str | None,
        event: AutonomyEventType,
        from_position: AutonomyPosition,
        to_position: AutonomyPosition,
        evidence_ref: str,
        evidence_sha256: str,
        actor: str,
        reason: str,
        created_at: str,
        event_schema: str = CURRENT_EVENT_SCHEMA,
    ) -> None:
        """Append one autonomy_events row.

        Callers must already be inside a ``self.db.transaction()`` or
        ``self.db.begin_immediate()`` context — this issues a single
        INSERT and relies on the caller's transaction boundary for
        atomicity with any sibling event write (e.g. approve's
        motion_approved + position_changed pair).

        Keyword order and the ``event_schema`` default mirror
        ``paa_runtime.store.EventStore.insert_autonomy_event`` so
        ``paa_event_store.ScoutEventStore`` can delegate straight through.
        ``scope`` is None for declarations that omit ``scopes:``.
        """
        self.db.execute(
            """
            INSERT INTO autonomy_events (
                event_schema, id, motion_id, task, declaration_version, scope, event,
                from_position, to_position, evidence_ref, evidence_sha256,
                actor, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_schema, event_id, motion_id, task, declaration_version, scope, event,
                from_position, to_position, evidence_ref, evidence_sha256,
                actor, reason, created_at,
            ),
        )

    def get_autonomy_events_for_motion(self, motion_id: str) -> list[sqlite3.Row]:
        """Every event recorded for one motion, oldest first."""
        return self.db.execute(
            "SELECT * FROM autonomy_events WHERE motion_id = ? ORDER BY created_at, id",
            (motion_id,),
        ).fetchall()

    def get_latest_position_changed_event(
        self, *, task: str, declaration_version: int, scope: str | None,
    ) -> sqlite3.Row | None:
        """The most recent position_changed event for the exact (task,
        declaration_version, scope) triple, or None if authority has never
        moved from the declaration's initial_position under that exact
        version and scope.

        ``scope IS ?`` rather than ``scope = ?``: in SQLite ``= NULL`` is
        never true, so a scope-less declaration would silently resolve to
        its initial position forever, ignoring every promotion ever
        recorded for it."""
        return cast(
            sqlite3.Row | None,
            self.db.execute(
                """
                SELECT * FROM autonomy_events
                WHERE task = ? AND declaration_version = ? AND scope IS ?
                  AND event = 'position_changed'
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (task, declaration_version, scope),
            ).fetchone(),
        )

    def get_position_changed_event_before(
        self, *, task: str, declaration_version: int, scope: str | None,
        created_at: str, event_id: str,
    ) -> sqlite3.Row | None:
        """The position_changed event that was latest strictly before the
        given (created_at, event_id) point, or None if none existed yet.

        Used to detect an intervening position change between a proposal
        and its approval: comparing this baseline against the *current*
        latest position_changed event catches a position that cycled back
        to the same value through a demotion and re-promotion, which a
        plain value comparison would miss.

        ``scope IS ?`` for the same null-scope reason as
        get_latest_position_changed_event.
        """
        return cast(
            sqlite3.Row | None,
            self.db.execute(
                """
                SELECT * FROM autonomy_events
                WHERE task = ? AND declaration_version = ? AND scope IS ?
                  AND event = 'position_changed'
                  AND (created_at < ? OR (created_at = ? AND id < ?))
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (task, declaration_version, scope, created_at, created_at, event_id),
            ).fetchone(),
        )

    def get_autonomy_events(self, *, task: str | None = None) -> list[sqlite3.Row]:
        """Every autonomy event, oldest first, optionally filtered to one task."""
        if task is None:
            return self.db.execute(
                "SELECT * FROM autonomy_events ORDER BY created_at, id"
            ).fetchall()
        return self.db.execute(
            "SELECT * FROM autonomy_events WHERE task = ? ORDER BY created_at, id",
            (task,),
        ).fetchall()

    def close(self) -> None:
        self.db.close()
