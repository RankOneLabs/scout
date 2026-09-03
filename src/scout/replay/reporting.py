"""Canonical segmented JSON/Markdown reports for a completed batch or sweep
replay.

Reads only durable, already-persisted evidence — one or more batch/sweep
`experiment_runs` parents (a plain batch's one parent, or every variant
parent that shares one sweep) — for the parents' own persisted plan
identity, resolved population, and per-variant skipped-pair evidence
(`experiment_runs.candidate_config`, BATCH_CANDIDATE_CONFIG_VERSION v4+),
and their `evaluation_experiments` attempts (latest attempt per case for
scoring; every attempt, including a retried case's superseded one, for
actual spend) and each complete attempt's `trace_comparisons.score_
evidence`. Never touches Jig's trace store and never reads a raw prompt,
correction, or structured-output value: only versioned identity hashes,
numeric distances/deltas, and cost. `experiment_runs.candidate_config`'s
literal `system_prompt_override` text is deliberately never read here —
only its sha256 and the variant/model identity around it.

Every given experiment_run_id must share one authorized plan
(`plan_sha256`) — mixing parents from different plans (a mistaken
--experiment-run-id list) is rejected before any other work.

Segmentation is exact and mandatory: cases (attempted *and* skipped) are
grouped by (baseline_model, baseline_prompt_sha256). A report never pools
two segments together and never names an overall winner — only, within
each segment, a ranking of that segment's own candidate variants by mean
paired distance delta on their common successfully-scored case
intersection, plus each variant's own full coverage (scored, failed, and
skipped by classification) and a deterministic 95% paired-bootstrap
interval (unavailable below two paired cases).
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from scout.replay.experiments import BATCH_CANDIDATE_CONFIG_VERSION, DEFAULT_BATCH_VARIANT_NAME
from scout.storage.state import StateManager

REPORT_SCHEMA_VERSION = 2
BOOTSTRAP_METHOD = "paired_bootstrap_percentile"
BOOTSTRAP_VERSION = 1
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_MIN_PAIRED_CASES = 2
BOOTSTRAP_CI_LOWER_QUANTILE = 0.025
BOOTSTRAP_CI_UPPER_QUANTILE = 0.975

REPORTABLE_ATTEMPT_STATUSES = ("complete", "failed")
SKIP_CLASSIFICATIONS = ("unscored", "no_op", "unpriceable")


class ReportError(Exception):
    """The requested experiment_run_ids do not resolve to reportable
    batch/sweep evidence, or do not all share one authorized plan."""


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    )


def _segment_key(baseline_model: str, baseline_prompt_sha256: str) -> str:
    return f"{baseline_model}|{baseline_prompt_sha256}"


def _bootstrap_seed(experiment_run_ids: Sequence[int], segment_key: str, variant_name: str) -> int:
    """A deterministic seed derived from the report's own identity — the
    exact set of experiment_run_ids being reported plus the segment and
    variant — so the same report input always resamples identically, and
    distinct segments/variants never accidentally share a resampling
    stream."""
    identity = f"{sorted(experiment_run_ids)}:{segment_key}:{variant_name}"
    return int.from_bytes(hashlib.sha256(identity.encode("utf-8")).digest()[:8], "big")


def _bootstrap_ci(deltas: list[float], *, seed: int) -> tuple[float, float]:
    """Deterministic 95% percentile paired-bootstrap interval: `seed`
    fully determines every resample, so identical inputs always reproduce
    an identical interval."""
    rng = random.Random(seed)
    n = len(deltas)
    means = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        means.append(sum(deltas[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    lower = means[int(BOOTSTRAP_CI_LOWER_QUANTILE * (BOOTSTRAP_RESAMPLES - 1))]
    upper = means[int(BOOTSTRAP_CI_UPPER_QUANTILE * (BOOTSTRAP_RESAMPLES - 1))]
    return lower, upper


def _collect_parents(
    state: StateManager, experiment_run_ids: Sequence[int],
) -> list[dict[str, Any]]:
    """Read and validate every given experiment_runs parent's batch/sweep
    candidate_config. Raises ReportError for an unknown id, a non-batch/
    sweep parent, or a set of parents that do not all share one
    authorized plan (plan_sha256)."""
    parents = []
    for experiment_run_id in experiment_run_ids:
        run = state.get_experiment_run(experiment_run_id)
        if run is None:
            raise ReportError(f"no experiment_runs row with id={experiment_run_id}")
        config = json.loads(run["candidate_config"])
        if config.get("version") != BATCH_CANDIDATE_CONFIG_VERSION:
            raise ReportError(
                f"experiment_run {experiment_run_id} is not a batch/sweep parent "
                f"(candidate_config version {config.get('version')!r} != "
                f"{BATCH_CANDIDATE_CONFIG_VERSION!r})"
            )
        parents.append({"experiment_run_id": experiment_run_id, **config})

    plan_hashes = {parent["plan_sha256"] for parent in parents}
    if len(plan_hashes) > 1:
        raise ReportError(
            f"experiment_run_ids {sorted(experiment_run_ids)} do not share one authorized plan "
            f"-- found {len(plan_hashes)} distinct plan_sha256 values: {sorted(plan_hashes)}"
        )
    return parents


@dataclass(frozen=True, slots=True)
class ReportCase:
    """One attempted (case, variant)'s durable, publication-safe
    evidence — no raw prompt, correction, or structured-output text."""

    phase_run_id: int
    variant_name: str
    segment_key: str
    baseline_model: str
    baseline_prompt_sha256: str
    status: str
    baseline_distance: float | None
    candidate_distance: float | None
    delta: float | None
    estimated_usd: float | None
    actual_usd: float | None
    error_detail: str | None


@dataclass(frozen=True, slots=True)
class SkippedPair:
    """One (case, variant) a skip policy excluded before any attempt --
    never produced its own evaluation_experiments row, so this is read
    entirely from the parent's own persisted skipped_pairs evidence."""

    phase_run_id: int
    variant_name: str
    segment_key: str
    classification: str
    reason: str | None


