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
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from time import perf_counter, process_time
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter

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
    encode_lineage,
    validate_bundle,
)
from scout.grading.assistance import (
    assemble_queue,
    duplicate_key,
    eligible_candidates,
    execute_assistance,
    freeze_partition,
    population_grouping_posts,
    select_positive_ranked,
    select_random,
    select_ranked,
)
from scout.grading.assistance_population import read_population, retain_population
from scout.grading.assistance_types import (
    ASSISTANCE_PRODUCER_VERSION_ADAPTER,
    ASSISTANCE_PRODUCER_VERSIONS,
    AssistanceConfig,
    AssistanceOutputs,
    AssistanceProducerVersion,
    AssistanceReport,
    ExecutionTiming,
    FittedPositiveTfidf,
    FittedSelector,
    FittedTfidf,
    FrozenPartition,
    GroupingPost,
    PositiveSimilarityResult,
    PositiveSimilaritySelector,
    RankedResult,
    RejectedInput,
    RejectedPopulation,
    ReplayUnavailable,
    ReplayVerification,
    ReviewOutcome,
    ReviewQueue,
    SelectionReference,
    SelectorResult,
    TrainingExample,
    producer_version_for,
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
    producer_version: AssistanceProducerVersion = "2"


@dataclass(frozen=True, slots=True)
class AssistanceExecution:
    bundle: ArtifactBundle
    outputs: AssistanceOutputs
    lineage: ArtifactLineage
    new_artifacts: tuple[RetainedArtifact, ...]
    timing: ExecutionTiming


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


_EXPECTED_RUNTIME_PACKAGE_NAMES = (
    "numpy", "scipy", "scikit-learn", "threadpoolctl", "pydantic"
)


def _packages() -> tuple[RuntimePackage, ...]:
    return tuple(
        RuntimePackage(name=name, version=version(name))
        for name in _EXPECTED_RUNTIME_PACKAGE_NAMES
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
    except (ValueError, OSError, TypeError, AttributeError, PackageNotFoundError):
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
        # One joined population read replaces one post query per evaluation.
        post_rows = conn.execute(
            "SELECT DISTINCT p.* FROM posts p JOIN evaluations e ON e.post_id = p.id "
            "WHERE e.project_key = ? OR e.project_key IS NULL OR trim(e.project_key) = '' "
            "ORDER BY p.id",
            (project,),
        ).fetchall()
        posts = {row["id"]: RecordedPost.model_validate(dict(row)) for row in post_rows}
        grouping = tuple(
            GroupingPost(
                id=post.id,
                platform=post.platform,
                platform_msg_id=post.platform_msg_id,
                parent_id=post.parent_id,
                duplicate_digest=duplicate_key(post)
                if post.content and post.content.strip()
                else None,
            )
            for post in posts.values()
        )
        items: list[RejectedInput] = []
        contexts: dict[tuple[str, str, str], DossierResolution | None] = {}
        for row in rows:
            evaluation = RecordedEvaluation.model_validate(
                {name: row[name] for name in RecordedEvaluation.model_fields}
            )
            needs_context = (
                evaluation.project_key == project
                and evaluation.relevant == 0
                and evaluation.surface_status == "not_relevant"
                and not row["has_grade"]
            )
            context = None
            if (
                needs_context
                and evaluation.project_key
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
                    post=posts.get(evaluation.post_id) if needs_context else None,
                    context=context,
                    has_grade=bool(row["has_grade"]),
                )
            )
        return Ok(
            RejectedPopulation(project_key=project, items=tuple(items), grouping_posts=grouping)
        )
    except (sqlite3.Error, ValueError, OSError, IndexError, KeyError, TypeError):
        return Err(
            ArtifactError("capture_rejected", project, "Invalid recorded candidate row or schema")
        )


def supports_assistance(lineage: ArtifactLineage) -> bool:
    return (
        lineage.kind == "scout.grading.assistance"
        and lineage.process.id == "scout.grading.assistance"
        and lineage.process.version in ASSISTANCE_PRODUCER_VERSIONS
    )


def load_training_examples(
    bundle: ArtifactBundle,
    snapshot_digest: ArtifactDigest,
    provenance_queues: tuple[ArtifactDigest, ...] = (),
) -> Result[tuple[TrainingExample, ...], ArtifactError]:
    """Resolve only explicit, retained snapshot and queue identities, not latest grades."""
    validated = load_corpus_snapshot(bundle, snapshot_digest)
    if isinstance(validated, Err):
        return validated
    contents = {item.digest: item.content for item in bundle.artifacts}
    try:
        snapshot = validated.value
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
            pool_result = read_population(queue.population_digest, contents)
            if isinstance(pool_result, Err):
                return pool_result
            queues.append((digest, queue, pool_result.value))
        examples: list[TrainingExample] = []
        for member in snapshot.members:
            item = FrozenGradeInput.model_validate_json(contents[member.input_digest])
            if item.post is None or item.evaluation is None:
                return Err(
                    ArtifactError("load_training", member.input_digest, "Missing corpus context")
                )
            provenance: list[SelectionReference] = []
            for digest, queue, pool in queues:
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


def load_corpus_snapshot(
    bundle: ArtifactBundle, snapshot_digest: ArtifactDigest
) -> Result[CorpusSnapshot, ArtifactError]:
    """Validate a pinned snapshot without materializing a discarded training projection."""
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
        return Ok(CorpusSnapshot.model_validate_json(contents[snapshot_digest]))
    except (ValueError, KeyError):
        return Err(
            ArtifactError("load_training", snapshot_digest, "Invalid retained training references")
        )


def derive_assistance(
    request: AssistanceRequest, *, population_digest: ArtifactDigest | None = None
) -> Result[AssistanceOutputs, ArtifactError]:
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
        related_posts=population_grouping_posts(request.population),
    )
    if isinstance(partition, Err):
        return partition
    # Bundle construction already encoded this population. Replay independently
    # derives its canonical identity from the recorded source as before.
    if population_digest is None:
        population_digest = (
            retain_population(request.population).digest
            if request.producer_version != "1"
            else digest_artifact(encode_population(request.population))
        )
    return execute_assistance(
        examples.value,
        request.population,
        partition.value,
        request.config,
        population_digest=population_digest,
    )


