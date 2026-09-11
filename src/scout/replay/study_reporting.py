"""Manifest-indexed study export using only the canonical retained-evidence report."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from scout.replay.reporting import ReportError, build_batch_report, render_json, render_markdown
from scout.storage.state import StateManager


class StudyCell(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sweep_name: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]*$")
    variant: str
    model: str
    reasoning: bool | None = None
    # Accepted grid annotations; these are not verified execution provenance.
    sweep_file: str | None = None
    project: str | None = None
    prompt: str | None = None
    backend: str | None = None
    model_id: str | None = None
    quant: str | None = None
    repeat: int | None = Field(default=None, ge=1)


class StudyManifest(BaseModel):
    manifest_version: int
    grid: str
    cells: list[StudyCell] = Field(min_length=1)


class SweepOutcome(BaseModel):
    version: int
    experiment_run_ids: dict[str, int]


def export_study_reports(state: StateManager, manifest_path: Path, out_dir: Path) -> bool:
    """Export every declared sweep; never select a run by its observed success.

    Outcomes bind variant names to explicit run IDs. Missing outcomes stay
    visible in the index and make the campaign incomplete. Each repeat/sweep
    retains its own report; no post-hoc pooling across different plan hashes.
    """
    manifest = StudyManifest.model_validate_json(manifest_path.read_bytes())
    if manifest.manifest_version != 1:
        raise ReportError("unsupported study manifest version")
    out_dir.mkdir(parents=True, exist_ok=True)
    index = [f"# {manifest.grid}", "", "Reports use retained targets and all attempt costs.", ""]
    complete = True
    for sweep in sorted({cell.sweep_name for cell in manifest.cells}):
        cells = [cell for cell in manifest.cells if cell.sweep_name == sweep]
        if len({cell.variant for cell in cells}) != len(cells):
            raise ReportError(f"{sweep}: duplicate manifest variant")
        outcome_path = manifest_path.parent / f"{sweep}.outcome.json"
        if not outcome_path.exists():
            index.append(f"- {sweep}: **missing outcome**")
            complete = False
            continue
        outcome = SweepOutcome.model_validate_json(outcome_path.read_bytes())
        if outcome.version != 1 or set(outcome.experiment_run_ids) != {c.variant for c in cells}:
            raise ReportError(f"{sweep}: outcome does not match manifest variants")
        for cell in cells:
            run = state.get_experiment_run(outcome.experiment_run_ids[cell.variant])
            if run is None:
                raise ReportError(f"{sweep}: missing experiment run")
            config = json.loads(run["candidate_config"])
            if (
                config.get("variant_name") != cell.variant
                or config.get("model_override") != cell.model
                or config.get("reasoning_override") != cell.reasoning
                or (config.get("sweep") or {}).get("name") != sweep
            ):
                raise ReportError(f"{sweep}: retained configuration differs from manifest")
        report = build_batch_report(
            state, experiment_run_ids=list(outcome.experiment_run_ids.values()),
        )
        # Publish only fields matched to immutable run configuration above.
        # Project/prompt/backend/quant labels remain annotations in the input
        # manifest; they cannot override the report's retained score targets.
        report["study_cells"] = [
            cell.model_dump(include={"sweep_name", "variant", "model", "reasoning"})
            for cell in sorted(cells, key=lambda cell: cell.variant)
        ]
        (out_dir / f"{sweep}.json").write_text(render_json(report), encoding="utf-8")
        (out_dir / f"{sweep}.md").write_text(render_markdown(report), encoding="utf-8")
        index.append(f"- [{sweep}]({sweep}.md): **{report['status']}**")
        complete = complete and report["status"] == "complete"
    index.insert(2, f"Status: **{'complete' if complete else 'incomplete'}**\n")
    (out_dir / "index.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    return complete
