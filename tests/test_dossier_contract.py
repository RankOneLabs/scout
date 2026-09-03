"""Tests for dossier_contract.py: the validate_dossier_semantics facade
entry point and its DossierContractViolation result type.

dossier_contract.py is now a facade over three extracted sub-concerns, each
with its own focused test file: tests/test_dossier_normalization.py
(Unicode normalization), tests/test_portable_regex.py (the portable regex
grammar and compiler), and tests/test_prohibition_matching.py (prohibition
matcher dispatch). validate_dossier_semantics itself stays implemented
directly in the facade (see dossier_contract.py's module docstring), so its
tests stay here.

The full manifest-driven corpus (dossier-source's conformance/v1
normalization-vectors.json, prohibition-vectors.json, and manifest.json
fixtures) runs for real, and is required in CI, via
tests/test_dossier_conformance.py — see docs/dossier-contract.md's
"Running the shared conformance corpus" section. This file keeps direct
unit coverage of validate_dossier_semantics's rule-by-rule behavior on
small hand-built dossiers — a fast, offline complement to the conformance
corpus's producer-fixture parity check.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from scout.dossiers.contract import DossierContractViolation, validate_dossier_semantics


def _valid_dossier(**overrides: Any) -> dict[str, Any]:
    dossier: dict[str, Any] = {
        "project_key": "gateway",
        "last_reviewed": date.today().isoformat(),
        "reviewer": {"id": "reviewer", "display_name": "Reviewer"},
        "evidence": [
            {"id": "ev-1", "kind": "git-blob", "locator": "repository:summary.yaml",
             "immutable_ref": "a" * 40},
        ],
        "facts": [{
            "id": "fact-1", "claim": "Gateway is planned.", "status": "planned",
            "safe_phrasings": ["Gateway is planned."], "evidence_ids": ["ev-1"],
        }],
        "resources": [{
            "id": "res-1", "label": "Gateway", "canonical_url": "https://example.com/gateway",
            "evidence_ids": ["ev-1"],
        }],
        "prohibitions": [{
            "id": "proh-1", "normalized_phrase": "already live",
            "forbidden_phrasings": ["Gateway is already live."],
            "evidence_ids": ["ev-1"], "resource_ids": ["res-1"],
        }],
        "known_gaps": [{
            "id": "gap-1", "question": "What is the timeline?",
            "related_fact_ids": ["fact-1"], "related_resource_ids": ["res-1"],
        }],
    }
    dossier.update(overrides)
    return dossier


def test_validate_dossier_semantics_accepts_valid_dossier() -> None:
    assert validate_dossier_semantics(_valid_dossier()) == ()


def test_rejects_duplicate_id_across_record_types() -> None:
    dossier = _valid_dossier()
    dossier["resources"][0]["id"] = "fact-1"
    violations = validate_dossier_semantics(dossier)
    assert any(v.rule_id == "scout.dossier.duplicate-id" for v in violations)


def test_rejects_future_last_reviewed() -> None:
    dossier = _valid_dossier(last_reviewed=(date.today() + timedelta(days=1)).isoformat())
    violations = validate_dossier_semantics(dossier)
    assert [v.rule_id for v in violations] == ["scout.dossier.future-last-reviewed"]


def test_rejects_broken_evidence_reference() -> None:
    dossier = _valid_dossier()
    dossier["facts"][0]["evidence_ids"] = ["missing-evidence"]
    violations = validate_dossier_semantics(dossier)
    assert [v.rule_id for v in violations] == ["scout.dossier.broken-evidence-reference"]


def test_rejects_broken_resource_reference_in_prohibition() -> None:
    dossier = _valid_dossier()
    dossier["prohibitions"][0]["resource_ids"] = ["missing-resource"]
    violations = validate_dossier_semantics(dossier)
    assert [v.rule_id for v in violations] == ["scout.dossier.broken-resource-reference"]


def test_rejects_broken_gap_fact_reference() -> None:
    dossier = _valid_dossier()
    dossier["known_gaps"][0]["related_fact_ids"] = ["missing-fact"]
    violations = validate_dossier_semantics(dossier)
    assert [v.rule_id for v in violations] == ["scout.dossier.broken-gap-fact-reference"]


def test_rejects_broken_gap_resource_reference() -> None:
    dossier = _valid_dossier()
    dossier["known_gaps"][0]["related_resource_ids"] = ["missing-resource"]
    violations = validate_dossier_semantics(dossier)
    assert [v.rule_id for v in violations] == ["scout.dossier.broken-gap-resource-reference"]


def test_rejects_duplicate_safe_phrasing() -> None:
    dossier = _valid_dossier()
    dossier["facts"].append({
        "id": "fact-2", "claim": "Gateway ships soon.", "status": "planned",
        "safe_phrasings": ["Gateway is planned."], "evidence_ids": ["ev-1"],
    })
    violations = validate_dossier_semantics(dossier)
    assert [v.rule_id for v in violations] == ["scout.dossier.duplicate-safe-phrasing"]


def test_rejects_duplicate_forbidden_phrasing() -> None:
    dossier = _valid_dossier()
    dossier["prohibitions"].append({
        "id": "proh-2", "normalized_phrase": "shipped today",
        "forbidden_phrasings": ["Gateway is already live."],
        "evidence_ids": ["ev-1"], "resource_ids": [],
    })
    violations = validate_dossier_semantics(dossier)
    assert [v.rule_id for v in violations] == ["scout.dossier.duplicate-forbidden-phrasing"]


def test_rejects_duplicate_canonical_url() -> None:
    dossier = _valid_dossier()
    dossier["resources"].append({
        "id": "res-2", "label": "Gateway Mirror", "canonical_url": "https://example.com/gateway",
        "evidence_ids": ["ev-1"],
    })
    violations = validate_dossier_semantics(dossier)
    assert [v.rule_id for v in violations] == ["scout.dossier.duplicate-canonical-url"]


def test_rejects_nonportable_regex() -> None:
    dossier = _valid_dossier()
    dossier["prohibitions"][0] = {
        "id": "proh-bad-regex", "regex": "\\d+ uptime", "flags": "i",
        "forbidden_phrasings": ["100% uptime"], "evidence_ids": ["ev-1"], "resource_ids": [],
    }
    violations = validate_dossier_semantics(dossier)
    assert [v.rule_id for v in violations] == ["scout.dossiers.resolver.nonportable-regex"]


def test_violations_are_ordered_and_all_reported() -> None:
    """Every violation is reported (not just the first), in a fixed,
    field-order-driven sequence — not an unordered set."""
    dossier = _valid_dossier(last_reviewed=(date.today() + timedelta(days=1)).isoformat())
    dossier["resources"].append({
        "id": "fact-1", "label": "Duplicate", "canonical_url": "https://example.com/other",
        "evidence_ids": ["ev-1"],
    })
    violations = validate_dossier_semantics(dossier)
    assert isinstance(violations, tuple)
    assert [v.rule_id for v in violations] == [
        "scout.dossier.future-last-reviewed",
        "scout.dossier.duplicate-id",
    ]
    assert all(
        isinstance(v, DossierContractViolation) and v.stage == "semantic" for v in violations
    )
