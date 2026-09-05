from __future__ import annotations

import pytest

from scout.model_identity import (
    ModelDeveloper,
    ModelFamily,
    ModelId,
    ModelIdentity,
    ModelRoute,
    check_model_diversity,
    resolve_model_identity,
)
from scout.result import Err, Ok


@pytest.mark.parametrize(("model", "route", "developer", "family"), [
    ("claude-sonnet-4-6", "anthropic", "anthropic", "claude"),
    ("gpt-5-mini", "openai", "openai", "gpt"),
    ("chatgpt-4o-latest", "openai", "openai", "gpt"),
    ("o3", "openai", "openai", "o-series"),
    ("o4-mini", "openai", "openai", "o-series"),
    ("gemini-2.5-flash", "google", "google", "gemini"),
    ("openrouter/anthropic/claude-sonnet-4.6", "openrouter", "anthropic", "claude"),
    ("openrouter/openai/gpt-5-mini", "openrouter", "openai", "gpt"),
    ("openrouter/openai/o3", "openrouter", "openai", "o-series"),
    ("openrouter/google/gemini-2.5-flash", "openrouter", "google", "gemini"),
    ("openrouter/moonshotai/kimi-k2", "openrouter", "moonshotai", "kimi"),
    ("openrouter/moonshotai/kimi-k2-thinking", "openrouter", "moonshotai", "kimi"),
    ("openrouter/qwen/qwen3-235b-a22b", "openrouter", "qwen", "qwen"),
    ("openrouter/qwen/qwen-2.5-72b-instruct", "openrouter", "qwen", "qwen"),
    ("openrouter/qwen/qwen3-32b:free", "openrouter", "qwen", "qwen"),
])
def test_resolves_route_and_family_separately(
    model: str, route: ModelRoute, developer: ModelDeveloper, family: ModelFamily,
) -> None:
    assert resolve_model_identity(ModelId(model)) == Ok(
        ModelIdentity(ModelId(model), route, developer, family)
    )


@pytest.mark.parametrize("model", [
    "", "openrouter/", "openrouter/anthropic/", "openrouter/claude-sonnet-4.6",
    "openrouter/openai/claude-sonnet-4.6", "openrouter/unknown/gpt-5-mini",
    "openrouter/anthropic/unknown", "openrouter/qwen/not-qwen3",
    "openrouter/qwen/qwenish", "openrouter/moonshotai/not-kimi",
    "openrouter/openai/gpt-5/extra", " openrouter/openai/gpt-5-mini",
    "openrouter/openai/gpt-5-mini\n", "OpenRouter/openai/gpt-5-mini",
    "dispatch/sonnet", "dispatch/claude-sonnet-4.6", "ollama/qwen3:32b",
    "unknown-model", "o11-mini", "kimi-k2", "qwen3-32b",
])
def test_unknown_or_opaque_names_do_not_invent_family(model: str) -> None:
    result = resolve_model_identity(ModelId(model))
    assert isinstance(result, Err)
    assert result.error.model == model


@pytest.mark.parametrize(("models", "is_diverse"), [
    (("claude-sonnet-4-6", "openrouter/anthropic/claude-opus-4.6"), False),
    (("gpt-5-mini", "openrouter/openai/gpt-5"), False),
    (("openrouter/qwen/qwen3-32b", "openrouter/qwen/qwen-2.5-72b-instruct"), False),
    (("openrouter/moonshotai/kimi-k2", "openrouter/moonshotai/kimi-k2-thinking"), False),
    (("openrouter/moonshotai/kimi-k2", "openrouter/qwen/qwen3-32b"), True),
    ((), False),
])
def test_diversity_counts_families_not_routes_or_versions(
    models: tuple[str, ...], is_diverse: bool,
) -> None:
    identities: list[ModelIdentity] = []
    for model in models:
        result = resolve_model_identity(ModelId(model))
        assert isinstance(result, Ok)
        identities.append(result.value)
    assert isinstance(check_model_diversity(identities), Ok) is is_diverse