def _collect_attempted_cases(
    state: StateManager, parents: list[dict[str, Any]],
) -> list[ReportCase]:
    cases: list[ReportCase] = []
    for parent in parents:
        experiment_run_id = parent["experiment_run_id"]
        variant_name = parent.get("variant_name", DEFAULT_BATCH_VARIANT_NAME)

        latest_by_case: dict[int, dict[str, Any]] = {}
        for attempt in state.list_experiment_attempts(experiment_run_id):
            current = latest_by_case.get(attempt["phase_run_id"])
            if current is None or attempt["attempt_number"] > current["attempt_number"]:
                latest_by_case[attempt["phase_run_id"]] = attempt

        for attempt in latest_by_case.values():
            if attempt["status"] not in REPORTABLE_ATTEMPT_STATUSES:
                continue
            evidence = json.loads(attempt["baseline_evidence"])
            segment_key = _segment_key(
                evidence["baseline_model"], evidence["baseline_prompt_sha256"],
            )
            baseline_distance = candidate_distance = delta = None
            if attempt["status"] == "complete":
                comparison = state.get_trace_comparison(attempt["id"])
                if comparison is not None and comparison["score_evidence"] is not None:
                    score = json.loads(comparison["score_evidence"])
                    baseline_distance = score["baseline_distance"]
                    candidate_distance = score["candidate_distance"]
                    delta = score["delta"]
            cases.append(
                ReportCase(
                    phase_run_id=attempt["phase_run_id"],
                    variant_name=variant_name,
                    segment_key=segment_key,
                    baseline_model=evidence["baseline_model"],
                    baseline_prompt_sha256=evidence["baseline_prompt_sha256"],
                    status=attempt["status"],
                    baseline_distance=baseline_distance,
                    candidate_distance=candidate_distance,
                    delta=delta,
                    estimated_usd=evidence.get("estimated_usd"),
                    actual_usd=attempt["candidate_cost"],
                    error_detail=attempt["error_detail"],
                )
            )
    return cases


