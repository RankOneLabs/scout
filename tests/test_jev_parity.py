"""The ported route tests, and the redistributable fixtures run through them.

Two things live here. The first is the seven route tests from the pinned
authoritative revision, ported across: same cases, same invented exclusion
names, adapted only to the port's ``Result``/``RouteDecision`` return. The
second runs Scout's fixtures through the real router and holds each case's
decision against the expectation recorded beside it.

Those expectations were produced by running the authoritative ``route()`` at
the pinned revision, so a disagreement here is the port diverging from the
source rather than a restatement of documentation disagreeing with itself.

This is not the parity gate. Parity with the graded runs is established once,
operationally, against the private catalogue before JEV is enabled — see
docs/relevance-holdouts.md. The fixtures here carry invented content only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import validate as validate_schema

from scout.result import Err, Ok
from scout.scanning.jev_catalogue import load_jev_catalogue
from scout.scanning.jev_router import EXCLUSION_PREFIX, ROUTED, RouteDecision, route

CATALOGUE = Path("tests/fixtures/relevance/routed-features.fixture.yaml")
ANSWERS = Path("tests/fixtures/relevance/routed-answers.fixture.json")
INTERCHANGE = Path("tests/fixtures/relevance/interchange.fixture.json")

#: The authoritative source these expectations were produced at. Pinned here
#: as well as in the fixture so parity evidence names one revision, and so a
#: fixture re-recorded against a different revision has to say so in both
#: places rather than one.
AUTHORITATIVE = {
    "repository": "RankOneLabs/assay",
    "revision": "282479cdc877d1d470840684e8cbea891f9c54d3",
    "path": "experiments/typesafe_relevance/route.py",
}

#: SHA-256 of each fixture, so a parity claim names the exact bytes it was
#: made against. Editing a fixture without re-recording its expectations
#: against the pinned revision fails here rather than silently changing what
#: "parity" refers to.
FIXTURE_DIGESTS = {
    CATALOGUE: "f629925d8c88b75e560fa60f0ae1d725d33b5da6b92290f5b4df7bf47688a83c",
    ANSWERS: "445ba7d2b4cb0cfb78123a16c779898cf6da59e93c223012551dbb5f4726d413",
    INTERCHANGE: "af18d2c56078421ee73297eea117090d28c79bec802b473db5f2f3463e3888c7",
}

ACTIONS = frozenset({"respond", "review", "drop"})
LINES = frozenset({"exclusion", "needs_thread", "respond", "points_somewhere", "otherwise"})
#: The projection the authoritative build_state emits, by group.
PROJECTION = {
    "post": ["platform", "channel", "url", "text"],
    "parent_context_only": ["author_name", "text"],
    "author": ["name", "handle"],
    "project": ["key", "name", "description"],
}


# --------------------------------------------------------------------------
# The seven route tests, ported from the pinned revision
# --------------------------------------------------------------------------


def _answers(**values: float) -> dict[str, float]:
    base = {"excl_hype": 0.0, "excl_hardware": 0.0} | dict.fromkeys(ROUTED, 0.0)
    return base | values


def _decide(**values: float) -> RouteDecision:
    decision = route(_answers(**values))
    assert isinstance(decision, Ok), decision
    return decision.value


def test_exclusion_drops_first() -> None:
    decision = _decide(excl_hype=0.9, answerable_from_post=0.9, about_agent_work=0.9)
    assert (decision.action, decision.exclusion) == ("drop", "hype")


def test_any_excl_answer_is_an_exclusion() -> None:
    decision = _decide(excl_benchmark=0.9, answerable_from_post=0.9, about_agent_work=0.9)
    assert (decision.action, decision.exclusion) == ("drop", "benchmark")


def test_needs_thread_reviews_before_respond() -> None:
    decision = _decide(needs_thread=0.9, answerable_from_post=0.9, about_agent_work=0.9)
    assert (decision.action, decision.line) == ("review", "needs_thread")


def test_respond_needs_both_answerable_and_about() -> None:
    assert _decide(answerable_from_post=0.9, about_agent_work=0.9).action == "respond"
    assert _decide(answerable_from_post=0.9, about_agent_work=0.1).action == "drop"


def test_pointer_reviews_and_nothing_drops() -> None:
    assert _decide(points_somewhere=0.9).line == "points_somewhere"
    assert _decide().action == "drop"


def test_margin_overrides_the_path() -> None:
    decision = _decide(answerable_from_post=0.9, about_agent_work=0.55)
    assert (decision.path_action, decision.action) == ("respond", "review")
    assert decision.margin == ("about_agent_work",)


def test_margin_only_counts_consulted_features() -> None:
    decision = _decide(excl_hardware=0.9, points_somewhere=0.5)
    assert decision.action == "drop"


# --------------------------------------------------------------------------
# Rejections: an incomplete answer vector never becomes a silent negative
# --------------------------------------------------------------------------


def test_an_answer_vector_with_no_exclusion_is_rejected() -> None:
    result = route(dict.fromkeys(ROUTED, 0.0))
    assert isinstance(result, Err)
    assert result.error.detail == "no excl_* answers"


def test_a_missing_routed_feature_is_rejected() -> None:
    answers = _answers()
    del answers["about_agent_work"]
    result = route(answers)
    assert isinstance(result, Err)
    assert "about_agent_work" in result.error.detail


# --------------------------------------------------------------------------
# What this evidence is anchored to
# --------------------------------------------------------------------------


def test_the_recorded_expectations_name_the_authoritative_source() -> None:
    recorded = json.loads(ANSWERS.read_text(encoding="utf-8"))["expected_source"]
    assert {key: recorded[key] for key in AUTHORITATIVE} == AUTHORITATIVE


@pytest.mark.parametrize("path", list(FIXTURE_DIGESTS), ids=lambda path: path.name)
def test_each_fixture_matches_the_digest_the_evidence_cites(path: Path) -> None:
    assert hashlib.sha256(path.read_bytes()).hexdigest() == FIXTURE_DIGESTS[path]


def test_the_catalogue_fixture_cannot_be_mistaken_for_the_private_one() -> None:
    """Scout is public. The redistributable fixture says so about itself."""
    catalogue = _catalogue()
    assert catalogue["id"] == "routed-features-fixture"
    exclusions = {
        name for name in catalogue["questions"] if name.startswith(EXCLUSION_PREFIX)
    }
    assert exclusions == {"excl_recipe", "excl_weather", "excl_sports_score"}


def test_no_credential_appears_in_the_redistributable_fixtures() -> None:
    """A tracked fixture carries no bearer token, key or endpoint secret."""
    for path in FIXTURE_DIGESTS:
        text = path.read_text(encoding="utf-8").lower()
        assert "bearer " not in text
        assert "typesafe_api_key" not in text
        assert "authorization" not in text


# --------------------------------------------------------------------------
# The fixtures, through the real loader and router
# --------------------------------------------------------------------------


def _catalogue() -> dict[str, Any]:
    loaded = load_jev_catalogue(CATALOGUE)
    assert isinstance(loaded, Ok), loaded
    return dict(loaded.value.document)


def _cases() -> list[dict[str, Any]]:
    return json.loads(ANSWERS.read_text(encoding="utf-8"))["cases"]


def test_synthetic_assay_interchange_matches_the_recorded_shapes() -> None:
    fixture = json.loads(INTERCHANGE.read_text(encoding="utf-8"))
    for name, schema_name in (
        ("packet", "assay-packet.v1.schema.json"),
        ("labels", "assay-labels.v3.schema.json"),
        ("key", "assay-key.v2.schema.json"),
    ):
        schema = json.loads(
            (Path("contracts/relevance") / schema_name).read_text(encoding="utf-8")
        )
        validate_schema(fixture[name], schema)
    assert fixture["packet"]["digest"] == fixture["labels"]["packet_digest"]
    assert fixture["packet"]["digest"] == fixture["key"]["digest"]
    assert fixture["labels"]["cases"][0]["case_id"] == fixture["key"]["cases"][0]["case_id"]


def _probabilities(case: dict[str, Any]) -> dict[str, float]:
    return {name: float(answer["noul"]) for name, answer in case["answers"].items()}


def test_catalogue_declares_every_required_top_level_key() -> None:
    assert set(_catalogue()) == {"id", "decide", "description", "state", "questions"}


def test_catalogue_state_matches_the_authoritative_projection() -> None:
    assert _catalogue()["state"] == PROJECTION


def test_catalogue_questions_are_a_mapping_keyed_by_name() -> None:
    assert isinstance(_catalogue()["questions"], dict)


def test_catalogue_declares_every_routed_feature() -> None:
    assert set(ROUTED) <= set(_catalogue()["questions"])


def test_catalogue_declares_at_least_one_exclusion() -> None:
    names = _catalogue()["questions"]
    assert [name for name in names if name.startswith(EXCLUSION_PREFIX)]


def test_every_question_is_a_noul_feature() -> None:
    questions = _catalogue()["questions"]
    assert {question["type"] for question in questions.values()} == {"noul"}


def test_criteria_keys_survive_as_literal_strings() -> None:
    questions = _catalogue()["questions"]
    assert {key for question in questions.values() for key in question["criteria"]} == {
        "true",
        "false",
    }


def test_a_plain_safe_load_destroys_the_criteria_keys() -> None:
    """The trap the loader avoids, held so it cannot be reintroduced."""
    questions = yaml.safe_load(CATALOGUE.read_text(encoding="utf-8"))["questions"]
    assert {key for question in questions.values() for key in question["criteria"]} == {
        True,
        False,
    }


@pytest.mark.parametrize("case", _cases(), ids=lambda case: str(case["name"]))
def test_case_answers_cover_every_catalogue_question(case: dict[str, Any]) -> None:
    assert set(case["answers"]) == set(_catalogue()["questions"])


@pytest.mark.parametrize("case", _cases(), ids=lambda case: str(case["name"]))
def test_case_answers_are_noul_probabilities(case: dict[str, Any]) -> None:
    answers = case["answers"].values()
    assert all(
        answer["type"] == "noul" and 0.0 <= float(answer["noul"]) <= 1.0 for answer in answers
    )


@pytest.mark.parametrize("case", _cases(), ids=lambda case: str(case["name"]))
def test_case_routes_to_its_recorded_decision(case: dict[str, Any]) -> None:
    """The port's decision, against the authoritative router's recorded output."""
    decided = route(_probabilities(case))
    assert isinstance(decided, Ok), decided
    expected = case["expected"]
    assert decided.value.as_record() | {"router_version": ""} == {
        "action": expected["action"],
        "path_action": expected["path_action"],
        "line": expected["line"],
        "margin": expected["margin"],
        "exclusion": expected["exclusion"],
        "features": expected["features"],
        "router_version": "",
    }


def test_cases_cover_every_action() -> None:
    assert {case["expected"]["action"] for case in _cases()} == ACTIONS


def test_cases_cover_every_deciding_line() -> None:
    assert {case["expected"]["line"] for case in _cases()} == LINES


def test_one_case_has_the_margin_override_the_path() -> None:
    overridden = [
        case
        for case in _cases()
        if case["expected"]["action"] != case["expected"]["path_action"]
    ]
    assert [case["expected"]["margin"] for case in overridden] == [["about_agent_work"]]
