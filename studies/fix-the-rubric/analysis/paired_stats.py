"""Export the historical decisions and compute the scoped paired statistics."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import scipy
from scipy.stats import binomtest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SOURCE_BLOB = "264ef0f^:studies/validate-the-schema/data/decisions.csv"
HEADER = (
    "evaluation_id",
    "round",
    "human_action",
    "production_keep",
    "jev_action",
    "gemini_action",
)
ACTIONS = {"respond", "review", "drop"}
SEED = 20261021
DRAWS = 10_000

# Pin the historical arms. Round 4 jev_v5 is a populated development replay,
# not its JEV v4 result. Round 4 llm_v5 is entirely empty; the confirmed
# Gemini v4 arm uses llm_v4 so its published counts remain reproducible.
ARM_COLUMNS = {
    4: {"jev_action": "jev_v4", "gemini_action": "llm_v4"},
    5: {"jev_action": "jev_v5", "gemini_action": "llm_v5"},
}

# These ten published counts must hold before any statistics are calculated.
EXPECTED_COUNTS = {
    (5, "keep_drop", "production"): 64,
    (5, "keep_drop", "jev"): 77,
    (5, "keep_drop", "gemini"): 58,
    (5, "exact_action", "jev"): 71,
    (5, "exact_action", "gemini"): 52,
    (4, "keep_drop", "production"): 51,
    (4, "keep_drop", "jev"): 57,
    (4, "keep_drop", "gemini"): 62,
    (4, "exact_action", "jev"): 52,
    (4, "exact_action", "gemini"): 51,
}


@dataclass(frozen=True)
class Row:
    evaluation_id: int
    round: int
    human_action: str
    production_keep: bool
    jev_action: str
    gemini_action: str


def keep(action: str) -> bool:
    return action in {"respond", "review"}


def export_source() -> None:
    """Split only the six approved fields from the historical git blob."""
    result = subprocess.run(
        ["git", "show", SOURCE_BLOB],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    )
    source_rows = list(csv.DictReader(io.StringIO(result.stdout)))
    counts = {round_number: 0 for round_number in ARM_COLUMNS}
    ids: set[int] = set()
    exported: dict[int, list[dict[str, str | int]]] = {4: [], 5: []}
    if len(source_rows) != 190:
        raise ValueError(f"source blob has {len(source_rows)} rows; expected 190")
    for source in source_rows:
        raw_round = source["round"]
        if raw_round not in {"round4", "round5"}:
            raise ValueError(f"unexpected source round: {raw_round!r}")
        round_number = int(raw_round[-1])
        evaluation_id = int(source["evaluation_id"])
        if evaluation_id in ids:
            raise ValueError(f"duplicate evaluation_id: {evaluation_id}")
        ids.add(evaluation_id)
        counts[round_number] += 1
        if round_number == 4 and (not source["jev_v5"] or source["llm_v5"]):
            raise ValueError("round 4 source does not match the documented v5 replay shape")
        if round_number == 4 and not source["llm_v4"]:
            raise ValueError("round 4 Gemini v4 source llm_v4 is missing")
        columns = ARM_COLUMNS[round_number]
        exported[round_number].append(
            {
                "evaluation_id": evaluation_id,
                "round": round_number,
                "human_action": source["human_action"],
                "production_keep": "true" if source["production_keep"] == "1" else "false",
                "jev_action": source[columns["jev_action"]],
                "gemini_action": source[columns["gemini_action"]],
            }
        )
    if counts != {4: 90, 5: 100} or len(ids) != 190:
        raise ValueError(f"source round counts or IDs differ from expected: {counts}")
    data_dir = HERE / "data"
    data_dir.mkdir(exist_ok=True)
    for round_number, rows in exported.items():
        with (data_dir / f"round{round_number}.csv").open("w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=HEADER, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)


def read_round(round_number: int) -> list[Row]:
    path = HERE / "data" / f"round{round_number}.csv"
    with path.open(newline="") as file:
        reader = csv.DictReader(file)
        if tuple(reader.fieldnames or ()) != HEADER:
            raise ValueError(f"{path}: unexpected header; expected {','.join(HEADER)}")
        rows: list[Row] = []
        ids: set[int] = set()
        for record in reader:
            if None in record or any(value is None for value in record.values()):
                raise ValueError(f"{path}: malformed CSV row")
            evaluation_id = int(record["evaluation_id"])
            if evaluation_id in ids:
                raise ValueError(f"{path}: duplicate evaluation_id {evaluation_id}")
            ids.add(evaluation_id)
            if record["round"] != str(round_number):
                raise ValueError(f"{path}: wrong round for evaluation_id {evaluation_id}")
            if record["production_keep"] not in {"true", "false"}:
                raise ValueError(f"{path}: invalid production_keep for {evaluation_id}")
            action_fields = ("human_action", "jev_action", "gemini_action")
            if any(record[field] not in ACTIONS for field in action_fields):
                raise ValueError(f"{path}: invalid or missing action for {evaluation_id}")
            rows.append(
                Row(
                    evaluation_id=evaluation_id,
                    round=round_number,
                    human_action=record["human_action"],
                    production_keep=record["production_keep"] == "true",
                    jev_action=record["jev_action"],
                    gemini_action=record["gemini_action"],
                )
            )
    expected = 90 if round_number == 4 else 100
    if len(rows) != expected:
        raise ValueError(f"{path}: {len(rows)} rows; expected {expected}")
    return rows


def correct(row: Row, metric: str, arm: str) -> bool:
    human_keep = keep(row.human_action)
    if arm == "production":
        if metric != "keep_drop":
            raise ValueError("production has no exact-action metric")
        return row.production_keep == human_keep
    action = row.jev_action if arm == "jev" else row.gemini_action
    if metric == "keep_drop":
        return keep(action) == human_keep
    if metric == "exact_action":
        return action == row.human_action
    raise ValueError(f"unknown metric: {metric}")


def validate_published_counts(rounds: dict[int, list[Row]]) -> None:
    for (round_number, metric, arm), expected in EXPECTED_COUNTS.items():
        actual = sum(correct(row, metric, arm) for row in rounds[round_number])
        if actual != expected:
            raise ValueError(
                f"published count mismatch for round {round_number} {metric} {arm}: "
                f"got {actual}, expected {expected}; statistics were not generated"
            )


def wilson_interval(successes: int, total: int) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion."""
    z = 1.959963984540054
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    half_width = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return center - half_width / denominator, center + half_width / denominator


