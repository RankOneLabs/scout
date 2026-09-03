"""Tests for the shared Result type."""

from __future__ import annotations

from scout.result import Err, Ok, and_then, map_ok, unwrap_or


class TestOkErr:
    def test_ok_carries_value(self) -> None:
        r = Ok(5)
        assert r.value == 5

    def test_err_carries_error(self) -> None:
        r = Err("boom")
        assert r.error == "boom"

    def test_ok_and_err_are_frozen(self) -> None:
        r = Ok(1)
        try:
            r.value = 2  # type: ignore[misc]
        except Exception:
            pass
        else:
            raise AssertionError("Ok should be frozen")


class TestMapOk:
    def test_maps_ok_value(self) -> None:
        assert map_ok(Ok(2), lambda x: x * 2) == Ok(4)

    def test_leaves_err_unchanged(self) -> None:
        err: Err[str] = Err("boom")
        assert map_ok(err, lambda x: x * 2) is err


class TestAndThen:
    def test_chains_on_ok(self) -> None:
        def half(x: int) -> Ok[int] | Err[str]:
            return Ok(x // 2) if x % 2 == 0 else Err("odd")

        assert and_then(Ok(4), half) == Ok(2)
        assert and_then(Ok(3), half) == Err("odd")

    def test_short_circuits_on_err(self) -> None:
        err: Err[str] = Err("boom")
        assert and_then(err, lambda x: Ok(x)) is err


class TestUnwrapOr:
    def test_returns_ok_value(self) -> None:
        assert unwrap_or(Ok(5), 0) == 5

    def test_returns_default_on_err(self) -> None:
        assert unwrap_or(Err("boom"), 0) == 0


