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
each segment, each variant's mean paired distance delta on the common
successfully-scored case intersection, each variant's own full coverage
(scored, failed, and skipped by classification), and a deterministic 95%
paired-bootstrap interval (unavailable below BOOTSTRAP_MIN_PAIRED_CASES
paired cases, where a percentile bootstrap is little more than the range
of the points).

The interval decides what gets ordered. A segment's `ranking` holds only
the variants whose interval excludes zero, ascending by mean delta; every
other variant with a mean — interval unavailable, or straddling zero — is
listed unordered under `indistinguishable_from_baseline`. The number of
intervals in the segment (`interval_family_size`) is recorded so the
unadjusted per-interval level is auditable; no multiple-comparison
correction is applied.

An abstain assembles to empty text and always scores the maximum
distance, so it is indistinguishable from a maximally wrong draft by
distance alone. A case where either side abstained (per the retained
score evidence) is kept out of the distance population and counted per
variant as its own outcome class; evidence written before the abstain
flags existed is treated as unknown and stays in the population.

A plan may run every scored pair more than once (`repeats`); each repeat
is its own attempt row and retry chain. The paired bootstrap resamples
cases, not attempts: a case's delta is the mean over its repeats that are
in the distance population, and the mean within-case range (max − min
over a case's repeat deltas) is reported beside the mean so run-to-run
variance is visible rather than attributed to the variant. With one
repeat the range is unavailable and every count below reduces to the
single-attempt reading.

Relevance reports (a separate document, RELEVANCE_REPORT_SCHEMA_VERSION)
carry, beside symmetric accuracy, each side's precision and recall and
the segment's majority-class reference — the accuracy of always
predicting the more common label on the common case set — because an
accuracy figure with no trivial reference beside it is uninterpretable
on an imbalanced population.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from scout.replay.experiments import (
    DEFAULT_BATCH_VARIANT_NAME,
    RELEVANCE_BATCH_CANDIDATE_CONFIG_VERSION,
    SUPPORTED_BATCH_CANDIDATE_CONFIG_VERSIONS,
    latest_attempts_by_case,
)
from scout.replay.tasks import RelevanceScore, RelevanceTask
from scout.storage.evaluations import Experiment
from scout.storage.experiment_plan import expected_experiment_pairs
from scout.storage.state import StateManager

REPORT_SCHEMA_VERSION = 7
RELEVANCE_REPORT_SCHEMA_VERSION = 6
BOOTSTRAP_METHOD = "paired_bootstrap_percentile"
BOOTSTRAP_VERSION = 1
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_MIN_PAIRED_CASES = 10
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
        if config.get("version") not in SUPPORTED_BATCH_CANDIDATE_CONFIG_VERSIONS:
            raise ReportError(
                f"experiment_run {experiment_run_id} is not a batch/sweep parent "
                f"(candidate_config version {config.get('version')!r}; expected one of "
                f"{SUPPORTED_BATCH_CANDIDATE_CONFIG_VERSIONS})"
            )
        try:
            expected = expected_experiment_pairs(config)
        except ValueError as exc:
            raise ReportError(f"invalid experiment plan: {exc}") from exc
        actual = {
            (row["phase_run_id"], row["repeat_index"])
            for row in state.list_experiment_attempts(experiment_run_id)
        }
        if expected is None or not actual <= expected:
            raise ReportError(f"experiment_run {experiment_run_id} has out-of-plan attempts")
        if run["status"] in ("complete", "partial", "failed") and actual != expected:
            raise ReportError(f"experiment_run {experiment_run_id} has an incomplete terminal plan")
        parents.append({"experiment_run_id": experiment_run_id, **config})

    plan_hashes = {parent["plan_sha256"] for parent in parents}
    if len(plan_hashes) > 1:
        raise ReportError(
            f"experiment_run_ids {sorted(experiment_run_ids)} do not share one authorized plan "
            f"-- found {len(plan_hashes)} distinct plan_sha256 values: {sorted(plan_hashes)}"
        )
    return parents


@dataclass(frozen=True, slots=True)
class RelevanceReportCase:
    phase_run_id: int
    repeat_index: int
    variant: str
    baseline_model: str
    baseline_prompt_sha256: str
    status: str
    score: RelevanceScore | None
    actual_usd: float | None


def _relevance_confusion(scores: list[RelevanceScore], *, candidate: bool) -> dict[str, float]:
    """Each case contributes total weight one, divided across successful repeats."""
    repeats = Counter(score.target.evaluation_id for score in scores)
    counts = dict.fromkeys(
        ("true_positive", "true_negative", "false_positive", "false_negative"), 0.0,
    )
    for score in scores:
        prediction = score.candidate_relevant if candidate else score.baseline_relevant
        key = ("true" if prediction == score.target.is_relevant else "false") + (
            "_positive" if prediction else "_negative"
        )
        counts[key] += 1 / repeats[score.target.evaluation_id]
    return counts


def _ratio(numerator: float, denominator: float) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _precision(confusion: dict[str, float]) -> float | None:
    """Share of predicted-relevant cases that were relevant; None when
    nothing was predicted relevant."""
    return _ratio(
        confusion["true_positive"], confusion["true_positive"] + confusion["false_positive"]
    )


def _recall(confusion: dict[str, float]) -> float | None:
    """Share of relevant cases that were predicted relevant; None when no
    case was relevant."""
    return _ratio(
        confusion["true_positive"], confusion["true_positive"] + confusion["false_negative"]
    )


def _delta(candidate: float | None, baseline: float | None) -> float | None:
    if candidate is None or baseline is None:
        return None
    return candidate - baseline


def _one_score_per_case(scores: Sequence[RelevanceScore]) -> list[RelevanceScore]:
    """Repeats share a target; keep the first observation of each case so
    case-level references are not weighted by repeat count."""
    seen: set[int] = set()
    unique = []
    for score in scores:
        if score.target.evaluation_id not in seen:
            seen.add(score.target.evaluation_id)
            unique.append(score)
    return unique


def _majority_class_reference(scores: Sequence[RelevanceScore]) -> dict[str, Any]:
    """The trivial reference every variant's accuracy has to beat: always
    predict the more common label on the common case set. Ties report the
    relevant label."""
    relevant_count = sum(1 for score in scores if score.target.is_relevant)
    majority_relevant = relevant_count * 2 >= len(scores)
    majority_count = relevant_count if majority_relevant else len(scores) - relevant_count
    return {
        "common_case_count": len(scores),
        "relevant_case_count": relevant_count,
        "majority_label_relevant": majority_relevant if scores else None,
        "majority_class_accuracy": _ratio(majority_count, len(scores)),
    }


def _build_relevance_report(state: StateManager, parents: list[dict[str, Any]]) -> dict[str, Any]:
    task = RelevanceTask.model_validate(parents[0]["task"])
    if any(
        parent.get("phase") != "relevance" or parent.get("task") != task.model_dump(mode="json")
        for parent in parents
    ):
        raise ReportError("Relevance report parents do not share a task/population")
    cases: list[RelevanceReportCase] = []
    for parent in parents:
        latest = latest_attempts_by_case([
            Experiment(**row)
            for row in state.list_experiment_attempts(parent["experiment_run_id"])
        ])
        for (phase_run_id, repeat_index), attempt in sorted(latest.items()):
            if attempt.status not in REPORTABLE_ATTEMPT_STATUSES:
                continue
            evidence = json.loads(attempt.baseline_evidence)
            score = None
            if attempt.status == "complete":
                comparison = state.get_trace_comparison(attempt.id)
                if comparison is None or comparison["score_evidence"] is None:
                    raise ReportError("Completed relevance attempt lacks score evidence")
                score = RelevanceScore.model_validate_json(comparison["score_evidence"])
                if (
                    score.target.model_dump(mode="json") != evidence["target"]
                    or score.target.task != task
                ):
                    raise ReportError("Relevance score differs from pinned target")
            cases.append(
                RelevanceReportCase(
                    phase_run_id=phase_run_id,
                    repeat_index=repeat_index,
                    variant=parent["variant_name"],
                    baseline_model=evidence["baseline_model"],
                    baseline_prompt_sha256=evidence["baseline_prompt_sha256"],
                    status=attempt.status,
                    score=score,
                    actual_usd=attempt.candidate_cost,
                )
            )
    skipped_pairs = [
        {"variant": parent["variant_name"], **pair}
        for parent in parents for pair in parent["skipped_pairs"]
    ]
    if not cases and not skipped_pairs and not any(
        state.list_experiment_attempts(parent["experiment_run_id"]) for parent in parents
    ):
        raise ReportError(
            "no reportable evidence (attempts or skipped pairs) under the given experiment_run_ids"
        )
    variants = sorted(parent["variant_name"] for parent in parents)
    repeats_by_variant = {parent["variant_name"]: parent.get("repeats", 1) for parent in parents}
    segment_keys = sorted({(case.baseline_model, case.baseline_prompt_sha256) for case in cases})
    segments = []
    for model, prompt in segment_keys:
        members = [
            case
            for case in sorted(cases, key=lambda item: (item.phase_run_id, item.variant))
            if (case.baseline_model, case.baseline_prompt_sha256) == (model, prompt)
        ]
        by_variant = {
            variant: [
                case for case in members if case.variant == variant and case.score is not None
            ]
            for variant in variants
        }
        common = set.intersection(
            *[{case.phase_run_id for case in values} for values in by_variant.values()]
        )
        summaries = []
        common_scores: list[RelevanceScore] = []
        for variant, values in by_variant.items():
            # Average successful repeats within each case before comparing
            # cases; missing draws must not reweight the baseline population.
            scores = [
                case.score
                for case in values
                if case.phase_run_id in common and case.score is not None
            ]
            common_scores = common_scores or scores
            by_case: dict[int, set[bool]] = defaultdict(set)
            for case in values:
                if case.phase_run_id in common and case.score is not None:
                    by_case[case.phase_run_id].add(case.score.candidate_relevant)
            baseline_confusion = _relevance_confusion(scores, candidate=False)
            candidate_confusion = _relevance_confusion(scores, candidate=True)
            baseline_precision = _precision(baseline_confusion)
            candidate_precision = _precision(candidate_confusion)
            baseline_recall = _recall(baseline_confusion)
            candidate_recall = _recall(candidate_confusion)
            baseline_accuracy = _ratio(
                baseline_confusion["true_positive"] + baseline_confusion["true_negative"],
                len(by_case),
            )
            candidate_accuracy = _ratio(
                candidate_confusion["true_positive"] + candidate_confusion["true_negative"],
                len(by_case),
            )
            summaries.append(
                {
                    "variant": variant,
                    "repeat_count": repeats_by_variant.get(variant, 1),
                    "scored_case_count": len({case.phase_run_id for case in values}),
                    "scored_attempt_count": len(values),
                    "common_case_count": len(by_case),
                    "common_attempt_count": len(scores),
                    # Cases whose repeats did not agree on the candidate's label.
                    "unstable_case_count": sum(1 for labels in by_case.values() if len(labels) > 1),
                    "baseline_confusion": baseline_confusion,
                    "candidate_confusion": candidate_confusion,
                    "baseline_accuracy": baseline_accuracy,
                    "candidate_accuracy": candidate_accuracy,
                    "accuracy_delta": _delta(candidate_accuracy, baseline_accuracy),
                    "baseline_precision": baseline_precision,
                    "candidate_precision": candidate_precision,
                    "precision_delta": _delta(candidate_precision, baseline_precision),
                    "baseline_recall": baseline_recall,
                    "candidate_recall": candidate_recall,
                    "recall_delta": _delta(candidate_recall, baseline_recall),
                }
            )
        segments.append(
            {
                "baseline_model": model,
                "baseline_prompt_sha256": prompt,
                # The common set is shared by every variant in the segment,
                # so one reference applies to all of them.
                "reference": _majority_class_reference(_one_score_per_case(common_scores)),
                "variants": summaries,
            }
        )
    return {
        "version": RELEVANCE_REPORT_SCHEMA_VERSION,
        "repeat_weighting": "equal_case_mean_of_successful_repeats",
        "task": task.model_dump(mode="json"),
        "experiment_run_ids": sorted(parent["experiment_run_id"] for parent in parents),
        "plan_sha256": parents[0]["plan_sha256"],
        "population_phase_run_ids": parents[0]["phase_run_ids"],
        "source_exclusions": parents[0]["source_exclusions"],
        "skipped_pairs": skipped_pairs,
        "segments": segments,
        "cases": [
            {
                "phase_run_id": case.phase_run_id,
                "repeat_index": case.repeat_index,
                "variant": case.variant,
                "status": case.status,
                "score": case.score.model_dump(mode="json") if case.score else None,
                "actual_usd": case.actual_usd,
            }
            for case in cases
        ],
        "cost": {
            "task": "relevance",
            "actual_usd": _total_actual_cost_including_superseded(
                state, [parent["experiment_run_id"] for parent in parents]
            ),
        },
        "interpretation": (
            "Accuracy, precision, and recall on the selected labeled corpus only. Ranked "
            "discovery yield and random-slice population rates are separate review reports. "
            "Variant comparisons use common successful cases within each baseline "
            "model/prompt segment; each segment's majority-class reference is the accuracy "
            "of always predicting its more common label on that common set, which any "
            "variant must beat before its accuracy means anything. A false positive (a reply "
            "to an irrelevant post) and a false negative (a missed relevant post) are "
            "reported separately because they do not cost the same."
        ),
    }


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
    repeat_index: int
    repeat_count: int
    baseline_distance: float | None
    candidate_distance: float | None
    delta: float | None
    baseline_abstained: bool | None
    candidate_abstained: bool | None
    estimated_usd: float | None
    actual_usd: float | None
    error_detail: str | None

    @property
    def in_distance_population(self) -> bool:
        """A scored case contributes a paired distance delta unless the
        retained evidence says either side abstained. Evidence without
        the abstain flags (written before they existed) is unknown and
        stays in the population."""
        return (
            self.status == "complete"
            and self.delta is not None
            and not self.baseline_abstained
            and not self.candidate_abstained
        )


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
    # The parent's planned repeats, so a variant whose every pair was
    # skipped still reports the repeat count its plan authorized.
    repeat_count: int


def _collect_attempted_cases(
    state: StateManager, parents: list[dict[str, Any]],
) -> list[ReportCase]:
    cases: list[ReportCase] = []
    for parent in parents:
        experiment_run_id = parent["experiment_run_id"]
        variant_name = parent.get("variant_name", DEFAULT_BATCH_VARIANT_NAME)
        repeat_count = parent.get("repeats", 1)

        latest_by_case = latest_attempts_by_case([
            Experiment(**row) for row in state.list_experiment_attempts(experiment_run_id)
        ])

        for attempt in latest_by_case.values():
            if attempt.status not in REPORTABLE_ATTEMPT_STATUSES:
                continue
            evidence = json.loads(attempt.baseline_evidence)
            segment_key = _segment_key(
                evidence["baseline_model"], evidence["baseline_prompt_sha256"],
            )
            baseline_distance = candidate_distance = delta = None
            baseline_abstained = candidate_abstained = None
            if attempt.status == "complete":
                comparison = state.get_trace_comparison(attempt.id)
                if comparison is not None and comparison["score_evidence"] is not None:
                    score = json.loads(comparison["score_evidence"])
                    baseline_distance = score["baseline_distance"]
                    candidate_distance = score["candidate_distance"]
                    delta = score["delta"]
                    baseline_abstained = score.get("baseline_abstained")
                    candidate_abstained = score.get("candidate_abstained")
            cases.append(
                ReportCase(
                    phase_run_id=attempt.phase_run_id,
                    variant_name=variant_name,
                    segment_key=segment_key,
                    baseline_model=evidence["baseline_model"],
                    baseline_prompt_sha256=evidence["baseline_prompt_sha256"],
                    status=attempt.status,
                    repeat_index=attempt.repeat_index,
                    repeat_count=repeat_count,
                    baseline_distance=baseline_distance,
                    candidate_distance=candidate_distance,
                    delta=delta,
                    baseline_abstained=baseline_abstained,
                    candidate_abstained=candidate_abstained,
                    estimated_usd=evidence.get("estimated_usd"),
                    actual_usd=attempt.candidate_cost,
                    error_detail=attempt.error_detail,
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
                    reason=raw["reason"], repeat_count=parent.get("repeats", 1),
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
    baseline_abstain_count: int
    candidate_abstain_count: int
    common_case_count: int
    mean_delta: float | None
    interval_available: bool
    interval_seed: int | None
    ci_lower: float | None
    ci_upper: float | None
    interval_excludes_zero: bool | None
    repeat_count: int = 1
    scored_attempt_count: int = 0
    failed_attempt_count: int = 0
    # Mean over common cases with >1 repeat delta of (max − min); None
    # when no case has more than one.
    within_case_range_mean: float | None = None


def _ranking_key(summary: VariantSegmentSummary) -> float:
    assert summary.mean_delta is not None
    return summary.mean_delta


def _interval_excludes_zero(ci_lower: float | None, ci_upper: float | None) -> bool | None:
    if ci_lower is None or ci_upper is None:
        return None
    return ci_lower > 0.0 or ci_upper < 0.0


def _partition_by_interval(
    summaries: Sequence[VariantSegmentSummary],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split the variants that have a mean into (ranking, indistinguishable):
    the ranking is ascending by mean delta over variants whose interval
    excludes zero; everything else — interval unavailable or containing
    zero — is an unordered set, returned sorted by name only for
    determinism."""
    with_mean = [s for s in summaries if s.mean_delta is not None]
    ranked = sorted((s for s in with_mean if s.interval_excludes_zero), key=_ranking_key)
    indistinguishable = sorted(
        (s for s in with_mean if not s.interval_excludes_zero), key=lambda s: s.variant_name,
    )
    return (
        tuple(s.variant_name for s in ranked),
        tuple(s.variant_name for s in indistinguishable),
    )


@dataclass(frozen=True, slots=True)
class SegmentReport:
    segment_key: str
    baseline_model: str
    baseline_prompt_sha256: str
    variants: tuple[VariantSegmentSummary, ...]
    ranking: tuple[str, ...]
    indistinguishable_from_baseline: tuple[str, ...]
    interval_family_size: int


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

        # The paired distance population: cases every variant scored and
        # on which nobody abstained, so one variant's abstain removes the
        # case from every variant's comparison set alike.
        distance_ids_by_variant = {
            variant: {
                c.phase_run_id
                for c in by_variant_cases.get(variant, [])
                if c.in_distance_population
            }
            for variant in variant_names
        }
        common_ids: set[int] = (
            set.intersection(*distance_ids_by_variant.values())
            if distance_ids_by_variant
            else set()
        )

        summaries = []
        for variant_name in sorted(variant_names):
            variant_cases = by_variant_cases.get(variant_name, [])
            variant_skipped = by_variant_skipped.get(variant_name, [])
            scored = [c for c in variant_cases if c.status == "complete"]
            failed = [c for c in variant_cases if c.status == "failed"]
            # One paired delta per common case: the mean over its repeats
            # that are in the distance population, in phase_run_id order so
            # the seeded bootstrap sees the same sequence every render.
            deltas_by_case: dict[int, list[float]] = defaultdict(list)
            for c in scored:
                if c.phase_run_id in common_ids and c.in_distance_population:
                    assert c.delta is not None
                    deltas_by_case[c.phase_run_id].append(c.delta)
            common_deltas = [
                sum(values) / len(values) for _, values in sorted(deltas_by_case.items())
            ]
            ranges = [max(v) - min(v) for v in deltas_by_case.values() if len(v) > 1]
            baseline_abstains = sum(1 for c in scored if c.baseline_abstained)
            candidate_abstains = sum(1 for c in scored if c.candidate_abstained)
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
                    scored_case_count=len({c.phase_run_id for c in scored}),
                    failed_case_count=len({c.phase_run_id for c in failed}),
                    scored_attempt_count=len(scored),
                    failed_attempt_count=len(failed),
                    repeat_count=max(
                        [c.repeat_count for c in variant_cases]
                        + [s.repeat_count for s in variant_skipped],
                        default=1,
                    ),
                    within_case_range_mean=sum(ranges) / len(ranges) if ranges else None,
                    unscored_count=skip_counts.get("unscored", 0),
                    no_op_count=skip_counts.get("no_op", 0),
                    unpriceable_count=skip_counts.get("unpriceable", 0),
                    baseline_abstain_count=baseline_abstains,
                    candidate_abstain_count=candidate_abstains,
                    common_case_count=len(common_deltas),
                    mean_delta=mean_delta,
                    interval_available=interval_available,
                    interval_seed=interval_seed,
                    ci_lower=ci_lower,
                    ci_upper=ci_upper,
                    interval_excludes_zero=_interval_excludes_zero(ci_lower, ci_upper),
                )
            )

        ranking, indistinguishable = _partition_by_interval(summaries)
        baseline_model, baseline_prompt_sha256 = segment_key.split("|", 1)
        segments.append(
            SegmentReport(
                segment_key=segment_key, baseline_model=baseline_model,
                baseline_prompt_sha256=baseline_prompt_sha256,
                variants=tuple(summaries), ranking=ranking,
                indistinguishable_from_baseline=indistinguishable,
                interval_family_size=sum(1 for s in summaries if s.interval_available),
            )
        )
    return segments


def build_batch_report(state: StateManager, *, experiment_run_ids: Sequence[int]) -> dict[str, Any]:
    """Build scores and completion evidence from one consistent database snapshot."""
    with state.db.read_transaction():
        report = _build_batch_report(state, experiment_run_ids=experiment_run_ids)
        runs = []
        for run_id in experiment_run_ids:
            run = state.get_experiment_run(run_id)
            assert run is not None
            expected = expected_experiment_pairs(json.loads(run["candidate_config"]))
            assert expected is not None
            latest = latest_attempts_by_case([
                Experiment(**row) for row in state.list_experiment_attempts(run_id)
            ])
            counts = Counter(attempt.status for attempt in latest.values())
            runs.append({
                "experiment_run_id": run_id, "status": run["status"],
                "planned_chain_count": len(expected), "created_chain_count": len(latest),
                "missing_chain_count": len(expected - latest.keys()),
                "status_counts": dict(counts),
            })
        report["runs"] = runs
        report["cost"]["unknown_cost_attempt_count"] = sum(
            attempt["status"] != "queued" and attempt["candidate_cost"] is None
            and attempt["candidate_llm_call_count"] != 0
            for run_id in experiment_run_ids for attempt in state.list_experiment_attempts(run_id)
        )
        report["cost"]["actual_usd_is_complete"] = (
            report["cost"]["unknown_cost_attempt_count"] == 0
        )
        report["provisional"] = any(run["status"] in ("queued", "running") for run in runs)
        report["status"] = (
            "in_progress" if report["provisional"] else
            "complete" if all(run["status"] == "complete" for run in runs) else "partial"
        )
        if report["provisional"]:
            for segment in report["segments"]:
                if "ranking" in segment:
                    segment["ranking"] = []
        return report


def _build_batch_report(
    state: StateManager, *, experiment_run_ids: Sequence[int],
) -> dict[str, Any]:
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
    if any(parent["version"] == RELEVANCE_BATCH_CANDIDATE_CONFIG_VERSION for parent in parents):
        if any(parent["version"] != RELEVANCE_BATCH_CANDIDATE_CONFIG_VERSION for parent in parents):
            raise ReportError("Cannot combine drafting and relevance tasks in one report")
        try:
            return _build_relevance_report(state, parents)
        except (ValueError, KeyError, TypeError) as exc:
            raise ReportError("Invalid retained relevance report evidence") from exc
    plan_sha256 = parents[0]["plan_sha256"]
    population = tuple(parents[0].get("phase_run_ids", ()))
    dropped_duplicates = tuple(parents[0].get("dropped_duplicate_phase_run_ids", ()))

    cases = _collect_attempted_cases(state, parents)
    skipped = _collect_skipped_pairs(parents)
    if not cases and not skipped and not any(
        state.list_experiment_attempts(parent["experiment_run_id"]) for parent in parents
    ):
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
            "repeat_index": None, "classification": pair.classification, "reason": pair.reason,
        }
        for pair in skipped
    ] + [
        {
            "phase_run_id": case.phase_run_id, "variant": case.variant_name, "kind": "failed",
            "repeat_index": case.repeat_index, "classification": None,
            "reason": case.error_detail,
        }
        for case in failed_cases
    ]
    # A skipped pair never ran, so it has no repeat; a failed attempt names
    # its repeat because each repeat of one case fails on its own.
    exclusions.sort(
        key=lambda item: (
            item["phase_run_id"], item["variant"], item["kind"], item["repeat_index"] or 0,
        )
    )

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
                "indistinguishable_from_baseline": list(segment.indistinguishable_from_baseline),
                "interval_family_size": segment.interval_family_size,
                "variants": [
                    {
                        "variant_name": variant.variant_name,
                        "scored_case_count": variant.scored_case_count,
                        "failed_case_count": variant.failed_case_count,
                        "unscored_count": variant.unscored_count,
                        "no_op_count": variant.no_op_count,
                        "unpriceable_count": variant.unpriceable_count,
                        "baseline_abstain_count": variant.baseline_abstain_count,
                        "candidate_abstain_count": variant.candidate_abstain_count,
                        "repeat_count": variant.repeat_count,
                        "scored_attempt_count": variant.scored_attempt_count,
                        "failed_attempt_count": variant.failed_attempt_count,
                        "within_case_range_mean": variant.within_case_range_mean,
                        "common_case_count": variant.common_case_count,
                        "mean_delta": variant.mean_delta,
                        "interval_available": variant.interval_available,
                        "interval_seed": variant.interval_seed,
                        "ci_lower": variant.ci_lower,
                        "ci_upper": variant.ci_upper,
                        "interval_excludes_zero": variant.interval_excludes_zero,
                    }
                    for variant in segment.variants
                ],
                "cases": [
                    {
                        "phase_run_id": case.phase_run_id,
                        "repeat_index": case.repeat_index,
                        "variant": case.variant_name,
                        "status": case.status,
                        "baseline_distance": case.baseline_distance,
                        "candidate_distance": case.candidate_distance,
                        "delta": case.delta,
                        "baseline_abstained": case.baseline_abstained,
                        "candidate_abstained": case.candidate_abstained,
                        "estimated_usd": case.estimated_usd,
                        "actual_usd": case.actual_usd,
                    }
                    for case in sorted(
                        (c for c in cases if c.segment_key == segment.segment_key),
                        key=lambda c: (c.phase_run_id, c.variant_name, c.repeat_index),
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


def _format_metric(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def _format_abstained(baseline: bool | None, candidate: bool | None) -> str:
    """Name each side that abstained, and each side whose flag is missing
    from the retained evidence, so an unknown state is never read as
    "no"."""
    if baseline is None and candidate is None:
        return "unknown"
    sides = (("baseline", baseline), ("candidate", candidate))
    abstained = [name for name, flag in sides if flag]
    unknown = [f"{name} unknown" for name, flag in sides if flag is None]
    return ", ".join((*abstained, *unknown)) or "no"


def _render_relevance_markdown(report: dict[str, Any]) -> str:
    task = report["task"]
    lines = [
        "# Scout relevance replay report",
        "",
        report["interpretation"],
        "",
        f"Schema: `{report['version']}`",
        f"Experiment runs: {', '.join(str(i) for i in report['experiment_run_ids'])}",
        f"Authorized plan: `{report['plan_sha256']}`",
        f"Snapshot: `{task['snapshot_digest']}`; partition: `{task['partition']}`",
        f"Partition digest: {task['partition_digest'] or 'n/a'}",
        "",
        "## Coverage",
        "",
        f"- Population: {len(report['population_phase_run_ids'])}",
        f"- Latest reportable attempts: {len(report['cases'])}",
        f"- Skipped pairs: {len(report['skipped_pairs'])}",
        f"- Source exclusions: {len(report['source_exclusions'])}",
        "",
        "## Cost",
        "",
        "- Actual (all immutable attempts, including superseded retries): "
        f"{_format_usd(report['cost']['actual_usd'])}",
        "",
    ]
    if report["source_exclusions"]:
        lines.extend(["## Source exclusions", ""])
        lines.extend(
            f"- evaluation `{item['evaluation_id']}`: {item['reason']}"
            for item in report["source_exclusions"]
        )
        lines.append("")
    if report["skipped_pairs"]:
        lines.extend(["## Skipped pairs", ""])
        lines.extend(
            f"- phase_run_id `{item['phase_run_id']}` variant `{item['variant']}` "
            f"({item['classification']}): {item['reason'] or 'n/a'}"
            for item in report["skipped_pairs"]
        )
        lines.append("")
    for segment in report["segments"]:
        reference = segment["reference"]
        majority_label = (
            "n/a"
            if reference["majority_label_relevant"] is None
            else ("relevant" if reference["majority_label_relevant"] else "not relevant")
        )
        lines.extend(
            [
                f"## {segment['baseline_model']} / {segment['baseline_prompt_sha256']}",
                "",
                f"Majority-class reference: accuracy "
                f"{_format_metric(reference['majority_class_accuracy'])} from always "
                f"predicting `{majority_label}` on {reference['common_case_count']} common "
                f"cases ({reference['relevant_case_count']} relevant). A variant's accuracy "
                "means nothing unless it beats this.",
                "",
                "| variant | repeats | scored cases | common cases | unstable cases "
                "| baseline accuracy | candidate accuracy | delta |",
                "|---|---|---|---|---|---|---|---|",
            ]
        )
        for variant in segment["variants"]:
            lines.append(
                f"| `{variant['variant']}` | {variant['repeat_count']} | "
                f"{variant['scored_case_count']} | {variant['common_case_count']} | "
                f"{variant['unstable_case_count']} | "
                f"{_format_metric(variant['baseline_accuracy'])} | "
                f"{_format_metric(variant['candidate_accuracy'])} | "
                f"{_format_metric(variant['accuracy_delta'])} |"
            )
        lines.extend(
            [
                "",
                "Precision, recall, and confusion counts on common successful attempts, "
                "every completed repeat one observation "
                "(FP = replied to an irrelevant post, FN = missed a relevant post):",
                "",
                "| variant | prediction | precision | recall | TP | FP | TN | FN |",
                "|---|---|---|---|---|---|---|---|",
            ]
        )
        for variant in segment["variants"]:
            for side in ("baseline", "candidate"):
                counts = variant[f"{side}_confusion"]
                lines.append(
                    f"| `{variant['variant']}` | {side} | "
                    f"{_format_metric(variant[f'{side}_precision'])} | "
                    f"{_format_metric(variant[f'{side}_recall'])} | "
                    f"{counts['true_positive']} | {counts['false_positive']} | "
                    f"{counts['true_negative']} | {counts['false_negative']} |"
                )
        lines.append("")
    return "\n".join(lines) + "\n"


def render_markdown(report: dict[str, Any]) -> str:
    """Render Markdown exclusively from an already-built report document."""
    rendered = _render_markdown(report)
    if "status" not in report:
        return rendered
    label = "PROVISIONAL — execution is incomplete" if report["provisional"] else report["status"]
    coverage = "\n".join(
        f"- Run {run['experiment_run_id']}: {run['created_chain_count']}/"
        f"{run['planned_chain_count']} planned chains created; {run['status_counts']}"
        for run in report["runs"]
    )
    if not report["cost"]["actual_usd_is_complete"]:
        coverage += (
            f"\n- Cost is a known subtotal; {report['cost']['unknown_cost_attempt_count']} "
            "attempts have unknown cost."
        )
    return rendered.replace("\n", f"\n\nStatus: **{label}**\n\n{coverage}\n", 1)


def _render_markdown(report: dict[str, Any]) -> str:
    if "task" in report:  # relevance reports carry their task document; batch reports never do
        return _render_relevance_markdown(report)
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
            repeat = exclusion.get("repeat_index")
            repeat_label = f" repeat {repeat}" if repeat is not None else ""
            lines.append(
                f"- phase_run_id `{exclusion['phase_run_id']}` variant `{exclusion['variant']}`"
                f"{repeat_label} ({detail}): {exclusion['reason']}"
            )
        lines.append("")

    for segment in report["segments"]:
        lines.append(f"## Segment `{segment['segment_key']}`")
        lines.append("")
        lines.append(f"- Baseline model: `{segment['baseline_model']}`")
        lines.append(f"- Baseline prompt sha256: `{segment['baseline_prompt_sha256']}`")
        ranking_text = ", ".join(f"`{name}`" for name in segment["ranking"]) or "none"
        indistinguishable_text = (
            ", ".join(f"`{name}`" for name in segment["indistinguishable_from_baseline"])
            or "none"
        )
        lines.append(
            "- Ranking by mean paired distance delta, ascending (more negative is closer to "
            f"the correction), variants whose 95% interval excludes zero only: {ranking_text}"
        )
        lines.append(
            "- Indistinguishable from baseline (interval unavailable or containing zero; "
            f"unordered): {indistinguishable_text}"
        )
        lines.append(
            f"- Intervals in this segment: {segment['interval_family_size']} "
            "(each at the unadjusted 95% level; no multiple-comparison correction)"
        )
        lines.append(
            "- Abstains are their own outcome: a case where either side abstained is "
            "counted below and left out of the common distance set"
        )
        lines.append(
            "- Repeats: a case's delta is the mean over its repeats; within-case range is "
            "the mean (max − min) over cases with more than one repeat delta"
        )
        lines.append("")
        lines.append(
            "| variant | repeats | scored cases (attempts) | failed cases (attempts) "
            "| unscored | no-op | unpriceable "
            "| abstains (baseline / candidate) | common "
            "| mean delta | within-case range | 95% CI | excludes zero | seed |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for variant in segment["variants"]:
            mean_delta = "n/a" if variant["mean_delta"] is None else f"{variant['mean_delta']:.4f}"
            ci = (
                f"[{variant['ci_lower']:.4f}, {variant['ci_upper']:.4f}]"
                if variant["interval_available"]
                else f"unavailable (< {BOOTSTRAP_MIN_PAIRED_CASES} paired cases)"
            )
            excludes_zero = (
                "n/a"
                if variant["interval_excludes_zero"] is None
                else ("yes" if variant["interval_excludes_zero"] else "no")
            )
            seed = variant["interval_seed"] if variant["interval_seed"] is not None else "n/a"
            within = (
                "n/a"
                if variant["within_case_range_mean"] is None
                else f"{variant['within_case_range_mean']:.4f}"
            )
            lines.append(
                f"| `{variant['variant_name']}` | {variant['repeat_count']} | "
                f"{variant['scored_case_count']} ({variant['scored_attempt_count']}) | "
                f"{variant['failed_case_count']} ({variant['failed_attempt_count']}) | "
                f"{variant['unscored_count']} | "
                f"{variant['no_op_count']} | {variant['unpriceable_count']} | "
                f"{variant['baseline_abstain_count']} / {variant['candidate_abstain_count']} | "
                f"{variant['common_case_count']} | {mean_delta} | {within} | {ci} | "
                f"{excludes_zero} | {seed} |"
            )
        lines.append("")
        if segment["cases"]:
            lines.append("| phase_run_id | repeat | variant | status | baseline dist "
                         "| candidate dist | delta | abstained | est. USD | actual USD |")
            lines.append("|---|---|---|---|---|---|---|---|---|---|")
            for case in segment["cases"]:
                baseline_dist = (
                    "n/a"
                    if case["baseline_distance"] is None
                    else (f"{case['baseline_distance']:.4f}")
                )
                candidate_dist = (
                    "n/a"
                    if case["candidate_distance"] is None
                    else (f"{case['candidate_distance']:.4f}")
                )
                delta = "n/a" if case["delta"] is None else f"{case['delta']:.4f}"
                abstained = _format_abstained(
                    case["baseline_abstained"], case["candidate_abstained"]
                )
                est = _format_usd(case["estimated_usd"])
                actual = _format_usd(case["actual_usd"])
                lines.append(
                    f"| `{case['phase_run_id']}` | {case['repeat_index']} | "
                    f"`{case['variant']}` | {case['status']} | "
                    f"{baseline_dist} | {candidate_dist} | {delta} | {abstained} | {est} | "
                    f"{actual} |"
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
    "RELEVANCE_REPORT_SCHEMA_VERSION",
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
