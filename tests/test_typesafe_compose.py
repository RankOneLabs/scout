import json
from pathlib import Path

from scout.typesafe.compose import DECIDE_REGISTRY
from scout.typesafe.models import Answers, DecisionRecord


def test_registered_decides_match_acceptance_fixture() -> None:
    fixture = json.loads(Path("tests/fixtures/typesafe/acceptance-cases.json").read_text())
    assert set(DECIDE_REGISTRY) == {case["decide"] for case in fixture["cases"]}
    for case in fixture["cases"]:
        actual = DECIDE_REGISTRY[case["decide"]](Answers.model_validate(case["answers"]))
        assert actual == DecisionRecord.model_validate(case["expected"])