def build_assistance_bundle(
    request: AssistanceRequest, runtime: RetainedRuntime
) -> Result[AssistanceExecution, ArtifactError]:
    if request.producer_version != producer_version_for(
        request.config, legacy_inline=request.producer_version == "1"
    ):
        return Err(ArtifactError("assistance", None, "Selector/producer version mismatch"))
    # Only explicitly consumed provenance is a gate for a new run.
    prior = verify_provenance(request.bundle, request.provenance_queues)
    if isinstance(prior, Err):
        return prior
    started, cpu_started = perf_counter(), process_time()
    if request.producer_version != "1":
        retained_population = retain_population(request.population)
        population_digest = retained_population.digest
        population_artifacts = retained_population.artifacts
    else:
        population_artifacts = _retained((encode_population(request.population),))
        population_digest = population_artifacts[0].digest
    result = derive_assistance(request, population_digest=population_digest)
    if isinstance(result, Err):
        return result
    config = encode_config(request.config)
    outputs = encode_outputs(result.value)
    timing = ExecutionTiming(
        elapsed_ms=(perf_counter() - started) * 1000, cpu_ms=(process_time() - cpu_started) * 1000
    )
    environment = encode_wire_v1(runtime.identity, RUNTIME_WIRE)
    lineage = ArtifactLineage(
        kind=TransformKind("scout.grading.assistance"),
        inputs=(request.snapshot_digest, population_digest, *request.provenance_queues),
        process=ArtifactProcess(
            id=ProcessId("scout.grading.assistance"),
            version=request.producer_version,
            config_digest=digest_artifact(config),
            environment=EnvironmentIdentity(digest_artifact(environment)),
        ),
        outputs=tuple(digest_artifact(content) for content in outputs),
    )
    artifacts = _retained(
        (
            *(item.content for item in request.bundle.artifacts),
            *(item.content for item in runtime.artifacts),
            *(item.content for item in population_artifacts),
            config,
            *outputs,
        )
    )
    lineages = tuple(dict.fromkeys((*request.bundle.lineages, lineage)))
    bundle = ArtifactBundle(artifacts=artifacts, lineages=lineages)
    validated = validate_bundle(bundle)
    if isinstance(validated, Err):
        return validated
    existing = {item.digest for item in request.bundle.artifacts}
    return Ok(
        AssistanceExecution(
            bundle,
            result.value,
            lineage,
            tuple(item for item in artifacts if item.digest not in existing),
            timing,
        )
    )


