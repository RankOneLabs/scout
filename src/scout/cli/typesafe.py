"""CLI adapters for typesafe shadow reporting and offline training fits."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from scout.grading.assistance import validate_partition, validate_retained_group_assignments
from scout.grading.assistance_scope import read_assistance_bundle
from scout.grading.assistance_store import load_corpus_snapshot, load_training_examples
from scout.grading.assistance_types import FrozenPartition, RejectedPopulation
from scout.grading.snapshots import CorpusSnapshot
from scout.replay.tasks import EXPERIMENT_TASK_ADAPTER, RelevanceTask
from scout.result import Err
from scout.storage.db import read_only_connection
from scout.storage.shadow_relevance import ShadowRelevanceStore
from scout.typesafe.compose import DECIDE_REGISTRY, register_fitted_gate
from scout.typesafe.fitting import extract_features, fit_weight_set
from scout.typesafe.models import Answers
from scout.typesafe.reporting import (
    FixtureReplayUnavailableError,
    build_report,
    render_text,
    replay_acceptance_fixture,
)


@dataclass(slots=True)
class TypesafeFitError(RuntimeError):
    """Typed, operator-facing fitting refusal."""

    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


def _iso8601(value: str) -> str:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO-8601 timestamp") from exc
    return value


def add_typesafe_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], db_path: str
) -> None:
    parser = subparsers.add_parser("typesafe", help="Typesafe shadow operator commands")
    commands = parser.add_subparsers(dest="typesafe_command", required=True)
    report = commands.add_parser(
        "report", help="Compare shadow, LLM, and finalized human decisions"
    )
    selector = report.add_mutually_exclusive_group(required=True)
    selector.add_argument("--since", type=_iso8601)
    selector.add_argument("--scan-id", type=int)
    report.add_argument("--json", action="store_true")
    report.add_argument("--db-path", default=db_path)
    fit = commands.add_parser("fit", help="Fit a train-only shadow relevance gate")
    fit.add_argument("--task", required=True, help="RelevanceTask JSON or path to its JSON file")
    fit.add_argument("--catalogue-version", required=True, type=_sha256)
    fit.add_argument("--out", required=True, type=Path)
    fit.add_argument("--c-fp", type=_positive_cost, default=1.0)
    fit.add_argument("--c-fn", type=_positive_cost, default=1.0)
    fit.add_argument("--db-path", default=db_path)


def _sha256(value: str) -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise argparse.ArgumentTypeError("must be a lowercase SHA-256 digest")
    return value


def _positive_cost(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive finite number") from exc
    if not (parsed > 0.0 and parsed < float("inf")):
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return parsed


def _task_bytes(value: str) -> bytes:
    if value.lstrip().startswith("{"):
        return value.encode()
    try:
        return Path(value).read_bytes()
    except OSError as exc:
        raise TypesafeFitError("invalid_task", str(exc)) from exc


def _parse_fit_task(task_value: str) -> RelevanceTask:
    try:
        parsed = EXPERIMENT_TASK_ADAPTER.validate_json(_task_bytes(task_value))
    except ValidationError as exc:
        raise TypesafeFitError("invalid_task", str(exc)) from exc
    if not isinstance(parsed, RelevanceTask):
        raise TypesafeFitError("invalid_task", "fit requires a relevance task")
    # This refusal intentionally precedes opening SQLite. It is the leakage guard.
    if parsed.partition != "train" or parsed.partition_digest is None:
        raise TypesafeFitError(
            "train_partition_required",
            "fitting requires partition='train' with a partition_digest",
        )
    return parsed


def _load_fit_inputs(
    parsed: RelevanceTask, conn: sqlite3.Connection
) -> tuple[FrozenPartition, CorpusSnapshot]:
    partition_digest = parsed.partition_digest
    assert partition_digest is not None
    retained = read_assistance_bundle(conn, (parsed.snapshot_digest, partition_digest))
    if isinstance(retained, Err):
        raise TypesafeFitError("load_partition", retained.error.detail)
    bundle = retained.value
    snapshot_result = load_corpus_snapshot(bundle, parsed.snapshot_digest)
    if isinstance(snapshot_result, Err):
        raise TypesafeFitError("load_partition", snapshot_result.error.detail)
    examples_result = load_training_examples(bundle, parsed.snapshot_digest)
    if isinstance(examples_result, Err):
        raise TypesafeFitError("load_partition", examples_result.error.detail)
    contents = {artifact.digest: artifact.content for artifact in bundle.artifacts}
    try:
        partition = FrozenPartition.model_validate_json(contents[partition_digest])
    except (KeyError, ValidationError) as exc:
        raise TypesafeFitError(
            "load_partition", "retained partition or snapshot is invalid"
        ) from exc
    snapshot = snapshot_result.value
    if partition.snapshot_digest != parsed.snapshot_digest:
        raise TypesafeFitError("load_partition", "partition belongs to another snapshot")
    checked = validate_partition(
        examples_result.value,
        RejectedPopulation(project_key=snapshot.selection.project_key, items=()),
        partition,
    )
    if isinstance(checked, Err):
        raise TypesafeFitError("load_partition", checked.error.detail)
    retained_groups = validate_retained_group_assignments(partition)
    if isinstance(retained_groups, Err):
        raise TypesafeFitError("load_partition", retained_groups.error.detail)
    return partition, snapshot


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def _write_report(
    out: Path,
    *,
    task: RelevanceTask,
    weight_set_json: bytes,
    inventory: dict[str, object],
) -> None:
    if out.exists():
        raise TypesafeFitError("output_exists", f"refusing to replace {out}")
    out.mkdir(parents=True)
    plan = (
        "# Typesafe relevance fitting plan\n\n"
        "Fit an L2 logistic regression only on the pinned FrozenPartition train members. "
        "The shadow scan remains non-gating and no held-out evaluation id is queried.\n\n"
        f"- Snapshot: `{task.snapshot_digest}`\n"
        f"- Partition: `{task.partition_digest}` (`train`)\n"
    ).encode()
    decisions = inventory["training_decisions"]
    assert isinstance(decisions, list)
    correct = sum(item["decision"] == item["human_relevant"] for item in decisions)
    results = (
        "# Typesafe relevance fitting results\n\n"
        f"Fitted {len(decisions)} finalized training rows. "
        f"The fitted gate reproduced {correct}/{len(decisions)} training labels.\n\n"
        "The complete per-evaluation decisions are recorded in `inventory.json`; "
        "`weight-set.json` is data for tests and future promotion only.\n"
    ).encode()
    files = {
        "PLAN.md": plan,
        "RESULTS.md": results,
        "inventory.json": _json_bytes(inventory),
        "weight-set.json": weight_set_json,
    }
    for name, content in files.items():
        (out / name).write_bytes(content)
    checksums = {
        name: hashlib.sha256(content).hexdigest() for name, content in sorted(files.items())
    }
    (out / "checksums.json").write_bytes(_json_bytes(checksums))


def run_fit(args: argparse.Namespace) -> int:
    task = _parse_fit_task(args.task)
    with read_only_connection(args.db_path) as conn:
        partition, snapshot = _load_fit_inputs(task, conn)
        train_ids = {
            member.evaluation_id for member in partition.members if member.partition == "train"
        }
        revisions = {
            member.evaluation_id: member.grade_revision_id
            for member in snapshot.members
            if member.evaluation_id in train_ids
        }
        stored_rows = ShadowRelevanceStore.fitting_rows(
            conn,
            catalogue_version=args.catalogue_version,
            finalized_revisions=revisions,
        )
    fitted_ids = {row.evaluation_id for row in stored_rows}
    if fitted_ids != train_ids:
        missing = sorted(train_ids - fitted_ids)
        unexpected = sorted(fitted_ids - train_ids)
        raise TypesafeFitError(
            "fit_unavailable",
            f"training row coverage differs; missing={missing}, unexpected={unexpected}",
        )
    feature_rows: list[tuple[int, dict[str, float], bool]] = []
    answers_by_id: dict[int, Answers] = {}
    expected_labels = {
        member.evaluation_id: member.is_relevant
        for member in snapshot.members
        if member.evaluation_id in train_ids
    }
    for row in stored_rows:
        if row.human_relevant != expected_labels[row.evaluation_id]:
            raise TypesafeFitError(
                "label_mismatch", f"finalized label differs from snapshot for {row.evaluation_id}"
            )
        answers = Answers.model_validate(row.answers)
        try:
            features = extract_features(answers)
        except ValueError as exc:
            raise TypesafeFitError(
                "invalid_answers",
                f"invalid answers for evaluation {row.evaluation_id}: {exc}",
            ) from exc
        answers_by_id[row.evaluation_id] = answers
        feature_rows.append((row.evaluation_id, features, row.human_relevant))
    try:
        weight_set = fit_weight_set(
            feature_rows,
            catalogue_version=args.catalogue_version,
            c_fp=args.c_fp,
            c_fn=args.c_fn,
            fitted_at=datetime.now(UTC),
        )
    except ValueError as exc:
        raise TypesafeFitError("fit_unavailable", str(exc)) from exc
    gate_name = register_fitted_gate(weight_set)
    decide = DECIDE_REGISTRY[gate_name]
    training_decisions = [
        {
            "evaluation_id": evaluation_id,
            "human_relevant": label,
            "decision": decide(answers_by_id[evaluation_id]).eligible,
            "p_eligible": decide(answers_by_id[evaluation_id]).p_eligible,
        }
        for evaluation_id, _features, label in feature_rows
    ]
    inventory: dict[str, object] = {
        "format": "scout.typesafe-fit-inventory/v1",
        "catalogue_version": args.catalogue_version,
        "weight_set_version": weight_set.weight_set_version,
        "row_digest": weight_set.row_digest,
        "partition": "train",
        "train_evaluation_ids": sorted(train_ids),
        "fitted_evaluation_ids": [row[0] for row in feature_rows],
        "training_decisions": training_decisions,
    }
    _write_report(
        args.out,
        task=task,
        weight_set_json=(weight_set.model_dump_json(indent=2) + "\n").encode(),
        inventory=inventory,
    )
    print(f"Wrote fitted gate {gate_name} and report to {args.out}")
    return 0


def run_typesafe(args: argparse.Namespace) -> int:
    if getattr(args, "typesafe_command", "report") == "fit":
        try:
            return run_fit(args)
        except (TypesafeFitError, OSError, sqlite3.Error, ValidationError) as exc:
            print(f"Cannot fit typesafe gate: {exc}", file=sys.stderr)
            return 2
    with read_only_connection(args.db_path) as conn:
        if not ShadowRelevanceStore.table_exists(conn):
            print("shadow_relevance_runs is not present; this database predates schema v47.")
            return 0
        rows = ShadowRelevanceStore.report_rows(conn, since=args.since, scan_id=args.scan_id)
    try:
        fixture_check = replay_acceptance_fixture()
    except FixtureReplayUnavailableError as exc:
        print(f"Cannot produce typesafe report: {exc}")
        return 1
    report = build_report(rows, fixture_check)
    print(report.model_dump_json(indent=2) if args.json else render_text(report))
    return 0
