"""Retain and annotate existing experiment evidence without rewriting its history."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from scout.grading.artifacts import (
    ArtifactBundle,
    ArtifactDigest,
    ArtifactError,
    ArtifactLineage,
    ArtifactProcess,
    EnvironmentIdentity,
    ProcessId,
    RetainedArtifact,
    TransformKind,
    digest_artifact,
    validate_bundle,
)
from scout.result import Err, Ok, Result


class InventorySelection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    study: str
    files: tuple[str, ...]
    experiment_run_ids: tuple[int, ...]
    usability: Literal["unassessed", "usable", "invalid"] = "unassessed"
    reason: str | None = None


class EvidenceFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    digest: ArtifactDigest


class ExistingRunReference(BaseModel):
    """Projection of experiment_runs and evaluation_experiments, not copied history."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    id: int
    name: str
    observed_status: str
    attempt_ids: tuple[int, ...]


class StudyEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    format: Literal["scout.study-evidence/v1"] = "scout.study-evidence/v1"
    selection: InventorySelection
    files: tuple[EvidenceFile, ...]
    runs: tuple[ExistingRunReference, ...]


class EvidenceObservations(BaseModel):
    """Recorded file-name/digest associations and DB links at inventory time."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    files: tuple[EvidenceFile, ...]
    runs: tuple[ExistingRunReference, ...]


def assemble_study_evidence(
    observed: EvidenceObservations, selection: InventorySelection
) -> StudyEvidence:
    return StudyEvidence(selection=selection, files=observed.files, runs=observed.runs)


def inventory_evidence(
    conn: sqlite3.Connection, selection: InventorySelection, environment: bytes
) -> Result[ArtifactBundle, ArtifactError]:
    """Freeze explicit files and run links. This records OUR inventory process,
    never an inferred producer for an old experiment. Logs/env files aren't
    discovered or swept automatically; the operator names every evidence file.
    """
    if not conn.in_transaction:
        return Err(ArtifactError("inventory_evidence", selection.study, "Stable read required"))
    if not selection.study.strip() or not selection.files:
        return Err(
            ArtifactError(
                "inventory_evidence", selection.study, "Study and evidence files required"
            )
        )
    if selection.usability != "unassessed" and not (selection.reason and selection.reason.strip()):
        return Err(
            ArtifactError(
                "inventory_evidence", selection.study, "Evidence assessment needs a reason"
            )
        )
    try:
        retained: dict[ArtifactDigest, bytes] = {}
        files: list[EvidenceFile] = []
        for name in sorted(set(selection.files)):
            path = Path(name)
            if not path.is_file() or path.is_symlink():
                return Err(
                    ArtifactError("inventory_evidence", name, "Expected a regular evidence file")
                )
            content = path.read_bytes()
            digest = digest_artifact(content)
            retained[digest] = content
            files.append(EvidenceFile(name=name, digest=digest))
        runs: list[ExistingRunReference] = []
        for run_id in sorted(set(selection.experiment_run_ids)):
            row = conn.execute(
                "SELECT id, name, status FROM experiment_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return Err(
                    ArtifactError("inventory_evidence", str(run_id), "Unknown experiment run")
                )
            attempts = tuple(
                int(attempt[0])
                for attempt in conn.execute(
                    "SELECT id FROM evaluation_experiments WHERE experiment_run_id = ? ORDER BY id",
                    (run_id,),
                )
            )
            runs.append(
                ExistingRunReference(
                    id=row[0], name=row[1], observed_status=row[2], attempt_ids=attempts
                )
            )
        observed = EvidenceObservations(files=tuple(files), runs=tuple(runs))
        evidence = assemble_study_evidence(observed, selection)
        evidence_bytes = evidence.model_dump_json().encode()
        config_bytes = selection.model_dump_json().encode()
        # The observed DB links are retained inputs too; status remains distinct
        # from the operator's evidence-usability annotation in selection.
        run_bytes = observed.model_dump_json().encode()
        inputs = (*sorted(retained), digest_artifact(run_bytes))
        for content in (evidence_bytes, config_bytes, environment, run_bytes):
            retained[digest_artifact(content)] = content
        lineage = ArtifactLineage(
            kind=TransformKind("scout.evidence.inventory"),
            inputs=inputs,
            process=ArtifactProcess(
                id=ProcessId("scout.evidence.inventory"),
                version="1",
                config_digest=digest_artifact(config_bytes),
                environment=EnvironmentIdentity(digest_artifact(environment)),
            ),
            outputs=(digest_artifact(evidence_bytes),),
        )
        return Ok(
            ArtifactBundle(
                artifacts=tuple(
                    RetainedArtifact(digest=digest, content=retained[digest])
                    for digest in sorted(retained)
                ),
                lineages=(lineage,),
            )
        )
    except (sqlite3.Error, OSError, ValueError):
        return Err(
            ArtifactError(
                "inventory_evidence", selection.study, "Cannot retain experiment evidence"
            )
        )


def verify_inventory_replay(bundle: ArtifactBundle) -> Result[int, ArtifactError]:
    match validate_bundle(bundle):
        case Err() as error:
            return error
        case Ok():
            pass
    contents = {artifact.digest: artifact.content for artifact in bundle.artifacts}
    verified = 0
    for lineage in bundle.lineages:
        if lineage.kind != "scout.evidence.inventory":
            continue
        if (
            lineage.process.id != "scout.evidence.inventory"
            or lineage.process.version != "1"
            or len(lineage.inputs) < 2
            or len(lineage.outputs) != 1
        ):
            return Err(
                ArtifactError("verify_inventory_replay", None, "Unsupported inventory producer")
            )
        try:
            selection = InventorySelection.model_validate_json(
                contents[lineage.process.config_digest]
            )
            observed = EvidenceObservations.model_validate_json(contents[lineage.inputs[-1]])
        except ValueError:
            return Err(ArtifactError("verify_inventory_replay", None, "Invalid inventory inputs"))
        if not observed.files or any(
            file.digest not in lineage.inputs[:-1] for file in observed.files
        ):
            return Err(
                ArtifactError("verify_inventory_replay", None, "Missing evidence file reference")
            )
        expected = assemble_study_evidence(observed, selection).model_dump_json().encode()
        if expected != contents[lineage.outputs[0]]:
            return Err(
                ArtifactError(
                    "verify_inventory_replay", lineage.outputs[0], "Inventory is not re-derivable"
                )
            )
        verified += 1
    return Ok(verified)