def replay_assistance_lineage(
    lineage: ArtifactLineage, bundle: ArtifactBundle
) -> Result[AssistanceOutputs | ReplayUnavailable, ArtifactError]:
    """Replay supported code only; retained source is evidence, never executed."""
    if not supports_assistance(lineage) or len(lineage.inputs) < 2 or len(lineage.outputs) != 4:
        return Err(ArtifactError("replay_assistance", None, "Invalid assistance producer shape"))
    contents: Mapping[ArtifactDigest, bytes] = {
        item.digest: item.content for item in bundle.artifacts
    }
    try:
        # Validate retained structure/references BEFORE considering runtime drift.
        retained = read_assistance_outputs(lineage, contents)
        if isinstance(retained, Err):
            return retained
        population = read_population(lineage.inputs[1], contents)
        if isinstance(population, Err):
            return population
        config = AssistanceConfig.model_validate_json(contents[lineage.process.config_digest])
        if lineage.process.version != producer_version_for(
            config, legacy_inline=lineage.process.version == "1"
        ):
            return Err(
                ArtifactError("replay_assistance", None, "Selector/producer version mismatch")
            )
        examples = load_training_examples(bundle, lineage.inputs[0], lineage.inputs[2:])
        if isinstance(examples, Err):
            return examples
        partition = freeze_partition(
            examples.value,
            lineage.inputs[0],
            config,
            related_posts=population_grouping_posts(population.value),
        )
        if isinstance(partition, Err):
            return partition
        if (
            partition.value != retained.value.partition
            or retained.value.queue.population_digest != lineage.inputs[1]
            or retained.value.queue.project_key != population.value.project_key
            or retained.value.report.project_key != population.value.project_key
            or (config.ranked is None) != (retained.value.model is None)
        ):
            return Err(
                ArtifactError(
                    "replay_assistance", lineage.outputs[2], "Inconsistent retained outputs"
                )
            )
        structural = validate_retained_selection(
            retained.value,
            AssistanceRequest(
                bundle=bundle,
                snapshot_digest=lineage.inputs[0],
                population=population.value,
                config=config,
                provenance_queues=lineage.inputs[2:],
            ),
            examples.value,
        )
        if isinstance(structural, Err):
            return structural
        available = check_replay_runtime(lineage, contents)
        if isinstance(available, Err):
            return available
        if available.value is not None:
            return Ok(available.value)
        derived = derive_assistance(
            AssistanceRequest(
                bundle=bundle,
                snapshot_digest=lineage.inputs[0],
                population=population.value,
                config=config,
                provenance_queues=lineage.inputs[2:],
                producer_version=ASSISTANCE_PRODUCER_VERSION_ADAPTER.validate_python(
                    lineage.process.version
                ),
            )
        )
        if isinstance(derived, Err):
            return derived
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


