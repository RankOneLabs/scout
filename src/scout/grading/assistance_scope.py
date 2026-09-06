"""Load a requested artifact dependency set without loading historical payloads.

This is a bounded preservation read, not a graph service. Lineage rows remain
the only index; known payload adapters enumerate their own retained references.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from pydantic import BaseModel

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
from scout.grading.assistance_store import AssistanceRuntime
from scout.grading.assistance_types import (
    RejectedInputReference,
    RejectedPopulationManifest,
    ReviewQueue,
)
from scout.grading.snapshots import CorpusSnapshot, PopulationManifest
from scout.result import Err, Ok, Result


class PayloadHeader(BaseModel):
    format: str | None = None


def payload_references(content: bytes) -> Result[tuple[ArtifactDigest, ...], ArtifactError]:
    """Arbitrary source bytes are leaves. Recognized structured payloads must parse."""
    try:
        header = PayloadHeader.model_validate_json(content)
    except ValueError:
        return Ok(())
    try:
        match header.format:
            case "scout.grade-population/v2":
                return Ok(PopulationManifest.model_validate_json(content).items)
            case "scout.relevance-corpus/v1":
                return Ok(
                    tuple(
                        item.input_digest
                        for item in CorpusSnapshot.model_validate_json(content).members
                    )
                )
            case "scout.rejected-population/v2":
                population = RejectedPopulationManifest.model_validate_json(content)
                return Ok((*population.items, *population.grouping_posts))
            case "scout.rejected-input/v2":
                item = RejectedInputReference.model_validate_json(content)
                return Ok(
                    tuple(key for key in (item.post_digest, item.context_digest) if key is not None)
                )
            case "scout.review-queue/v1":
                return Ok((ReviewQueue.model_validate_json(content).population_digest,))
            case "scout.assistance-runtime/v1":
                runtime = AssistanceRuntime.model_validate_json(content)
                return Ok(
                    (
                        runtime.declared_environment_digest,
                        runtime.lock_digest,
                        runtime.source_digest,
                    )
                )
            case _:
                return Ok(())
    except ValueError:
        return Err(
            ArtifactError(
                "read_analysis_scope", digest_artifact(content), "Invalid retained payload"
            )
        )


def read_assistance_bundle(
    conn: sqlite3.Connection,
    roots: tuple[ArtifactDigest, ...],
) -> Result[ArtifactBundle, ArtifactError]:
    if not conn.in_transaction:
        return Err(ArtifactError("read_analysis_scope", None, "Stable read transaction required"))
    try:
        producers: dict[ArtifactDigest, list[ArtifactLineage]] = defaultdict(list)
        # Only small indexed lineage documents are scanned, not artifact BLOBs.
        for row in conn.execute(
            "SELECT l.digest, a.content FROM analysis_lineage l "
            "LEFT JOIN analysis_artifacts a ON a.digest = l.digest ORDER BY l.digest"
        ):
            if not isinstance(row[1], bytes) or digest_artifact(row[1]) != row[0]:
                return Err(ArtifactError("read_analysis_scope", row[0], "Corrupt lineage index"))
            parsed = decode_lineage(row[1])
            if isinstance(parsed, Err):
                return parsed
            if encode_lineage(parsed.value) != row[1]:
                return Err(
                    ArtifactError("read_analysis_scope", row[0], "Noncanonical lineage document")
                )
            for output in parsed.value.outputs:
                producers[output].append(parsed.value)
        pending = list(roots)
        contents: dict[ArtifactDigest, bytes] = {}
        selected: dict[ArtifactDigest, ArtifactLineage] = {}
        while pending:
            digest = pending.pop()
            if digest in contents:
                continue
            row = conn.execute(
                "SELECT content FROM analysis_artifacts WHERE digest = ?", (digest,)
            ).fetchone()
            if row is None or not isinstance(row[0], bytes) or digest_artifact(row[0]) != digest:
                return Err(
                    ArtifactError(
                        "read_analysis_scope", digest, "Missing or corrupt retained artifact"
                    )
                )
            contents[digest] = row[0]
            references = payload_references(row[0])
            if isinstance(references, Err):
                return references
            pending.extend(references.value)
            for lineage in producers.get(digest, ()):
                key = digest_artifact(encode_lineage(lineage))
                if key not in selected:
                    selected[key] = lineage
                    pending.extend(lineage_references(lineage))
        return validate_bundle(
            ArtifactBundle(
                artifacts=tuple(
                    RetainedArtifact(digest=key, content=contents[key]) for key in sorted(contents)
                ),
                lineages=tuple(selected[key] for key in sorted(selected)),
            )
        )
    except (sqlite3.Error, ValueError):
        return Err(
            ArtifactError("read_analysis_scope", None, "Cannot read artifact dependency set")
        )
