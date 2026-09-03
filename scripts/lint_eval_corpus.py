"""Validate the Phase 1 corpus and its pinned dossier-source dossier references."""
# ruff: noqa: E501

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, cast

import jsonschema
import yaml
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

sys.path.insert(0, str(Path(__file__).parent.parent))
from scout.dossiers.resolver import DossierResolutionError, resolve_dossier  # noqa: E402

DEFAULT_CORPUS = Path(__file__).parent.parent / "src" / "scout" / "evals" / "phase1"
_REPO_ROOT = Path(__file__).parent.parent
_GRADING_SCHEMA_PATH = _REPO_ROOT / "web" / "grading_schema.json"


def build_offline_registry() -> Registry[Any]:
    """Build a jsonschema Registry that resolves the grading contract's
    canonical $id (web/grading_schema.json) without any network access.

    The eval case schema (evals/phase1/case.schema.json) $refs
    web/grading_schema.json's $defs by canonical $id for posture,
    judgment, dimension, and gradePayload — this registry is what lets
    both the linter and tests resolve that $ref offline and
    deterministically in CI.
    """
    grading_schema = json.loads(_GRADING_SCHEMA_PATH.read_text())
    schema_id = grading_schema.get("$id")
    if not isinstance(schema_id, str) or not schema_id:
        raise ValueError("grading schema must declare a non-empty string $id")
    resource = Resource.from_contents(grading_schema, default_specification=DRAFT202012)
    registry: Registry[Any] = Registry()
    return cast(Registry[Any], registry.with_resource(schema_id, resource))


