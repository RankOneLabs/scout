import json
from importlib.resources import files

import pytest
from pydantic import ValidationError

from scout.typesafe.compose import DECIDE_REGISTRY
from scout.typesafe.models import Answers, DecisionRecord


def test_registered_decides_match_acceptance_fixture() -> None:
    fixture = json.loads(
        files("scout.typesafe").joinpath("acceptance-cases.json").read_text()
    )
    assert set(DECIDE_REGISTRY) == {case["decide"] for case in fixture["cases"]}
    for case in fixture["cases"]:
        actual = DECIDE_REGISTRY[case["decide"]](Answers.model_validate(case["answers"]))
        assert actual == DecisionRecord.model_validate(case["expected"])


def test_decides_resolve_questions_by_explicit_id() -> None:
    answers = Answers.model_validate(
        {
            "request_id": "request",
            "model": "fixture",
            "answers": {
                "unrelated_probability": {"kind": "probability", "probability": 0.1},
                "unrelated_choice": {
                    "kind": "choice",
                    "probabilities": {"wrong": 1.0},
                    "confidence": 1.0,
                },
                "relevance": {"kind": "probability", "probability": 0.9},
                "account_type": {
                    "kind": "choice",
                    "probabilities": {"individual": 0.8, "unknown": 0.2},
                    "confidence": 0.8,
                },
            },
        }
    )

    gate = DECIDE_REGISTRY["gate_v1"](answers)
    annotation = DECIDE_REGISTRY["account_annotation"](answers)
    assert gate.eligible and gate.account_label == "individual"
    assert annotation.account_label == "individual"


@pytest.mark.parametrize(
    "probabilities",
    [{}, {"negative": -0.1}, {"too_large": 1.1}],
)
def test_choice_probabilities_are_nonempty_and_bounded(
    probabilities: dict[str, float],
) -> None:
    with pytest.raises(ValidationError):
        Answers.model_validate(
            {
                "request_id": "request",
                "model": "fixture",
                "answers": {
                    "account_type": {
                        "kind": "choice",
                        "probabilities": probabilities,
                        "confidence": 1.0,
                    }
                },
            }
        )


def test_answers_rejects_empty_request_id() -> None:
    with pytest.raises(ValidationError):
        Answers.model_validate({"request_id": "", "model": "fixture", "answers": {}})
