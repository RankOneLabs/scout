"""Read-only capture and replay/persistence assembly for grading assistance.

One lineage describes the nested typed pipeline (partition → fit → select →
queue/report). Its four ordered outputs remain individually addressable. Runtime
measurements are source observations, never deterministic transform outputs.
"""

from __future__ import annotations

import contextlib
import math
import platform
import sqlite3
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from scout.dossiers.resolver import DossierResolution, DossierResolutionError, resolve_dossier
from scout.grading.artifacts import (
    ArtifactBundle,
    ArtifactDigest,
    ArtifactError,
    ArtifactLineage,
    ArtifactProcess,
    EnvironmentIdentity,
    ProcessId,
    ProducerEnvironment,
    RetainedArtifact,
    TransformKind,
    digest_artifact,
    validate_bundle,
)
from scout.grading.assistance import execute_assistance, freeze_partition
from scout.grading.assistance_types import (
    AssistanceConfig,
    AssistanceOutputs,
    AssistanceReport,
    FittedTfidf,
    FrozenPartition,
    RejectedInput,
    RejectedPopulation,
    ReviewOutcome,
    ReviewQueue,
    SelectionReference,
    SelectorResult,
    TrainingExample,
)
from scout.grading.assistance_wire import encode_config, encode_outputs, encode_population
from scout.grading.snapshots import (
    CorpusSnapshot,
    FrozenGradeInput,
    RecordedEvaluation,
    RecordedPost,
    supports_snapshot,
    verify_snapshot_replay,
)
from scout.grading.wire import ArrayWire, encode_wire_v1, record_wire
from scout.result import Err, Ok, Result


class RuntimePackage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    version: str


class LockedPackage(BaseModel):
    """Only name/version are needed from the richer uv.lock package record."""

    name: str
    version: str


class DependencyLock(BaseModel):
    package: tuple[LockedPackage, ...]


class SourceFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    path: str
    content: str


