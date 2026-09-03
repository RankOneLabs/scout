"""Shared conformance corpus runner: proves Scout's schema validation,
semantic checks, canonical normalization, portable regex grammar, and
prohibition matching agree with the upstream producer on the exact same
fixtures — not merely similarly-named local cases.

Reads dossier-source/conformance/v1/manifest.json from a dossier-source checkout
(never a copy committed into Scout) and runs every declared fixture against
jsonschema Draft202012Validator (schema stage) plus
``dossier_contract.validate_dossier_semantics`` (semantic stage) — the same
production helper ``dossier.py`` uses while resolving dossiers — plus
``dossier_contract``'s canonical_normalize, portable regex parser, and
matcher.

Corpus discovery and the pinned/candidate/required-mode contract are
implemented in tests/conftest.py (``find_dossier_source_checkout``,
``verify_dossier_source_checkout``) and shared with
``test_eval_corpus.py::TestLinterScript`` and
``test_phase1_eval_runner.py::TestHermeticPhase1Sweep``. See
docs/dossier-contract.md's "Running the shared conformance corpus" section
for the full contract. If no checkout is found, every test in this module
is skipped with a clear message (or fails, under
DOSSIER_SOURCE_CONFORMANCE_REQUIRED).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from scout.dossiers.contract import (
    PortableRegexError,
    canonical_normalize,
    parse_portable_regex,
    prohibition_matches,
    validate_dossier_semantics,
)
from tests.conftest import (
    DOSSIER_SOURCE_CONTRACT_MARKER,
    find_dossier_source_checkout,
    load_dossier_source_pin,
    verify_dossier_source_checkout,
)

_PIN = load_dossier_source_pin()
_CHECKOUT = find_dossier_source_checkout()
_CORPUS_DIR = _CHECKOUT / _PIN["corpus_path"] if _CHECKOUT is not None else None

pytestmark = [
    getattr(pytest.mark, DOSSIER_SOURCE_CONTRACT_MARKER),
    pytest.mark.skipif(
        _CHECKOUT is None,
        reason="no local dossier-source checkout found (set DOSSIER_SOURCE_PINNED_CHECKOUT)",
    ),
]


# ---------------------------------------------------------------------------
# Schema-stage validation
# ---------------------------------------------------------------------------

_MATCHER_KEYS = frozenset({"exact_phrase", "normalized_phrase", "regex"})


def _build_registry(checkout: Path) -> tuple[dict[str, Any], dict[str, Any], Registry[Any]]:
    index_schema = json.loads((checkout / _PIN["index_schema_path"]).read_text())
    summary_schema = json.loads((checkout / _PIN["summary_schema_path"]).read_text())
    registry: Registry[Any] = Registry()
    for schema in (index_schema, summary_schema):
        resource = Resource.from_contents(schema, default_specification=DRAFT202012)
        registry = registry.with_resource(schema["$id"], resource)
    return index_schema, summary_schema, registry


def _classify_schema_rule(document: dict[str, Any], errors: list[ValidationError]) -> set[str]:
    """Map jsonschema errors to the manifest's stable rule identifiers.

    Most rules are a direct (keyword, path-pattern) match. The prohibition
    oneOf branch is special: both matcher-exclusivity and flags-coupling
    surface as a single top-level ``oneOf`` failure at ``/prohibitions/{i}``
    under Python's jsonschema (unlike AJV, which synthesizes a distinct
    ``false schema`` sub-error keyed to the offending property), so those
    two are disambiguated by inspecting the instance directly.
    """
    rules: set[str] = set()
    for error in errors:
        path = "/" + "/".join(str(p) for p in error.absolute_path)
        keyword = error.validator
        if keyword == "oneOf" and re.search(r"/prohibitions/\d+$", path):
            index = int(re.search(r"/prohibitions/(\d+)$", path).group(1))  # type: ignore[union-attr]
            prohibition = document["dossier"]["prohibitions"][index]
            present_matchers = _MATCHER_KEYS & set(prohibition)
            if len(present_matchers) != 1:
                rules.add("scout.dossiers.resolver.prohibition.matcher-exclusivity")
            elif "flags" in prohibition and "regex" not in prohibition:
                rules.add("scout.dossiers.resolver.prohibition.flags-coupling")
            else:
                rules.add("scout.dossiers.resolver.prohibition.matcher-exclusivity")
        elif keyword == "enum" and path.endswith("/flags"):
            rules.add("scout.dossiers.resolver.prohibition.flags-enum")
        elif keyword == "type" and re.search(r"/(exact_phrase|normalized_phrase|regex)$", path):
            rules.add("scout.dossiers.resolver.prohibition.matcher-type")
        elif keyword == "pattern" and path.endswith("/immutable_ref"):
            rules.add("scout.dossiers.resolver.evidence.immutable-ref-pattern")
        elif keyword == "enum" and re.search(r"/facts/\d+/status$", path):
            rules.add("scout.dossiers.resolver.fact.status-enum")
        elif keyword == "additionalProperties" and path == "/dossier":
            rules.add("scout.dossiers.resolver.additional-properties")
        elif keyword == "pattern" and path.endswith("/canonical_url"):
            rules.add("scout.dossiers.resolver.resource.canonical-url-pattern")
    return rules


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def checkout() -> Path:
    assert _CHECKOUT is not None
    return _CHECKOUT


@pytest.fixture(scope="module")
def corpus_dir() -> Path:
    assert _CORPUS_DIR is not None
    return _CORPUS_DIR


@pytest.fixture(scope="module")
def manifest(corpus_dir: Path) -> dict[str, Any]:
    return json.loads((corpus_dir / "manifest.json").read_text())


@pytest.fixture(scope="module")
def schemas(checkout: Path) -> tuple[dict[str, Any], dict[str, Any], Registry[Any]]:
    return _build_registry(checkout)


def test_checkout_head_matches_pinned_revision(checkout: Path) -> None:
    """CI must have checked out the exact revision the active source mode
    requires (the pin's revision in pinned mode, DOSSIER_SOURCE_CANDIDATE_SHA in
    candidate mode), not a moving branch — assert this, and the rest of the
    preflight (manifest/schemas present and non-empty), before any
    fixture-driven test runs."""
    problems = verify_dossier_source_checkout(checkout)
    assert not problems, "\n".join(problems)


class TestManifestFixtures:
    def test_fixtures(
        self,
        corpus_dir: Path,
        manifest: dict[str, Any],
        schemas: tuple[dict[str, Any], dict[str, Any], Registry[Any]],
    ) -> None:
        index_schema, summary_schema, registry = schemas
        index_validator = Draft202012Validator(index_schema, registry=registry)
        summary_validator = Draft202012Validator(summary_schema, registry=registry)

        failures: list[str] = []
        for fixture in manifest["fixtures"]:
            document = yaml.safe_load((corpus_dir / fixture["file"]).read_text())
            expected = fixture["expected"]

            fid = fixture["id"]
            if fixture["kind"] == "index":
                errors = list(index_validator.iter_errors(document))
                valid = not errors
                if valid != expected["valid"]:
                    failures.append(f"{fid}: expected valid={expected['valid']}, got {valid}")
                continue

            schema_errors = list(summary_validator.iter_errors(document))
            if schema_errors:
                if expected["valid"]:
                    failures.append(f"{fid}: expected valid, got schema errors {schema_errors}")
                    continue
                if expected["failingStage"] != "schema":
                    failures.append(f"{fid}: expected {expected['failingStage']} stage, got schema")
                    continue
                rules = _classify_schema_rule(document, schema_errors)
                if expected["rule"] not in rules:
                    failures.append(f"{fid}: expected rule {expected['rule']!r}, got {rules}")
                continue

            dossier = document.get("dossier")
            violations = validate_dossier_semantics(dossier) if dossier is not None else ()
            if expected["valid"]:
                if violations:
                    failures.append(f"{fid}: expected valid, got semantic violations {violations}")
                continue
            if not violations:
                failures.append(f"{fid}: expected invalid ({expected['rule']}), got no violations")
                continue
            if expected["failingStage"] != "semantic":
                failures.append(f"{fid}: expected {expected['failingStage']} stage, got semantic")
                continue
            rules = {v.rule_id for v in violations}
            if expected["rule"] not in rules:
                failures.append(f"{fid}: expected rule {expected['rule']!r}, got {rules}")

        assert not failures, "\n".join(failures)


class TestNormalizationVectors:
    def test_vectors(self, corpus_dir: Path, manifest: dict[str, Any]) -> None:
        data = json.loads((corpus_dir / manifest["normalizationVectors"]).read_text())
        failures = [
            f"{v['id']}: expected {v['expected']!r}, got {canonical_normalize(v['input'])!r}"
            for v in data["vectors"]
            if canonical_normalize(v["input"]) != v["expected"]
        ]
        assert not failures, "\n".join(failures)


class TestProhibitionVectors:
    def test_regex_grammar_accept_reject(self, corpus_dir: Path, manifest: dict[str, Any]) -> None:
        data = json.loads((corpus_dir / manifest["prohibitionVectors"]).read_text())
        failures = []
        for v in data["regexGrammar"]:
            accepted = True
            try:
                parse_portable_regex(v["pattern"])
            except PortableRegexError:
                accepted = False
            if accepted != v["accepted"]:
                failures.append(f"{v['id']}: expected accepted={v['accepted']}, got {accepted}")
        assert not failures, "\n".join(failures)

    def test_matcher_verdicts(self, corpus_dir: Path, manifest: dict[str, Any]) -> None:
        data = json.loads((corpus_dir / manifest["prohibitionVectors"]).read_text())
        failures = []
        for v in data["matchVectors"]:
            matcher = v["matcher"]
            actual = prohibition_matches(
                matcher["type"], matcher["pattern"], matcher.get("flags", ""), v["subject"]
            )
            if actual != v["expected"]:
                failures.append(f"{v['id']}: expected {v['expected']}, got {actual}")
        assert not failures, "\n".join(failures)