def _git(repo: Path, revision: str, path: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{revision}:{path}"],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or f"cannot read {revision}:{path}")
    return result.stdout


def _load_cases(corpus: Path, schema: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    cases: list[dict[str, Any]] = []
    errors: list[str] = []
    validator = jsonschema.Draft202012Validator(
        schema,
        registry=build_offline_registry(),
        format_checker=jsonschema.FormatChecker(),
    )
    for path in sorted((corpus / "cases").glob("*.yaml")):
        try:
            case = yaml.safe_load(path.read_text())
            violations = sorted(validator.iter_errors(case), key=lambda e: list(e.path))
            if violations:
                errors.extend(
                    f"{path.name}: schema error at "
                    f"/{'/'.join(map(str, error.path))}: {error.message}"
                    for error in violations
                )
            else:
                cases.append(case)
        except (OSError, yaml.YAMLError) as exc:
            errors.append(f"{path.name}: {exc}")
    return cases, errors


def _resolve_dossiers(
    cases: list[dict[str, Any]], manifest: dict[str, Any], repo: Path
) -> tuple[dict[tuple[str, str, str], dict[str, Any]], list[str]]:
    """Resolve every (project_key, revision, summary_id) triple cases reference.

    The case is the caller of resolve_dossier and therefore supplies project
    identity under the upstream contract; index entries carry only
    ``type`` and ``path``, never ``project_key``.
    """
    resolved: dict[tuple[str, str, str], dict[str, Any]] = {}
    errors: list[str] = []

    project_keys_by_dossier: dict[tuple[str, str], set[str]] = defaultdict(set)
    for case in cases:
        project_keys_by_dossier[(case["dossier_revision"], case["dossier_summary_id"])].add(
            case["project_key"]
        )
    inconsistent: set[tuple[str, str]] = set()
    for (revision, summary_id), project_keys in sorted(project_keys_by_dossier.items()):
        if len(project_keys) > 1:
            inconsistent.add((revision, summary_id))
            errors.append(
                f"revision {revision!r} summary {summary_id!r} is used with inconsistent "
                f"case project_keys {sorted(project_keys)}"
            )

    for case in cases:
        revision, summary_id = case["dossier_revision"], case["dossier_summary_id"]
        if (revision, summary_id) in inconsistent:
            continue
        project_key = case["project_key"]
        key = (project_key, revision, summary_id)
        if key in resolved:
            continue
        try:
            result = resolve_dossier(repo, revision, project_key, summary_id)
            document = yaml.safe_load(_git(repo, revision, result.metadata.path))
            if not isinstance(document, dict) or not isinstance(document.get("dossier"), dict):
                raise ValueError("summary document has no dossier object")
            canonical = document["dossier"]
            resolved[key] = {
                "id": summary_id,
                "dossier": {
                    "project_key": result.summary.project_key,
                    "facts": [{"id": item["id"]} for item in canonical["facts"]],
                    "resources": [{"id": item["id"]} for item in canonical["resources"]],
                    "prohibitions": [{"id": item["id"]} for item in canonical["prohibitions"]],
                },
            }
        except (ValueError, yaml.YAMLError, DossierResolutionError) as exc:
            errors.append(str(exc))
    return resolved, errors


def _check(
    cases: list[dict[str, Any]],
    manifest: dict[str, Any],
    dossiers: dict[tuple[str, str, str], dict[str, Any]],
) -> list[str]:
    out: list[str] = []
    cfg = manifest["constraints"]
    ids = [c["id"] for c in cases]
    duplicates = sorted(k for k, n in Counter(ids).items() if n > 1)
    if duplicates:
        out.append(f"duplicate case IDs: {duplicates}")
    if len(cases) < cfg["min_cases"]:
        out.append(f"cases: {len(cases)} < {cfg['min_cases']}")

    response_cases = [c for c in cases if c["case_kind"] == "scout_response"]
    project_minima = (
        ("gateway", "min_gateway_cases"),
        ("zk-extension", "min_zk_extension_cases"),
    )
    for project, field in project_minima:
        count = sum(c["project_key"] == project for c in response_cases)
        if count < cfg[field]:
            out.append(f"{project} response cases: {count} < {cfg[field]}")
    rails = [c for c in cases if c["case_kind"] == "dossier_bad_write"]
    if len(rails) < cfg["min_phase2_rail_cases"]:
        out.append(f"Phase 2 rail cases: {len(rails)} < {cfg['min_phase2_rail_cases']}")

    implication = sum("unsupported_implication" in c.get("tags", []) for c in response_cases)
    pct = 100 * implication / len(response_cases) if response_cases else 0
    if pct < cfg["min_unsupported_implication_pct"]:
        out.append(f"unsupported implication: {implication}/{len(response_cases)} = {pct:.1f}%")

    tags = Counter(tag for c in cases for tag in c["tags"])
    for tag, minimum in cfg["coverage_minima"].items():
        if tags[tag] < minimum:
            out.append(f"coverage {tag}: {tags[tag]} < {minimum}")

    pairs: dict[str, set[str]] = defaultdict(set)
    for case in response_cases:
        if case.get("pair_id"):
            pairs[case["pair_id"]].add(case["resource_state"])
        if "none" in case["required_memory"] and len(case["required_memory"]) != 1:
            out.append(f"{case['id']}: required_memory 'none' must be exclusive")
        if "parent_changes_outcome" in case["tags"]:
            cf = case.get("counterfactual_without_parent")
            counterfactual_outcome = (
                (
                    cf["expected_source_relevant"],
                    cf["expected_posture"],
                )
                if cf
                else None
            )
            expected_outcome = (
                case["expected_source_relevant"],
                case["expected_posture"],
            )
            if cf is None or counterfactual_outcome == expected_outcome:
                out.append(f"{case['id']}: parent counterfactual does not change the outcome")

        summary = dossiers.get(
            (case["project_key"], case["dossier_revision"], case["dossier_summary_id"])
        )
        if not summary:
            continue
        dossier = summary["dossier"]
        if dossier.get("project_key") != case["project_key"]:
            out.append(f"{case['id']}: dossier project mismatch")
        valid = {
            "required_fact_ids": {x["id"] for x in dossier.get("facts", [])},
            "required_resource_ids": {x["id"] for x in dossier.get("resources", [])},
            "required_prohibition_ids": {x["id"] for x in dossier.get("prohibitions", [])},
        }
        for field, allowed in valid.items():
            unknown = sorted(set(case.get(field, [])) - allowed)
            if unknown:
                out.append(f"{case['id']}: unknown {field}: {unknown}")

    complete_pairs = sum(states == {"available", "unavailable"} for states in pairs.values())
    if complete_pairs < cfg["min_complete_resource_pairs"]:
        out.append(
            f"complete resource pairs: {complete_pairs} < {cfg['min_complete_resource_pairs']}"
        )
    return out


def lint_report(corpus: Path, content_repo: Path) -> dict[str, Any]:
    """Return machine-consumable corpus evidence from the canonical checks.

    The audit imports this seam directly so it never tries to infer pass/fail
    status by scraping this script's human console output.
    """
    schema = json.loads((corpus / "case.schema.json").read_text())
    manifest = yaml.safe_load((corpus / "manifest.yaml").read_text())
    cases, errors = _load_cases(corpus, schema)
    dossiers, dossier_errors = _resolve_dossiers(cases, manifest, content_repo)
    errors.extend(dossier_errors)
    violations = [] if errors else _check(cases, manifest, dossiers)
    response_cases = [case for case in cases if case.get("case_kind") == "scout_response"]
    project_distribution = Counter(str(case.get("project_key") or "") for case in response_cases)
    implicature_count = sum(
        "unsupported_implication" in case.get("tags", []) for case in response_cases
    )
    rail_count = sum(case.get("case_kind") == "dossier_bad_write" for case in cases)
    return {
        "ok": not errors and not violations,
        "errors": errors + violations,
        "case_count": len(cases),
        "minimum_cases": int(manifest["constraints"]["min_cases"]),
        "case_ids": sorted(str(case.get("id") or "") for case in cases),
        "project_distribution": dict(sorted(project_distribution.items())),
        "implicature_count": implicature_count,
        "implicature_denominator": len(response_cases),
        "reserved_phase2_rail_count": rail_count,
        "resolvable_pinned_dossiers": len(dossiers),
    }


def lint(corpus: Path, content_repo: Path) -> int:
    report = lint_report(corpus, content_repo)
    if report["errors"]:
        for error in report["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2 if any("schema error" in error for error in report["errors"]) else 1
    print(
        f"Corpus: {report['case_count']} cases; "
        f"{report['resolvable_pinned_dossiers']} pinned dossiers at verified git revisions"
    )
    print("OK")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-dir", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument(
        "--content-repo",
        type=Path,
        default=Path(os.getenv("DOSSIER_SOURCE_REPO", "../dossier-source")),
    )
    args = parser.parse_args()
    if not args.content_repo.exists():
        print(
            "ERROR: --content-repo must name an existing local checkout; lint never clones",
            file=sys.stderr,
        )
        raise SystemExit(2)
    raise SystemExit(lint(args.corpus_dir, args.content_repo))


if __name__ == "__main__":
    main()