class SourceArchive(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    files: tuple[SourceFile, ...]


class AssistanceRuntime(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    format: Literal["scout.assistance-runtime/v1"] = "scout.assistance-runtime/v1"
    declared_environment_digest: ArtifactDigest
    lock_digest: ArtifactDigest
    source_digest: ArtifactDigest
    python_version: str
    system: str
    machine: str
    packages: tuple[RuntimePackage, ...]
    numeric_threads: Literal[1] = 1
    score_absolute_tolerance: Literal["1e-10"] = "1e-10"
    rank_decimal_places: Literal[12] = 12


RUNTIME_WIRE = record_wire(
    "format declared_environment_digest lock_digest source_digest python_version system machine "
    "packages numeric_threads score_absolute_tolerance rank_decimal_places",
    packages=ArrayWire(record_wire("name version")),
)
SOURCE_WIRE = record_wire("files", files=ArrayWire(record_wire("path content")))


@dataclass(frozen=True, slots=True)
class RetainedRuntime:
    identity: AssistanceRuntime
    artifacts: tuple[RetainedArtifact, ...]


@dataclass(frozen=True, slots=True)
class AssistanceRequest:
    bundle: ArtifactBundle
    snapshot_digest: ArtifactDigest
    population: RejectedPopulation
    config: AssistanceConfig
    provenance_queues: tuple[ArtifactDigest, ...] = ()


@dataclass(frozen=True, slots=True)
class AssistanceExecution:
    bundle: ArtifactBundle
    outputs: AssistanceOutputs
    lineage: ArtifactLineage


def _retained(contents: tuple[bytes, ...]) -> tuple[RetainedArtifact, ...]:
    unique = {digest_artifact(content): content for content in contents}
    return tuple(RetainedArtifact(digest=key, content=unique[key]) for key in sorted(unique))


def _installed_source() -> bytes:
    root = Path(__file__).resolve().parent.parent
    archive = SourceArchive(
        files=tuple(
            SourceFile(path=str(path.relative_to(root)), content=path.read_bytes().decode("utf-8"))
            for path in sorted(root.rglob("*.py"))
        )
    )
    return encode_wire_v1(archive, SOURCE_WIRE)


def _packages() -> tuple[RuntimePackage, ...]:
    return tuple(
        RuntimePackage(name=name, version=version(name))
        for name in (
            "numpy",
            "scipy",
            "scikit-learn",
            "threadpoolctl",
            "pydantic",
        )
    )


def capture_runtime(declared: bytes, lock: bytes) -> Result[RetainedRuntime, ArtifactError]:
    """Retain actual installed source as well as the operator-declared Git pin.

    This distinguishes an uncommitted development build from the named revision;
    a code revision string alone is never presented as proof of running bytes.
    """
    try:
        environment = ProducerEnvironment.model_validate_json(declared)
        if environment.dependency_lock_digest != digest_artifact(lock):
            return Err(ArtifactError("capture_runtime", None, "Environment lock digest mismatch"))
        if environment.python_version != platform.python_version():
            return Err(
                ArtifactError("capture_runtime", None, "Environment Python version mismatch")
            )
        locked = DependencyLock.model_validate(tomllib.loads(lock.decode())).package
        packages = _packages()
        for package in packages:
            if not any(
                item.name == package.name and item.version == package.version for item in locked
            ):
                return Err(
                    ArtifactError("capture_runtime", package.name, "Installed version not in lock")
                )
        source = _installed_source()
        runtime = AssistanceRuntime(
            declared_environment_digest=digest_artifact(declared),
            lock_digest=digest_artifact(lock),
            source_digest=digest_artifact(source),
            python_version=platform.python_version(),
            system=platform.system(),
            machine=platform.machine(),
            packages=packages,
        )
        content = encode_wire_v1(runtime, RUNTIME_WIRE)
        return Ok(RetainedRuntime(runtime, _retained((content, declared, lock, source))))
    except (ValueError, OSError, TypeError, AttributeError):
        return Err(ArtifactError("capture_runtime", None, "Cannot capture declared/runtime pins"))


def read_rejected_population(
    conn: sqlite3.Connection, project: str, dossier_root: Path
) -> Result[RejectedPopulation, ArtifactError]:
    if not conn.in_transaction:
        return Err(ArtifactError("capture_rejected", project, "Stable read transaction required"))
    if not project.strip():
        return Err(ArtifactError("capture_rejected", project, "Nonblank project required"))
    try:
        # Restrict project before decoding. Missing project rows remain explicit
        # exclusions rather than being assigned to the requested project.
        rows = conn.execute(
            "SELECT e.*, EXISTS(SELECT 1 FROM grades g WHERE g.evaluation_id = e.id "
            "OR (g.evaluation_id IS NULL AND g.post_id = e.post_id)) AS has_grade "
            "FROM evaluations e WHERE e.project_key = ? OR e.project_key IS NULL "
            "OR trim(e.project_key) = '' ORDER BY e.id",
            (project,),
        ).fetchall()
        items: list[RejectedInput] = []
        contexts: dict[tuple[str, str, str], DossierResolution | None] = {}
        for row in rows:
            evaluation = RecordedEvaluation.model_validate(
                {name: row[name] for name in RecordedEvaluation.model_fields}
            )
            post_row = conn.execute(
                "SELECT * FROM posts WHERE id = ?", (evaluation.post_id,)
            ).fetchone()
            context = None
            if (
                evaluation.project_key
                and evaluation.dossier_revision
                and evaluation.dossier_summary_id
            ):
                key = (
                    evaluation.project_key,
                    evaluation.dossier_revision,
                    evaluation.dossier_summary_id,
                )
                if key not in contexts:
                    with contextlib.suppress(DossierResolutionError):
                        context = resolve_dossier(dossier_root, key[1], key[0], key[2])
                    contexts[key] = context
                context = contexts[key]
            items.append(
                RejectedInput(
                    evaluation=evaluation,
                    post=None if post_row is None else RecordedPost.model_validate(dict(post_row)),
                    context=context,
                    has_grade=bool(row["has_grade"]),
                )
            )
        return Ok(RejectedPopulation(project_key=project, items=tuple(items)))
    except (sqlite3.Error, ValueError, OSError, IndexError, KeyError, TypeError):
        return Err(
            ArtifactError("capture_rejected", project, "Invalid recorded candidate row or schema")
        )


def supports_assistance(lineage: ArtifactLineage) -> bool:
    return (
        lineage.kind == "scout.grading.assistance"
        and lineage.process.id == "scout.grading.assistance"
        and lineage.process.version == "1"
    )


def load_training_examples(
    bundle: ArtifactBundle,
    snapshot_digest: ArtifactDigest,
    provenance_queues: tuple[ArtifactDigest, ...] = (),
) -> Result[tuple[TrainingExample, ...], ArtifactError]:
    """Resolve only explicit, retained snapshot and queue identities, not latest grades."""
    producers = tuple(
        lineage
        for lineage in bundle.lineages
        if supports_snapshot(lineage) and lineage.outputs == (snapshot_digest,)
    )
    verified = verify_snapshot_replay(bundle.model_copy(update={"lineages": producers}))
    if isinstance(verified, Err):
        return verified
    contents = {item.digest: item.content for item in bundle.artifacts}
    if not producers:
        return Err(
            ArtifactError("load_training", snapshot_digest, "Expected a supported corpus snapshot")
        )
    try:
        snapshot = CorpusSnapshot.model_validate_json(contents[snapshot_digest])
        queues: list[tuple[ArtifactDigest, ReviewQueue, RejectedPopulation]] = []
        for digest in provenance_queues:
            if not any(
                supports_assistance(lineage)
                and len(lineage.outputs) == 4
                and lineage.outputs[2] == digest
                for lineage in bundle.lineages
            ):
                return Err(
                    ArtifactError("load_training", digest, "Expected a retained assistance queue")
                )
            queue = ReviewQueue.model_validate_json(contents[digest])
            if queue.project_key != snapshot.selection.project_key:
                return Err(
                    ArtifactError("load_training", digest, "Queue project differs from snapshot")
                )
            pool = RejectedPopulation.model_validate_json(contents[queue.population_digest])
            queues.append((digest, queue, pool))
        examples: list[TrainingExample] = []
        for member in snapshot.members:
            item = FrozenGradeInput.model_validate_json(contents[member.input_digest])
            if item.post is None or item.evaluation is None:
                return Err(
                    ArtifactError("load_training", member.input_digest, "Missing corpus context")
                )
            provenance: list[SelectionReference] = []
            for digest, queue, pool in queues:
                # Retain provenance only for the exact observed input, not an ID
                # reused after post edits or a changed project/evaluation.
                if not any(
                    candidate.evaluation == item.evaluation
                    and candidate.post == item.post
                    and not candidate.has_grade
                    for candidate in pool.items
                ):
                    continue
                if member.evaluation_id in queue.random.selected_evaluation_ids:
                    provenance.append(SelectionReference(queue_digest=digest, method="random"))
                if queue.ranked and member.evaluation_id in queue.ranked.selected_evaluation_ids:
                    provenance.append(SelectionReference(queue_digest=digest, method="ranked"))
            examples.append(
                TrainingExample(
                    evaluation_id=member.evaluation_id,
                    input_digest=member.input_digest,
                    post=item.post,
                    is_relevant=member.is_relevant,
                    exposed=bool(item.exposures),
                    provenance=tuple(provenance),
                )
            )
        return Ok(tuple(sorted(examples, key=lambda item: item.evaluation_id)))
    except (ValueError, KeyError):
        return Err(
            ArtifactError("load_training", snapshot_digest, "Invalid retained training references")
        )


def derive_assistance(request: AssistanceRequest) -> Result[AssistanceOutputs, ArtifactError]:
    examples = load_training_examples(
        request.bundle, request.snapshot_digest, request.provenance_queues
    )
    if isinstance(examples, Err):
        return examples
    contents = {item.digest: item.content for item in request.bundle.artifacts}
    snapshot = CorpusSnapshot.model_validate_json(contents[request.snapshot_digest])
    if request.population.project_key != snapshot.selection.project_key:
        return Err(
            ArtifactError(
                "assistance", request.snapshot_digest, "Candidate/corpus project mismatch"
            )
        )
    partition = freeze_partition(
        examples.value,
        request.snapshot_digest,
        request.config,
        related_posts=tuple(
            item.post for item in request.population.items if item.post is not None
        ),
    )
    if isinstance(partition, Err):
        return partition
    return execute_assistance(examples.value, request.population, partition.value, request.config)


def build_assistance_bundle(
    request: AssistanceRequest, runtime: RetainedRuntime
) -> Result[AssistanceExecution, ArtifactError]:
    # Do not let a corrupt earlier queue supply fabricated random provenance.
    prior = verify_assistance_replay(request.bundle)
    if isinstance(prior, Err):
        return prior
    result = derive_assistance(request)
    if isinstance(result, Err):
        return result
    population = encode_population(request.population)
    config = encode_config(request.config)
    outputs = encode_outputs(result.value)
    environment = encode_wire_v1(runtime.identity, RUNTIME_WIRE)
    lineage = ArtifactLineage(
        kind=TransformKind("scout.grading.assistance"),
        inputs=(request.snapshot_digest, digest_artifact(population), *request.provenance_queues),
        process=ArtifactProcess(
            id=ProcessId("scout.grading.assistance"),
            version="1",
            config_digest=digest_artifact(config),
            environment=EnvironmentIdentity(digest_artifact(environment)),
        ),
        outputs=tuple(digest_artifact(content) for content in outputs),
    )
    artifacts = _retained(
        (
            *(item.content for item in request.bundle.artifacts),
            *(item.content for item in runtime.artifacts),
            population,
            config,
            *outputs,
        )
    )
    lineages = tuple(dict.fromkeys((*request.bundle.lineages, lineage)))
    bundle = ArtifactBundle(artifacts=artifacts, lineages=lineages)
    validated = validate_bundle(bundle)
    if isinstance(validated, Err):
        return validated
    return Ok(AssistanceExecution(bundle, result.value, lineage))


def replay_assistance_lineage(
    lineage: ArtifactLineage, bundle: ArtifactBundle
) -> Result[AssistanceOutputs, ArtifactError]:
    """Replay supported code only; retained source is evidence, never executed."""
    if not supports_assistance(lineage) or len(lineage.inputs) < 2 or len(lineage.outputs) != 4:
        return Err(ArtifactError("replay_assistance", None, "Invalid assistance producer shape"))
    contents: Mapping[ArtifactDigest, bytes] = {
        item.digest: item.content for item in bundle.artifacts
    }
    try:
        runtime = AssistanceRuntime.model_validate_json(
            contents[ArtifactDigest(lineage.process.environment)]
        )
        recaptured = capture_runtime(
            contents[runtime.declared_environment_digest], contents[runtime.lock_digest]
        )
        if isinstance(recaptured, Err):
            return recaptured
        # Current code may contain a compatible v1 adapter after unrelated Scout
        # changes. Retain the actual original source, but establish compatibility
        # by replaying outputs, not by requiring unrelated files to match.
        compatible_runtime = recaptured.value.identity.model_copy(
            update={"source_digest": runtime.source_digest}
        )
        if compatible_runtime != runtime or runtime.source_digest not in contents:
            return Err(
                ArtifactError(
                    "replay_assistance",
                    lineage.outputs[2],
                    "Pinned numerical runtime differs; replay in its environment",
                )
            )
        population = RejectedPopulation.model_validate_json(contents[lineage.inputs[1]])
        config = AssistanceConfig.model_validate_json(contents[lineage.process.config_digest])
        derived = derive_assistance(
            AssistanceRequest(
                bundle=bundle,
                snapshot_digest=lineage.inputs[0],
                population=population,
                config=config,
                provenance_queues=lineage.inputs[2:],
            )
        )
        if isinstance(derived, Err):
            return derived
        retained = read_assistance_outputs(lineage, contents)
        if isinstance(retained, Err):
            return retained
        if not outputs_match(retained.value, derived.value):
            return Err(
                ArtifactError(
                    "replay_assistance", lineage.outputs[2], "Outputs are not re-derivable"
                )
            )
        return derived
    except (ValueError, KeyError, OSError):
        return Err(
            ArtifactError(
                "replay_assistance", None, "Invalid assistance inputs or runtime references"
            )
        )


def _numbers_match(left: tuple[float, ...], right: tuple[float, ...]) -> bool:
    return len(left) == len(right) and all(
        math.isclose(a, b, rel_tol=0, abs_tol=1e-10) for a, b in zip(left, right, strict=True)
    )


def read_assistance_outputs(
    lineage: ArtifactLineage,
    contents: Mapping[ArtifactDigest, bytes],
) -> Result[AssistanceOutputs, ArtifactError]:
    """Decode supported output shapes for projections; this does not prove replay."""
    if not supports_assistance(lineage) or len(lineage.inputs) < 2 or len(lineage.outputs) != 4:
        return Err(ArtifactError("read_assistance", None, "Invalid assistance producer shape"))
    try:
        return Ok(
            AssistanceOutputs(
                partition=FrozenPartition.model_validate_json(contents[lineage.outputs[0]]),
                model=None
                if contents[lineage.outputs[1]] == b"null"
                else FittedTfidf.model_validate_json(contents[lineage.outputs[1]]),
                queue=ReviewQueue.model_validate_json(contents[lineage.outputs[2]]),
                report=AssistanceReport.model_validate_json(contents[lineage.outputs[3]]),
            )
        )
    except (ValueError, KeyError):
        return Err(ArtifactError("read_assistance", None, "Invalid assistance output documents"))


def _selectors_match(left: SelectorResult | None, right: SelectorResult | None) -> bool:
    if left is None or right is None:
        return left is right
    if left.model_copy(update={"scores": ()}) != right.model_copy(update={"scores": ()}):
        return False
    if len(left.scores) != len(right.scores):
        return False
    for a, b in zip(left.scores, right.scores, strict=True):
        if a.evaluation_id != b.evaluation_id or tuple(t.term for t in a.explanation) != tuple(
            t.term for t in b.explanation
        ):
            return False
        if not _numbers_match(
            (a.probability, *(t.contribution for t in a.explanation)),
            (b.probability, *(t.contribution for t in b.explanation)),
        ):
            return False
    return True


def outputs_match(left: AssistanceOutputs, right: AssistanceOutputs) -> bool:
    """Discrete identities/order are exact; only numerical payloads allow 1e-10."""
    if left.partition != right.partition or left.report != right.report:
        return False
    a, b = left.model, right.model
    if a is None or b is None:
        if a is not b:
            return False
    else:
        if (a.train_evaluation_ids, a.vocabulary, a.iterations) != (
            b.train_evaluation_ids,
            b.vocabulary,
            b.iterations,
        ):
            return False
        if not _numbers_match(
            (*a.idf, *a.coefficients, a.intercept), (*b.idf, *b.coefficients, b.intercept)
        ):
            return False
        if len(a.idf) != len(a.vocabulary) or len(a.coefficients) != len(a.vocabulary):
            return False
    left_queue = left.queue.model_copy(update={"ranked": None})
    right_queue = right.queue.model_copy(update={"ranked": None})
    return left_queue == right_queue and _selectors_match(left.queue.ranked, right.queue.ranked)


def resolve_queue_outcomes(
    bundle: ArtifactBundle,
    snapshot_digest: ArtifactDigest,
    queue: ReviewQueue,
) -> Result[tuple[ReviewOutcome, ...], ArtifactError]:
    examples = load_training_examples(bundle, snapshot_digest)
    if isinstance(examples, Err):
        return examples
    contents = {item.digest: item.content for item in bundle.artifacts}
    try:
        snapshot = CorpusSnapshot.model_validate_json(contents[snapshot_digest])
        pool = RejectedPopulation.model_validate_json(contents[queue.population_digest])
        if (
            snapshot.selection.project_key != queue.project_key
            or pool.project_key != queue.project_key
        ):
            return Err(ArtifactError("review_report", snapshot_digest, "Project mismatch"))
        original = {item.evaluation.id: item for item in pool.items}
        outcomes: list[ReviewOutcome] = []
        for member in snapshot.members:
            item = FrozenGradeInput.model_validate_json(contents[member.input_digest])
            candidate = original.get(member.evaluation_id)
            if (
                candidate is not None
                and candidate.post == item.post
                and candidate.evaluation == item.evaluation
            ):
                outcomes.append(
                    ReviewOutcome(
                        evaluation_id=member.evaluation_id,
                        is_relevant=member.is_relevant,
                        grade_revision_id=member.grade_revision_id,
                    )
                )
        return Ok(tuple(outcomes))
    except (ValueError, KeyError):
        return Err(
            ArtifactError("review_report", snapshot_digest, "Invalid snapshot/queue references")
        )


def verify_assistance_replay(bundle: ArtifactBundle) -> Result[int, ArtifactError]:
    checked = validate_bundle(bundle)
    if isinstance(checked, Err):
        return checked
    verified = 0
    for lineage in bundle.lineages:
        if supports_assistance(lineage):
            result = replay_assistance_lineage(lineage, bundle)
            if isinstance(result, Err):
                return result
            verified += 1
    return Ok(verified)
