"""Tests for the relevance classifier configuration surface."""

from __future__ import annotations

import importlib
from inspect import signature
from types import ModuleType

import pytest
from jig.jev import JevClient

import scout.config as config_module

_NEW_ENV_KEYS = (
    "RELEVANCE_CLASSIFIER",
    "RELEVANCE_ZEROSHOT_CATALOGUE_PATH",
    "RELEVANCE_JEV_MODEL",
    "RELEVANCE_JEV_ENDPOINT",
    "TYPESAFE_API_KEY",
    "RELEVANCE_HOLDOUT_RATE",
)


@pytest.fixture(autouse=True)
def _reload_clean_afterward() -> None:
    yield
    importlib.reload(config_module)
    config_module.get_env_errors()


def _reload(monkeypatch: pytest.MonkeyPatch, **env: str) -> ModuleType:
    for key in _NEW_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(config_module)


def test_relevance_classifier_defaults_match_pinned_jig(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reloaded = _reload(monkeypatch)
    jev_parameters = signature(JevClient).parameters

    assert reloaded.RELEVANCE_CLASSIFIER == "llm"
    assert reloaded.RELEVANCE_ZEROSHOT_CATALOGUE_PATH == ""
    assert reloaded.RELEVANCE_JEV_MODEL == jev_parameters["model"].default == "jev-latest"
    assert jev_parameters["endpoint"].default == "https://api.typesafe.ai/v1/systemone"
    assert reloaded.RELEVANCE_JEV_ENDPOINT == "https://api.typesafe.ai/v1/systemone"
    assert reloaded.TYPESAFE_API_KEY == ""
    assert reloaded.RELEVANCE_HOLDOUT_RATE == 0.0
    assert reloaded.get_env_errors() == []


def test_relevance_classifier_accepts_configured_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reloaded = _reload(
        monkeypatch,
        RELEVANCE_CLASSIFIER="zeroshot",
        RELEVANCE_ZEROSHOT_CATALOGUE_PATH=" /catalogues/agent-ops.yaml ",
        RELEVANCE_JEV_MODEL="jev-v6",
        RELEVANCE_JEV_ENDPOINT="https://typesafe.test/systemone/",
        TYPESAFE_API_KEY="secret-value",
        RELEVANCE_HOLDOUT_RATE="0.25",
    )

    assert reloaded.RELEVANCE_CLASSIFIER == "zeroshot"
    assert reloaded.RELEVANCE_ZEROSHOT_CATALOGUE_PATH == "/catalogues/agent-ops.yaml"
    assert reloaded.RELEVANCE_JEV_MODEL == "jev-v6"
    assert reloaded.RELEVANCE_JEV_ENDPOINT == "https://typesafe.test/systemone"
    assert reloaded.TYPESAFE_API_KEY == "secret-value"
    assert reloaded.RELEVANCE_HOLDOUT_RATE == 0.25
    assert reloaded.get_env_errors() == []


def test_unknown_relevance_classifier_records_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reloaded = _reload(monkeypatch, RELEVANCE_CLASSIFIER="typo")

    assert reloaded.RELEVANCE_CLASSIFIER == "llm"
    assert any(
        "RELEVANCE_CLASSIFIER" in error for error in reloaded.get_env_errors()
    )


@pytest.mark.parametrize("value", ["-0.1", "1.5"])
def test_out_of_range_holdout_rate_records_both_bounds(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    reloaded = _reload(monkeypatch, RELEVANCE_HOLDOUT_RATE=value)

    assert reloaded.RELEVANCE_HOLDOUT_RATE == 0.0
    errors = reloaded.get_env_errors()
    assert any(
        "RELEVANCE_HOLDOUT_RATE" in error
        and ">= 0.0" in error
        and "<= 1.0" in error
        for error in errors
    )


def test_typesafe_api_key_never_appears_in_config_output(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "do-not-emit-this-secret"
    reloaded = _reload(
        monkeypatch,
        RELEVANCE_CLASSIFIER="typo",
        RELEVANCE_HOLDOUT_RATE="1.5",
        TYPESAFE_API_KEY=secret,
    )

    errors = reloaded.get_env_errors()
    captured = capsys.readouterr()
    emitted = "\n".join((repr(errors), caplog.text, captured.out, captured.err))
    assert secret not in emitted
