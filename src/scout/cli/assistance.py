"""Local-only selector operator commands. No grades, promotions, or paid calls."""

from __future__ import annotations

import argparse
import platform
import resource
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter, process_time

from pydantic import BaseModel

from scout.grading.artifacts import ArtifactDigest, ArtifactError, digest_artifact, encode_lineage
from scout.grading.assistance import eligible_candidates, freeze_partition, report_queue_reviews
from scout.grading.assistance_store import (
    AssistanceRequest,
    build_assistance_bundle,
    capture_runtime,
    load_training_examples,
    read_rejected_population,
    replay_assistance_lineage,
    resolve_queue_outcomes,
    supports_assistance,
)
from scout.grading.assistance_types import (
    AssistanceConfig,
    CandidateExclusion,
    ExecutionObservation,
)
from scout.grading.assistance_wire import OBSERVATION_WIRE, encode_queue
from scout.grading.snapshots import CorpusSnapshot
from scout.grading.wire import encode_wire_v1
from scout.result import Err, Ok, Result
from scout.storage.artifacts import read_artifact_bundle
from scout.storage.db import read_only_connection
from scout.storage.state import StateManager

ASSISTANCE_COMMANDS = (
    "assistance-preview",
    "assistance-run",
    "assistance-replay",
    "assistance-report",
)


class AssistancePreview(BaseModel):
    project_key: str
    training_example_count: int
    train_count: int
    heldout_count: int
    candidate_count: int
    requested_ranked_count: int
    requested_random_count: int
    exclusions: tuple[CandidateExclusion, ...]
    limitations: tuple[str, ...]


class AssistanceReceipt(BaseModel):
    queue_digest: ArtifactDigest
    lineage_digest: ArtifactDigest
    output_digests: tuple[ArtifactDigest, ...]
    execution_observation_digest: ArtifactDigest


def add_assistance_parsers(
    commands: argparse._SubParsersAction[argparse.ArgumentParser], default_db_path: str
) -> None:
    for command in ASSISTANCE_COMMANDS:
        child = commands.add_parser(command, help="Reproducible local grading assistance")
        child.add_argument("--db-path", default=default_db_path)
        if command in ("assistance-preview", "assistance-run"):
            child.add_argument("--snapshot", required=True, help="Retained corpus output digest")
            child.add_argument("--dossier-root", type=Path, required=True)
            child.add_argument("--config", type=Path, required=True)
            child.add_argument("--provenance-queue", action="append", default=[])
        if command == "assistance-run":
            child.add_argument("--environment", type=Path, required=True)
            child.add_argument(
                "--lock", type=Path, required=True, help="Exact dependency lock bytes"
            )
        if command in ("assistance-replay", "assistance-report"):
            child.add_argument("--queue", required=True)
            child.add_argument(
                "--lineage", help="Disambiguate multiple producers of the same queue"
            )
        if command == "assistance-report":
            child.add_argument("--snapshot", required=True, help="New snapshot of reviewed grades")


