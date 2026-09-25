"""Explicit operator boundaries for private Scout analysis artifacts.

Read commands open SQLite with mode=ro and query_only before any query. Write
commands use StateManager and its normal migration/UoW path. Explicit assistance
execution/replay uses local classical models; no paid calls or grade writes occur.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from scout.grading.artifacts import (
    ArtifactBundle,
    ArtifactDigest,
    ArtifactError,
    ArtifactLineage,
    ProducerEnvironment,
    decode_bundle,
    digest_artifact,
    encode_lineage,
    validate_bundle,
)
from scout.grading.assistance_store import (
    read_assistance_outputs,
    supports_assistance,
    verify_assistance_replay,
)
from scout.grading.assistance_types import ReplayUnavailable, ReplayVerification
from scout.grading.snapshots import (
    CorpusSelection,
    build_snapshot_bundle,
    preview_corpus,
    read_grade_population,
    select_corpus,
    supports_snapshot,
    verify_snapshot_replay,
)
from scout.grading.studies import (
    InventorySelection,
    inventory_evidence,
    replay_inventory_lineage,
    supports_inventory,
    verify_inventory_replay,
)
from scout.result import Err, Ok, Result
from scout.storage.artifacts import read_artifact_bundle
from scout.storage.db import read_only_connection
from scout.storage.evaluations import is_actionable_for_posting
from scout.storage.state import StateManager


class AnalysisReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str
    artifact_count: int
    lineage_count: int
    outputs: tuple[ArtifactDigest, ...]
    unsupported_lineage_count: int
    unverified_here: tuple[ReplayUnavailable, ...] = ()


class AnalysisVerification(BaseModel):
    artifact_count: int
    lineage_count: int
    replayed_lineage_count: int
    unsupported_lineage_count: int
    unverified_here: tuple[ReplayUnavailable, ...] = ()


class UnsupportedProducer(BaseModel):
    kind: Literal["unsupported_producer"] = "unsupported_producer"
    detail: str = "No adapter for this producer identity/version"


class InvalidStudyEvidence(BaseModel):
    kind: Literal["invalid_study_evidence"] = "invalid_study_evidence"
    detail: str


class StudyIndexEntry(BaseModel):
    """Projection of existing lineage documents, not an independently stored catalog."""

    lineage_digest: ArtifactDigest
    kind: str
    process_id: str
    process_version: str
    inputs: tuple[ArtifactDigest, ...]
    outputs: tuple[ArtifactDigest, ...]
    study: str | None = None
    usability: str | None = None
    reason: str | None = None
    experiment_run_ids: tuple[int, ...] = ()
    issue: UnsupportedProducer | InvalidStudyEvidence | None = None


class StudyIndex(BaseModel):
    entries: tuple[StudyIndexEntry, ...]


class StatusAuditFinding(BaseModel):
    """One invariant the holdout lifecycle guarantees, found violated."""

    model_config = ConfigDict(extra="forbid")
    invariant: str
    count: int
    detail: str


class StatusConsumerAudit(BaseModel):
    """How held, released and classified rows actually sit in one database.

    The executable half of the status-consumer audit: every count a status
    view reads, plus the lifecycle invariants those views depend on, read
    through the same read-only connection the other read commands use.

    It carries counts and invariant names only — never post content, answer
    vectors, catalogue identity or a label — so its output is safe to attach
    to a deployment record. A database predating the holdout schema audits
    clean with empty classifier and holdout counts: an absent table is an
    honest absence, not a violation.
    """

    model_config = ConfigDict(extra="forbid")
    evaluations_by_surface_status: dict[str, int]
    held_evaluations: int
    actionable_evaluations: int
    holdouts_by_status: dict[str, int]
    decisions_by_classifier: dict[str, int]
    decisions_by_action: dict[str, int]
    released_by_authority: dict[str, int]
    released_targets_by_surface_status: dict[str, int]
    evaluations_without_classifier_record: int
    release_targets_without_classifier_record: int
    held_evaluations_graded_in_scout: int
    findings: tuple[StatusAuditFinding, ...]


#: One lifecycle invariant: its name, the query counting rows that violate it,
#: and what the violation would mean. Held as data so the audit reports every
#: invariant it knows rather than however many an if-chain remembered.
_STATUS_INVARIANTS: tuple[tuple[str, str, str], ...] = (
    (
        "held_evaluation_has_no_draft",
        """SELECT COUNT(*) FROM evaluations e
           WHERE e.surface_status = 'held'
             AND EXISTS (SELECT 1 FROM draft_comments d WHERE d.evaluation_id = e.id)""",
        "a held evaluation carries a draft; a hold stops before drafting",
    ),
    (
        "held_evaluation_was_never_surfaced",
        """SELECT COUNT(*) FROM evaluations e
           WHERE e.surface_status = 'held'
             AND EXISTS (
               SELECT 1 FROM surfaced_events s WHERE s.evaluation_id = e.id
             )""",
        "a held evaluation carries a surfaced event; a hold never surfaces",
    ),
    (
        "hold_source_is_held",
        """SELECT COUNT(*) FROM relevance_holdouts h
           JOIN evaluations e ON e.id = h.evaluation_id
           WHERE e.surface_status <> 'held'""",
        "a hold references a source evaluation that is not held",
    ),
    (
        "release_target_is_not_held",
        """SELECT COUNT(*) FROM relevance_holdouts h
           JOIN evaluations e ON e.id = h.target_evaluation_id
           WHERE e.surface_status = 'held'""",
        "a released outcome landed on a held evaluation",
    ),
    (
        "selected_decision_has_a_hold",
        """SELECT COUNT(*) FROM relevance_decisions d
           WHERE d.selected_for_holdout = 1
             AND NOT EXISTS (
               SELECT 1 FROM relevance_holdouts h WHERE h.evaluation_id = d.evaluation_id
             )""",
        "a decision sampled into the holdout has no hold recorded",
    ),
    (
        # Existence, checked separately from content: the invariant below joins
        # `relevance_decisions`, so a hold with no decision row at all drops out
        # of it before its predicate is reached. Nothing in the schema forbids
        # that row's absence — there is no foreign key from a hold to a
        # decision, and `HoldoutStore.hold` sets `selected_for_holdout` with an
        # UPDATE that is a silent no-op when no row is there.
        "hold_has_a_decision_row",
        """SELECT COUNT(*) FROM relevance_holdouts h
           WHERE NOT EXISTS (
             SELECT 1 FROM relevance_decisions d WHERE d.evaluation_id = h.evaluation_id
           )""",
        "a hold has no decision recorded; an ungraded release has no action to use",
    ),
    (
        "hold_has_a_selected_decision",
        """SELECT COUNT(*) FROM relevance_holdouts h
           JOIN relevance_decisions d ON d.evaluation_id = h.evaluation_id
           WHERE d.selected_for_holdout = 0""",
        "a hold references a decision that records it was not sampled",
    ),
    (
        "released_hold_records_its_authority",
        """SELECT COUNT(*) FROM relevance_holdouts h
           WHERE h.status = 'released'
             AND (h.release_authority IS NULL OR h.release_action IS NULL
                  OR h.released_at IS NULL)""",
        "a released hold does not record what released it",
    ),
)


def _tables(conn: sqlite3.Connection) -> frozenset[str]:
    return frozenset(
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    )


def _counts(conn: sqlite3.Connection, query: str) -> dict[str, int]:
    """A `key, count` query as a mapping, with NULL keys named `unrecorded`."""
    return {
        ("unrecorded" if row[0] is None else str(row[0])): int(row[1])
        for row in conn.execute(query)
    }


def _scalar(conn: sqlite3.Connection, query: str) -> int:
    row = conn.execute(query).fetchone()
    return 0 if row is None else int(row[0])


def audit_status_consumers(conn: sqlite3.Connection) -> StatusConsumerAudit:
    """Project the status counts and lifecycle invariants of one database.

    Every query is guarded by the tables it reads, so the audit runs against
    a pre-holdout database and reports absence rather than failing on it.
    """
    tables = _tables(conn)
    has_decisions = "relevance_decisions" in tables
    has_holdouts = "relevance_holdouts" in tables
    has_grades = "grades" in tables

    by_status = _counts(
        conn, "SELECT surface_status, COUNT(*) FROM evaluations GROUP BY surface_status"
    )
    invariants = [
        (name, query, detail)
        for name, query, detail in _STATUS_INVARIANTS
        if ("relevance_holdouts" not in query or has_holdouts)
        and ("relevance_decisions" not in query or has_decisions)
    ]
    findings = tuple(
        StatusAuditFinding(invariant=name, count=violations, detail=detail)
        for name, query, detail in invariants
        if (violations := _scalar(conn, query)) > 0
    )
    return StatusConsumerAudit(
        evaluations_by_surface_status=by_status,
        held_evaluations=by_status.get("held", 0),
        actionable_evaluations=sum(
            count
            for status, count in by_status.items()
            if is_actionable_for_posting(status)
        ),
        holdouts_by_status=(
            _counts(conn, "SELECT status, COUNT(*) FROM relevance_holdouts GROUP BY status")
            if has_holdouts
            else {}
        ),
        decisions_by_classifier=(
            _counts(
                conn, "SELECT classifier, COUNT(*) FROM relevance_decisions GROUP BY classifier"
            )
            if has_decisions
            else {}
        ),
        decisions_by_action=(
            _counts(conn, "SELECT action, COUNT(*) FROM relevance_decisions GROUP BY action")
            if has_decisions
            else {}
        ),
        released_by_authority=(
            _counts(
                conn,
                "SELECT release_authority, COUNT(*) FROM relevance_holdouts "
                "WHERE status = 'released' GROUP BY release_authority",
            )
            if has_holdouts
            else {}
        ),
        released_targets_by_surface_status=(
            _counts(
                conn,
                "SELECT e.surface_status, COUNT(*) FROM relevance_holdouts h "
                "JOIN evaluations e ON e.id = h.target_evaluation_id GROUP BY e.surface_status",
            )
            if has_holdouts
            else {}
        ),
        evaluations_without_classifier_record=(
            _scalar(
                conn,
                "SELECT COUNT(*) FROM evaluations e WHERE NOT EXISTS ("
                "SELECT 1 FROM relevance_decisions d WHERE d.evaluation_id = e.id)",
            )
            if has_decisions
            else _scalar(conn, "SELECT COUNT(*) FROM evaluations")
        ),
        # A released target legitimately has none: its authority is the hold's
        # release record, not a classifier run. Counted separately so the
        # unknown-classifier total above is not read as a gap.
        release_targets_without_classifier_record=(
            _scalar(
                conn,
                "SELECT COUNT(*) FROM relevance_holdouts h "
                "WHERE h.target_evaluation_id IS NOT NULL AND NOT EXISTS ("
                "SELECT 1 FROM relevance_decisions d "
                "WHERE d.evaluation_id = h.target_evaluation_id)",
            )
            if has_holdouts and has_decisions
            else 0
        ),
        # Not an invariant: a held row carrying a sighted Scout grade is a
        # fact an operator should see, because the blind label for that case
        # is written in assay against the same post.
        held_evaluations_graded_in_scout=(
            _scalar(
                conn,
                "SELECT COUNT(*) FROM evaluations e JOIN grades g ON g.evaluation_id = e.id "
                "WHERE e.surface_status = 'held'",
            )
            if has_grades
            else 0
        ),
        findings=findings,
    )


def project_study_index(bundle: ArtifactBundle) -> Result[StudyIndex, ArtifactError]:
    validated = validate_bundle(bundle)
    if isinstance(validated, Err):
        return validated
    contents = {artifact.digest: artifact.content for artifact in bundle.artifacts}

    def entry(lineage: ArtifactLineage) -> StudyIndexEntry:
        evidence = None
        issue: UnsupportedProducer | InvalidStudyEvidence | None = None
        if (
            not supports_inventory(lineage)
            and not supports_snapshot(lineage)
            and not supports_assistance(lineage)
        ):
            issue = UnsupportedProducer()
        elif supports_assistance(lineage):
            decoded = read_assistance_outputs(lineage, contents)
            if isinstance(decoded, Err):
                issue = InvalidStudyEvidence(detail=decoded.error.detail)
        elif supports_inventory(lineage):
            match replay_inventory_lineage(lineage, contents):
                case Err(error):
                    issue = InvalidStudyEvidence(detail=error.detail)
                case Ok(value):
                    evidence = value
        return StudyIndexEntry(
            lineage_digest=digest_artifact(encode_lineage(lineage)),
            kind=lineage.kind,
            process_id=lineage.process.id,
            process_version=lineage.process.version,
            inputs=lineage.inputs,
            outputs=lineage.outputs,
            study=None if evidence is None else evidence.selection.study,
            usability=None if evidence is None else evidence.selection.usability,
            reason=None if evidence is None else evidence.selection.reason,
            experiment_run_ids=() if evidence is None else tuple(run.id for run in evidence.runs),
            issue=issue,
        )

    return Ok(StudyIndex(entries=tuple(entry(lineage) for lineage in bundle.lineages)))


def verify_analysis_bundle(bundle: ArtifactBundle) -> Result[ReplayVerification, ArtifactError]:
    snapshots = verify_snapshot_replay(bundle)
    if isinstance(snapshots, Err):
        return snapshots
    inventories = verify_inventory_replay(bundle)
    if isinstance(inventories, Err):
        return inventories
    assistance = verify_assistance_replay(bundle)
    if isinstance(assistance, Err):
        return assistance
    return Ok(
        ReplayVerification(
            replayed_lineage_count=snapshots.value
            + inventories.value
            + assistance.value.replayed_lineage_count,
            unverified_here=assistance.value.unverified_here,
        )
    )


def add_analysis_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], default_db_path: str
) -> None:
    parser = subparsers.add_parser(
        "analysis", help="Private grading artifacts and local selectors (no paid model calls)"
    )
    commands = parser.add_subparsers(dest="analysis_command", required=True)
    from scout.cli.assistance import add_assistance_parsers

    add_assistance_parsers(commands, default_db_path)
    for command in (
        "preview",
        "snapshot",
        "export",
        "import",
        "index",
        "verify",
        "inventory",
        "status-audit",
    ):
        child = commands.add_parser(command)
        child.add_argument("--db-path", default=default_db_path)
        if command in ("preview", "snapshot"):
            child.add_argument("--project", required=True)
            child.add_argument("--dossier-root", type=Path, required=True)
        if command in ("snapshot", "inventory"):
            child.add_argument(
                "--environment",
                type=Path,
                required=True,
                help="Retained environment description with code/dependency/runtime pins",
            )
        if command == "export":
            child.add_argument("--out", type=Path, required=True)
        if command == "import":
            child.add_argument("--bundle", type=Path, required=True)
            child.add_argument(
                "--create-db",
                action="store_true",
                help="Explicitly allow creation of a new restore database",
            )
        if command == "inventory":
            child.add_argument("--study", required=True)
            child.add_argument("--file", action="append", required=True)
            child.add_argument("--experiment-run-id", type=int, action="append", default=[])
            child.add_argument(
                "--usability", choices=("unassessed", "usable", "invalid"), default="unassessed"
            )
            child.add_argument("--reason")


def _write_private_export(path: Path, content: bytes) -> Result[None, ArtifactError]:
    """Publish complete bytes atomically, mode 0600, without replacing a target."""
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".scout-export-", dir=path.parent)
    except OSError:
        return Err(ArtifactError("prepare_export", str(path), "Cannot create export staging file"))
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)
    except FileExistsError:
        return Err(ArtifactError("analysis", None, "Refusing to replace an existing export path"))
    except OSError:
        return Err(ArtifactError("publish_export", str(path), "Export was not published"))
    finally:
        os.unlink(temporary)
    # Persist publication (and temporary-name removal), not only file bytes.
    try:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        return Err(
            ArtifactError(
                "sync_export_directory",
                str(path),
                "Export published; directory durability unconfirmed. "
                "Retained file was not removed; "
                "inspect it or choose a new destination. Existing paths are never overwritten.",
            )
        )
    return Ok(None)


def _receipt(
    operation: str, bundle: ArtifactBundle, *, unverified: tuple[ReplayUnavailable, ...] = ()
) -> AnalysisReceipt:
    return AnalysisReceipt(
        operation=operation,
        artifact_count=len(bundle.artifacts),
        lineage_count=len(bundle.lineages),
        outputs=tuple(output for lineage in bundle.lineages for output in lineage.outputs),
        unsupported_lineage_count=sum(
            not supports_snapshot(lineage)
            and not supports_inventory(lineage)
            and not supports_assistance(lineage)
            for lineage in bundle.lineages
        ),
        unverified_here=unverified,
    )


def run_analysis(args: argparse.Namespace) -> Result[BaseModel, ArtifactError]:
    """IO dispatch; output errors do not contain corpus or environment content."""
    try:
        from scout.cli.assistance import ASSISTANCE_COMMANDS, run_assistance

        if args.analysis_command in ASSISTANCE_COMMANDS:
            return run_assistance(args)
        if args.analysis_command == "inventory":
            selection_inventory = InventorySelection(
                study=args.study,
                files=tuple(args.file),
                experiment_run_ids=tuple(args.experiment_run_id),
                usability=args.usability,
                reason=args.reason,
            )
            environment = args.environment.read_bytes()
            ProducerEnvironment.model_validate_json(environment)
            with read_only_connection(args.db_path) as conn:
                inventoried = inventory_evidence(conn, selection_inventory, environment)
            if isinstance(inventoried, Err):
                return inventoried
            with StateManager(args.db_path) as state:
                saved_inventory = state.artifacts.import_bundle(inventoried.value)
            if isinstance(saved_inventory, Err):
                return saved_inventory
            return Ok(_receipt("inventory", inventoried.value))
        if args.analysis_command in ("preview", "snapshot"):
            selection = CorpusSelection(project_key=args.project)
            if not selection.project_key.strip():
                return Err(ArtifactError("analysis", None, "Project must be nonblank"))
            with read_only_connection(args.db_path) as conn:
                population = read_grade_population(conn, args.dossier_root)
            if isinstance(population, Err):
                return population
            if args.analysis_command == "preview":
                return Ok(preview_corpus(select_corpus(population.value, selection)))
            environment = args.environment.read_bytes()
            ProducerEnvironment.model_validate_json(environment)
            built = build_snapshot_bundle(population.value, selection, environment)
            if isinstance(built, Err):
                return built
            bundle = built.value
            with StateManager(args.db_path) as state:
                saved = state.artifacts.import_bundle(bundle)
            if isinstance(saved, Err):
                return saved
            return Ok(_receipt("snapshot", bundle))
        if args.analysis_command == "status-audit":
            with read_only_connection(args.db_path) as conn:
                return Ok(audit_status_consumers(conn))
        if args.analysis_command == "import":
            parsed = decode_bundle(args.bundle.read_bytes())
            if isinstance(parsed, Err):
                return parsed
            verified = verify_analysis_bundle(parsed.value)
            if isinstance(verified, Err):
                return verified
            if not args.create_db and not Path(args.db_path).is_file():
                return Err(
                    ArtifactError(
                        "import",
                        args.db_path,
                        "Restore destination must exist; use --create-db to create a new database",
                    )
                )
            with StateManager(args.db_path, allow_create=args.create_db) as state:
                saved = state.artifacts.import_bundle(parsed.value)
            if isinstance(saved, Err):
                return saved
            return Ok(_receipt("import", parsed.value, unverified=verified.value.unverified_here))
        with read_only_connection(args.db_path) as conn:
            exported = read_artifact_bundle(conn)
        if isinstance(exported, Err):
            return exported
        if args.analysis_command == "index":
            return project_study_index(exported.value)
        if args.analysis_command == "verify":
            verified = verify_analysis_bundle(exported.value)
            if isinstance(verified, Err):
                return verified
            return Ok(
                AnalysisVerification(
                    artifact_count=len(exported.value.artifacts),
                    lineage_count=len(exported.value.lineages),
                    replayed_lineage_count=verified.value.replayed_lineage_count,
                    unsupported_lineage_count=len(exported.value.lineages)
                    - verified.value.replayed_lineage_count
                    - len(verified.value.unverified_here),
                    unverified_here=verified.value.unverified_here,
                )
            )
        if args.analysis_command == "export":
            # Never overwrite an existing export (including the source DB).
            published = _write_private_export(args.out, exported.value.model_dump_json().encode())
            if isinstance(published, Err):
                return published
            return Ok(_receipt("export", exported.value))
        return Err(ArtifactError("analysis", None, "Unknown analysis command"))
    except (sqlite3.Error, OSError, ValueError):
        return Err(ArtifactError("analysis", None, "Analysis IO failed; verify paths and schema"))
