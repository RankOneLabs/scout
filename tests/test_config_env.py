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
