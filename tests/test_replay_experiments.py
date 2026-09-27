"""Focused replay refusal and classifier identity tests."""

from __future__ import annotations

import pytest

from scout.relevance.classifier_identity import (
    UnknownClassifier,
    classifier_of,
    jev_classifier,
)
from scout.result import Err, Ok


@pytest.mark.parametrize("model", ["jev:jev-latest", "jev:jev-v6"])
def test_classifier_of_known_jev_models(model: str) -> None:
    assert classifier_of(model) == Ok("jev")


@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-4-6",
        "gpt-5-mini",
        "gemini-2.5-flash",
        "openrouter/qwen/qwen3-32b",
    ],
)
def test_classifier_of_known_llm_models(model: str) -> None:
    assert classifier_of(model) == Ok("llm")


def test_classifier_of_unknown_model_is_explicit() -> None:
    model = "unrecognised-relevance-engine"
    assert classifier_of(model) == Err(UnknownClassifier(model))


def test_jev_classifier_builds_namespaced_payload_identity() -> None:
    assert jev_classifier("jev-latest") == "jev:jev-latest"


@pytest.mark.parametrize("model", ["", " leading-space", "bad:model"])
def test_jev_classifier_rejects_invalid_payload_model(model: str) -> None:
    with pytest.raises(ValueError, match="non-empty model identifier"):
        jev_classifier(model)