def _collect_skipped_pairs(parents: list[dict[str, Any]]) -> list[SkippedPair]:
    skipped: list[SkippedPair] = []
    for parent in parents:
        variant_name = parent.get("variant_name", DEFAULT_BATCH_VARIANT_NAME)
        for raw in parent.get("skipped_pairs", []):
            segment_key = _segment_key(raw["baseline_model"], raw["baseline_prompt_sha256"])
            skipped.append(
                SkippedPair(
                    phase_run_id=raw["phase_run_id"], variant_name=variant_name,
                    segment_key=segment_key, classification=raw["classification"],
                    reason=raw["reason"],
                )
            )
    return skipped


def _total_actual_cost_including_superseded(
    state: StateManager, experiment_run_ids: Sequence[int],
) -> float | None:
    """Sum candidate_cost across every immutable attempt -- including a
    retried case's superseded (failed) attempt, which may have already
    recorded a real cost before failing. This is the true total spend;
    per-case entries elsewhere report only the latest attempt's cost."""
    total = 0.0
    found_any = False
    for experiment_run_id in experiment_run_ids:
        for attempt in state.list_experiment_attempts(experiment_run_id):
            if attempt["candidate_cost"] is not None:
                total += attempt["candidate_cost"]
                found_any = True
    return total if found_any else None


@dataclass(frozen=True, slots=True)
class VariantSegmentSummary:
    variant_name: str
    scored_case_count: int
    failed_case_count: int
    unscored_count: int
    no_op_count: int
    unpriceable_count: int
    common_case_count: int
    mean_delta: float | None
    interval_available: bool
    interval_seed: int | None
    ci_lower: float | None
    ci_upper: float | None


def _ranking_key(summary: VariantSegmentSummary) -> float:
    assert summary.mean_delta is not None
    return summary.mean_delta


@dataclass(frozen=True, slots=True)
class SegmentReport:
    segment_key: str
    baseline_model: str
    baseline_prompt_sha256: str
    variants: tuple[VariantSegmentSummary, ...]
    ranking: tuple[str, ...]


