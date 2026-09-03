"""Load, validate, and register the Phase 1 corpus.

This is the sole place the raw YAML corpus is read. It reuses the existing
corpus linter (``scripts/lint_eval_corpus.py``) for schema + distributional
validation and pinned-dossier resolution, then builds the immutable
``Phase1Registry`` every other eval module consumes. Loading happens once,
before any model execution — expected answers never reach rendered model
input (see ``scout.evals.phase1.runner.render_eval_cases``).
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from scout.evals.phase1.models import Phase1Case, Phase1Registry
from scout.grading.service import validate_grade_envelope
from scripts.lint_eval_corpus import lint_report

DEFAULT_CORPUS_DIR = Path(__file__).resolve().parent


class Phase1LoadError(ValueError):
    """Raised when the corpus fails schema, linter, or contract validation."""


def _grade_contract_errors(case: dict[str, Any]) -> list[str]:
    """Validate a scout_response case's stored grade against the shared
    validationEnvelope contract, using the case's ``expected_posture`` as
    the graded evaluation posture (design decision #5/#13: a recorded
    counterfactual grade stays keyed to the causal-override posture, not
    the terminal status)."""
    grade = case.get("grade")
    if grade is None:
        return [f"{case['id']}: scout_response case missing grade"]
    errors = validate_grade_envelope(grade, case.get("expected_posture"))
    return [f"{case['id']}: grade contract violation: {e}" for e in errors]


def load_phase1_corpus(
    corpus_dir: Path = DEFAULT_CORPUS_DIR,
    content_repo: Path | None = None,
) -> Phase1Registry:
    """Load, validate, and register every case in ``corpus_dir``.

    Runs the existing schema + distributional linter (``lint_report``) —
    which resolves every pinned dossier at ``content_repo`` — and rejects
    the whole corpus on any linter error. Also validates every
    scout_response case's stored grade against the shared grading contract
    (design decision #13: contract-invalid corpus data must fail loading,
    not just runtime grading).

    ``content_repo`` defaults to the ``DOSSIER_SOURCE_REPO`` env var (matching
    the linter script's own default) when not supplied.
    """
    repo = content_repo or Path(
        os.getenv("DOSSIER_SOURCE_REPO", "../dossier-source")
    )
    report = lint_report(corpus_dir, repo)
    if not report["ok"]:
        raise Phase1LoadError(
            "Phase 1 corpus failed schema/linter validation:\n"
            + "\n".join(f"  - {e}" for e in report["errors"])
        )

    manifest = yaml.safe_load((corpus_dir / "manifest.yaml").read_text())

    raw_cases: list[dict[str, Any]] = []
    for path in sorted((corpus_dir / "cases").glob("*.yaml")):
        raw_cases.append(yaml.safe_load(path.read_text()))

    ids = [c["id"] for c in raw_cases]
    dupes = sorted(k for k, n in Counter(ids).items() if n > 1)
    if dupes:
        raise Phase1LoadError(f"Duplicate case IDs in corpus: {dupes}")

    contract_errors: list[str] = []
    for case in raw_cases:
        if case["case_kind"] == "scout_response":
            contract_errors.extend(_grade_contract_errors(case))
    if contract_errors:
        raise Phase1LoadError(
            "Phase 1 corpus failed grade contract validation:\n"
            + "\n".join(f"  - {e}" for e in contract_errors)
        )

    cases: dict[str, Phase1Case] = {}
    for case in raw_cases:
        cases[case["id"]] = Phase1Case(
            id=case["id"],
            case_kind=case["case_kind"],
            origin=case["origin"],
            tags=tuple(case["tags"]),
            project_key=case["project_key"],
            dossier_summary_id=case["dossier_summary_id"],
            dossier_revision=case["dossier_revision"],
            reserved_phase2_rail=case["reserved_phase2_rail"],
            raw=case,
        )

    return Phase1Registry(cases=cases, manifest=manifest)


__all__ = ["DEFAULT_CORPUS_DIR", "Phase1LoadError", "load_phase1_corpus"]
