"""Atomic, deterministic export of held zero-shot relevance evaluations."""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from scout.replay.population_export import (
    PopulationExportRecord,
    record_from_held_row,
    render_population_jsonl,
)
from scout.storage.state import StateManager

_SAFE_PROJECT_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True, slots=True)
class HoldoutExportResult:
    batch_id: str
    exported_at: str
    row_count: int
    files: tuple[Path, ...]


def _render_manifest(
    batch_id: str,
    exported_at: str,
    projects: dict[str, list[int]],
) -> bytes:
    document = {
        "format": "scout.relevance-holdout-batch/v1",
        "batch_id": batch_id,
        "exported_at": exported_at,
        "row_count": sum(map(len, projects.values())),
        "projects": [
            {
                "project_key": key,
                "file": f"{key}.jsonl",
                "evaluation_ids": sorted(projects[key]),
                "row_count": len(projects[key]),
            }
            for key in sorted(projects)
        ],
    }
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _write_files(output_dir: Path, payloads: dict[str, bytes]) -> tuple[Path, ...]:
    output_dir.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[Path, Path]] = []
    try:
        for name, payload in sorted(payloads.items()):
            descriptor, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=output_dir)
            temporary_path = Path(temporary)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            staged.append((temporary_path, output_dir / name))
        for temporary_path, destination in staged:
            os.replace(temporary_path, destination)
        return tuple(destination for _, destination in staged)
    finally:
        for temporary_path, _ in staged:
            temporary_path.unlink(missing_ok=True)


def export_holdouts(
    state: StateManager,
    output_dir: Path,
    *,
    batch_id: str | None = None,
    now: datetime | None = None,
    writer: Callable[[Path, dict[str, bytes]], tuple[Path, ...]] = _write_files,
) -> HoldoutExportResult:
    """Export a new batch, or reproduce an existing batch byte-for-byte."""
    requested_batch = batch_id
    batch_id = batch_id or str(uuid.uuid4())
    with state.db.begin_immediate():
        rows = state.relevance_holdouts.rows_for_export(batch_id=requested_batch)
        if not rows:
            qualifier = f"batch {batch_id!r}" if requested_batch else "unexported holdouts"
            raise ValueError(f"no {qualifier} found")

        if requested_batch:
            timestamps = {str(row["exported_at"]) for row in rows}
            if len(timestamps) != 1 or None in {row["exported_at"] for row in rows}:
                raise ValueError(f"batch {batch_id!r} has inconsistent export timestamps")
            exported_at = timestamps.pop()
        else:
            exported_at = (now or datetime.now(UTC)).isoformat()

        grouped_records: dict[str, list[PopulationExportRecord]] = defaultdict(list)
        project_evaluations: dict[str, list[int]] = defaultdict(list)
        for row in rows:
            project_key = row["project_key"]
            if not isinstance(project_key, str) or not _SAFE_PROJECT_KEY.fullmatch(project_key):
                raise ValueError(f"unsafe or missing holdout project key: {project_key!r}")
            grouped_records[project_key].append(record_from_held_row(dict(row)))
            project_evaluations[project_key].append(int(row["evaluation_id"]))

        payloads = {
            f"{project_key}.jsonl": render_population_jsonl(records)
            for project_key, records in grouped_records.items()
        }
        payloads["batch.json"] = _render_manifest(
            batch_id, exported_at, dict(project_evaluations)
        )
        files = writer(output_dir, payloads)
        if requested_batch is None:
            state.relevance_holdouts.assign_export_batch(
                tuple(sorted(row["id"] for row in rows)),
                batch_id=batch_id,
                exported_at=exported_at,
            )

    return HoldoutExportResult(batch_id, exported_at, len(rows), files)


__all__ = ["HoldoutExportResult", "export_holdouts"]
