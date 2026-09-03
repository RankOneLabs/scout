"""Tests for shared platform-scanning primitives."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import scout.platforms.bluesky as bluesky_client
import scout.platforms.farcaster as farcaster_client
from scout.errors import PlatformFetchFailure
from scout.platforms.base import (
    classify_http_failure,
    paginate_cursor,
    parse_platform_ts,
    parse_retry_after,
)
from scout.result import Err, Ok, Result

# ---------------------------------------------------------------------------
# parse_platform_ts
# ---------------------------------------------------------------------------


class TestParsePlatformTs:
    def test_parses_z_suffixed_timestamp_as_aware_utc(self) -> None:
        dt = parse_platform_ts("2026-03-18T12:00:00.000Z")
        assert dt == datetime(2026, 3, 18, 12, 0, 0, 0, tzinfo=UTC)

    def test_parses_explicit_offset(self) -> None:
        dt = parse_platform_ts("2026-03-18T12:00:00+05:00")
        assert dt is not None
        assert dt.utcoffset() == timedelta(hours=5)

    def test_empty_string_returns_none(self) -> None:
        assert parse_platform_ts("") is None

    def test_malformed_string_returns_none(self) -> None:
        assert parse_platform_ts("not-a-timestamp") is None

    def test_naive_timestamp_returns_none(self) -> None:
        assert parse_platform_ts("2026-01-01T00:00:00") is None

    def test_only_terminal_z_is_translated(self) -> None:
        # A "Z" that isn't the terminal character must not be blindly
        # replaced — this string is malformed and must return None, not
        # silently parse something unintended.
        assert parse_platform_ts("2026-01-01TZZ00:00:00Z") is None


# ---------------------------------------------------------------------------
# classify_http_failure
# ---------------------------------------------------------------------------


def _status_error(status: int, headers: dict[str, str] | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.test/x")
    response = httpx.Response(status, text="body", headers=headers or {}, request=request)
    return httpx.HTTPStatusError("error", request=request, response=response)


class TestClassifyHttpFailure:
    def test_429_is_retryable_rate_limited_with_retry_after(self) -> None:
        e = _status_error(429, {"Retry-After": "30"})
        failure = classify_http_failure("bluesky", e, context="ctx")
        assert failure.kind == "rate_limited"
        assert failure.retryable is True
        assert failure.retry_after == "30"
        assert failure.http_status == 429
        assert failure.platform == "bluesky"
        assert failure.context == "ctx"

    def test_429_falls_back_to_x_ratelimit_reset_after_header(self) -> None:
        e = _status_error(429, {"x-ratelimit-reset-after": "12"})
        failure = classify_http_failure("farcaster", e)
        assert failure.retry_after == "12"

    def test_401_is_non_retryable_auth_error(self) -> None:
        e = _status_error(401)
        failure = classify_http_failure("bluesky", e)
        assert failure.kind == "auth_error"
        assert failure.retryable is False

    def test_403_is_non_retryable_auth_error(self) -> None:
        e = _status_error(403)
        failure = classify_http_failure("farcaster", e)
        assert failure.kind == "auth_error"
        assert failure.retryable is False

    def test_other_status_is_retryable_network_error(self) -> None:
        e = _status_error(500)
        failure = classify_http_failure("bluesky", e)
        assert failure.kind == "network_error"
        assert failure.retryable is True


# ---------------------------------------------------------------------------
# parse_retry_after
# ---------------------------------------------------------------------------


class TestParseRetryAfter:
    def test_delta_seconds(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        assert parse_retry_after("2.5", now) == 2.5

    def test_negative_delta_seconds_is_none(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        assert parse_retry_after("-1", now) is None

    def test_http_date_relative_to_injected_now(self) -> None:
        now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        assert parse_retry_after("Thu, 01 Jan 2026 00:00:07 GMT", now) == 7.0

    def test_naive_http_date_treated_as_utc(self) -> None:
        now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        delay = parse_retry_after("Thu, 01 Jan 2026 00:00:07", now)
        assert delay == 7.0

    def test_http_date_in_the_past_is_none(self) -> None:
        now = datetime(2026, 1, 1, 0, 0, 10, tzinfo=UTC)
        assert parse_retry_after("Thu, 01 Jan 2026 00:00:07 GMT", now) is None

    def test_blank_is_none(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        assert parse_retry_after("   ", now) is None

    def test_malformed_is_none(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        assert parse_retry_after("not-a-value", now) is None


# ---------------------------------------------------------------------------
# paginate_cursor
# ---------------------------------------------------------------------------


Item = dict[str, object]
Page = dict[str, object]


def _page(items: list[Item], next_cursor: str | None) -> Page:
    return {"items": items, "cursor": next_cursor}


def _extract(page: Page) -> tuple[list[Item], str | None]:
    items = page["items"]
    assert isinstance(items, list)
    cursor = page["cursor"]
    assert cursor is None or isinstance(cursor, str)
    return items, cursor


def _ts_of(item: Item) -> datetime | None:
    raw = item.get("ts")
    if raw is None:
        return None
    assert isinstance(raw, datetime)
    return raw


def _fetch_pages(
    pages: list[Result[Page, PlatformFetchFailure]],
) -> tuple[list[dict[str, object]], Callable[..., Awaitable[Result[Page, PlatformFetchFailure]]]]:
    """Return (calls, fetch_page) where calls records every (cursor, page_number)."""
    queue = list(pages)
    calls: list[dict[str, object]] = []

    async def fetch_page(
        *, cursor: str | None, page_number: int
    ) -> Result[Page, PlatformFetchFailure]:
        calls.append({"cursor": cursor, "page_number": page_number})
        if not queue:
            raise AssertionError("fetch_page called more times than pages queued")
        return queue.pop(0)

    return calls, fetch_page


class TestPaginateCursor:
    @pytest.mark.asyncio
    async def test_accumulates_across_pages_to_cursor_exhaustion(self) -> None:
        page1 = _page([{"id": "a"}, {"id": "b"}], "cur1")
        page2 = _page([{"id": "c"}], None)
        calls, fetch_page = _fetch_pages([Ok(page1), Ok(page2)])

        result = await paginate_cursor(
            fetch_page=fetch_page,
            extract=_extract,
            timestamp_of=_ts_of,
            since=None,
            max_pages=10,
            chronological=False,
            platform="test",
        )

        assert result.failure is None
        assert not result.page_ceiling_reached
        assert [i["id"] for i in result.items] == ["a", "b", "c"]
        assert calls == [
            {"cursor": None, "page_number": 1},
            {"cursor": "cur1", "page_number": 2},
        ]

    @pytest.mark.asyncio
    async def test_empty_page_stops_without_ceiling(self) -> None:
        page1 = _page([], "cur1")
        calls, fetch_page = _fetch_pages([Ok(page1)])

        result = await paginate_cursor(
            fetch_page=fetch_page,
            extract=_extract,
            timestamp_of=_ts_of,
            since=None,
            max_pages=10,
            chronological=False,
            platform="test",
        )

        assert result.items == []
        assert not result.page_ceiling_reached
        assert result.failure is None
        assert len(calls) == 1  # no further page fetched after empty page

    @pytest.mark.asyncio
    async def test_absent_next_cursor_stops_without_ceiling(self) -> None:
        page1 = _page([{"id": "a"}], None)
        calls, fetch_page = _fetch_pages([Ok(page1)])

        result = await paginate_cursor(
            fetch_page=fetch_page,
            extract=_extract,
            timestamp_of=_ts_of,
            since=None,
            max_pages=10,
            chronological=False,
            platform="test",
        )

        assert [i["id"] for i in result.items] == ["a"]
        assert not result.page_ceiling_reached
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_fetch_failure_returns_accumulated_items_and_failure(self) -> None:
        page1 = _page([{"id": "a"}], "cur1")
        failure = PlatformFetchFailure(platform="test", kind="network_error", message="boom")
        calls, fetch_page = _fetch_pages([Ok(page1), Err(failure)])

        result = await paginate_cursor(
            fetch_page=fetch_page,
            extract=_extract,
            timestamp_of=_ts_of,
            since=None,
            max_pages=10,
            chronological=False,
            platform="test",
        )

        # Partial results from the succeeded first page survive.
        assert [i["id"] for i in result.items] == ["a"]
        assert result.failure is failure
        assert not result.page_ceiling_reached

    @pytest.mark.asyncio
    async def test_ceiling_reached_when_cursor_remains_at_max_pages(self) -> None:
        page1 = _page([{"id": "a"}], "cur1")
        page2 = _page([{"id": "b"}], "cur2")  # cursor remains after last allowed page
        calls, fetch_page = _fetch_pages([Ok(page1), Ok(page2)])

        result = await paginate_cursor(
            fetch_page=fetch_page,
            extract=_extract,
            timestamp_of=_ts_of,
            since=None,
            max_pages=2,
            chronological=False,
            platform="test",
        )

        assert result.page_ceiling_reached is True
        assert [i["id"] for i in result.items] == ["a", "b"]
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_no_ceiling_when_last_page_has_no_next_cursor(self) -> None:
        page1 = _page([{"id": "a"}], "cur1")
        page2 = _page([{"id": "b"}], None)
        calls, fetch_page = _fetch_pages([Ok(page1), Ok(page2)])

        result = await paginate_cursor(
            fetch_page=fetch_page,
            extract=_extract,
            timestamp_of=_ts_of,
            since=None,
            max_pages=2,
            chronological=False,
            platform="test",
        )

        assert result.page_ceiling_reached is False
        assert [i["id"] for i in result.items] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_chronological_stops_on_item_at_or_before_since(self) -> None:
        now = datetime.now(UTC)
        since = now - timedelta(minutes=5)
        page1 = _page(
            [{"id": "new", "ts": now}, {"id": "old", "ts": now - timedelta(minutes=6)}],
            "cur1",
        )
        calls, fetch_page = _fetch_pages([Ok(page1)])

        result = await paginate_cursor(
            fetch_page=fetch_page,
            extract=_extract,
            timestamp_of=_ts_of,
            since=since,
            max_pages=10,
            chronological=True,
            platform="test",
        )

        assert [i["id"] for i in result.items] == ["new", "old"]
        assert not result.page_ceiling_reached
        assert len(calls) == 1  # page 2 never fetched — boundary hit on page 1

    @pytest.mark.asyncio
    async def test_chronological_item_exactly_at_since_stops(self) -> None:
        since = datetime.now(UTC)
        page1 = _page([{"id": "boundary", "ts": since}], "cur1")
        calls, fetch_page = _fetch_pages([Ok(page1)])

        result = await paginate_cursor(
            fetch_page=fetch_page,
            extract=_extract,
            timestamp_of=_ts_of,
            since=since,
            max_pages=10,
            chronological=True,
            platform="test",
        )

        assert len(calls) == 1
        assert not result.page_ceiling_reached

    @pytest.mark.asyncio
    async def test_relevance_ordered_ignores_since_and_continues(self) -> None:
        """chronological=False must never stop early on `since`, even when an
        old item appears before a newer one — feed/channel ordering can't
        prove later pages hold nothing new."""
        now = datetime.now(UTC)
        since = now - timedelta(minutes=5)
        page1 = _page([{"id": "old", "ts": now - timedelta(hours=2)}], "cur1")
        page2 = _page([{"id": "new", "ts": now}], None)
        calls, fetch_page = _fetch_pages([Ok(page1), Ok(page2)])

        result = await paginate_cursor(
            fetch_page=fetch_page,
            extract=_extract,
            timestamp_of=_ts_of,
            since=since,
            max_pages=10,
            chronological=False,
            platform="test",
        )

        assert [i["id"] for i in result.items] == ["old", "new"]
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_missing_timestamp_does_not_terminate_pagination(self) -> None:
        now = datetime.now(UTC)
        since = now - timedelta(minutes=5)
        page1 = _page([{"id": "no-ts"}], "cur1")  # timestamp_of returns None
        page2 = _page([{"id": "old", "ts": now - timedelta(minutes=6)}], None)
        calls, fetch_page = _fetch_pages([Ok(page1), Ok(page2)])

        result = await paginate_cursor(
            fetch_page=fetch_page,
            extract=_extract,
            timestamp_of=_ts_of,
            since=since,
            max_pages=10,
            chronological=True,
            platform="test",
        )

        # page1's missing timestamp didn't stop pagination; page2's item did.
        assert [i["id"] for i in result.items] == ["no-ts", "old"]
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_naive_timestamp_does_not_crash_or_terminate_pagination(self) -> None:
        """A timestamp_of that returns a naive datetime (no tzinfo) at or
        before `since` must not raise TypeError comparing naive to aware, and
        must not be treated as a valid boundary — inconclusive, like None."""
        now = datetime.now(UTC)
        since = now - timedelta(minutes=5)
        naive_ts = (now - timedelta(minutes=6)).replace(tzinfo=None)
        page1 = _page([{"id": "naive", "ts": naive_ts}], "cur1")
        page2 = _page([{"id": "old", "ts": now - timedelta(minutes=6)}], None)
        calls, fetch_page = _fetch_pages([Ok(page1), Ok(page2)])

        result = await paginate_cursor(
            fetch_page=fetch_page,
            extract=_extract,
            timestamp_of=_ts_of,
            since=since,
            max_pages=10,
            chronological=True,
            platform="test",
        )

        # page1's naive item didn't crash or stop pagination; page2's aware,
        # older-than-since item did.
        assert [i["id"] for i in result.items] == ["naive", "old"]
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_max_pages_must_be_positive(self) -> None:
        calls, fetch_page = _fetch_pages([])

        with pytest.raises(ValueError):
            await paginate_cursor(
                fetch_page=fetch_page,
                extract=_extract,
                timestamp_of=_ts_of,
                since=None,
                max_pages=0,
                chronological=False,
                platform="test",
            )

    @pytest.mark.asyncio
    async def test_page_number_starts_at_one_and_increments(self) -> None:
        page1 = _page([{"id": "a"}], "cur1")
        page2 = _page([{"id": "b"}], "cur2")
        page3 = _page([{"id": "c"}], None)
        calls, fetch_page = _fetch_pages([Ok(page1), Ok(page2), Ok(page3)])

        await paginate_cursor(
            fetch_page=fetch_page,
            extract=_extract,
            timestamp_of=_ts_of,
            since=None,
            max_pages=10,
            chronological=False,
            platform="test",
        )

        assert [c["page_number"] for c in calls] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Regression: bluesky_client / farcaster_client must not re-implement the
# extracted primitives locally. Passing behavioral tests alone would not
# prove the duplicate implementations were actually removed.
# ---------------------------------------------------------------------------


class TestNoLocalDuplication:
    @pytest.mark.parametrize("module", [bluesky_client, farcaster_client])
    def test_no_local_timestamp_http_or_retry_helpers(self, module: object) -> None:
        source = inspect.getsource(module)  # type: ignore[arg-type]
        # These names were the local, byte-identical duplicates before the
        # extraction — they must not reappear as local defs.
        assert "def _classify_http_failure(" not in source
        assert "def _parse_retry_delay(" not in source
        assert "def paginate_cursor(" not in source
        assert "def parse_platform_ts(" not in source
        # The manual ISO-8601 parsing fallback these primitives replaced.
        assert "fromisoformat" not in source
        assert "parsedate_to_datetime" not in source

    @pytest.mark.parametrize(
        "module,attr",
        [
            (bluesky_client, "classify_http_failure"),
            (bluesky_client, "parse_platform_ts"),
            (bluesky_client, "parse_retry_after"),
            (bluesky_client, "paginate_cursor"),
            (farcaster_client, "classify_http_failure"),
            (farcaster_client, "parse_platform_ts"),
            (farcaster_client, "parse_retry_after"),
            (farcaster_client, "paginate_cursor"),
        ],
    )
    def test_imports_shared_primitive_by_identity(self, module: object, attr: str) -> None:
        """Each adapter's imported name must be the exact platform_primitives
        object, not a shadowing local redefinition."""
        import scout.platforms.base as platform_primitives

        assert getattr(module, attr) is getattr(platform_primitives, attr)