def validate_retained_selection(
    outputs: AssistanceOutputs,
    request: AssistanceRequest,
    examples: tuple[TrainingExample, ...],
) -> Result[None, ArtifactError]:
    """Check nonnumerical invariants even when fitting cannot replay locally."""
    failure = Err(ArtifactError("replay_assistance", None, "Invalid retained selection structure"))
    candidates, exclusions = eligible_candidates(request.population, examples)
    candidate_ids = tuple(item.evaluation.id for item in candidates)
    sampled = select_random(candidate_ids, request.config.random)
    if isinstance(sampled, Err):
        return sampled
    if sampled.value != outputs.queue.random or exclusions != outputs.report.exclusions:
        return failure
    model, ranked = outputs.model, outputs.queue.ranked
    if request.config.ranked is None:
        if model is not None or ranked is not None or outputs.report.heldout is not None:
            return failure
    elif isinstance(request.config.ranked, PositiveSimilaritySelector):
        if not isinstance(model, FittedPositiveTfidf) or not isinstance(
            ranked, PositiveSimilarityResult
        ):
            return failure
        positive_ids = {item.evaluation_id for item in examples if item.is_relevant}
        if (
            outputs.report.heldout is not None
            or not model.vocabulary
            or len(set(model.vocabulary)) != len(model.vocabulary)
            or len(model.idf) != len(model.vocabulary)
            or len(model.centroid) != len(model.vocabulary)
            or any(value < 1 for value in model.idf)
            or any(value < 0 for value in model.centroid)
            or not math.isclose(
                math.fsum(value * value for value in model.centroid), 1, abs_tol=1e-10
            )
            or not model.train_evaluation_ids
            or model.train_evaluation_ids
            != tuple(
                member.evaluation_id
                for member in outputs.partition.members
                if member.partition == "train" and member.evaluation_id in positive_ids
            )
            or sorted(score.evaluation_id for score in ranked.scores) != sorted(candidate_ids)
        ):
            return failure
        if select_positive_ranked(ranked.scores, candidates, request.config.ranked) != ranked:
            return failure
    else:
        if (
            not isinstance(model, FittedTfidf)
            or not isinstance(ranked, SelectorResult)
            or outputs.report.heldout is None
        ):
            return failure
        if (
            not model.vocabulary
            or len(set(model.vocabulary)) != len(model.vocabulary)
            or len(model.idf) != len(model.vocabulary)
            or len(model.coefficients) != len(model.vocabulary)
            or model.train_evaluation_ids
            != tuple(
                member.evaluation_id
                for member in outputs.partition.members
                if member.partition == "train"
            )
            or sorted(score.evaluation_id for score in ranked.scores) != sorted(candidate_ids)
        ):
            return failure
        if select_ranked(ranked.scores, candidates, request.config.ranked) != ranked:
            return failure
    if (
        assemble_queue(
            request.population,
            ranked,
            sampled.value,
            population_digest=outputs.queue.population_digest,
        )
        != outputs.queue
    ):
        return failure
    return Ok(None)


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
                else TypeAdapter(FittedSelector).validate_json(contents[lineage.outputs[1]]),
                queue=ReviewQueue.model_validate_json(contents[lineage.outputs[2]]),
                report=AssistanceReport.model_validate_json(contents[lineage.outputs[3]]),
            )
        )
    except (ValueError, KeyError):
        return Err(ArtifactError("read_assistance", None, "Invalid assistance output documents"))


def _selectors_match(left: RankedResult | None, right: RankedResult | None) -> bool:
    if left is None or right is None:
        return left is right
    if left.model_copy(update={"scores": ()}) != right.model_copy(update={"scores": ()}):
        return False
    if len(left.scores) != len(right.scores):
        return False
    if isinstance(left, PositiveSimilarityResult) and isinstance(right, PositiveSimilarityResult):
        return all(
            a.evaluation_id == b.evaluation_id
            and tuple(t.term for t in a.explanation) == tuple(t.term for t in b.explanation)
            and _numbers_match(
                (a.similarity, *(t.contribution for t in a.explanation)),
                (b.similarity, *(t.contribution for t in b.explanation)),
            )
            for a, b in zip(left.scores, right.scores, strict=True)
        )
    if not isinstance(left, SelectorResult) or not isinstance(right, SelectorResult):
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
        if (a.train_evaluation_ids, a.vocabulary) != (
            b.train_evaluation_ids,
            b.vocabulary,
        ):
            return False
        if isinstance(a, FittedPositiveTfidf) and isinstance(b, FittedPositiveTfidf):
            if len(a.centroid) != len(a.vocabulary) or len(b.centroid) != len(b.vocabulary):
                return False
            if not _numbers_match((*a.idf, *a.centroid), (*b.idf, *b.centroid)):
                return False
        elif isinstance(a, FittedTfidf) and isinstance(b, FittedTfidf):
            if len(a.coefficients) != len(a.vocabulary) or len(b.coefficients) != len(b.vocabulary):
                return False
            if not _numbers_match(
                (*a.idf, *a.coefficients, a.intercept), (*b.idf, *b.coefficients, b.intercept)
            ):
                return False
        else:
            return False
        if len(a.idf) != len(a.vocabulary):
            return False
    left_queue = left.queue.model_copy(update={"ranked": None})
    right_queue = right.queue.model_copy(update={"ranked": None})
    return left_queue == right_queue and _selectors_match(left.queue.ranked, right.queue.ranked)


