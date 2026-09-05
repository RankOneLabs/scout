"""Scout artifact-lineage boundary types, independent of persistence.

The four-part transform is the approved grading-workflow model: opaque kind,
input digests, identified process, output digests. This is not a registry of
artifact kinds. Historical evidence without a known process is a retained source
observation, not a transform with manufactured provenance.

These types validate structure only. A persistence boundary must also resolve
references to retained bytes; a producer-specific replay test proves derivation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Annotated, Literal, NewType, TypedDict

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from scout.result import Err, Ok, Result

ArtifactDigest = NewType("ArtifactDigest", str)
TransformKind = NewType("TransformKind", str)
ProcessId = NewType("ProcessId", str)
EnvironmentIdentity = NewType("EnvironmentIdentity", str)

type DigestReference = Annotated[ArtifactDigest, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
type NonblankText = Annotated[str, StringConstraints(pattern=r"\S")]


class ArtifactProcess(BaseModel):
    """Identity supplied by the producer, not inferred from artifact filenames.

    ``environment`` identifies a retained environment description containing the
    producer's code revision and dependency/runtime pins. Its opaque identity
    must resolve at the storage boundary; it is not a friendly environment name.
    ``config_digest`` addresses the exact retained process configuration bytes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Annotated[ProcessId, StringConstraints(pattern=r"\S")]
    version: NonblankText
    config_digest: DigestReference
    environment: Annotated[EnvironmentIdentity, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ArtifactLineage(BaseModel):
    """One producing transform; snapshot, selector, queue, etc. are opaque kinds.

    Order is retained: input/output ordering can be meaningful to a producer.
    Wall-clock recording time and operator annotations are not process identity.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Annotated[TransformKind, StringConstraints(pattern=r"\S")]
    inputs: Annotated[tuple[DigestReference, ...], Field(min_length=1)]
    process: ArtifactProcess
    outputs: Annotated[tuple[DigestReference, ...], Field(min_length=1)]


class LineageProcessDocumentV1(TypedDict):
    """Frozen wire projection of the original ArtifactProcess fields."""

    id: ProcessId
    version: str
    config_digest: ArtifactDigest
    environment: EnvironmentIdentity


class LineageDocumentV1(TypedDict):
    """Original untagged lineage document; field order is part of encoding v1."""

    kind: TransformKind
    inputs: tuple[ArtifactDigest, ...]
    process: LineageProcessDocumentV1
    outputs: tuple[ArtifactDigest, ...]


@dataclass(frozen=True, slots=True)
class ArtifactError:
    operation: str
    entity_id: str | None
    detail: str


class ProducerEnvironment(BaseModel):
    """Operator-supplied pins: git revision, uv.lock bytes, Python runtime version.

    Kept as a source artifact, not inferred from a friendly deployment label.
    Additional environment specifics can be retained separately as evidence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    code_revision: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
    dependency_lock_digest: DigestReference
    python_version: NonblankText


class RetainedArtifact(BaseModel):
    """Exact BLOB content of one analysis_artifacts row, base64 in JSON exports."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", ser_json_bytes="base64", val_json_bytes="base64"
    )

    digest: DigestReference
    content: bytes


class ArtifactBundle(BaseModel):
    """Self-contained preservation boundary, not a new source of identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal["scout.artifact-bundle/v1"] = "scout.artifact-bundle/v1"
    artifacts: tuple[RetainedArtifact, ...]
    lineages: tuple[ArtifactLineage, ...]


def lineage_references(lineage: ArtifactLineage) -> tuple[ArtifactDigest, ...]:
    return (
        *lineage.inputs,
        lineage.process.config_digest,
        ArtifactDigest(lineage.process.environment),
        *lineage.outputs,
    )


def validate_bundle(bundle: ArtifactBundle) -> Result[ArtifactBundle, ArtifactError]:
    """Validate bytes and closure before any import writes; no implicit DB lookup."""
    contents: dict[ArtifactDigest, bytes] = {}
    for artifact in bundle.artifacts:
        if artifact.digest in contents:
            return Err(
                ArtifactError("validate_bundle", artifact.digest, "Duplicate artifact digest")
            )
        if digest_artifact(artifact.content) != artifact.digest:
            return Err(
                ArtifactError("validate_bundle", artifact.digest, "Artifact digest mismatch")
            )
        contents[artifact.digest] = artifact.content
    seen: set[ArtifactDigest] = set()
    for lineage in bundle.lineages:
        digest = digest_artifact(encode_lineage(lineage))
        if digest in seen:
            return Err(ArtifactError("validate_bundle", digest, "Duplicate lineage"))
        seen.add(digest)
        for reference in lineage_references(lineage):
            if reference not in contents:
                return Err(
                    ArtifactError("validate_bundle", reference, "Missing retained reference")
                )
    return Ok(bundle)


def decode_bundle(content: bytes) -> Result[ArtifactBundle, ArtifactError]:
    try:
        bundle = ArtifactBundle.model_validate_json(content)
    except ValidationError:
        return Err(
            ArtifactError("decode_bundle", digest_artifact(content), "Invalid artifact bundle")
        )
    return validate_bundle(bundle)


def digest_artifact(content: bytes) -> ArtifactDigest:
    """Address actual bytes; do not normalize source text or hash a mutable path."""
    return ArtifactDigest(hashlib.sha256(content).hexdigest())


def project_lineage_document_v1(lineage: ArtifactLineage) -> LineageDocumentV1:
    """Pin wire fields and their order independently of Pydantic model rendering."""
    return LineageDocumentV1(
        kind=lineage.kind,
        inputs=lineage.inputs,
        process=LineageProcessDocumentV1(
            id=lineage.process.id,
            version=lineage.process.version,
            config_digest=lineage.process.config_digest,
            environment=lineage.process.environment,
        ),
        outputs=lineage.outputs,
    )


def encode_lineage(lineage: ArtifactLineage) -> bytes:
    """Encode untagged v1 JSON, byte-compatible with already retained lineage.

    Fixed object-field order (not sorted keys), ordered arrays, compact JSON,
    unescaped Unicode and UTF-8 are the v1 contract. No model defaults or JSON
    renderer settings participate. A different encoding needs a new version,
    never a rewrite of v1 bytes or their digest identities.
    """
    return json.dumps(
        project_lineage_document_v1(lineage),
        sort_keys=False,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def decode_lineage(content: bytes) -> Result[ArtifactLineage, ArtifactError]:
    """Deserialize at an IO boundary without exposing private input in errors."""
    try:
        return Ok(ArtifactLineage.model_validate_json(content))
    except ValidationError:
        return Err(
            ArtifactError(
                operation="decode_lineage",
                entity_id=digest_artifact(content),
                detail="Invalid artifact-lineage document",
            )
        )