def _build_segments(
    cases: list[ReportCase], skipped: list[SkippedPair], *, experiment_run_ids: Sequence[int],
) -> list[SegmentReport]:
    segment_keys = {c.segment_key for c in cases} | {s.segment_key for s in skipped}

    by_segment_cases: dict[str, list[ReportCase]] = defaultdict(list)
    for case in cases:
        by_segment_cases[case.segment_key].append(case)
    by_segment_skipped: dict[str, list[SkippedPair]] = defaultdict(list)
    for pair in skipped:
        by_segment_skipped[pair.segment_key].append(pair)

    segments: list[SegmentReport] = []
    for segment_key in sorted(segment_keys):
        segment_cases = by_segment_cases.get(segment_key, [])
        segment_skipped = by_segment_skipped.get(segment_key, [])
        variant_names = {c.variant_name for c in segment_cases} | {
            p.variant_name for p in segment_skipped
        }

        by_variant_cases: dict[str, list[ReportCase]] = defaultdict(list)
        for case in segment_cases:
            by_variant_cases[case.variant_name].append(case)
        by_variant_skipped: dict[str, list[SkippedPair]] = defaultdict(list)
        for pair in segment_skipped:
            by_variant_skipped[pair.variant_name].append(pair)

        scored_ids_by_variant = {
            variant: {
                c.phase_run_id for c in by_variant_cases.get(variant, []) if c.status == "complete"
            }
            for variant in variant_names
        }
        common_ids: set[int] = (
            set.intersection(*scored_ids_by_variant.values()) if scored_ids_by_variant else set()
        )

        summaries = []
        for variant_name in sorted(variant_names):
            variant_cases = by_variant_cases.get(variant_name, [])
            variant_skipped = by_variant_skipped.get(variant_name, [])
            scored = [c for c in variant_cases if c.status == "complete"]
            failed = [c for c in variant_cases if c.status == "failed"]
            common_deltas = [
                c.delta for c in scored if c.phase_run_id in common_ids and c.delta is not None
            ]
            mean_delta = sum(common_deltas) / len(common_deltas) if common_deltas else None
            interval_available = len(common_deltas) >= BOOTSTRAP_MIN_PAIRED_CASES
            interval_seed = ci_lower = ci_upper = None
            if interval_available:
                interval_seed = _bootstrap_seed(experiment_run_ids, segment_key, variant_name)
                ci_lower, ci_upper = _bootstrap_ci(common_deltas, seed=interval_seed)
            skip_counts = Counter(pair.classification for pair in variant_skipped)
            summaries.append(
                VariantSegmentSummary(
                    variant_name=variant_name,
                    scored_case_count=len(scored),
                    failed_case_count=len(failed),
                    unscored_count=skip_counts.get("unscored", 0),
                    no_op_count=skip_counts.get("no_op", 0),
                    unpriceable_count=skip_counts.get("unpriceable", 0),
                    common_case_count=len(common_deltas),
                    mean_delta=mean_delta,
                    interval_available=interval_available,
                    interval_seed=interval_seed,
                    ci_lower=ci_lower,
                    ci_upper=ci_upper,
                )
            )

        ranking = tuple(
            summary.variant_name
            for summary in sorted(
                (s for s in summaries if s.mean_delta is not None), key=_ranking_key,
            )
        )
        baseline_model, baseline_prompt_sha256 = segment_key.split("|", 1)
        segments.append(
            SegmentReport(
                segment_key=segment_key, baseline_model=baseline_model,
                baseline_prompt_sha256=baseline_prompt_sha256,
                variants=tuple(summaries), ranking=ranking,
            )
        )
    return segments


