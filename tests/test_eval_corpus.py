"""Tests for the evals/phase1 eval corpus structural and distributional constraints."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts.lint_eval_corpus import build_offline_registry  # noqa: E402
from tests.conftest import (  # noqa: E402
    DOSSIER_SOURCE_CONTRACT_MARKER,
    find_dossier_source_checkout,
)

_CORPUS_DIR = Path(__file__).parent.parent / "src" / "scout" / "evals" / "phase1"
_CASES_DIR = _CORPUS_DIR / "cases"
_SCHEMA_PATH = _CORPUS_DIR / "case.schema.json"
_MANIFEST_PATH = _CORPUS_DIR / "manifest.yaml"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text())


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return yaml.safe_load(_MANIFEST_PATH.read_text())


@pytest.fixture(scope="module")
def cases() -> list[dict[str, Any]]:
    return [
        yaml.safe_load(p.read_text()) for p in sorted(_CASES_DIR.glob("*.yaml"))
    ]


@pytest.fixture(scope="module")
def response_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [case for case in cases if case["case_kind"] == "scout_response"]


class TestCorpusStructure:
    def test_manifest_exists(self) -> None:
        assert _MANIFEST_PATH.exists()

    def test_schema_exists(self) -> None:
        assert _SCHEMA_PATH.exists()

    def test_cases_dir_exists(self) -> None:
        assert _CASES_DIR.is_dir()

    def test_at_least_40_case_files(self) -> None:
        count = len(list(_CASES_DIR.glob("*.yaml")))
        assert count >= 40, f"Expected ≥40 case files, found {count}"


class TestCaseSchema:
    def test_all_cases_parse_as_yaml(self) -> None:
        for path in sorted(_CASES_DIR.glob("*.yaml")):
            data = yaml.safe_load(path.read_text())
            assert isinstance(data, dict), f"{path.name} did not parse to a dict"

    def test_all_cases_validate_against_schema(
        self, cases: list[dict[str, Any]], schema: dict[str, Any]
    ) -> None:
        # case.schema.json $refs web/grading_schema.json's $defs by its
        # canonical $id (posture, judgment, dimension, gradePayload) — the
        # offline registry resolves that $ref without any network access.
        validator = jsonschema.Draft202012Validator(schema, registry=build_offline_registry())
        for case in cases:
            validator.validate(case)

    def test_all_case_ids_are_unique(self, cases: list[dict[str, Any]]) -> None:
        ids = [c["id"] for c in cases]
        dups = {id_ for id_ in ids if ids.count(id_) > 1}
        assert not dups, f"Duplicate case IDs: {sorted(dups)}"

    def test_all_cases_have_dossier_revision(self, cases: list[dict[str, Any]]) -> None:
        missing = [c["id"] for c in cases if not c.get("dossier_revision")]
        assert not missing, f"Cases missing dossier_revision: {missing}"


class TestCorpusConstraints:
    def test_at_least_15_gateway_cases(self, cases: list[dict[str, Any]]) -> None:
        gateway = [c for c in cases if c["project_key"] == "gateway"]
        assert len(gateway) >= 15, f"Expected ≥15 gateway cases, got {len(gateway)}"

    def test_at_least_15_zk_extension_cases(self, cases: list[dict[str, Any]]) -> None:
        zk = [c for c in cases if c["project_key"] == "zk-extension"]
        assert len(zk) >= 15, f"Expected ≥15 zk-extension cases, got {len(zk)}"

    def test_at_least_25pct_unsupported_implication(
        self, response_cases: list[dict[str, Any]]
    ) -> None:
        n = len(response_cases)
        ui_cases = [
            c for c in response_cases
            if "unsupported_implication" in c["tags"]
        ]
        pct = 100 * len(ui_cases) / n if n else 0
        assert pct >= 25, (
            f"Expected ≥25% unsupported_implication cases,"
            f" got {pct:.1f}% ({len(ui_cases)}/{n})"
        )


class TestGradeContracts:
    _CAUSAL_KEYS = frozenset({
        "dimensions",
        "failure_note",
        "context_missing_input",
        "posture_should_have_been",
        "factual_offending_claim",
        "factual_disposition",
        "factual_contradicting_evidence",
        "implication_implied_claim",
        "implication_missing_support",
    })

    def test_accept_grades_carry_no_causal_fields(
        self, response_cases: list[dict[str, Any]]
    ) -> None:
        for case in response_cases:
            grade = case["grade"]
            if grade["action_judgment"] == "accept":
                stale = [
                    k for k in self._CAUSAL_KEYS
                    if grade.get(k) is not None
                ]
                assert not stale, (
                    f"Case {case['id']}: accept grade has non-null causal fields: {stale}"
                )

    def test_fail_grades_have_dimensions(self, response_cases: list[dict[str, Any]]) -> None:
        for case in response_cases:
            grade = case["grade"]
            if grade["action_judgment"] == "fail":
                dims = grade.get("dimensions")
                assert dims and len(dims) > 0, (
                    f"Case {case['id']}: fail grade missing dimensions"
                )

    def test_fail_grades_have_failure_note(self, response_cases: list[dict[str, Any]]) -> None:
        for case in response_cases:
            grade = case["grade"]
            if grade["action_judgment"] == "fail":
                assert grade.get("failure_note"), (
                    f"Case {case['id']}: fail grade missing failure_note"
                )

    def test_unsupported_implication_cases_have_causal_fields(
        self, response_cases: list[dict[str, Any]]
    ) -> None:
        for case in response_cases:
            grade = case["grade"]
            if "unsupported_implication" in (grade.get("dimensions") or []):
                assert grade.get("implication_implied_claim"), (
                    f"Case {case['id']}: unsupported_implication missing implication_implied_claim"
                )
                assert grade.get("implication_missing_support"), (
                    f"Case {case['id']}: unsupported_implication missing "
                    "implication_missing_support"
                )

    def test_contextual_understanding_cases_have_missing_input(
        self, response_cases: list[dict[str, Any]]
    ) -> None:
        for case in response_cases:
            grade = case["grade"]
            if "contextual_understanding" in (grade.get("dimensions") or []):
                assert grade.get("context_missing_input"), (
                    f"Case {case['id']}: contextual_understanding missing context_missing_input"
                )

    def test_posture_cases_have_should_have_been(
        self, response_cases: list[dict[str, Any]]
    ) -> None:
        for case in response_cases:
            grade = case["grade"]
            if "posture" in (grade.get("dimensions") or []):
                assert grade.get("posture_should_have_been"), (
                    f"Case {case['id']}: posture dimension missing posture_should_have_been"
                )

    def test_factual_support_cases_have_offending_claim_and_disposition(
        self, response_cases: list[dict[str, Any]]
    ) -> None:
        for case in response_cases:
            grade = case["grade"]
            if "factual_support" in (grade.get("dimensions") or []):
                assert grade.get("factual_offending_claim"), (
                    f"Case {case['id']}: factual_support missing factual_offending_claim"
                )
                assert grade.get("factual_disposition"), (
                    f"Case {case['id']}: factual_support missing factual_disposition"
                )


@getattr(pytest.mark, DOSSIER_SOURCE_CONTRACT_MARKER)
class TestLinterScript:
    def test_linter_exits_clean_on_valid_corpus(
        self, tmp_path: Path, cases: list[dict[str, Any]]
    ) -> None:
        """The linter now calls the real, schema-validating resolve_dossier
        (see scripts/lint_eval_corpus.py), so this test needs a real dossier-source
        checkout to source schemas/*.v1.schema.json from — Scout itself never
        vendors a copy. Skips if no local checkout is found (or fails, under
        DOSSIER_SOURCE_CONFORMANCE_REQUIRED — see tests/conftest.py). A required
        invocation must set DOSSIER_SOURCE_PINNED_CHECKOUT explicitly."""
        checkout = find_dossier_source_checkout()
        if checkout is None:
            pytest.skip(
                "no local dossier-source checkout found "
                "(set DOSSIER_SOURCE_PINNED_CHECKOUT)"
            )

        content_repo = tmp_path / "dossier-source"
        summaries_dir = content_repo / "summaries"
        schemas_dir = content_repo / "schemas"
        summaries_dir.mkdir(parents=True)
        schemas_dir.mkdir(parents=True)
        for schema_file in ("index.v1.schema.json", "summary.v1.schema.json"):
            (schemas_dir / schema_file).write_text((checkout / "schemas" / schema_file).read_text())

        for project_key, summary_id in (
            ("gateway", "gateway-dossier"),
            ("zk-extension", "zk-extension-dossier"),
        ):
            project_cases = [
                case for case in cases if case["project_key"] == project_key
            ]
            dossier = {
                "project_key": project_key,
                "last_reviewed": "2026-07-01",
                "reviewer": {"id": f"{project_key}-reviewer", "display_name": "Test Reviewer"},
                "evidence": [{
                    "id": "evidence-e1", "kind": "test-fixture", "locator": "test",
                    "immutable_ref": "a" * 40,
                }],
                "facts": [
                    {"id": value, "claim": value, "status": "supported",
                     "safe_phrasings": [value], "evidence_ids": ["evidence-e1"]}
                    for value in sorted({
                        value
                        for case in project_cases
                        for value in case.get("required_fact_ids", [])
                    })
                ],
                "resources": [
                    {"id": value, "label": value, "canonical_url": f"https://example.com/{value}",
                     "purpose": value, "evidence_ids": ["evidence-e1"]}
                    for value in sorted({
                        value
                        for case in project_cases
                        for value in case.get("required_resource_ids", [])
                    })
                ],
                "prohibitions": [
                    {
                        "id": value, "exact_phrase": value, "forbidden_phrasings": [value],
                        "evidence_ids": ["evidence-e1"], "resource_ids": [],
                    }
                    for value in sorted({
                        value
                        for case in project_cases
                        for value in case.get("required_prohibition_ids", [])
                    })
                ],
                "known_gaps": [],
            }
            (summaries_dir / f"{summary_id}.yaml").write_text(
                yaml.safe_dump({
                    "id": summary_id, "type": "summary", "topic": project_key,
                    "status": "active", "summary": f"Fixture summary for {project_key}.",
                    "dossier": dossier,
                })
            )

        (content_repo / "index.yaml").write_text(yaml.safe_dump({
            "entries": {
                summary_id: {
                    "path": f"summaries/{summary_id}.yaml",
                    "type": "summary",
                }
                for summary_id in ("gateway-dossier", "zk-extension-dossier")
            },
            "version": "1.0.0",
        }))
        subprocess.run(["git", "init", "-q", str(content_repo)], check=True)
        subprocess.run(
            ["git", "-C", str(content_repo), "config", "user.name", "Scout Tests"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(content_repo), "config", "user.email", "scout-tests@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(content_repo), "config", "commit.gpgsign", "false"],
            check=True,
        )
        subprocess.run(["git", "-C", str(content_repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(content_repo), "commit", "-qm", "fixture dossiers"],
            check=True,
        )
        revision = subprocess.run(
            ["git", "-C", str(content_repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        corpus_dir = tmp_path / "phase1"
        shutil.copytree(_CORPUS_DIR, corpus_dir)
        fixture_manifest = yaml.safe_load((corpus_dir / "manifest.yaml").read_text())
        for dossier_config in fixture_manifest["dossiers"].values():
            dossier_config["revision"] = revision
        (corpus_dir / "manifest.yaml").write_text(
            yaml.safe_dump(fixture_manifest, sort_keys=False)
        )
        for case_path in (corpus_dir / "cases").glob("*.yaml"):
            case = yaml.safe_load(case_path.read_text())
            case["dossier_revision"] = revision
            case_path.write_text(yaml.safe_dump(case, sort_keys=False))

        result = subprocess.run(
            [
                sys.executable,
                "scripts/lint_eval_corpus.py",
                "--corpus-dir",
                str(corpus_dir),
                "--content-repo",
                str(content_repo),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"Linter failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "OK" in result.stdout
