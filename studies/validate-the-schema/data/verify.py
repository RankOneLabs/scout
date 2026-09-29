"""Verify the public study bundle without private receipts or model calls."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, cast

from scout.typesafe.routes import route

ROOT = Path(__file__).resolve().parent


def read(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((ROOT / name).read_text()))


def human_action(label: dict[str, Any]) -> str:
    if label["exclusion"] != "none":
        return "drop"
    return {"in_post": "respond", "pointer": "review", "none": "drop"}[label["substance"]]


def verify() -> None:
    for name, digest in read("checksums.json").items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name
    provenance = read("provenance.json")
    policy = ROOT.parents[2] / provenance["route_source"]
    assert hashlib.sha256(policy.read_bytes()).hexdigest() == provenance["route_source_sha256"]
    results = read("results.json")["rounds"]
    with (ROOT / "decisions.csv").open(newline="") as stream:
        csv_rows = {(r["round"], int(r["evaluation_id"])): r for r in csv.DictReader(stream)}
    total = 0
    for name, expected in results.items():
        rows = read(f"{name}.json")["rows"]
        assert len(rows) == expected["scored"]
        assert dict(Counter(r["human"]["action"] for r in rows)) == expected["human_actions"]
        for row in rows:
            assert row["human"]["action"] == human_action(row["human"])
            for decision in row["arms"].values():
                actual = route(decision["feature_probabilities"])
                assert actual.action == decision["action"]
                assert actual.path_action == decision["path_action"]
                assert actual.line == decision["line"]
                assert list(actual.margin) == decision["margin"]
                assert actual.exclusion == decision["exclusion"]
            replay = dict(row["arms"]["jev:v5"]["feature_probabilities"])
            replay["excl_benchmark"] = row["benchmark_variant_probabilities"][
                "excl_benchmark__scores"
            ]
            assert replay == row["arms"]["jev:benchmark-replay"]["feature_probabilities"]
            flat = csv_rows[(name, row["evaluation_id"])]
            assert flat["project"] == row["project"]
            assert flat["human_action"] == row["human"]["action"]
            assert bool(int(flat["production_keep"])) == row["production_keep"]
            for column, arm in (
                ("jev_v4", "jev:v4"),
                ("jev_v5", "jev:v5"),
                ("llm_v4", "llm:v4"),
                ("llm_v5", "llm:v5"),
                ("benchmark_replay", "jev:benchmark-replay"),
            ):
                assert flat[column] == row["arms"].get(arm, {}).get("action", "")
            assert float(flat["benchmark_probability"]) == replay["excl_benchmark"]
        for arm, metrics in expected["arms"].items():
            if arm == "production":
                agree = sum(r["production_keep"] == (r["human"]["action"] != "drop") for r in rows)
                assert metrics["keep_or_drop"] == {"agree": agree, "n": len(rows)}
                continue
            confusion = Counter((r["human"]["action"], r["arms"][arm]["action"]) for r in rows)
            assert metrics["action_confusion"] == {
                f"{h}->{a}": n for (h, a), n in sorted(confusion.items())
            }
            exact = sum(n for (h, a), n in confusion.items() if h == a)
            keep = sum(n for (h, a), n in confusion.items() if (h != "drop") == (a != "drop"))
            assert metrics["final_action"] == {"agree": exact, "n": len(rows)}
            assert metrics["keep_or_drop"] == {"agree": keep, "n": len(rows)}
            for project, counts in metrics["by_project"].items():
                subset = [r for r in rows if r["project"] == project]
                assert counts == {
                    "n": len(subset),
                    "final_action_agree": sum(
                        r["human"]["action"] == r["arms"][arm]["action"] for r in subset
                    ),
                    "keep_or_drop_agree": sum(
                        (r["human"]["action"] != "drop") == (r["arms"][arm]["action"] != "drop")
                        for r in subset
                    ),
                }
        changes = [
            r
            for r in rows
            if r["arms"]["jev:v5"]["action"] != r["arms"]["jev:benchmark-replay"]["action"]
        ]
        assert len(changes) == expected["benchmark_changed_decisions"]
        assert (
            sum(
                r["human"]["action"] == r["arms"]["jev:benchmark-replay"]["action"] for r in changes
            )
            == expected["benchmark_corrected_decisions"]
        )
        assert (
            sum(
                r["human"]["action"] != "drop"
                and r["arms"]["jev:v5"]["action"] != "drop"
                and r["arms"]["jev:benchmark-replay"]["action"] == "drop"
                for r in rows
            )
            == expected["benchmark_additional_drops_of_human_kept"]
        )
        hits = [
            r for r in rows if r["benchmark_variant_probabilities"]["excl_benchmark__scores"] >= 0.5
        ]
        assert len(hits) == expected["benchmark_feature_at_least_half"]
        assert all(r["human"]["action"] == "drop" for r in hits)
        total += len(rows)
    assert len(csv_rows) == total == 190
    first = read("human-schema/round1.json")["rows"]
    assert len(first) == 30
    for field, count in (("exclusion", 27), ("relevant", 23), ("band", 19)):
        assert sum(r["first"][field] == r["second"][field] for r in first) == count
    old_new = read("human-schema/sitting-comparison.json")["rows"]
    assert len(old_new) == 79
    assert sum(r["sept19_exclusion"] == r["sept20_exclusion"] for r in old_new) == 61
    assert sum(r["sept19_disposition"] == r["derived_route"] for r in old_new) == 47
    crosswalk = [r for r in old_new if r["sept19_band"] in {"substantive", "pointer"}]
    stable = [r for r in crosswalk if r["sept19_exclusion"] == r["sept20_exclusion"] == "none"]
    assert len(crosswalk) == 51 and len(stable) == 42
    for subset in (crosswalk, stable):
        assert (
            sum(
                r["sept20_substance"]
                == {"substantive": "in_post", "pointer": "pointer"}[r["sept19_band"]]
                for r in subset
            )
            == 37
        )
    repeat = read("human-schema/grading-round-comparison.json")["rows"]
    assert len(repeat) == 79
    assert sum(r["first_round"] == r["second_round"] for r in repeat) == 64
    assert (
        sum(r["first_round"]["exclusion"] == r["second_round"]["exclusion"] for r in repeat) == 71
    )
    both_clear = [
        r
        for r in repeat
        if r["first_round"]["exclusion"] == r["second_round"]["exclusion"] == "none"
    ]
    assert len(both_clear) == 51
    assert (
        sum(r["first_round"]["substance"] == r["second_round"]["substance"] for r in both_clear)
        == 44
    )
    changed_substance = [r for r in both_clear if r["first_round"] != r["second_round"]]
    assert len(changed_substance) == 7
    assert all(r["second_round_note_present"] for r in changed_substance)
    assert sum(r["note_names_earlier_answer"] for r in changed_substance) == 6
    assert (
        sum(human_action(r["first_round"]) == human_action(r["second_round"]) for r in repeat) == 68
    )
    print("Verified source policy, bundle hashes, earlier label comparisons and 190 model cases.")


if __name__ == "__main__":
    verify()