def build_batch_report(state: StateManager, *, experiment_run_ids: Sequence[int]) -> dict[str, Any]:
    """Build the canonical batch/sweep report document for the given
    experiment_runs parent id(s) — a plain batch's one parent, or every
    variant parent sharing one sweep. Raises ReportError for an unknown
    id, a non-batch/sweep parent (single-replay CLI's v2 candidate_config),
    parents that do not all share one authorized plan, or a set with no
    reportable evidence at all (no attempts and no skipped pairs).
    """
    if not experiment_run_ids:
        raise ReportError("at least one experiment_run_id is required")
    duplicate_ids = sorted(
        experiment_run_id
        for experiment_run_id, count in Counter(experiment_run_ids).items()
        if count > 1
    )
    if duplicate_ids:
        raise ReportError(f"duplicate experiment_run_id(s) are not allowed: {duplicate_ids}")
    parents = _collect_parents(state, experiment_run_ids)
    plan_sha256 = parents[0]["plan_sha256"]
    population = tuple(parents[0].get("phase_run_ids", ()))
    dropped_duplicates = tuple(parents[0].get("dropped_duplicate_phase_run_ids", ()))

    cases = _collect_attempted_cases(state, parents)
    skipped = _collect_skipped_pairs(parents)
    if not cases and not skipped:
        raise ReportError(
            "no reportable evidence (attempts or skipped pairs) under the given experiment_run_ids"
        )
    segments = _build_segments(cases, skipped, experiment_run_ids=experiment_run_ids)

    complete_cases = [case for case in cases if case.status == "complete"]
    failed_cases = [case for case in cases if case.status == "failed"]
    estimated_costs = [case.estimated_usd for case in cases if case.estimated_usd is not None]
    total_actual_usd = _total_actual_cost_including_superseded(state, experiment_run_ids)

    exclusions = [
        {
            "phase_run_id": pair.phase_run_id, "variant": pair.variant_name, "kind": "skipped",
            "classification": pair.classification, "reason": pair.reason,
        }
        for pair in skipped
    ] + [
        {
            "phase_run_id": case.phase_run_id, "variant": case.variant_name, "kind": "failed",
            "classification": None, "reason": case.error_detail,
        }
        for case in failed_cases
    ]
    exclusions.sort(key=lambda item: (item["phase_run_id"], item["variant"], item["kind"]))

    return {
        "version": REPORT_SCHEMA_VERSION,
        "experiment_run_ids": sorted(experiment_run_ids),
        "plan_sha256": plan_sha256,
        "interval_method": BOOTSTRAP_METHOD,
        "interval_version": BOOTSTRAP_VERSION,
        "interval_resamples": BOOTSTRAP_RESAMPLES,
        "correction_coverage": {
            "population_size": len(population),
            "dropped_duplicate_phase_run_ids": sorted(dropped_duplicates),
            "attempted": len(cases),
            "scored_attempts": len(complete_cases),
            "failed_attempts": len(failed_cases),
            "skipped": {
                classification: sum(1 for p in skipped if p.classification == classification)
                for classification in SKIP_CLASSIFICATIONS
            },
        },
        "exclusions": exclusions,
        "cost": {
            "estimated_usd": sum(estimated_costs) if estimated_costs else None,
            "actual_usd": total_actual_usd,
        },
        "segments": [
            {
                "segment_key": segment.segment_key,
                "baseline_model": segment.baseline_model,
                "baseline_prompt_sha256": segment.baseline_prompt_sha256,
                "ranking": list(segment.ranking),
                "variants": [
                    {
                        "variant_name": variant.variant_name,
                        "scored_case_count": variant.scored_case_count,
                        "failed_case_count": variant.failed_case_count,
                        "unscored_count": variant.unscored_count,
                        "no_op_count": variant.no_op_count,
                        "unpriceable_count": variant.unpriceable_count,
                        "common_case_count": variant.common_case_count,
                        "mean_delta": variant.mean_delta,
                        "interval_available": variant.interval_available,
                        "interval_seed": variant.interval_seed,
                        "ci_lower": variant.ci_lower,
                        "ci_upper": variant.ci_upper,
                    }
                    for variant in segment.variants
                ],
                "cases": [
                    {
                        "phase_run_id": case.phase_run_id,
                        "variant": case.variant_name,
                        "status": case.status,
                        "baseline_distance": case.baseline_distance,
                        "candidate_distance": case.candidate_distance,
                        "delta": case.delta,
                        "estimated_usd": case.estimated_usd,
                        "actual_usd": case.actual_usd,
                    }
                    for case in sorted(
                        (c for c in cases if c.segment_key == segment.segment_key),
                        key=lambda c: (c.phase_run_id, c.variant_name),
                    )
                ],
            }
            for segment in segments
        ],
    }


def render_json(report: dict[str, Any]) -> str:
    """Canonical JSON serialization: sorted keys, compact separators —
    stable byte-for-byte across identical report documents."""
    return _canonical_json(report)


def _format_usd(value: float | None) -> str:
    return "unavailable" if value is None else f"${value:.6f}"


