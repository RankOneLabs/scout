"""Focused replay refusal and classifier identity tests."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import scout.replay.experiments as experiments
from scout.relevance.classifier_identity import (
    UnknownClassifier,
    classifier_of,
    zeroshot_classifier,
)
from scout.result import Err, Ok


@pytest.mark.parametrize("model", ["zeroshot:jev-latest", "zeroshot:jev-v6"])
def test_classifier_of_known_zeroshot_models(model: str) -> None:
    assert classifier_of(model) == Ok("zeroshot")


@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-4-6",
        "gpt-5-mini",
        "gemini-2.5-flash",
        "openrouter/qwen/qwen3-32b",
        "dispatch/private-relevance",
        "ollama/qwen3:32b",
    ],
)
def test_classifier_of_known_llm_models(model: str) -> None:
    assert classifier_of(model) == Ok("llm")


def test_classifier_of_unknown_model_is_explicit() -> None:
    model = "unrecognised-relevance-engine"
    assert classifier_of(model) == Err(UnknownClassifier(model))


def test_zeroshot_classifier_builds_namespaced_payload_identity() -> None:
    assert zeroshot_classifier("jev-latest") == "zeroshot:jev-latest"


@pytest.mark.parametrize("model", ["", " leading-space", "bad:model"])
def test_zeroshot_classifier_rejects_invalid_payload_model(model: str) -> None:
    with pytest.raises(ValueError, match="non-empty model identifier"):
        zeroshot_classifier(model)


async def test_resolve_baseline_refuses_zeroshot_phase_run() -> None:
    phase_run = {
        "id": 17,
        "status": "complete",
        "phase": "relevance",
        "model": "zeroshot:jev-latest",
    }
    state = SimpleNamespace(
        db=SimpleNamespace(read_transaction=nullcontext),
        get_phase_run=lambda _phase_run_id: phase_run,
    )
    tracer = AsyncMock()

    with pytest.raises(experiments.BaselineResolutionError, match="produced by zero-shot"):
        await experiments.resolve_baseline(state, tracer, 17)
    tracer.get_trace.assert_not_awaited()
