"""Tests for replay-sweep-grid v1 expansion (scout.replay.grid)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import scout.replay.grid as rg
from scout.replay.experiments import validate_sweep_document


@pytest.fixture(autouse=True)
def _routable_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """validate_sweep_document proves every model routable via from_model,
    which constructs a client; give it a key and a stand-in ollama module."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    import jig.llm.ollama as jig_ollama

    monkeypatch.setattr(jig_ollama, "OllamaAsyncClient", lambda host=None: object())


def test_unroutable_model_is_a_grid_validation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jig.llm.ollama as jig_ollama

    monkeypatch.setattr(jig_ollama, "OllamaAsyncClient", None)  # ollama extra not installed
    with pytest.raises(rg.GridValidationError, match="not routable"):
        _expand(tmp_path)


def _write_grid(tmp_path: Path, **overrides: object) -> Path:
    (tmp_path / "prompts").mkdir(exist_ok=True)
    (tmp_path / "prompts" / "ops-topical.md").write_text("ops rubric", encoding="utf-8")
    (tmp_path / "prompts" / "evals-topical.md").write_text("evals rubric", encoding="utf-8")
    (tmp_path / "pricing.json").write_text("{}", encoding="utf-8")
    (tmp_path / "identity.json").write_text('{"identities": []}', encoding="utf-8")
    doc: dict[str, object] = {
        "version": 1,
        "name": "study",
        "projects": {
            "agent-ops": {"task_config": "task-ops.json", "dossier_root": "/srv/content-agn"},
            "agent-evals": {"task_config": "task-evals.json"},
        },
        "prompts": {
            "current": None,
            "topical": {
                "files": {
                    "agent-ops": "prompts/ops-topical.md",
                    "agent-evals": "prompts/evals-topical.md",
                }
            },
        },
        "backends": {
            "openrouter": {"pricing_catalog": "pricing.json", "identity_config": "identity.json"},
            "frink": {
                "pricing_catalog": "pricing.json",
                "env": {"OLLAMA_HOST": "http://frink:11434"},
            },
        },
        "models": [
            {
                "id": "gemma-4-26b-a4b",
                "backend": "openrouter",
                "model": "openrouter/google/gemma-4-26b-a4b-it",
            },
            {
                "id": "qwen3-30b-a3b",
                "backend": "openrouter",
                "model": "openrouter/qwen/qwen3-30b-a3b-instruct-2507",
            },
            {
                "id": "gemma-4-26b-a4b",
                "backend": "frink",
                "model": "ollama/gemma4:26b",
                "quant": "Q4_K_M",
                "reasoning": False,
            },
            {
                "id": "qwen3-30b-a3b",
                "backend": "frink",
                "model": "ollama/qwen3:30b-a3b-instruct-2507-q4_K_M",
                "quant": "Q4_K_M",
            },
        ],
    }
    doc.update(overrides)
    path = tmp_path / "study.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _expand(tmp_path: Path, **overrides: object) -> rg.GridExpansion:
    path = _write_grid(tmp_path, **overrides)
    grid = rg.load_grid(path)
    return rg.expand_grid(grid, grid_path=path, out_dir=tmp_path / "out")


def test_expands_projects_x_prompts_x_backends_into_model_axis_sweeps(tmp_path: Path) -> None:
    exp = _expand(tmp_path)
    assert len(exp.sweeps) == 2 * 2 * 2
    assert all(s.document["axis"] == "model" for s in exp.sweeps)
    assert len(exp.cells) == 8 * 2


def test_variant_names_carry_quant_reasoning_and_backend(tmp_path: Path) -> None:
    exp = _expand(tmp_path)
    names = {c.variant for c in exp.cells}
    assert "gemma-4-26b-a4b-q4km-nothink-frink" in names
    assert "qwen3-30b-a3b-q4km-frink" in names
    assert "gemma-4-26b-a4b-openrouter" in names


def test_topical_prompt_resolves_per_project_relative_to_out_dir(tmp_path: Path) -> None:
    exp = _expand(tmp_path)
    ops = next(s for s in exp.sweeps if s.project == "agent-ops" and s.prompt == "topical")
    assert ops.document["prompt_file"] == "../prompts/ops-topical.md"
    current = next(s for s in exp.sweeps if s.prompt == "current")
    assert "prompt_file" not in current.document