def resolve_queue_outcomes(
    bundle: ArtifactBundle,
    snapshot_digest: ArtifactDigest,
    queue: ReviewQueue,
) -> Result[tuple[ReviewOutcome, ...], ArtifactError]:
    validated = load_corpus_snapshot(bundle, snapshot_digest)
    if isinstance(validated, Err):
        return validated
    contents = {item.digest: item.content for item in bundle.artifacts}
    try:
        snapshot = validated.value
        pool_result = read_population(queue.population_digest, contents)
        if isinstance(pool_result, Err):
            return pool_result
        pool = pool_result.value
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


def verify_assistance_replay(bundle: ArtifactBundle) -> Result[ReplayVerification, ArtifactError]:
    checked = validate_bundle(bundle)
    if isinstance(checked, Err):
        return checked
    verified = 0
    unavailable: list[ReplayUnavailable] = []
    for lineage in bundle.lineages:
        if supports_assistance(lineage):
            result = replay_assistance_lineage(lineage, bundle)
            if isinstance(result, Err):
                return result
            if isinstance(result.value, ReplayUnavailable):
                unavailable.append(result.value)
            else:
                verified += 1
    return Ok(
        ReplayVerification(replayed_lineage_count=verified, unverified_here=tuple(unavailable))
    )


def check_replay_runtime(
    lineage: ArtifactLineage,
    contents: Mapping[ArtifactDigest, bytes],
) -> Result[ReplayUnavailable | None, ArtifactError]:
    """Foreign runtime is unavailable here, not corruption or successful replay."""
    try:
        runtime = AssistanceRuntime.model_validate_json(
            contents[ArtifactDigest(lineage.process.environment)]
        )
        declared = ProducerEnvironment.model_validate_json(
            contents[runtime.declared_environment_digest]
        )
        lock = contents[runtime.lock_digest]
        locked = DependencyLock.model_validate(tomllib.loads(lock.decode()))
        SourceArchive.model_validate_json(contents[runtime.source_digest])
        if (
            declared.dependency_lock_digest != runtime.lock_digest
            or digest_artifact(lock) != runtime.lock_digest
            or declared.python_version != runtime.python_version
            or tuple(package.name for package in runtime.packages)
            != _EXPECTED_RUNTIME_PACKAGE_NAMES
            or any(
                not any(
                    pin.name == package.name and pin.version == package.version
                    for pin in locked.package
                )
                for package in runtime.packages
            )
        ):
            return Err(ArtifactError("replay_runtime", None, "Inconsistent retained runtime pins"))
    except (ValueError, KeyError):
        return Err(
            ArtifactError("replay_runtime", None, "Invalid or missing retained runtime evidence")
        )
    detail = "Pinned numerical runtime differs; replay in its recorded environment"
    try:
        compatible = (
            runtime.python_version == platform.python_version()
            and runtime.system == platform.system()
            and runtime.machine == platform.machine()
            and runtime.packages == _packages()
        )
    except PackageNotFoundError:
        compatible = False
        detail = "Required package metadata unavailable in this environment"
    return Ok(
        None
        if compatible
        else ReplayUnavailable(
            lineage_digest=digest_artifact(encode_lineage(lineage)),
            detail=detail,
        )
    )


def verify_provenance(
    bundle: ArtifactBundle,
    queue_digests: tuple[ArtifactDigest, ...],
) -> Result[None, ArtifactError]:
    """Check the transitive provenance dependency set, never unrelated history.

    An explicitly consumed queue must have a verified producer here. Unavailable
    dependencies are a targeted limitation, not an implicit verified assertion.
    """
    pending = list(queue_digests)
    visited: set[ArtifactDigest] = set()
    while pending:
        digest = pending.pop()
        if digest in visited:
            continue
        visited.add(digest)
        producers = tuple(
            lineage
            for lineage in bundle.lineages
            if supports_assistance(lineage)
            and len(lineage.outputs) == 4
            and lineage.outputs[2] == digest
        )
        if not producers:
            return Err(
                ArtifactError("verify_provenance", digest, "No supported provenance producer")
            )
        has_verified = False
        for lineage in producers:
            result = replay_assistance_lineage(lineage, bundle)
            if isinstance(result, Err):
                return result
            if not isinstance(result.value, ReplayUnavailable):
                has_verified = True
                pending.extend(lineage.inputs[2:])
        if not has_verified:
            return Err(
                ArtifactError(
                    "verify_provenance",
                    digest,
                    "Requested provenance is unverified here; use its recorded runtime",
                )
            )
    return Ok(None)