def exact_mcnemar_p(b: int, c: int) -> float:
    """Exact two-sided binomial McNemar p; isolated for a stdlib swap if needed."""
    discordant = b + c
    if discordant == 0:
        return 1.0
    return float(binomtest(b, discordant, p=0.5, alternative="two-sided").pvalue)


def bootstrap_difference(
    first: list[bool], second: list[bool], seed: int = SEED, draws: int = DRAWS
) -> tuple[float, float]:
    """Paired-post resampling with PCG64 and linear percentile interpolation."""
    if len(first) != len(second) or not first:
        raise ValueError("paired bootstrap needs two nonempty, equal-length vectors")
    differences = np.asarray(first, dtype=np.int8) - np.asarray(second, dtype=np.int8)
    generator = np.random.default_rng(seed)
    indices = generator.integers(0, len(first), size=(draws, len(first)))
    samples = differences[indices].mean(axis=1)
    limits = np.percentile(samples, [2.5, 97.5], method="linear")
    return float(limits[0]), float(limits[1])


def paired_result(rows: list[Row], metric: str, first: str, second: str) -> dict[str, Any]:
    first_correct = [correct(row, metric, first) for row in rows]
    second_correct = [correct(row, metric, second) for row in rows]
    a = sum(x and y for x, y in zip(first_correct, second_correct, strict=True))
    b = sum(x and not y for x, y in zip(first_correct, second_correct, strict=True))
    c = sum(not x and y for x, y in zip(first_correct, second_correct, strict=True))
    d = sum(not x and not y for x, y in zip(first_correct, second_correct, strict=True))
    lower, upper = bootstrap_difference(first_correct, second_correct)
    return {
        "n": len(rows),
        "a_both_correct": a,
        "b_first_only": b,
        "c_second_only": c,
        "d_both_wrong": d,
        "difference_points": round(100 * (b - c) / len(rows), 1),
        "bootstrap_95_ci_points": [round(100 * lower, 1), round(100 * upper, 1)],
        "exact_mcnemar_p": round(exact_mcnemar_p(b, c), 6),
    }


