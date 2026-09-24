"""Tests for safe env-parsing helpers in config.py."""

from __future__ import annotations

import pytest

from scout.config import (
    _env_bool,
    _env_float,
    _env_int,
    _env_int_list,
    _env_min_int,
    _env_str_list,
    get_env_errors,
)


@pytest.fixture(autouse=True)
def _clear_env_errors() -> None:
    """Ensure each test starts with a clean error accumulator."""
    get_env_errors()
    yield
    get_env_errors()


def test_env_int_returns_default_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCOUT_TEST_INT", raising=False)
    assert _env_int("SCOUT_TEST_INT", 42) == 42
    assert get_env_errors() == []


def test_env_int_returns_default_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOUT_TEST_INT", "")
    assert _env_int("SCOUT_TEST_INT", 7) == 7
    assert get_env_errors() == []


def test_env_int_parses_valid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOUT_TEST_INT", "123")
    assert _env_int("SCOUT_TEST_INT", 0) == 123
    assert get_env_errors() == []


def test_env_int_records_error_on_non_numeric(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOUT_TEST_INT", "abc")
    assert _env_int("SCOUT_TEST_INT", 5) == 5
    errors = get_env_errors()
    assert len(errors) == 1
    assert "SCOUT_TEST_INT" in errors[0]
    assert "abc" in errors[0]


def test_env_min_int_accepts_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOUT_TEST_MIN_INT", "1")
    assert _env_min_int("SCOUT_TEST_MIN_INT", 5, 1) == 1
    assert get_env_errors() == []


def test_env_min_int_records_error_below_minimum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCOUT_TEST_MIN_INT", "0")
    assert _env_min_int("SCOUT_TEST_MIN_INT", 5, 1) == 5
    errors = get_env_errors()
    assert len(errors) == 1
    assert "SCOUT_TEST_MIN_INT" in errors[0]
    assert "must be >= 1" in errors[0]


def test_env_float_returns_default_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCOUT_TEST_FLOAT", raising=False)
    assert _env_float("SCOUT_TEST_FLOAT", 1.5) == 1.5
    assert get_env_errors() == []


def test_env_float_returns_default_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOUT_TEST_FLOAT", "")
    assert _env_float("SCOUT_TEST_FLOAT", 2.5) == 2.5
    assert get_env_errors() == []


def test_env_float_parses_valid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOUT_TEST_FLOAT", "3.14")
    assert _env_float("SCOUT_TEST_FLOAT", 0.0) == 3.14
    assert get_env_errors() == []


def test_env_float_records_error_on_non_numeric(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOUT_TEST_FLOAT", "not-a-number")
    assert _env_float("SCOUT_TEST_FLOAT", 0.5) == 0.5
    errors = get_env_errors()
    assert len(errors) == 1
    assert "SCOUT_TEST_FLOAT" in errors[0]
    assert "not-a-number" in errors[0]


def test_env_int_list_returns_empty_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCOUT_TEST_LIST", raising=False)
    assert _env_int_list("SCOUT_TEST_LIST") == []
    assert get_env_errors() == []


def test_env_int_list_returns_empty_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOUT_TEST_LIST", "")
    assert _env_int_list("SCOUT_TEST_LIST") == []
    assert get_env_errors() == []


def test_env_int_list_parses_valid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOUT_TEST_LIST", "1,2,3")
    assert _env_int_list("SCOUT_TEST_LIST") == [1, 2, 3]
    assert get_env_errors() == []


def test_env_int_list_keeps_valid_records_error_for_bad(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCOUT_TEST_LIST", "1,oops,3")
    assert _env_int_list("SCOUT_TEST_LIST") == [1, 3]
    errors = get_env_errors()
    assert len(errors) == 1
    assert "SCOUT_TEST_LIST" in errors[0]
    assert "oops" in errors[0]


def test_env_int_list_skips_whitespace_and_empty_parts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCOUT_TEST_LIST", " 1 , , 2 ,")
    assert _env_int_list("SCOUT_TEST_LIST") == [1, 2]
    assert get_env_errors() == []


def test_env_str_list_parses_non_empty_trimmed_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCOUT_TEST_STR_LIST", " en, , fr-CA ")
    assert _env_str_list("SCOUT_TEST_STR_LIST") == ("en", "fr-CA")
    assert get_env_errors() == []


def test_env_bool_returns_default_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCOUT_TEST_BOOL", raising=False)
    assert _env_bool("SCOUT_TEST_BOOL", False) is False
    assert _env_bool("SCOUT_TEST_BOOL", True) is True
    assert get_env_errors() == []


def test_env_bool_returns_default_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOUT_TEST_BOOL", "")
    assert _env_bool("SCOUT_TEST_BOOL", True) is True
    assert get_env_errors() == []


@pytest.mark.parametrize("raw", ["true", "True", "TRUE", "1", "yes", "on"])
def test_env_bool_accepts_true_tokens(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("SCOUT_TEST_BOOL", raw)
    assert _env_bool("SCOUT_TEST_BOOL", False) is True
    assert get_env_errors() == []


@pytest.mark.parametrize("raw", ["false", "False", "FALSE", "0", "no", "off"])
def test_env_bool_accepts_false_tokens(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("SCOUT_TEST_BOOL", raw)
    assert _env_bool("SCOUT_TEST_BOOL", True) is False
    assert get_env_errors() == []


def test_env_bool_records_error_on_unrecognized_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCOUT_TEST_BOOL", "enabled")
    assert _env_bool("SCOUT_TEST_BOOL", False) is False
    errors = get_env_errors()
    assert len(errors) == 1
    assert "SCOUT_TEST_BOOL" in errors[0]
    assert "enabled" in errors[0]


def test_get_env_errors_returns_and_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOUT_TEST_INT", "bad")
    _env_int("SCOUT_TEST_INT", 0)
    first = get_env_errors()
    assert len(first) == 1
    # A second call should return an empty list — the accumulator was cleared.
    second = get_env_errors()
    assert second == []


def test_typesafe_backend_rejects_unregistered_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    import scout.config as config_module

    monkeypatch.setenv("TYPESAFE_SHADOW_BACKEND", "typesafe")
    try:
        reloaded = importlib.reload(config_module)
        assert reloaded.TYPESAFE_SHADOW_BACKEND == "placeholder"
        assert any("TYPESAFE_SHADOW_BACKEND" in error for error in reloaded.get_env_errors())
    finally:
        monkeypatch.delenv("TYPESAFE_SHADOW_BACKEND")
        importlib.reload(config_module)


class TestLeaseAndRecoveryConfig:
    """Module-level constants computed at import time, so each test reloads
    scout.config under a patched environment rather than monkeypatching an
    already-evaluated module attribute."""

    @pytest.fixture(autouse=True)
    def _reload_clean_afterward(self) -> None:
        yield
        import importlib

        import scout.config as config_module

        importlib.reload(config_module)
        get_env_errors()

    def _reload(self, monkeypatch: pytest.MonkeyPatch, **env: str) -> object:
        import importlib

        import scout.config as config_module

        for key in (
            "SCOUT_LEASE_TTL_SECONDS",
            "SCOUT_LEASE_HEARTBEAT_SECONDS",
            "SCOUT_RECOVERY_LOCK_TTL_SECONDS",
            "SCOUT_CUTOVER_PROBE_MAX_AGE_SECONDS",
            "SCOUT_BACKFILL_MAX_PAGES_PER_SOURCE",
            "SCOUT_STALE_WATERMARK_HOURS",
        ):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return importlib.reload(config_module)

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reloaded = self._reload(monkeypatch)
        assert reloaded.SCOUT_LEASE_TTL_SECONDS == 120
        assert reloaded.SCOUT_LEASE_HEARTBEAT_SECONDS == 30
        assert reloaded.SCOUT_RECOVERY_LOCK_TTL_SECONDS == 1800
        assert reloaded.SCOUT_CUTOVER_PROBE_MAX_AGE_SECONDS == 3600
        assert reloaded.SCOUT_BACKFILL_MAX_PAGES_PER_SOURCE == 20
        assert reloaded.SCOUT_STALE_WATERMARK_HOURS == 24
        assert get_env_errors() == []

    def test_heartbeat_too_close_to_ttl_records_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._reload(
            monkeypatch,
            SCOUT_LEASE_TTL_SECONDS="30",
            SCOUT_LEASE_HEARTBEAT_SECONDS="20",
        )
        errors = get_env_errors()
        assert any("SCOUT_LEASE_HEARTBEAT_SECONDS" in e for e in errors)

    def test_heartbeat_with_room_for_two_misses_is_clean(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._reload(
            monkeypatch,
            SCOUT_LEASE_TTL_SECONDS="120",
            SCOUT_LEASE_HEARTBEAT_SECONDS="30",
        )
        assert get_env_errors() == []


class TestRelevanceClassifierConfig:
    """RELEVANCE_CLASSIFIER and the JEV-only settings beside it.

    These are module-level constants computed at import time, so each test
    reloads scout.config under a patched environment.
    """

    _KEYS = (
        "RELEVANCE_CLASSIFIER",
        "TYPESAFE_API_KEY",
        "TYPESAFE_BASE_URL",
        "TYPESAFE_CATALOGUE_PATH",
        "RELEVANCE_HOLDOUT_RATE",
        "KEYWORD_PREFILTER",
    )

    @pytest.fixture(autouse=True)
    def _reload_clean_afterward(self) -> None:
        yield
        import importlib

        import scout.config as config_module

        importlib.reload(config_module)
        get_env_errors()

    def _reload(self, monkeypatch: pytest.MonkeyPatch, **env: str) -> object:
        import importlib

        import scout.config as config_module

        for key in self._KEYS:
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_env_errors()
        return importlib.reload(config_module)

    def test_classifier_defaults_to_llm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reloaded = self._reload(monkeypatch)
        assert reloaded.RELEVANCE_CLASSIFIER == "llm"
        assert get_env_errors() == []

    def test_classifier_accepts_jev(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reloaded = self._reload(monkeypatch, RELEVANCE_CLASSIFIER="jev")
        assert reloaded.RELEVANCE_CLASSIFIER == "jev"
        assert get_env_errors() == []

    def test_an_unknown_classifier_records_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reloaded = self._reload(monkeypatch, RELEVANCE_CLASSIFIER="gemini")
        assert reloaded.RELEVANCE_CLASSIFIER == "llm"
        assert any("RELEVANCE_CLASSIFIER" in error for error in get_env_errors())

    def test_base_url_defaults_to_the_public_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reloaded = self._reload(monkeypatch)
        assert reloaded.TYPESAFE_BASE_URL == "https://api.typesafe.ai"

    def test_base_url_trailing_slash_is_trimmed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reloaded = self._reload(monkeypatch, TYPESAFE_BASE_URL="https://ts.internal/")
        assert reloaded.TYPESAFE_BASE_URL == "https://ts.internal"

    def test_holdout_rate_defaults_to_one_tenth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reloaded = self._reload(monkeypatch)
        assert reloaded.RELEVANCE_HOLDOUT_RATE == 0.1

    def test_an_out_of_range_holdout_rate_records_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reloaded = self._reload(monkeypatch, RELEVANCE_HOLDOUT_RATE="1.5")
        assert reloaded.RELEVANCE_HOLDOUT_RATE == 0.1
        assert any("RELEVANCE_HOLDOUT_RATE" in error for error in get_env_errors())

    def test_a_zero_holdout_rate_is_accepted_and_holds_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reloaded = self._reload(monkeypatch, RELEVANCE_HOLDOUT_RATE="0")
        assert reloaded.RELEVANCE_HOLDOUT_RATE == 0.0
        assert get_env_errors() == []

    def test_a_full_holdout_rate_is_accepted_and_holds_everything(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reloaded = self._reload(monkeypatch, RELEVANCE_HOLDOUT_RATE="1")
        assert reloaded.RELEVANCE_HOLDOUT_RATE == 1.0
        assert get_env_errors() == []

    def test_the_holdout_rate_is_independent_of_the_classifier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The two are separate controls, so rollback is one variable.

        Rolling the classifier back to `llm` does not silently reset or
        disable sampling, and a non-default rate does not require any JEV
        setting. See docs/relevance-holdouts.md.
        """
        rolled_back = self._reload(
            monkeypatch, RELEVANCE_CLASSIFIER="llm", RELEVANCE_HOLDOUT_RATE="0.25"
        )
        assert rolled_back.RELEVANCE_CLASSIFIER == "llm"
        assert rolled_back.RELEVANCE_HOLDOUT_RATE == 0.25
        assert get_env_errors() == []

    def test_a_rollback_to_llm_needs_no_jev_setting_at_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every JEV requirement is validated only under `jev`.

        The whole rollback is `RELEVANCE_CLASSIFIER=llm`: the credential, the
        catalogue path and the prefilter requirement all stop applying, so a
        rolled-back deployment cannot fail startup on a JEV setting it no
        longer uses.
        """
        import scout.config as config_module
        from scout.scanning.runner import validate_jev_config

        monkeypatch.setattr(config_module, "RELEVANCE_CLASSIFIER", "llm")
        monkeypatch.setattr(config_module, "TYPESAFE_API_KEY", "")
        monkeypatch.setattr(config_module, "KEYWORD_PREFILTER", False)
        monkeypatch.delenv("TYPESAFE_CATALOGUE_PATH", raising=False)
        assert validate_jev_config() == []

    def _jev_errors(self, monkeypatch: pytest.MonkeyPatch, **env: str) -> list[str]:
        """Patch the already-imported config module rather than reloading
        scout.scanning.runner: validate_jev_config reads `_config.X` at call
        time, and reloading the runner rebinds objects other modules hold."""
        import scout.config as config_module
        from scout.scanning.runner import validate_jev_config

        settings: dict[str, object] = {
            "RELEVANCE_CLASSIFIER": "jev",
            "TYPESAFE_API_KEY": env.get("TYPESAFE_API_KEY", ""),
            "TYPESAFE_BASE_URL": env.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai"),
            "KEYWORD_PREFILTER": env.get("KEYWORD_PREFILTER", "true") == "true",
        }
        for name, value in settings.items():
            monkeypatch.setattr(config_module, name, value)
        catalogue_path = env.get("TYPESAFE_CATALOGUE_PATH", "")
        if catalogue_path:
            monkeypatch.setenv("TYPESAFE_CATALOGUE_PATH", catalogue_path)
            monkeypatch.setattr(config_module, "TYPESAFE_CATALOGUE_PATH", catalogue_path)
        else:
            monkeypatch.delenv("TYPESAFE_CATALOGUE_PATH", raising=False)
        return validate_jev_config()

    def test_llm_needs_no_jev_configuration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import scout.config as config_module
        from scout.scanning.runner import validate_jev_config

        monkeypatch.setattr(config_module, "RELEVANCE_CLASSIFIER", "llm")
        assert validate_jev_config() == []

    def test_jev_requires_an_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        errors = self._jev_errors(monkeypatch)
        assert any("TYPESAFE_API_KEY" in error for error in errors)

    def test_jev_requires_an_explicit_catalogue_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        errors = self._jev_errors(monkeypatch, TYPESAFE_API_KEY="k")
        assert any("TYPESAFE_CATALOGUE_PATH" in error for error in errors)

    def test_jev_requires_the_keyword_prefilter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        errors = self._jev_errors(
            monkeypatch, TYPESAFE_API_KEY="k", KEYWORD_PREFILTER="false"
        )
        assert any("KEYWORD_PREFILTER" in error for error in errors)

    def test_jev_rejects_a_non_http_base_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        errors = self._jev_errors(
            monkeypatch, TYPESAFE_API_KEY="k", TYPESAFE_BASE_URL="ftp://ts.internal"
        )
        assert any("TYPESAFE_BASE_URL" in error for error in errors)

    def test_jev_rejects_an_unloadable_catalogue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        errors = self._jev_errors(
            monkeypatch,
            TYPESAFE_API_KEY="k",
            TYPESAFE_CATALOGUE_PATH="tests/fixtures/relevance/missing.yaml",
        )
        assert any("not a usable JEV catalogue" in error for error in errors)

    def test_a_fully_configured_jev_deployment_is_clean(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        errors = self._jev_errors(
            monkeypatch,
            TYPESAFE_API_KEY="k",
            TYPESAFE_CATALOGUE_PATH="tests/fixtures/relevance/routed-features.fixture.yaml",
        )
        assert errors == []
