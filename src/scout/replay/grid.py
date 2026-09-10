"""replay-sweep-grid v1: one document for a whole study.

A grid names the projects, candidate prompts, backends and models of a
study once. `expand_grid` turns it into the per-axis replay-sweep v1
documents that `scout feedback batch-replay` consumes (one model-axis sweep
per project x prompt x backend x repeat), a run script that previews and
executes each of them in order, and a manifest that records every cell's
attributes so results can be pooled by attribute instead of by parsing
variant names.

The expander is pure: it reads the grid and the referenced prompt files,
validates every generated sweep with the same rules batch-replay applies,
and returns text. Writing happens only in `write_expansion`.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shlex
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scout.replay.experiments import (
    SweepValidationError,
    load_sweep_document,
    validate_sweep_document,
)
from scout.resources import runtime_resource

GRID_SCHEMA_PATH = runtime_resource("contracts", "replay-sweep-grid.v1.schema.json")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_AXIS_KEY = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
MANIFEST_VERSION = 1


class GridValidationError(ValueError):
    """A grid document failed replay-sweep-grid v1 structural or semantic
    validation, or one of the sweeps it expands to failed replay-sweep v1."""


class GridProject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_config: str
    dossier_root: str | None = None


class GridPromptFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file: str


class GridPromptFiles(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: dict[str, str]


class GridBackend(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pricing_catalog: str
    identity_config: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


class GridModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    backend: str
    model: str
    quant: str | None = None
    reasoning: bool | None = None


class GridSelector(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: str | None = None
    prompt: str | None = None
    backend: str | None = None

    def matches(self, project: str, prompt: str, backend: str) -> bool:
        return (
            (self.project is None or self.project == project)
            and (self.prompt is None or self.prompt == prompt)
            and (self.backend is None or self.backend == backend)
        )


class GridDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int
    name: str
    projects: dict[str, GridProject]
    prompts: dict[str, GridPromptFile | GridPromptFiles | None]
    backends: dict[str, GridBackend]
    models: list[GridModel]
    only: list[GridSelector] = Field(default_factory=list)
    skip: list[GridSelector] = Field(default_factory=list)
    repeats: int = 1


@dataclass(frozen=True, slots=True)
class GridCell:
    """One (variant, sweep) pair with every attribute the study varies."""

    sweep_name: str
    sweep_file: str
    variant: str
    project: str
    prompt: str
    backend: str
    model_id: str
    model: str
    quant: str | None
    reasoning: bool | None
    repeat: int


@dataclass(frozen=True, slots=True)
class GridSweep:
    """One generated replay-sweep v1 document plus how to run it."""

    name: str
    file: str
    document: dict[str, Any]
    project: str
    prompt: str
    backend: str
    repeat: int


@dataclass(frozen=True, slots=True)
class GridExpansion:
    grid: GridDocument
    sweeps: tuple[GridSweep, ...]
    cells: tuple[GridCell, ...]
    manifest: dict[str, Any]
    run_script: str


def _load_grid_schema() -> dict[str, Any]:
    with GRID_SCHEMA_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)  # type: ignore[no-any-return]


def load_grid(path: Path | str) -> GridDocument:
    """Parse and validate one grid file (YAML or JSON) into a GridDocument.

    Structural rules come from the schema; semantic rules checked here:
    every model's backend exists, every `files` prompt names only known
    projects and covers every project, and at least one cell survives the
    `only`/`skip` selectors.
    """
    try:
        raw = load_sweep_document(path)
    except SweepValidationError as exc:
        raise GridValidationError(str(exc)) from exc
    validator = jsonschema.Draft202012Validator(_load_grid_schema())
    errors = sorted(validator.iter_errors(raw), key=lambda e: list(e.path))
    if errors:
        messages = "; ".join(e.message for e in errors)
        raise GridValidationError(f"grid failed replay-sweep-grid v1 validation: {messages}")
    try:
        grid = GridDocument.model_validate(raw)
    except ValidationError as exc:
        raise GridValidationError(f"grid document is malformed: {exc}") from exc
    return validate_grid(grid)


def validate_grid(grid: GridDocument) -> GridDocument:
    """Semantic checks the schema cannot express; returns the grid unchanged.
    Mirrors the schema's slug and env-key constraints so a GridDocument
    built in code gets the same guarantees as one loaded from a file."""
    for axis_name, keys in (
        ("projects", grid.projects),
        ("prompts", grid.prompts),
        ("backends", grid.backends),
    ):
        bad = sorted(k for k in keys if not _AXIS_KEY.fullmatch(k))
        if bad:
            raise GridValidationError(
                f"{axis_name} keys must be slugs (they become file name components): {bad}"
            )
    unknown_backends = sorted({m.backend for m in grid.models} - set(grid.backends))
    if unknown_backends:
        raise GridValidationError(f"models reference undeclared backends: {unknown_backends}")
    for prompt_name, prompt in grid.prompts.items():
        if isinstance(prompt, GridPromptFiles):
            extra = sorted(set(prompt.files) - set(grid.projects))
            missing = sorted(set(grid.projects) - set(prompt.files))
            if extra or missing:
                raise GridValidationError(
                    f"prompt {prompt_name!r}: files must cover exactly the declared projects "
                    f"(unknown: {extra}, missing: {missing})"
                )
    for backend_name, backend in grid.backends.items():
        bad = sorted(k for k in backend.env if not _ENV_KEY.fullmatch(k))
        if bad:
            raise GridValidationError(
                f"backend {backend_name!r} env keys are not POSIX variable names: {bad}"
            )
    for selector_name in ("only", "skip"):
        for sel in getattr(grid, selector_name):
            for field_name, known in (
                ("project", grid.projects),
                ("prompt", grid.prompts),
                ("backend", grid.backends),
            ):
                value = getattr(sel, field_name)
                if value is not None and value not in known:
                    raise GridValidationError(
                        f"{selector_name} selector names unknown {field_name} {value!r}"
                    )
    return grid


def variant_name(model: GridModel) -> str:
    """Deterministic, attribute-bearing variant name: id, quant, reasoning, backend."""
    parts = [model.id]
    if model.quant:
        parts.append(model.quant.lower().replace("_", ""))
    if model.reasoning is True:
        parts.append("think")
    elif model.reasoning is False:
        parts.append("nothink")
    parts.append(model.backend)
    return "-".join(parts)


def _portable_path(target: Path, base: Path) -> str:
    """Relative when the target is near the base (so an expansion written
    beside its grid stays movable), absolute once the relative form would
    climb more than three directories (an expansion under /tmp pointing
    back into a checkout is not movable anyway and `../../../../..` chains
    are unreadable)."""
    rel = os.path.relpath(target, base)
    if rel.count("..") > 3:
        return str(target)
    return rel


def _prompt_file(grid: GridDocument, prompt: str, project: str) -> str | None:
    spec = grid.prompts[prompt]
    if spec is None:
        return None
    if isinstance(spec, GridPromptFile):
        return spec.file
    return spec.files[project]


def _selected_cells(grid: GridDocument) -> Iterable[tuple[str, str, str]]:
    for project, prompt, backend in itertools.product(grid.projects, grid.prompts, grid.backends):
        if grid.only and not any(s.matches(project, prompt, backend) for s in grid.only):
            continue
        if any(s.matches(project, prompt, backend) for s in grid.skip):
            continue
        yield project, prompt, backend


def _sweep_document(
    grid: GridDocument,
    *,
    name: str,
    prompt_file: str | None,
    models: Sequence[GridModel],
) -> dict[str, Any]:
    document: dict[str, Any] = {"version": 1, "name": name, "axis": "model"}
    if prompt_file is not None:
        document["prompt_file"] = prompt_file
    document["variants"] = [
        {"name": variant_name(m), "model": m.model}
        | ({} if m.reasoning is None else {"reasoning": m.reasoning})
        for m in models
    ]
    return document


def _grid_path(value: str) -> str:
    """A grid-relative path as a shell word: `"$GRID_DIR/"<quoted value>`,
    or the quoted value alone when it is absolute. Quoting each component
    keeps `$(...)`, spaces and quotes in grid values inert."""
    if os.path.isabs(value):
        return shlex.quote(value)
    return '"$GRID_DIR/"' + shlex.quote(value)


def _run_script(grid: GridDocument, sweeps: Sequence[GridSweep], *, grid_dir_from_out: str) -> str:
    """A plain bash runner: for each sweep, preview, capture the canonical
    plan hash, execute. Every grid value is shell-quoted. The script exits
    nonzero if any sweep produced no plan or its replay failed, so a caller
    cannot mistake a partial study for a complete one."""
    grid_dir = (
        shlex.quote(grid_dir_from_out)
        if os.path.isabs(grid_dir_from_out)
        else '"$HERE/"' + shlex.quote(grid_dir_from_out)
    )
    lines = [
        "#!/bin/bash",
        f"# Generated by `scout feedback grid expand` from grid {grid.name!r}.",
        "# Do not edit; edit the grid and expand again.",
        "# Runs every sweep in order: preview -> canonical plan sha256 -> authorized execution.",
        "set -u",
        'HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        f"GRID_DIR={grid_dir}",
        'SCOUT=${SCOUT:-"scout"}',
        "failed=0",
        "",
    ]
    for sweep in sweeps:
        backend = grid.backends[sweep.backend]
        project = grid.projects[sweep.project]
        env_parts = [f"{k}={shlex.quote(v)}" for k, v in sorted(backend.env.items())]
        if backend.identity_config:
            env_parts.append(
                f'SCOUT_MODEL_IDENTITY_CONFIG="$(cat {_grid_path(backend.identity_config)})"'
            )
        env_prefix = ("env " + " ".join(env_parts) + " ") if env_parts else ""
        args = [
            "feedback",
            "batch-replay",
            "--name",
            shlex.quote(sweep.name),
            "--task-config",
            _grid_path(project.task_config),
            "--sweep-file",
            '"$HERE/"' + shlex.quote(sweep.file),
            "--pricing-catalog",
            _grid_path(backend.pricing_catalog),
        ]
        if project.dossier_root:
            args += ["--dossier-root", _grid_path(project.dossier_root)]
        cmd = f'{env_prefix}"$SCOUT" ' + " ".join(args)
        name = shlex.quote(sweep.name)
        lines += [
            f'echo "PLAN {name} $(date -u +%FT%TZ)"',
            f'sha=$({cmd} 2>/dev/null | grep "canonical plan sha256" | grep -oE "[0-9a-f]{{64}}")',
            f'if [ -z "$sha" ]; then echo "NO SHA {name}"; failed=1; else',
            f'  {cmd} --authorize-plan-sha256 "$sha" --execute-paid-replay',
            "  status=$?",
            f'  echo "EXIT {name}=$status $(date -u +%FT%TZ)"',
            '  [ "$status" -eq 0 ] || failed=1',
            "fi",
            "",
        ]
    lines += [
        'if [ "$failed" -ne 0 ]; then echo "INCOMPLETE"; exit 1; fi',
        'echo "ALLDONE"',
    ]
    return "\n".join(lines) + "\n"


def expand_grid(grid: GridDocument, *, grid_path: Path, out_dir: Path) -> GridExpansion:
    """Expand a validated grid into sweeps, cells, manifest and run script.

    Every generated sweep is validated with `validate_sweep_document`, so a
    grid that would produce an unrunnable sweep (fewer than two models on a
    backend, a missing prompt file, an unroutable model) fails here, before
    anything is written.
    """
    grid_dir = grid_path.parent.resolve()
    out_dir = out_dir.resolve()
    rel_grid_dir = _portable_path(grid_dir, out_dir)
    by_backend: dict[str, list[GridModel]] = {}
    for m in grid.models:
        by_backend.setdefault(m.backend, []).append(m)

    sweeps: list[GridSweep] = []
    cells: list[GridCell] = []
    for project, prompt, backend in _selected_cells(grid):
        models = by_backend.get(backend, [])
        if len(models) < 2:
            raise GridValidationError(
                f"backend {backend!r} serves {len(models)} model(s); a replay sweep needs at "
                f"least two variants (cell {project}/{prompt}/{backend})"
            )
        prompt_file = _prompt_file(grid, prompt, project)
        prompt_rel = (
            _portable_path((grid_dir / prompt_file).resolve(), out_dir) if prompt_file else None
        )
        for repeat in range(1, grid.repeats + 1):
            name = f"{grid.name}-{project}-{prompt}-{backend}" + (
                f"-r{repeat}" if grid.repeats > 1 else ""
            )
            document = _sweep_document(grid, name=name, prompt_file=prompt_rel, models=models)
            # Validate with the prompt path made absolute: out_dir need not
            # exist yet, and `out/../prompts/x` cannot be opened through a
            # directory that is not there. The written document keeps the
            # relative path so it stays portable beside its prompts.
            to_validate = dict(document)
            if prompt_file is not None:
                to_validate["prompt_file"] = str((grid_dir / prompt_file).resolve())
            try:
                validate_sweep_document(to_validate, base_dir=out_dir)
            except SweepValidationError as exc:
                raise GridValidationError(f"generated sweep {name!r} is invalid: {exc}") from exc
            sweep_file = f"{name}.yaml"
            sweeps.append(GridSweep(name, sweep_file, document, project, prompt, backend, repeat))
            cells.extend(
                GridCell(
                    sweep_name=name,
                    sweep_file=sweep_file,
                    variant=variant_name(m),
                    project=project,
                    prompt=prompt,
                    backend=backend,
                    model_id=m.id,
                    model=m.model,
                    quant=m.quant,
                    reasoning=m.reasoning,
                    repeat=repeat,
                )
                for m in models
            )
    if not sweeps:
        raise GridValidationError("grid selects no cells (check `only`/`skip`)")
    names = [sweep.name for sweep in sweeps]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise GridValidationError(
            "generated sweep names collide (hyphens in project/prompt/backend names join "
            f"ambiguously); rename the axes: {duplicates}"
        )

    manifest: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "grid": grid.name,
        "grid_file": str(grid_path),
    }
    manifest["cells"] = [
        {
            "sweep_name": c.sweep_name,
            "sweep_file": c.sweep_file,
            "variant": c.variant,
            "project": c.project,
            "prompt": c.prompt,
            "backend": c.backend,
            "model_id": c.model_id,
            "model": c.model,
            "quant": c.quant,
            "reasoning": c.reasoning,
            "repeat": c.repeat,
        }
        for c in cells
    ]
    script = _run_script(grid, sweeps, grid_dir_from_out=rel_grid_dir)
    return GridExpansion(grid, tuple(sweeps), tuple(cells), manifest, script)


def write_expansion(expansion: GridExpansion, out_dir: Path) -> list[Path]:
    """Write sweeps, manifest.json and run.sh into out_dir; return the paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for sweep in expansion.sweeps:
        path = out_dir / sweep.file
        path.write_text(yaml.safe_dump(sweep.document, sort_keys=False), encoding="utf-8")
        written.append(path)
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(expansion.manifest, indent=2) + "\n", encoding="utf-8")
    written.append(manifest_path)
    script_path = out_dir / "run.sh"
    script_path.write_text(expansion.run_script, encoding="utf-8")
    script_path.chmod(0o755)
    written.append(script_path)
    return written


def cells_by_variant(manifest: Mapping[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Index a manifest by (sweep_name, variant) for pooling by attribute."""
    return {(c["sweep_name"], c["variant"]): c for c in manifest["cells"]}


__all__ = [
    "GridCell",
    "GridDocument",
    "GridExpansion",
    "GridSweep",
    "GridValidationError",
    "cells_by_variant",
    "expand_grid",
    "load_grid",
    "validate_grid",
    "variant_name",
    "write_expansion",
]
