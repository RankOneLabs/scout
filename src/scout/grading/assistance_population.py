"""V2 content-addressed population manifests, retaining legacy inline reads."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import TypeAdapter

from scout.dossiers.resolver import DossierResolution
from scout.grading.artifacts import ArtifactDigest, ArtifactError, RetainedArtifact, digest_artifact
from scout.grading.assistance_types import (
    GroupingPost,
    RejectedInput,
    RejectedInputReference,
    RejectedPopulation,
    RejectedPopulationManifest,
)
from scout.grading.assistance_wire import CONTEXT_WIRE
from scout.grading.snapshots import EVALUATION_WIRE_V1, POST_WIRE_V1, RecordedPost
from scout.grading.wire import encode_wire_v1, record_wire
from scout.result import Err, Ok, Result

INPUT_REFERENCE_WIRE = record_wire(
    "format evaluation post_digest context_digest has_grade",
    evaluation=EVALUATION_WIRE_V1,
)
GROUPING_WIRE = record_wire("id platform platform_msg_id parent_id duplicate_digest")
MANIFEST_WIRE = record_wire("format project_key items grouping_posts")


@dataclass(frozen=True, slots=True)
class RetainedPopulation:
    manifest: RejectedPopulationManifest
    artifacts: tuple[RetainedArtifact, ...]
    digest: ArtifactDigest


def retain_population(population: RejectedPopulation) -> RetainedPopulation:
    contents: dict[ArtifactDigest, bytes] = {}

    def retain(content: bytes) -> ArtifactDigest:
        digest = digest_artifact(content)
        contents[digest] = content
        return digest

    members: list[ArtifactDigest] = []
    for item in population.items:
        reference = RejectedInputReference(
            evaluation=item.evaluation,
            has_grade=item.has_grade,
            post_digest=None
            if item.post is None
            else retain(encode_wire_v1(item.post, POST_WIRE_V1)),
            context_digest=None
            if item.context is None
            else retain(encode_wire_v1(item.context, CONTEXT_WIRE)),
        )
        members.append(retain(encode_wire_v1(reference, INPUT_REFERENCE_WIRE)))
    groups = tuple(
        retain(encode_wire_v1(post, GROUPING_WIRE)) for post in population.grouping_posts
    )
    manifest = RejectedPopulationManifest(
        project_key=population.project_key, items=tuple(members), grouping_posts=groups
    )
    digest = retain(encode_wire_v1(manifest, MANIFEST_WIRE))
    return RetainedPopulation(
        manifest,
        tuple(RetainedArtifact(digest=key, content=contents[key]) for key in sorted(contents)),
        digest,
    )


def read_population(
    digest: ArtifactDigest,
    contents: Mapping[ArtifactDigest, bytes],
) -> Result[RejectedPopulation, ArtifactError]:
    try:
        document: RejectedPopulation | RejectedPopulationManifest = TypeAdapter(
            RejectedPopulation | RejectedPopulationManifest
        ).validate_json(contents[digest])
        if isinstance(document, RejectedPopulation):
            return Ok(document)
        inputs: list[RejectedInput] = []
        contexts: dict[ArtifactDigest, DossierResolution] = {}
        posts: dict[ArtifactDigest, RecordedPost] = {}
        for reference_digest in document.items:
            reference = RejectedInputReference.model_validate_json(contents[reference_digest])
            if reference.context_digest is not None and reference.context_digest not in contexts:
                contexts[reference.context_digest] = TypeAdapter(DossierResolution).validate_json(
                    contents[reference.context_digest]
                )
            if reference.post_digest is not None and reference.post_digest not in posts:
                posts[reference.post_digest] = RecordedPost.model_validate_json(
                    contents[reference.post_digest]
                )
            inputs.append(
                RejectedInput(
                    evaluation=reference.evaluation,
                    has_grade=reference.has_grade,
                    post=None if reference.post_digest is None else posts[reference.post_digest],
                    context=None
                    if reference.context_digest is None
                    else contexts[reference.context_digest],
                )
            )
        if len({item.evaluation.id for item in inputs}) != len(inputs):
            return Err(ArtifactError("read_population", digest, "Duplicate evaluation identity"))
        return Ok(
            RejectedPopulation(
                project_key=document.project_key,
                items=tuple(inputs),
                grouping_posts=tuple(
                    GroupingPost.model_validate_json(contents[key])
                    for key in document.grouping_posts
                ),
            )
        )
    except (ValueError, KeyError):
        return Err(
            ArtifactError(
                "read_population", digest, "Invalid population or missing retained reference"
            )
        )
