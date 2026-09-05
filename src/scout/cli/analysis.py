"""Explicit operator boundaries for private Scout analysis artifacts.

Read commands open SQLite with mode=ro and query_only before any query. Write
commands use StateManager and its normal migration/UoW path. No model execution
or grade writes occur in these commands.
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
from scout.storage.state import StateManager


class AnalysisReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str
    artifact_count: int
    lineage_count: int
    outputs: tuple[ArtifactDigest, ...]
    unsupported_lineage_count: int


class AnalysisVerification(BaseModel):
    artifact_count: int
    lineage_count: int
    replayed_lineage_count: int
    unsupported_lineage_count: int


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


def project_study_index(bundle: ArtifactBundle) -> Result[StudyIndex, ArtifactError]:
    validated = validate_bundle(bundle)
    if isinstance(validated, Err):
        return validated
    contents = {artifact.digest: artifact.content for artifact in bundle.artifacts}

    def entry(lineage: ArtifactLineage) -> StudyIndexEntry:
        evidence = None
        issue: UnsupportedProducer | InvalidStudyEvidence | None = None
        if not supports_inventory(lineage) and not supports_snapshot(lineage):
            issue = UnsupportedProducer()
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


def verify_analysis_bundle(bundle: ArtifactBundle) -> Result[int, ArtifactError]:
    snapshots = verify_snapshot_replay(bundle)
    if isinstance(snapshots, Err):
        return snapshots
    inventories = verify_inventory_replay(bundle)
    if isinstance(inventories, Err):
        return inventories
    return Ok(snapshots.value + inventories.value)


def add_analysis_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], default_db_path: str
) -> None:
    parser = subparsers.add_parser(
        "analysis", help="Private grading-corpus artifacts (no model calls)"
    )
    commands = parser.add_subparsers(dest="analysis_command", required=True)
    for command in ("preview", "snapshot", "export", "import", "index", "verify", "inventory"):
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


def _receipt(operation: str, bundle: ArtifactBundle) -> AnalysisReceipt:
    return AnalysisReceipt(
        operation=operation,
        artifact_count=len(bundle.artifacts),
        lineage_count=len(bundle.lineages),
        outputs=tuple(output for lineage in bundle.lineages for output in lineage.outputs),
        unsupported_lineage_count=sum(
            not supports_snapshot(lineage) and not supports_inventory(lineage)
            for lineage in bundle.lineages
        ),
    )


def run_analysis(args: argparse.Namespace) -> Result[BaseModel, ArtifactError]:
    """IO dispatch; output errors do not contain corpus or environment content."""
    try:
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
            return Ok(_receipt("import", parsed.value))
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
                    replayed_lineage_count=verified.value,
                    unsupported_lineage_count=len(exported.value.lineages) - verified.value,
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
