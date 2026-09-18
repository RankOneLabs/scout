from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scout.config import Account, Message, SourceParent
from scout.registry import ProjectTarget
from scout.typesafe.catalogue import load_catalogue
from scout.typesafe.compose import DECIDE_REGISTRY
from scout.typesafe.models import Answers, DecisionRecord
from scout.typesafe.state import build_state

ROOT = Path(__file__).parents[1]
CATALOGUE = ROOT / "src/scout/typesafe/catalogues/agent-ops-relevance.v1.yaml"
EVIDENCE = ROOT / "evidence/shadow-relevance-adoption"


def _answers(repeat: dict[str, Any]) -> Answers:
    converted: dict[str, dict[str, Any]] = {}
    for question_id, answer in repeat["answers"].items():
        if answer["type"] == "noul":
            converted[question_id] = {
                "kind": "probability",
                "probability": answer["noul"],
            }
        elif answer["type"] == "choice":
            converted[question_id] = {
                "kind": "choice",
                "probabilities": answer["probabilities"],
                "confidence": answer["confidence"],
            }
        elif answer["type"] == "score":
            converted[question_id] = {
                "kind": "score",
                "levels": [
                    {"level": str(level), "probability": probability}
                    for level, probability in answer["probabilities"].items()
                ],
                "confidence": answer["confidence"],
            }
        else:
            raise AssertionError(f"unexpected answer type: {answer['type']}")
    return Answers.model_validate(
        {
            "request_id": repeat["request_id"],
            "model": repeat["model"],
            "usage": repeat["usage"],
            "latency_ms": repeat["latency_ms"],
            "answers": converted,
        }
    )


def test_adopted_catalogue_is_the_studied_bytes_and_version() -> None:
    checksums = json.loads((EVIDENCE / "checksums.json").read_text())
    report = json.loads((EVIDENCE / "exported-answers.json").read_text())
    assert (
        hashlib.sha256(CATALOGUE.read_bytes()).hexdigest()
        == checksums["files"][
            "src/assay/investigations/relevance/catalogues/agent-ops-relevance.v1.yaml"
        ]
    )
    assert load_catalogue(CATALOGUE).version == checksums["catalogue_version_canonical_json"]
    assert load_catalogue(CATALOGUE).version == report["catalogue_version"]


def test_adopted_state_projection_matches_the_study_shape() -> None:
    message = Message(
        "bluesky",
        "post-1",
        "bluesky",
        "channel",
        Account("bluesky", "author-1", "Ada", "ada.test"),
        "An operational result.",
        datetime(2026, 9, 18, tzinfo=UTC),
        parent=SourceParent(
            "parent-1",
            Account("bluesky", "parent-author", "Grace", "grace.test"),
            "Prior context",
            "https://example.test/parent",
        ),
        url="https://example.test/post",
    )
    project = ProjectTarget("agent-ops", "AgentOperations", "operations", None)
    state = build_state(message, project, load_catalogue(CATALOGUE))
    assert state == {
        "post": {
            "platform": "bluesky",
            "channel": "bluesky",
            "url": "https://example.test/post",
            "text": "An operational result.",
        },
        "parent_context_only": {"author_name": "Grace", "text": "Prior context"},
        "author": {"name": "Ada", "handle": "ada.test"},
        "project": {
            "key": "agent-ops",
            "name": "AgentOperations",
            "description": "operations",
        },
    }


def test_registered_mapping_reproduces_every_study_verdict() -> None:
    catalogue = load_catalogue(CATALOGUE)
    decide = DECIDE_REGISTRY[catalogue.decide]
    report = json.loads((EVIDENCE / "exported-answers.json").read_text())
    mismatches: list[str] = []
    for case in report["cases"]:
        votes = []
        for repeat in case["repeats"]:
            actual = decide(_answers(repeat))
            expected = DecisionRecord.model_validate(repeat["decision"])
            if actual != expected:
                mismatches.append(
                    f"evaluation {case['evaluation_id']} repeat {repeat['repeat_index']}"
                )
            votes.append(actual.eligible)
        if (sum(votes) >= 2) != case["typesafe_decision"]:
            mismatches.append(f"evaluation {case['evaluation_id']} majority")
    assert mismatches == []
    assert report["typesafe_won"] is False