def render_markdown(report: dict[str, Any]) -> str:
    """Render Markdown exclusively from an already-built report document."""
    coverage = report["correction_coverage"]
    skipped_counts = coverage["skipped"]
    lines = [
        "# Scout batch/sweep replay report",
        "",
        f"Schema: `{report['version']}`",
        f"Experiment runs: {', '.join(str(i) for i in report['experiment_run_ids'])}",
        f"Authorized plan: `{report['plan_sha256']}`",
        f"Interval method: `{report['interval_method']}/v{report['interval_version']}` "
        f"({report['interval_resamples']} resamples)",
        "",
        "## Correction coverage",
        "",
        f"- Population: {coverage['population_size']}",
        f"- Dropped duplicate baselines: {coverage['dropped_duplicate_phase_run_ids']}",
        f"- Attempted: {coverage['attempted']} "
        f"(scored {coverage['scored_attempts']}, failed {coverage['failed_attempts']})",
        f"- Skipped: unscored {skipped_counts['unscored']}, no-op {skipped_counts['no_op']}, "
        f"unpriceable {skipped_counts['unpriceable']}",
        "",
        "## Cost",
        "",
        f"- Estimated: {_format_usd(report['cost']['estimated_usd'])}",
        f"- Actual (all immutable attempts, including superseded retries): "
        f"{_format_usd(report['cost']['actual_usd'])}",
        "",
    ]
    if report["exclusions"]:
        lines.append("## Exclusions")
        lines.append("")
        for exclusion in report["exclusions"]:
            detail = (
                exclusion["classification"] if exclusion["kind"] == "skipped" else exclusion["kind"]
            )
            lines.append(
                f"- phase_run_id `{exclusion['phase_run_id']}` variant `{exclusion['variant']}` "
                f"({detail}): {exclusion['reason']}"
            )
        lines.append("")

    for segment in report["segments"]:
        lines.append(f"## Segment `{segment['segment_key']}`")
        lines.append("")
        lines.append(f"- Baseline model: `{segment['baseline_model']}`")
        lines.append(f"- Baseline prompt sha256: `{segment['baseline_prompt_sha256']}`")
        ranking_text = ", ".join(f"`{name}`" for name in segment["ranking"]) or "n/a"
        lines.append(
            "- Ranking by mean paired distance delta, ascending (more negative is closer to "
            f"the correction): {ranking_text}"
        )
        lines.append("")
        lines.append(
            "| variant | scored | failed | unscored | no-op | unpriceable | common "
            "| mean delta | 95% CI | seed |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for variant in segment["variants"]:
            mean_delta = "n/a" if variant["mean_delta"] is None else f"{variant['mean_delta']:.4f}"
            ci = (
                f"[{variant['ci_lower']:.4f}, {variant['ci_upper']:.4f}]"
                if variant["interval_available"]
                else "unavailable (< 2 paired cases)"
            )
            seed = variant["interval_seed"] if variant["interval_seed"] is not None else "n/a"
            lines.append(
                f"| `{variant['variant_name']}` | {variant['scored_case_count']} | "
                f"{variant['failed_case_count']} | {variant['unscored_count']} | "
                f"{variant['no_op_count']} | {variant['unpriceable_count']} | "
                f"{variant['common_case_count']} | {mean_delta} | {ci} | {seed} |"
            )
        lines.append("")
        if segment["cases"]:
            lines.append("| phase_run_id | variant | status | baseline dist | candidate dist "
                         "| delta | est. USD | actual USD |")
            lines.append("|---|---|---|---|---|---|---|---|")
            for case in segment["cases"]:
                baseline_dist = "n/a" if case["baseline_distance"] is None else (
                    f"{case['baseline_distance']:.4f}"
                )
                candidate_dist = "n/a" if case["candidate_distance"] is None else (
                    f"{case['candidate_distance']:.4f}"
                )
                delta = "n/a" if case["delta"] is None else f"{case['delta']:.4f}"
                est = _format_usd(case["estimated_usd"])
                actual = _format_usd(case["actual_usd"])
                lines.append(
                    f"| `{case['phase_run_id']}` | `{case['variant']}` | {case['status']} | "
                    f"{baseline_dist} | {candidate_dist} | {delta} | {est} | {actual} |"
                )
            lines.append("")
    return "\n".join(lines)


__all__ = [
    "BOOTSTRAP_CI_LOWER_QUANTILE",
    "BOOTSTRAP_CI_UPPER_QUANTILE",
    "BOOTSTRAP_METHOD",
    "BOOTSTRAP_MIN_PAIRED_CASES",
    "BOOTSTRAP_RESAMPLES",
    "BOOTSTRAP_VERSION",
    "REPORTABLE_ATTEMPT_STATUSES",
    "REPORT_SCHEMA_VERSION",
    "SKIP_CLASSIFICATIONS",
    "ReportCase",
    "ReportError",
    "SegmentReport",
    "SkippedPair",
    "VariantSegmentSummary",
    "build_batch_report",
    "render_json",
    "render_markdown",
]