def test_every_generated_sweep_passes_replay_sweep_v1_validation(tmp_path: Path) -> None:
    exp = _expand(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    for s in exp.sweeps:
        validate_sweep_document(s.document, base_dir=out)


def test_manifest_records_cell_attributes(tmp_path: Path) -> None:
    exp = _expand(tmp_path)
    cell = rg.cells_by_variant(exp.manifest)[
        ("study-agent-ops-topical-frink", "gemma-4-26b-a4b-q4km-nothink-frink")
    ]
    assert cell == {
        "sweep_name": "study-agent-ops-topical-frink",
        "sweep_file": "study-agent-ops-topical-frink.yaml",
        "variant": "gemma-4-26b-a4b-q4km-nothink-frink",
        "project": "agent-ops",
        "prompt": "topical",
        "backend": "frink",
        "model_id": "gemma-4-26b-a4b",
        "model": "ollama/gemma4:26b",
        "quant": "Q4_K_M",
        "reasoning": False,
        "repeat": 1,
    }


def test_repeats_suffix_sweep_names(tmp_path: Path) -> None:
    exp = _expand(tmp_path, repeats=2)
    names = {s.name for s in exp.sweeps}
    assert "study-agent-ops-topical-frink-r1" in names
    assert "study-agent-ops-topical-frink-r2" in names
    assert len(exp.sweeps) == 16


def test_only_and_skip_select_cells(tmp_path: Path) -> None:
    exp = _expand(
        tmp_path,
        only=[{"prompt": "topical"}],
        skip=[{"project": "agent-evals", "backend": "frink"}],
    )
    keys = {(s.project, s.prompt, s.backend) for s in exp.sweeps}
    assert keys == {
        ("agent-ops", "topical", "openrouter"),
        ("agent-ops", "topical", "frink"),
        ("agent-evals", "topical", "openrouter"),
    }


def test_backend_with_one_model_is_rejected_before_writing(tmp_path: Path) -> None:
    models = [
        {
            "id": "gemma-4-26b-a4b",
            "backend": "openrouter",
            "model": "openrouter/google/gemma-4-26b-a4b-it",
        },
        {
            "id": "qwen3-30b-a3b",
            "backend": "openrouter",
            "model": "openrouter/qwen/qwen3-30b-a3b-instruct-2507",
        },
        {"id": "gemma-4-26b-a4b", "backend": "frink", "model": "ollama/gemma4:26b"},
    ]
    with pytest.raises(rg.GridValidationError, match="at least two variants"):
        _expand(tmp_path, models=models)
    assert not (tmp_path / "out").exists()


def test_undeclared_backend_is_rejected(tmp_path: Path) -> None:
    models = [
        {"id": "a", "backend": "mcbain", "model": "ollama/a"},
        {"id": "b", "backend": "mcbain", "model": "ollama/b"},
    ]
    with pytest.raises(rg.GridValidationError, match="undeclared backends"):
        _expand(tmp_path, models=models)


def test_unknown_top_level_key_is_rejected_by_schema(tmp_path: Path) -> None:
    with pytest.raises(rg.GridValidationError, match="replay-sweep-grid v1 validation"):
        _expand(tmp_path, extra_field=1)


def test_run_script_carries_backend_env_identity_and_replay_args(tmp_path: Path) -> None:
    exp = _expand(tmp_path)
    script = exp.run_script
    assert "env OLLAMA_HOST=http://frink:11434 " in script
    assert 'SCOUT_MODEL_IDENTITY_CONFIG="$(cat "$GRID_DIR/"identity.json)"' in script
    assert "--name study-agent-ops-topical-frink" in script
    assert '--task-config "$GRID_DIR/"task-ops.json' in script
    assert "--dossier-root /srv/content-agn" in script
    assert "--authorize-plan-sha256" in script and "--execute-paid-replay" in script
    assert script.count("PLAN ") == len(exp.sweeps)


def test_run_script_exits_nonzero_when_any_sweep_fails(tmp_path: Path) -> None:
    script = _expand(tmp_path).run_script
    assert "failed=0" in script
    assert 'if [ -z "$sha" ]; then echo "NO SHA' in script and "failed=1" in script
    assert '[ "$status" -eq 0 ] || failed=1' in script
    assert 'if [ "$failed" -ne 0 ]; then echo "INCOMPLETE"; exit 1; fi' in script
    assert script.rstrip().endswith('echo "ALLDONE"')


def test_run_script_quotes_grid_values(tmp_path: Path) -> None:
    backends = {
        "openrouter": {"pricing_catalog": "price $(id).json", "identity_config": "id $(x).json"},
        "frink": {
            "pricing_catalog": "pricing.json",
            "env": {"OLLAMA_HOST": "http://frink:11434 ; rm -rf /"},
        },
    }
    (tmp_path / "price $(id).json").write_text("{}", encoding="utf-8")
    (tmp_path / "id $(x).json").write_text("{}", encoding="utf-8")
    script = _expand(tmp_path, backends=backends).run_script
    assert "'id $(x).json'" in script and "'price $(id).json'" in script
    assert "OLLAMA_HOST='http://frink:11434 ; rm -rf /'" in script
    assert "$(id)" not in script.replace("'price $(id).json'", "")


def test_relative_dossier_root_resolves_from_grid_dir(tmp_path: Path) -> None:
    projects = {
        "agent-ops": {"task_config": "task-ops.json", "dossier_root": "dossiers"},
        "agent-evals": {"task_config": "task-evals.json"},
    }
    script = _expand(tmp_path, projects=projects).run_script
    assert '--dossier-root "$GRID_DIR/"dossiers' in script


def test_env_keys_must_be_posix_variable_names(tmp_path: Path) -> None:
    backends = {
        "openrouter": {"pricing_catalog": "pricing.json"},
        "frink": {"pricing_catalog": "pricing.json", "env": {"OLLAMA-HOST": "x"}},
    }
    with pytest.raises(rg.GridValidationError, match="replay-sweep-grid v1 validation"):
        _expand(tmp_path, backends=backends)


def test_conflicting_reasoning_within_a_backend_is_rejected(tmp_path: Path) -> None:
    models = [
        {
            "id": "gemma-4-26b-a4b",
            "backend": "openrouter",
            "model": "openrouter/google/gemma-4-26b-a4b-it",
        },
        {
            "id": "qwen3-30b-a3b",
            "backend": "openrouter",
            "model": "openrouter/qwen/qwen3-30b-a3b-instruct-2507",
        },
        {
            "id": "gemma-4-26b-a4b",
            "backend": "frink",
            "model": "ollama/gemma4:26b",
            "reasoning": False,
        },
        {"id": "qwen3-30b-a3b", "backend": "frink", "model": "ollama/qwen3:30b", "reasoning": True},
    ]
    with pytest.raises(rg.GridValidationError, match="reasoning: true and reasoning: false"):
        _expand(tmp_path, models=models)


def test_colliding_sweep_names_are_rejected(tmp_path: Path) -> None:
    projects = {"a-b": {"task_config": "t1.json"}, "a": {"task_config": "t2.json"}}
    prompts = {"c": None, "b-c": None}
    with pytest.raises(rg.GridValidationError, match="collide"):
        _expand(tmp_path, projects=projects, prompts=prompts)


def test_write_expansion_writes_sweeps_manifest_and_script(tmp_path: Path) -> None:
    exp = _expand(tmp_path)
    out = tmp_path / "out"
    written = rg.write_expansion(exp, out)
    assert {p.name for p in written} >= {"manifest.json", "run.sh"}
    assert len([p for p in written if p.suffix == ".yaml"]) == 8
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 1 and len(manifest["cells"]) == 16
    assert (out / "run.sh").stat().st_mode & 0o111
    reloaded = yaml.safe_load(
        (out / "study-agent-ops-topical-frink.yaml").read_text(encoding="utf-8")
    )
    assert reloaded["variants"][0]["model"] == "ollama/gemma4:26b"
