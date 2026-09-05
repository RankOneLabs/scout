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
)
from scout.grading.snapshots import (
    CorpusSelection,
    build_snapshot_bundle,
    preview_corpus,
    read_grade_population,
    select_corpus,
    verify_snapshot_replay,
)
from scout.grading.studies import (
    InventorySelection,
    StudyEvidence,
    inventory_evidence,
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


class AnalysisVerification(BaseModel):
    artifact_count: int
    lineage_count: int
    replayed_lineage_count: int
    unsupported_lineage_count: int


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


class StudyIndex(BaseModel):
    entries: tuple[StudyIndexEntry, ...]


def project_study_index(bundle: ArtifactBundle) -> Result[StudyIndex, ArtifactError]:
    contents = {artifact.digest: artifact.content for artifact in bundle.artifacts}

    def entry(lineage: ArtifactLineage) -> StudyIndexEntry:
        evidence = None
        if lineage.kind == "scout.evidence.inventory" and len(lineage.outputs) == 1:
            evidence = StudyEvidence.model_validate_json(contents[lineage.outputs[0]])
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
        )

    # This is the deserialization boundary for producer-specific annotations.
    try:
        return Ok(StudyIndex(entries=tuple(entry(lineage) for lineage in bundle.lineages)))
    except (ValueError, KeyError):
        return Err(ArtifactError("project_study_index", None, "Invalid retained study evidence"))


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
        if command == "inventory":
            child.add_argument("--study", required=True)
            child.add_argument("--file", action="append", required=True)
            child.add_argument("--experiment-run-id", type=int, action="append", default=[])
            child.add_argument(
                "--usability", choices=("unassessed", "usable", "invalid"), default="unassessed"
            )
            child.add_argument("--reason")


def _write_private_export(path: Path, content: bytes) -> None:
    """Publish complete bytes atomically, mode 0600, without replacing a target."""
    descriptor, temporary = tempfile.mkstemp(prefix=".scout-export-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)
    finally:
        os.unlink(temporary)
    # Persist publication (and temporary-name removal), not only file bytes.
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _receipt(operation: str, bundle: ArtifactBundle) -> AnalysisReceipt:
    return AnalysisReceipt(
        operation=operation,
        artifact_count=len(bundle.artifacts),
        lineage_count=len(bundle.lineages),
        outputs=tuple(output for lineage in bundle.lineages for output in lineage.outputs),
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
            bundle = build_snapshot_bundle(population.value, selection, environment)
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
            with StateManager(args.db_path) as state:
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
            try:
                _write_private_export(args.out, exported.value.model_dump_json().encode())
            except FileExistsError:
                return Err(
                    ArtifactError("analysis", None, "Refusing to replace an existing export path")
                )
            return Ok(_receipt("export", exported.value))
        return Err(ArtifactError("analysis", None, "Unknown analysis command"))
    except (sqlite3.Error, OSError, ValueError):
        return Err(ArtifactError("analysis", None, "Analysis IO failed; verify paths and schema"))
