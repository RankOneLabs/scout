"""Shape guards over the redistributable JEV fixtures.

These are not parity tests. Parity needs the pinned authoritative source and
its ported route tests, and is blocked; see docs/relevance-holdouts.md. What
these hold is the shape the port has to satisfy when it arrives: the routed
feature names, the exclusion prefix rule, the state projection, the criteria
keys that a careless YAML load destroys, and fixture cases that cover every
routing outcome.

The fixtures carry invented content only. The real catalogue is private and is
not in this repository.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

CATALOGUE = Path("tests/fixtures/relevance/routed-features.fixture.yaml")
ANSWERS = Path("tests/fixtures/relevance/routed-answers.fixture.json")

EXCLUSION_PREFIX = "excl_"
#: The features the router reads besides the exclusions.
ROUTED = ("needs_thread", "answerable_from_post", "about_agent_work", "points_somewhere")
ACTIONS = frozenset({"respond", "review", "drop"})
LINES = frozenset({"exclusion", "needs_thread", "respond", "points_somewhere", "otherwise"})
#: The projection the authoritative build_state emits, by group.
PROJECTION = {
    "post": ["platform", "channel", "url", "text"],
    "parent_context_only": ["author_name", "text"],
    "author": ["name", "handle"],
    "project": ["key", "name", "description"],
}


class _LiteralKeyLoader(yaml.SafeLoader):
    """YAML 1.2-style booleans: ``true``/``false`` criteria keys stay strings."""


_LiteralKeyLoader.yaml_implicit_resolvers = {
    key: [item for item in values if item[0] != "tag:yaml.org,2002:bool"]
    for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _catalogue() -> dict[str, Any]:
    return yaml.load(CATALOGUE.read_text(encoding="utf-8"), Loader=_LiteralKeyLoader)


def _cases() -> list[dict[str, Any]]:
    return json.loads(ANSWERS.read_text(encoding="utf-8"))["cases"]


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
    """The trap the port's loader has to avoid, held so it cannot be forgotten."""
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
def test_case_expectation_is_a_well_formed_decision(case: dict[str, Any]) -> None:
    expected = case["expected"]
    questions = set(_catalogue()["questions"])
    assert expected["action"] in ACTIONS
    assert expected["path_action"] in ACTIONS
    assert expected["line"] in LINES
    assert set(expected["margin"]) <= questions
    assert set(expected["features"]) == questions


@pytest.mark.parametrize("case", _cases(), ids=lambda case: str(case["name"]))
def test_case_exclusion_names_a_declared_exclusion(case: dict[str, Any]) -> None:
    exclusion = case["expected"]["exclusion"]
    assert exclusion is None or f"{EXCLUSION_PREFIX}{exclusion}" in _catalogue()["questions"]


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
