from __future__ import annotations

import json

import pytest

from scout.model_identity import (
    ModelDeveloper,
    ModelDiversityPolicy,
    ModelFamily,
    ModelId,
    ModelIdentity,
    ModelRoute,
    check_model_diversity,
    parse_model_identity_config,
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


def test_absent_local_config_preserves_two_family_default() -> None:
    result = parse_model_identity_config(None)
    assert isinstance(result, Ok)
    assert result.value.policy.minimum_families == 2
    assert result.value.identities == ()


@pytest.mark.parametrize("alias", ["dispatch/reviewer", "ollama/private-model:latest"])
def test_local_alias_resolves_exactly_without_changing_route(alias: str) -> None:
    result = parse_model_identity_config(json.dumps({"identities": [{
        "model": alias, "developer": "moonshotai", "family": "kimi",
    }]}))
    assert isinstance(result, Ok)
    resolved = resolve_model_identity(ModelId(alias), result.value.identities)
    assert isinstance(resolved, Ok)
    assert resolved.value.to_json() == {
        "model": alias, "route": alias.split("/", 1)[0],
        "developer": "moonshotai", "family": "kimi",
        "source": "declared",
    }
    unmatched = resolve_model_identity(ModelId(alias + "-other"), result.value.identities)
    assert isinstance(unmatched, Err)


def test_custom_family_needs_no_public_registry_change() -> None:
    result = parse_model_identity_config(json.dumps({"identities": [{
        "model": "openrouter/example/new-model", "developer": "example", "family": "new-family",
    }]}))
    assert isinstance(result, Ok)
    assert result.value.identities[0].family == "new-family"


@pytest.mark.parametrize("raw", [
    "", " ", "null", "[]", "not-json", '{"extra":"secret-value"}',
    '{"policy":{"minimum_families":0}}', '{"policy":{"minimum_families":4}}',
    '{"policy":{"minimum_families":true}}', '{"policy":{"minimum_families":"2"}}',
    '{"policy":{"minimum_families":2.0}}', '{"policy":{"unexpected":2}}',
    '{"identities":[{"model":"dispatch/a","developer":"example"}]}',
    '{"identities":[{"model":"dispatch/a","developer":"example","family":""}]}',
    '{"identities":[{"model":"dispatch/a","developer":"EXAMPLE","family":"x"}]}',
    '{"identities":[{"model":"dispatch/a","developer":"example","family":"x","route":"x"}]}',
])
def test_malformed_local_config_fails_without_echoing_payload(raw: str) -> None:
    result = parse_model_identity_config(raw)
    assert isinstance(result, Err)
    assert "invalid SCOUT_MODEL_IDENTITY_CONFIG" in result.error.detail
    assert "secret-value" not in result.error.detail


@pytest.mark.parametrize(("model", "developer", "family"), [
    ("claude-sonnet-4-6", "anthropic", "different-family"),
    ("openrouter/qwen/qwen3-32b", "different-developer", "qwen"),
    ("openrouter/example/claude-sonnet-4.6", "example", "claude"),
    ("openrouter/example/new-model", "different-developer", "new-family"),
    ("unsupported/private-model", "example", "new-family"),
    ("openrouter/example/", "example", "new-family"),
    ("private-model", "example", "new-family"),
    ("dispatch/reviewer", "example", "kimi"),
])
def test_conflicting_or_unsupported_declarations_fail(
    model: str, developer: str, family: str,
) -> None:
    result = parse_model_identity_config(json.dumps({"identities": [{
        "model": model, "developer": developer, "family": family,
    }]}))
    assert isinstance(result, Err)


def test_duplicate_exact_aliases_are_rejected() -> None:
    declaration = {"model": "dispatch/a", "developer": "qwen", "family": "qwen"}
    result = parse_model_identity_config(json.dumps({"identities": [declaration, declaration]}))
    assert isinstance(result, Err)
    assert "duplicate" in result.error.detail


@pytest.mark.parametrize(("minimum", "expected_ok"), [(1, True), (2, False), (3, False)])
def test_local_policy_controls_required_family_count(minimum: int, expected_ok: bool) -> None:
    result = resolve_model_identity(ModelId("claude-sonnet-4-6"))
    assert isinstance(result, Ok)
    policy = ModelDiversityPolicy(minimum_families=minimum)
    assert isinstance(check_model_diversity([result.value], policy), Ok) is expected_ok
