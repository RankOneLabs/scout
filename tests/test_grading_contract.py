"""Cross-runtime grading contract conformance.

Loads tests/fixtures/grading_contract_cases.json — the same
language-neutral fixture matrix web/__tests__/grading-contract.test.ts
loads on the TypeScript side — and asserts that the shared
web/grading_schema.json artifact produces the expected valid/invalid
verdict for each entry via Python's jsonschema.Draft202012Validator.

Verdict parity across runtimes is the contract; diagnostic wording is not
(see decision 14) — these tests check booleans only. A schema-compilation
smoke test also fails fast if web/grading_schema.json ever regresses to
an unsupported or malformed Draft 2020-12 construct.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "grading_contract_cases.json"
_SCHEMA_PATH = Path(__file__).parent.parent / "web" / "grading_schema.json"


def _load_fixture() -> list[dict[str, Any]]:
    return json.loads(_FIXTURE_PATH.read_text())["cases"]


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text())


@pytest.fixture(scope="module")
def validator(schema: dict[str, Any]) -> Draft202012Validator:
    return Draft202012Validator(schema)


class TestSchemaCompiles:
    def test_schema_is_valid_draft_2020_12(self, schema: dict[str, Any]) -> None:
        """Fails on any unsupported or malformed Draft 2020-12 construct."""
        Draft202012Validator.check_schema(schema)

    def test_schema_has_stable_canonical_id(self, schema: dict[str, Any]) -> None:
        assert schema.get("$id"), "grading_schema.json must declare a canonical $id"

    def test_schema_defines_required_defs(self, schema: dict[str, Any]) -> None:
        defs = schema.get("$defs") or {}
        for name in (
            "relevanceJudgment",
            "actionJudgment",
            "failureDimension",
            "posture",
            "factualDisposition",
            "nonBlankString",
            "gradePayload",
            "validationEnvelope",
        ):
            assert name in defs, f"missing $defs/{name}"


@pytest.mark.parametrize(
    "case",
    _load_fixture(),
    ids=lambda case: case["name"],
)
def test_fixture_matrix_verdict(
    case: dict[str, Any], validator: Draft202012Validator
) -> None:
    errors = list(validator.iter_errors(case["envelope"]))
    actual_valid = not errors
    assert actual_valid == case["valid"], (
        f"{case['name']}: expected valid={case['valid']}, got valid={actual_valid} "
        f"(errors: {[e.message for e in errors][:3]})"
    )


class TestFixtureCoverage:
    """Sanity checks on the fixture matrix itself so a coverage gap can't
    silently slip through — see decision 13's required coverage list."""

    def test_fixture_has_unique_names(self) -> None:
        names = [case["name"] for case in _load_fixture()]
        assert len(names) == len(set(names))

    def test_fixture_covers_valid_and_invalid(self) -> None:
        cases = _load_fixture()
        assert any(c["valid"] for c in cases)
        assert any(not c["valid"] for c in cases)

    def test_fixture_covers_all_four_equal_posture_branches(self) -> None:
        cases = {c["name"] for c in _load_fixture()}
        for posture in ("answer", "engage", "ask", "abstain"):
            assert f"invalid_equal_posture_{posture}" in cases

    def test_fixture_covers_contradicted_without_evidence(self) -> None:
        cases = {c["name"] for c in _load_fixture()}
        assert "invalid_factual_support_contradicted_without_evidence_T004" in cases

    def test_fixture_covers_unknown_field(self) -> None:
        tags = {tag for c in _load_fixture() for tag in c["tags"]}
        assert "unknown-field" in tags

    def test_fixture_covers_edited_text_null_on_accept(self) -> None:
        cases = {c["name"] for c in _load_fixture()}
        assert "invalid_accept_with_edited_text" in cases

    def test_fixture_covers_edited_text_satisfying_fail_without_failure_note(self) -> None:
        cases = {c["name"] for c in _load_fixture()}
        assert "valid_fail_edited_text_only_no_failure_note" in cases

    def test_fixture_covers_causal_detail_satisfying_fail_without_failure_note(self) -> None:
        cases = {c["name"] for c in _load_fixture()}
        assert "valid_fail_causal_detail_only_no_failure_note" in cases


class TestValidateGradeSourceGuard:
    """decision (scout-grade-contract): validate_grade_envelope is the sole
    ordinary grade contract entry point. A bare ``validate_grade(`` call or
    definition anywhere in production code (outside this guard and its own
    tests) means a second, drift-prone contract has crept back in."""

    _SCANNED = (
        "src/scout/config.py",
        "src/scout/grading/service.py",
        "src/scout/storage/state.py",
        "src/scout/evals/phase1/grader.py",
        "src/scout/evals/phase1/loader.py",
        "src/scout/evals/phase1/export_adapter.py",
    )
    _PATTERN = re.compile(r"\bvalidate_grade\s*\(")

    def test_no_production_file_calls_or_defines_validate_grade(self) -> None:
        repo_root = Path(__file__).parent.parent
        offending = []
        for rel in self._SCANNED:
            text = (repo_root / rel).read_text()
            for line in text.splitlines():
                if self._PATTERN.search(line) and "validate_grade_envelope" not in line:
                    offending.append(f"{rel}: {line.strip()}")
        assert not offending, (
            "validate_grade must not be called or defined in production code; "
            f"found: {offending}"
        )