def run_assistance(args: argparse.Namespace) -> Result[BaseModel, ArtifactError]:
    """Called inside analysis.run_analysis's SQLite/file/validation IO boundary."""
    started = perf_counter()
    cpu_started = process_time()
    with read_only_connection(args.db_path) as conn:
        loaded = read_artifact_bundle(conn)
        if isinstance(loaded, Err):
            return loaded
        bundle = loaded.value
        contents = {item.digest: item.content for item in bundle.artifacts}
        if args.analysis_command in ("assistance-replay", "assistance-report"):
            producers = tuple(
                item
                for item in bundle.lineages
                if supports_assistance(item)
                and len(item.outputs) == 4
                and item.outputs[2] == args.queue
                and (args.lineage is None or digest_artifact(encode_lineage(item)) == args.lineage)
            )
            if not producers:
                return Err(ArtifactError("assistance", args.queue, "Supported queue not found"))
            if len(producers) != 1:
                return Err(
                    ArtifactError(
                        "assistance",
                        args.queue,
                        "Multiple queue producers; specify --lineage from the run receipt",
                    )
                )
            lineage = producers[0]
            replayed = replay_assistance_lineage(lineage, bundle)
            if isinstance(replayed, Err):
                return replayed
            if args.analysis_command == "assistance-replay":
                return Ok(replayed.value.report)
            outcomes = resolve_queue_outcomes(
                bundle, ArtifactDigest(args.snapshot), replayed.value.queue
            )
            if isinstance(outcomes, Err):
                return outcomes
            report = report_queue_reviews(replayed.value.queue, outcomes.value)
            if isinstance(report, Err):
                return report
            return Ok(
                report.value.model_copy(update={"snapshot_digest": ArtifactDigest(args.snapshot)})
            )
        config = AssistanceConfig.model_validate_json(args.config.read_bytes())
        snapshot_digest = ArtifactDigest(args.snapshot)
        provenance = tuple(ArtifactDigest(value) for value in sorted(set(args.provenance_queue)))
        examples = load_training_examples(bundle, snapshot_digest, provenance)
        if isinstance(examples, Err):
            return examples
        snapshot = CorpusSnapshot.model_validate_json(contents[snapshot_digest])
        captured = read_rejected_population(conn, snapshot.selection.project_key, args.dossier_root)
        if isinstance(captured, Err):
            return captured
    # Release the DB read lock before fitting; every input is now frozen in memory.
    if args.analysis_command == "assistance-preview":
        candidates, exclusions = eligible_candidates(captured.value, examples.value)
        partition = freeze_partition(
            examples.value,
            snapshot_digest,
            config,
            related_posts=tuple(
                item.post for item in captured.value.items if item.post is not None
            ),
        )
        limitations: list[str] = []
        if isinstance(partition, Err):
            limitations.append(partition.error.detail)
        if config.random.count > len(candidates):
            limitations.append("Requested random count exceeds eligible pool")
        members = () if isinstance(partition, Err) else partition.value.members
        return Ok(
            AssistancePreview(
                project_key=snapshot.selection.project_key,
                training_example_count=len(examples.value),
                train_count=sum(item.partition == "train" for item in members),
                heldout_count=sum(item.partition == "heldout" for item in members),
                candidate_count=len(candidates),
                requested_ranked_count=0 if config.ranked is None else config.ranked.count,
                requested_random_count=config.random.count,
                exclusions=exclusions,
                limitations=tuple(limitations),
            )
        )
    runtime = capture_runtime(args.environment.read_bytes(), args.lock.read_bytes())
    if isinstance(runtime, Err):
        return runtime
    execution = build_assistance_bundle(
        AssistanceRequest(
            bundle=bundle,
            snapshot_digest=snapshot_digest,
            population=captured.value,
            config=config,
            provenance_queues=provenance,
        ),
        runtime.value,
    )
    if isinstance(execution, Err):
        return execution
    queue_digest = digest_artifact(encode_queue(execution.value.outputs.queue))
    lineage_digest = digest_artifact(encode_lineage(execution.value.lineage))
    # ru_maxrss is a process-lifetime high-water mark, not marginal model memory.
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    observation = ExecutionObservation(
        queue_digest=queue_digest,
        lineage_digest=lineage_digest,
        observed_at=datetime.now(UTC).isoformat(),
        elapsed_ms=(perf_counter() - started) * 1000,
        cpu_ms=(process_time() - cpu_started) * 1000,
        process_peak_rss_bytes=int(rss * 1024)
        if platform.system() == "Linux"
        else (int(rss) if platform.system() == "Darwin" else None),
    )
    observation_bytes = encode_wire_v1(observation, OBSERVATION_WIRE)
    # Include the source measurement in the same atomic import as the derivation.
    from scout.grading.artifacts import RetainedArtifact

    output_bundle = execution.value.bundle.model_copy(
        update={
            "artifacts": (
                *execution.value.bundle.artifacts,
                RetainedArtifact(
                    digest=digest_artifact(observation_bytes), content=observation_bytes
                ),
            )
        }
    )
    with StateManager(args.db_path, allow_create=False) as state:
        saved = state.artifacts.import_bundle(output_bundle)
    if isinstance(saved, Err):
        return saved
    return Ok(
        AssistanceReceipt(
            queue_digest=queue_digest,
            lineage_digest=lineage_digest,
            output_digests=execution.value.lineage.outputs,
            execution_observation_digest=digest_artifact(observation_bytes),
        )
    )
