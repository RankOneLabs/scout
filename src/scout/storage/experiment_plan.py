"""Exact case/repeat identity shared by experiment storage and reporting."""

from __future__ import annotations

from collections.abc import Mapping


def expected_experiment_pairs(config: Mapping[str, object]) -> frozenset[tuple[int, int]] | None:
    """Return a plan's allowed pairs; only legacy configs may omit a plan.

    A present but malformed plan must never fall back to counting its children.
    """
    if "phase_run_ids" not in config:
        return None
    planned = config["phase_run_ids"]
    if not isinstance(planned, list) or any(type(pid) is not int or pid < 1 for pid in planned):
        raise ValueError("phase_run_ids must be a list of positive integers")
    if len(set(planned)) != len(planned):
        raise ValueError("phase_run_ids contains duplicates")
    repeats = config.get("repeats", 1)
    if type(repeats) is not int or repeats < 1:
        raise ValueError("repeats must be a positive integer")
    skipped = config.get("skipped_pairs", [])
    if not isinstance(skipped, list):
        raise ValueError("skipped_pairs must be a list")
    skipped_ids: set[int] = set()
    for pair in skipped:
        if not isinstance(pair, dict) or type(pair.get("phase_run_id")) is not int:
            raise ValueError("skipped_pairs must identify a planned phase_run_id")
        pid = pair["phase_run_id"]
        if pid not in planned or pid in skipped_ids:
            raise ValueError("skipped_pairs contains an unknown or duplicate phase_run_id")
        skipped_ids.add(pid)
    return frozenset(
        (pid, repeat) for pid in planned if pid not in skipped_ids
        for repeat in range(1, repeats + 1)
    )