def build_payload(rounds: dict[int, list[Row]]) -> dict[str, Any]:
    marginals: dict[str, dict[str, dict[str, Any]]] = {}
    for (round_number, metric, arm), count in EXPECTED_COUNTS.items():
        round_key = f"round{round_number}"
        n = len(rounds[round_number])
        lower, upper = wilson_interval(count, n)
        marginals.setdefault(round_key, {}).setdefault(metric, {})[arm] = {
            "correct": count,
            "n": n,
            "percent": round(100 * count / n, 1),
            "wilson_95_ci_percent": [round(100 * lower, 1), round(100 * upper, 1)],
        }
    comparisons: dict[str, dict[str, dict[str, Any]]] = {}
    specs = (
        (5, "keep_drop", "jev_v5_vs_production", "jev", "production", "primary, pre-declared"),
        (5, "keep_drop", "jev_v5_vs_gemini_v5", "jev", "gemini", "exploratory"),
        (5, "exact_action", "jev_v5_vs_gemini_v5", "jev", "gemini", "exploratory"),
        (5, "keep_drop", "gemini_v5_vs_production", "gemini", "production", "exploratory"),
        (4, "keep_drop", "jev_v4_vs_production", "jev", "production", "exploratory"),
        (4, "keep_drop", "gemini_v4_vs_production", "gemini", "production", "exploratory"),
    )
    for round_number, metric, key, first, second, designation in specs:
        result = paired_result(rounds[round_number], metric, first, second)
        result["designation"] = designation
        comparisons.setdefault(f"round{round_number}", {}).setdefault(metric, {})[key] = result
    return {
        "bootstrap": {
            "generator": "numpy.random.default_rng (PCG64)",
            "seed": SEED,
            "draws": DRAWS,
            "percentiles": [2.5, 97.5],
            "interpolation": "linear",
        },
        "marginals": marginals,
        "comparisons": comparisons,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    marginals = payload["marginals"]
    comparisons = payload["comparisons"]
    lines = [
        "# Fix the rubric: agreement and paired comparisons",
        "",
        "Keep means respond or review. Percentages are agreement with the human action "
        "or keep/drop label.",
        "Production has no exact-action score because it has no review state.",
        "",
        "## Marginal agreement",
        "",
        "| Round | Metric | Arm | Correct | Agreement | 95% Wilson CI |",
        "| --- | --- | --- | ---: | ---: | ---: |",
    ]
    labels = {"jev": "JEV", "gemini": "Gemini", "production": "Production"}
    metric_labels = {"keep_drop": "keep/drop", "exact_action": "exact action"}
    for round_key, metrics in marginals.items():
        for metric, arms in metrics.items():
            for arm, value in arms.items():
                lower, upper = value["wilson_95_ci_percent"]
                lines.append(
                    f"| {round_key[-1]} | {metric_labels[metric]} | {labels[arm]} | "
                    f"{value['correct']}/{value['n']} | {value['percent']:.1f}% | "
                    f"{lower:.1f}% to {upper:.1f}% |"
                )
    primary = comparisons["round5"]["keep_drop"]["jev_v5_vs_production"]
    lines.extend(
        [
            "",
            "## Primary, pre-declared: round 5 JEV v5 vs production, keep/drop",
            "",
            "| JEV v5 correctness | Production correct | Production wrong |",
            "| --- | ---: | ---: |",
            f"| Correct | {primary['a_both_correct']} (a) | {primary['b_first_only']} (b) |",
            f"| Wrong | {primary['c_second_only']} (c) | {primary['d_both_wrong']} (d) |",
            "",
            f"Paired difference: {primary['difference_points']:+.1f} points "
            f"(95% bootstrap CI {primary['bootstrap_95_ci_points'][0]:+.1f} to "
            f"{primary['bootstrap_95_ci_points'][1]:+.1f}); exact two-sided McNemar "
            f"p = {primary['exact_mcnemar_p']:.4f} (b = {primary['b_first_only']}, "
            f"c = {primary['c_second_only']}).",
            "",
            "## Exploratory secondary comparisons",
            "",
            "| Round | Metric | First vs second | n | a | b | c | d | Difference (points) | "
            "95% bootstrap CI | Exact p |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |",
        ]
    )
    for round_key, metrics in comparisons.items():
        for metric, pairs in metrics.items():
            for key, result in pairs.items():
                if result["designation"] != "exploratory":
                    continue
                lower, upper = result["bootstrap_95_ci_points"]
                pair_label = (
                    key.replace("_vs_", " vs ")
                    .replace("_", " ")
                    .replace("jev", "JEV")
                    .replace("gemini", "Gemini")
                )
                lines.append(
                    f"| {round_key[-1]} | {metric_labels[metric]} | "
                    f"{pair_label} | {result['n']} | "
                    f"{result['a_both_correct']} | {result['b_first_only']} | "
                    f"{result['c_second_only']} | {result['d_both_wrong']} | "
                    f"{result['difference_points']:+.1f} | {lower:+.1f} to {upper:+.1f} | "
                    f"{result['exact_mcnemar_p']:.4f} |"
                )
    lines.extend(
        [
            "",
            "a = both correct; b = first correct only; c = second correct only; d = both wrong.",
            "The paired CI resamples posts with replacement (PCG64 seed 20261021, 10,000 draws,",
            "linear 2.5th and 97.5th percentiles). Round 4 is development data for v5.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-source", action="store_true", help="split the pinned git blob")
    args = parser.parse_args()
    try:
        if args.export_source:
            export_source()
            return
        rounds = {4: read_round(4), 5: read_round(5)}
        all_ids = [row.evaluation_id for rows in rounds.values() for row in rows]
        if len(set(all_ids)) != len(all_ids):
            raise ValueError("duplicate evaluation_id across rounds")
        validate_published_counts(rounds)
        payload = build_payload(rounds)
        result = {
            "numeric_payload": payload,
            "environment": {"numpy": np.__version__, "scipy": scipy.__version__},
        }
        (HERE / "results.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False, separators=(",", ": ")) + "\n"
        )
        (HERE / "results.md").write_text(render_markdown(payload))
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"error: {exc}\n")


if __name__ == "__main__":
    main()
