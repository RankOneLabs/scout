"""Checks for the reproducible fix-the-rubric analysis."""

from __future__ import annotations

import csv
import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "studies/fix-the-rubric/analysis/paired_stats.py"
SPEC = importlib.util.spec_from_file_location("paired_stats", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
paired_stats = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = paired_stats
SPEC.loader.exec_module(paired_stats)


def test_wilson_known_value() -> None:
    lower, upper = paired_stats.wilson_interval(64, 100)
    assert lower == pytest.approx(0.542354, abs=0.000001)
    assert upper == pytest.approx(0.727288, abs=0.000001)


def test_exact_mcnemar_uses_hand_computed_constant() -> None:
    # For b=3, c=1: 2 * (C(4,0) + C(4,1)) / 2**4 = 0.625.
    assert paired_stats.exact_mcnemar_p(3, 1) == 0.625
    assert paired_stats.exact_mcnemar_p(0, 0) == 1.0


def test_bootstrap_is_seeded() -> None:
    first = [True, True, True, False, True, False, False, True]
    second = [False, True, False, True, True, False, False, False]
    assert paired_stats.bootstrap_difference(first, second, seed=20261021) == (
        paired_stats.bootstrap_difference(first, second, seed=20261021)
    )


def test_exported_counts_and_round_four_arm() -> None:
    rounds = {4: paired_stats.read_round(4), 5: paired_stats.read_round(5)}
    paired_stats.validate_published_counts(rounds)
    assert len(rounds[4]) == 90
    assert len(rounds[5]) == 100
    assert sum(
        paired_stats.correct(row, "keep_drop", "jev") for row in rounds[4]
    ) == 57
    assert sum(
        paired_stats.correct(row, "keep_drop", "gemini") for row in rounds[4]
    ) == 62


def test_corrupted_csv_aborts_before_statistics(tmp_path, monkeypatch, capsys) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for round_number in (4, 5):
        shutil.copyfile(
            paired_stats.HERE / "data" / f"round{round_number}.csv",
            data_dir / f"round{round_number}.csv",
        )
    path = data_dir / "round5.csv"
    with path.open(newline="") as file:
        rows = list(csv.DictReader(file))
    rows[0]["jev_action"] = "drop" if rows[0]["jev_action"] != "drop" else "respond"
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=paired_stats.HEADER)
        writer.writeheader()
        writer.writerows(rows)

    monkeypatch.setattr(paired_stats, "HERE", tmp_path)
    monkeypatch.setattr(sys, "argv", ["paired_stats.py"])
    with pytest.raises(SystemExit) as raised:
        paired_stats.main()
    assert raised.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "published count mismatch" in output.err
    assert not (tmp_path / "results.json").exists()
    assert not (tmp_path / "results.md").exists()
