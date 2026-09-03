"""Hermetic full-corpus regression test for the Phase 1 Jig eval runner.

No network, no API keys, no live models — every inner phase LLM is
scripted (scout.evals.phase1.runner.make_scripted_phase_config_builder). Two
deterministic AgentConfig variants exercise the real
build_scout_pipeline/run_pipeline/classify_outcome machinery end to end
against a dossier materialized from the producer-real
tests/fixtures/dossier_source snapshot (design decision #14): "baseline"
scripts every case to a 1.0 grade (including the relevant-then-abstain
phase1-0018/phase1-0039 cases); "seeded-regression" flips phase1-0018's
scripted relevance to False so it terminates not_relevant instead of
abstained, and detect_regressions must catch the resulting outcome_semantics
drop (design decision #18).
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from jig import detect_regressions  # noqa: E402

from scout.evals.phase1.loader import load_phase1_corpus  # noqa: E402
from scout.evals.phase1.models import Phase1Registry  # noqa: E402
from scout.evals.phase1.runner import (  # noqa: E402
    InMemoryTracer,
    build_variant_agent_config,
    fixture_dossier_provider,
    make_scripted_phase_config_builder,
    run_phase1_sweep,
)
from tests.conftest import (  # noqa: E402
    DOSSIER_SOURCE_CONTRACT_MARKER,
    find_dossier_source_checkout,
)

_REPO_ROOT = Path(__file__).parent.parent
_FIXTURE_DOSSIER_SOURCE = _REPO_ROOT / "tests" / "fixtures" / "dossier_source"
_CORPUS_DIR = _REPO_ROOT / "src" / "scout" / "evals" / "phase1"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


@pytest.fixture(scope="module")
def materialized_corpus(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, str]:
    """Materialize the committed dossier_source fixture into a temp git repo,
    and a corpus copy pinned to that repo's HEAD SHA.

    The temp SHA is never treated as real corpus provenance — case YAML is
    only patched inside this throwaway copy, never in evals/phase1/cases/.

    load_phase1_corpus resolves dossiers through the real, schema-validating
    resolve_dossier, so this materialized repo needs the pinned dossier-source
    schemas — copied in from a live checkout (Scout itself never vendors a
    copy). Skips if no local checkout is found (or fails, under
    DOSSIER_SOURCE_CONFORMANCE_REQUIRED — see tests/conftest.py). A required
    invocation must set DOSSIER_SOURCE_PINNED_CHECKOUT explicitly.
    """
    checkout = find_dossier_source_checkout()
    if checkout is None:
        pytest.skip("no local dossier-source checkout found (set DOSSIER_SOURCE_PINNED_CHECKOUT)")

    base = tmp_path_factory.mktemp("phase1_hermetic")
    repo = base / "dossier-source"
    shutil.copytree(_FIXTURE_DOSSIER_SOURCE, repo)
    schemas_dir = repo / "schemas"
    schemas_dir.mkdir(exist_ok=True)
    for schema_file in ("index.v1.schema.json", "summary.v1.schema.json"):
        (schemas_dir / schema_file).write_text((checkout / "schemas" / schema_file).read_text())
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Scout Tests")
    _git(repo, "config", "user.email", "scout-tests@example.invalid")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture dossiers")
    revision = _git(repo, "rev-parse", "HEAD").stdout.strip()

    corpus_dir = base / "phase1"
    shutil.copytree(_CORPUS_DIR, corpus_dir)
    manifest = yaml.safe_load((corpus_dir / "manifest.yaml").read_text())
    for dossier_cfg in manifest["dossiers"].values():
        dossier_cfg["revision"] = revision
    (corpus_dir / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    for case_path in (corpus_dir / "cases").glob("*.yaml"):
        case: dict[str, Any] = yaml.safe_load(case_path.read_text())
        case["dossier_revision"] = revision
        case_path.write_text(yaml.safe_dump(case, sort_keys=False))

    return corpus_dir, repo, revision


@pytest.fixture(scope="module")
def registry(materialized_corpus: tuple[Path, Path, str]) -> Phase1Registry:
    corpus_dir, repo, _revision = materialized_corpus
    return load_phase1_corpus(corpus_dir, repo)


def test_scripted_builder_reports_model_identity(
    materialized_corpus: tuple[Path, Path, str], registry: Phase1Registry
) -> None:
    _corpus_dir, repo, revision = materialized_corpus
    bundle = make_scripted_phase_config_builder(
        fixture_dossier_provider(repo, revision), InMemoryTracer()
    )(registry["phase1-0001"])

    assert bundle.relevance_model == "scripted"
    assert bundle.reply_draft_model == "scripted"
    assert bundle.critic_model == "scripted"
    assert bundle.configs.relevance.name != bundle.relevance_model


@getattr(pytest.mark, DOSSIER_SOURCE_CONTRACT_MARKER)
class TestHermeticPhase1Sweep:
    def test_baseline_scores_perfect_on_every_dimension(
        self, materialized_corpus: tuple[Path, Path, str], registry: Phase1Registry
    ) -> None:
        _corpus_dir, repo, revision = materialized_corpus
        tracer = InMemoryTracer()
        provider = fixture_dossier_provider(repo, revision)
        builder = make_scripted_phase_config_builder(provider, tracer)
        baseline = build_variant_agent_config(
            name="baseline",
            registry=registry,
            dossier_provider=provider,
            phase_config_builder=builder,
            tracer=tracer,
        )

        result = asyncio.run(run_phase1_sweep(registry, [baseline], concurrency=4, seeds=1))
        assert len(result.runs) == len(registry)

        rollup = result.rollup()
        assert rollup["baseline"]["success_rate"] == 1.0
        for dimension, avg in rollup["baseline"]["avg_scores"].items():
            assert avg == 1.0, f"baseline dimension {dimension!r} averaged {avg}, expected 1.0"

    def test_seeded_regression_is_detected(
        self, materialized_corpus: tuple[Path, Path, str], registry: Phase1Registry
    ) -> None:
        _corpus_dir, repo, revision = materialized_corpus
        tracer = InMemoryTracer()
        provider = fixture_dossier_provider(repo, revision)

        baseline_builder = make_scripted_phase_config_builder(provider, tracer)
        baseline = build_variant_agent_config(
            name="baseline",
            registry=registry,
            dossier_provider=provider,
            phase_config_builder=baseline_builder,
            tracer=tracer,
        )

        # phase1-0018 is a relevant-then-abstain case (design decision #4):
        # relevance=true internally, terminal_status=abstained. Flipping its
        # scripted relevance to False makes it terminate not_relevant
        # instead — a real, detectable outcome_semantics regression.
        seeded_builder = make_scripted_phase_config_builder(
            provider, tracer, relevance_override={"phase1-0018": False}
        )
        seeded = build_variant_agent_config(
            name="seeded-regression",
            registry=registry,
            dossier_provider=provider,
            phase_config_builder=seeded_builder,
            tracer=tracer,
        )

        result = asyncio.run(
            run_phase1_sweep(registry, [baseline, seeded], concurrency=4, seeds=1)
        )
        rollup = result.rollup()
        assert rollup["baseline"]["avg_scores"]["outcome_semantics"] == 1.0
        assert rollup["seeded-regression"]["avg_scores"]["outcome_semantics"] < 1.0

        report = detect_regressions(result, baseline="baseline", threshold=0.0)
        assert report.has_regressions
        assert any(
            alert.config_name == "seeded-regression" and alert.dimension == "outcome_semantics"
            for alert in report.alerts
        )

    def test_grader_distinguishes_abstained_from_not_relevant(
        self, materialized_corpus: tuple[Path, Path, str], registry: Phase1Registry
    ) -> None:
        """phase1-0018/phase1-0039 must grade as abstained (not not_relevant)
        under a correct baseline, and as not_relevant once relevance is
        seeded false — proving the grader keys off terminal status + posture,
        never the internal relevance flag (design decision #11)."""
        _corpus_dir, repo, revision = materialized_corpus
        tracer = InMemoryTracer()
        provider = fixture_dossier_provider(repo, revision)

        baseline_builder = make_scripted_phase_config_builder(provider, tracer)
        baseline = build_variant_agent_config(
            name="baseline",
            registry=registry,
            dossier_provider=provider,
            phase_config_builder=baseline_builder,
            tracer=tracer,
        )
        result = asyncio.run(run_phase1_sweep(registry, [baseline], concurrency=4, seeds=1))
        by_case = {
            run.input.split("\n", 1)[0]: run
            for run in result.runs
        }
        run_0018 = by_case["[SCOUT_EVAL_CASE_ID:phase1-0018]"]
        assert run_0018.result.parsed is not None
        assert run_0018.result.parsed.terminal_status == "abstained"
        assert run_0018.result.parsed.posture == "abstain"
        scores_0018 = {s.dimension: s.value for s in (run_0018.result.scores or [])}
        assert scores_0018["outcome_semantics"] == 1.0

        seeded_builder = make_scripted_phase_config_builder(
            provider, tracer, relevance_override={"phase1-0018": False}
        )
        seeded = build_variant_agent_config(
            name="seeded",
            registry=registry,
            dossier_provider=provider,
            phase_config_builder=seeded_builder,
            tracer=tracer,
        )
        seeded_result = asyncio.run(
            run_phase1_sweep(registry, [seeded], concurrency=4, seeds=1)
        )
        seeded_run_0018 = next(
            run
            for run in seeded_result.runs
            if run.input.startswith("[SCOUT_EVAL_CASE_ID:phase1-0018]")
        )
        assert seeded_run_0018.result.parsed is not None
        assert seeded_run_0018.result.parsed.terminal_status == "not_relevant"
        scores_seeded = {s.dimension: s.value for s in (seeded_run_0018.result.scores or [])}
        assert scores_seeded["outcome_semantics"] == 0.0

    def test_rail_cases_never_execute_the_proposed_write(
        self, materialized_corpus: tuple[Path, Path, str], registry: Phase1Registry
    ) -> None:
        _corpus_dir, repo, revision = materialized_corpus
        tracer = InMemoryTracer()
        provider = fixture_dossier_provider(repo, revision)
        builder = make_scripted_phase_config_builder(provider, tracer)
        baseline = build_variant_agent_config(
            name="baseline",
            registry=registry,
            dossier_provider=provider,
            phase_config_builder=builder,
            tracer=tracer,
        )
        result = asyncio.run(run_phase1_sweep(registry, [baseline], concurrency=4, seeds=1))
        rail_runs = [
            run
            for run in result.runs
            if any(
                run.input.startswith(f"[SCOUT_EVAL_CASE_ID:{case.id}]")
                for case in registry
                if case.case_kind == "dossier_bad_write"
            )
        ]
        assert rail_runs, "expected at least one dossier_bad_write case in the corpus"
        for run in rail_runs:
            assert run.result.parsed is not None
            assert "automated_write_forbidden" in run.result.parsed.rail_violations
            scores = {s.dimension: s.value for s in (run.result.scores or [])}
            assert scores["outcome_semantics"] == 1.0
            assert scores["content_safety"] == 1.0

        # No write tool was ever registered on any variant's outer AgentConfig.
        assert baseline.tools.list() == []
