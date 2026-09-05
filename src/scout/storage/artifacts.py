"""Append-only content-addressed analysis storage on Scout's shared UnitOfWork.

No garbage collection, file-store coordination, or graph query authority. Both
retained observations and derived artifacts use the same exact-byte store.
Lineage rows merely identify which retained documents describe transforms.
"""

from __future__ import annotations

import sqlite3

from scout.grading.artifacts import (
    ArtifactBundle,
    ArtifactDigest,
    ArtifactError,
    ArtifactLineage,
    RetainedArtifact,
    decode_lineage,
    digest_artifact,
    encode_lineage,
    lineage_references,
    validate_bundle,
)
from scout.result import Err, Ok, Result
from scout.storage.db import TransactionError
from scout.storage.unit_of_work import UnitOfWork


class ArtifactStore:
    """Owns analysis_artifacts and analysis_lineage; never opens a connection."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    def _read(self, digest: ArtifactDigest) -> bytes:
        row = self._uow.conn.execute(
            "SELECT content FROM analysis_artifacts WHERE digest = ?", (digest,)
        ).fetchone()
        if row is None:
            raise ValueError("Missing retained artifact")
        content = row[0]
        if not isinstance(content, bytes) or digest_artifact(content) != digest:
            raise ValueError("Retained artifact digest mismatch")
        return content

    def _put(self, content: bytes) -> ArtifactDigest:
        digest = digest_artifact(content)
        existing = self._uow.conn.execute(
            "SELECT 1 FROM analysis_artifacts WHERE digest = ?", (digest,)
        ).fetchone()
        if existing is not None:
            if self._read(digest) != content:
                raise ValueError("Conflicting artifact bytes")
            return digest
        self._uow.conn.execute(
            "INSERT INTO analysis_artifacts(digest, content) VALUES (?, ?)", (digest, content)
        )
        return digest

    def _record(self, lineage: ArtifactLineage) -> ArtifactDigest:
        # Validate *all* bytes, including existing dependencies, before indexing.
        for reference in lineage_references(lineage):
            self._read(reference)
        digest = self._put(encode_lineage(lineage))
        if (
            self._uow.conn.execute(
                "SELECT 1 FROM analysis_lineage WHERE digest = ?", (digest,)
            ).fetchone()
            is None
        ):
            self._uow.conn.execute("INSERT INTO analysis_lineage(digest) VALUES (?)", (digest,))
        return digest

    def put(self, content: bytes) -> Result[ArtifactDigest, ArtifactError]:
        try:
            with self._uow.begin_immediate():
                digest = self._put(content)
            return Ok(digest)
        except (sqlite3.Error, ValueError, TransactionError):
            return Err(
                ArtifactError("put_artifact", digest_artifact(content), "Artifact write failed")
            )

    def get(self, digest: ArtifactDigest) -> Result[bytes, ArtifactError]:
        try:
            return Ok(self._read(digest))
        except (sqlite3.Error, ValueError, TransactionError):
            return Err(ArtifactError("get_artifact", digest, "Artifact missing or corrupt"))

    def record(self, lineage: ArtifactLineage) -> Result[ArtifactDigest, ArtifactError]:
        try:
            with self._uow.begin_immediate():
                digest = self._record(lineage)
            return Ok(digest)
        except (sqlite3.Error, ValueError, TransactionError):
            return Err(
                ArtifactError("record_lineage", None, "Invalid references or lineage write failed")
            )

    def import_bundle(self, bundle: ArtifactBundle) -> Result[None, ArtifactError]:
        match validate_bundle(bundle):
            case Err() as error:
                return error
            case Ok():
                pass
        try:
            with self._uow.begin_immediate():
                for artifact in bundle.artifacts:
                    self._put(artifact.content)
                for lineage in bundle.lineages:
                    self._record(lineage)
            return Ok(None)
        except (sqlite3.Error, ValueError, TransactionError):
            # Exceptions leave the transaction before becoming values: partial
            # imports must roll back, even when composed under an outer UoW.
            return Err(
                ArtifactError(
                    "import_bundle", None, "Artifact import failed; no bundle writes retained"
                )
            )

    def export_bundle(self) -> Result[ArtifactBundle, ArtifactError]:
        try:
            if self._uow.conn.in_transaction:
                # Borrow the caller's stable view; do not commit, roll back,
                # or change the transaction's locking/query-only guarantees.
                return read_artifact_bundle(self._uow.conn)
            with self._uow.read():
                return read_artifact_bundle(self._uow.conn)
        except (sqlite3.Error, TransactionError):
            return Err(
                ArtifactError("export_bundle", None, "Cannot open artifact read transaction")
            )


def read_artifact_bundle(conn: sqlite3.Connection) -> Result[ArtifactBundle, ArtifactError]:
    """Read-only caller-owned snapshot, also usable on preservation exports."""
    if not conn.in_transaction:
        return Err(ArtifactError("export_bundle", None, "A stable read transaction is required"))
    try:
        artifacts = tuple(
            RetainedArtifact(digest=row[0], content=row[1])
            for row in conn.execute(
                "SELECT digest, content FROM analysis_artifacts ORDER BY digest"
            )
        )
        contents = {artifact.digest: artifact.content for artifact in artifacts}
        lineages: list[ArtifactLineage] = []
        for row in conn.execute("SELECT digest FROM analysis_lineage ORDER BY digest"):
            content = contents.get(row[0])
            if content is None:
                raise ValueError("Missing lineage bytes")
            match decode_lineage(content):
                case Err():
                    raise ValueError("Invalid retained lineage")
                case Ok(lineage):
                    if encode_lineage(lineage) != content:
                        raise ValueError("Noncanonical lineage encoding")
                    lineages.append(lineage)
        return validate_bundle(ArtifactBundle(artifacts=artifacts, lineages=tuple(lineages)))
    except (sqlite3.Error, ValueError):
        return Err(ArtifactError("export_bundle", None, "Artifact export failed integrity checks"))
