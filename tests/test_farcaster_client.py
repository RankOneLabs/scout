"""Tests for FarcasterScanner.fetch_messages — pagination, error classification, and dedup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from scout.errors import PlatformFetchSuccess
from scout.platforms.farcaster import FarcasterScanner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cast(hash_: str, text: str, created_at: datetime, username: str = "alice") -> dict[str, Any]:
    return {
        "hash": hash_,
        "text": text,
        "timestamp": created_at.isoformat().replace("+00:00", "Z"),
        "author": {"username": username, "display_name": "Alice", "fid": 1},
        "channel": {"id": "dev"},
    }


def _make_transport(responses: list[httpx.Response]) -> httpx.MockTransport:
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if not queue:
            raise AssertionError(f"Unexpected request to {request.url}")
        return queue.pop(0)

    return httpx.MockTransport(handler)


def _patch_http(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    _original = httpx.AsyncClient

    def factory(**kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return _original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("scout.platforms.farcaster.httpx.AsyncClient", factory)


# ---------------------------------------------------------------------------
# Pagination tests
# ---------------------------------------------------------------------------


class TestSearchPagination:
    @pytest.mark.asyncio
    async def test_follows_cursor_across_two_pages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        page1 = [_cast("h1", "hello", now), _cast("h2", "world", now - timedelta(minutes=1))]
        page2 = [_cast("h3", "foo", now - timedelta(minutes=2))]

        transport = _make_transport([
            httpx.Response(200, json={"result": {"casts": page1, "next": {"cursor": "cur1"}}}),
            httpx.Response(200, json={"result": {"casts": page2, "next": {"cursor": None}}}),
        ])

        monkeypatch.setattr("scout.platforms.farcaster.NEYNAR_API_URL", "https://api.example")

        scanner = FarcasterScanner(api_key="test-key", max_results_per_query=10)
        async with httpx.AsyncClient(transport=transport) as client:
            casts, ceiling, failure = await scanner._search_paginated(
                client, "hello", since=None, max_pages=10
            )

        assert failure is None
        assert not ceiling
        assert len(casts) == 3
        assert [c["hash"] for c in casts] == ["h1", "h2", "h3"]

    @pytest.mark.asyncio
    async def test_stops_at_since_boundary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        since = now - timedelta(minutes=5)

        page1 = [
            _cast("h1", "new", now),
            _cast("h2", "old", now - timedelta(minutes=6)),  # older than since
        ]

        transport = _make_transport([
            httpx.Response(200, json={"result": {"casts": page1, "next": {"cursor": "cur1"}}}),
            # page2 must not be requested
        ])

        monkeypatch.setattr("scout.platforms.farcaster.NEYNAR_API_URL", "https://api.example")

        scanner = FarcasterScanner(api_key="test-key", max_results_per_query=10)
        async with httpx.AsyncClient(transport=transport) as client:
            casts, ceiling, failure = await scanner._search_paginated(
                client, "hello", since=since, max_pages=10
            )

        assert failure is None
        assert not ceiling
        assert len(casts) == 2

    @pytest.mark.asyncio
    async def test_naive_timestamp_does_not_crash_since_boundary_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A naive timestamp (no timezone) in the early-stop boundary check
        must not raise TypeError from comparing naive to aware `since` — it
        should be treated as inconclusive and pagination should continue.
        """
        now = datetime.now(UTC)
        since = now - timedelta(minutes=5)

        naive_cast = _cast("naive", "no tz", now)
        naive_cast["timestamp"] = "2026-01-01T00:00:00"  # no Z, no offset

        old_cast = _cast("old", "old", now - timedelta(minutes=6))  # older than since

        transport = _make_transport([
            httpx.Response(
                200, json={"result": {"casts": [naive_cast], "next": {"cursor": "cur1"}}}
            ),
            httpx.Response(
                200, json={"result": {"casts": [old_cast], "next": {"cursor": "cur2"}}}
            ),
        ])

        monkeypatch.setattr("scout.platforms.farcaster.NEYNAR_API_URL", "https://api.example")

        scanner = FarcasterScanner(api_key="test-key", max_results_per_query=10)
        async with httpx.AsyncClient(transport=transport) as client:
            casts, ceiling, failure = await scanner._search_paginated(
                client, "hello", since=since, max_pages=10
            )

        assert failure is None
        assert not ceiling
        # page1's naive item didn't crash or trigger early-stop; page2's aware,
        # older-than-since item did — so page3 was never fetched.
        assert len(casts) == 2

    @pytest.mark.asyncio
    async def test_page_ceiling_returns_ceiling_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        cast_item = _cast("h1", "msg", now)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"result": {"casts": [cast_item], "next": {"cursor": "next"}}}
            )

        monkeypatch.setattr("scout.platforms.farcaster.NEYNAR_API_URL", "https://api.example")

        scanner = FarcasterScanner(api_key="test-key", max_results_per_query=10)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            casts, ceiling, failure = await scanner._search_paginated(
                client, "hello", since=None, max_pages=2
            )

        assert failure is None
        assert ceiling is True
        assert len(casts) == 2  # 2 pages, 1 cast each

    @pytest.mark.asyncio
    async def test_channel_search_paginated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        page1 = [_cast("h1", "in channel", now)]
        page2 = [_cast("h2", "also in channel", now - timedelta(minutes=1))]

        transport = _make_transport([
            httpx.Response(200, json={"result": {"casts": page1, "next": {"cursor": "c1"}}}),
            httpx.Response(200, json={"result": {"casts": page2, "next": {"cursor": None}}}),
        ])

        monkeypatch.setattr("scout.platforms.farcaster.NEYNAR_API_URL", "https://api.example")

        scanner = FarcasterScanner(api_key="test-key", max_results_per_query=10)
        async with httpx.AsyncClient(transport=transport) as client:
            casts, ceiling, failure = await scanner._search_paginated(
                client, "hello", since=None, max_pages=10, channel_id="dev"
            )

        assert failure is None
        assert len(casts) == 2


class TestRetryOn429:
    @pytest.mark.asyncio
    async def test_retries_same_cursor_page_after_delta_seconds_delay(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        cast_item = _cast("h1", "recovered", now)

        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(
                    429, text="rate limited", headers={"Retry-After": "2.5"}
                )
            return httpx.Response(
                200, json={"result": {"casts": [cast_item], "next": {"cursor": None}}}
            )

        monkeypatch.setattr("scout.platforms.farcaster.NEYNAR_API_URL", "https://api.example")

        sleeps: list[float] = []

        async def fake_sleeper(delay: float) -> None:
            sleeps.append(delay)

        scanner = FarcasterScanner(
            api_key="test-key", max_results_per_query=10, sleeper=fake_sleeper
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            casts, ceiling, failure = await scanner._search_paginated(
                client, "hello", since=None, max_pages=10
            )

        assert failure is None
        assert not ceiling
        assert sleeps == [2.5]
        assert len(casts) == 1
        assert len(requests) == 2
        assert requests[0].url.params.get("cursor") == requests[1].url.params.get("cursor")

    @pytest.mark.asyncio
    async def test_retries_after_http_date_delay_using_injected_clock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        cast_item = _cast("h1", "recovered", now)
        clock_now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(
                    429,
                    text="rate limited",
                    headers={"Retry-After": "Thu, 01 Jan 2026 00:00:07 GMT"},
                )
            return httpx.Response(
                200, json={"result": {"casts": [cast_item], "next": {"cursor": None}}}
            )

        monkeypatch.setattr("scout.platforms.farcaster.NEYNAR_API_URL", "https://api.example")

        sleeps: list[float] = []

        async def fake_sleeper(delay: float) -> None:
            sleeps.append(delay)

        scanner = FarcasterScanner(
            api_key="test-key",
            max_results_per_query=10,
            sleeper=fake_sleeper,
            clock=lambda: clock_now,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            casts, ceiling, failure = await scanner._search_paginated(
                client, "hello", since=None, max_pages=10
            )

        assert failure is None
        assert not ceiling
        assert sleeps == [7.0]
        assert len(casts) == 1

    @pytest.mark.asyncio
    async def test_invalid_delay_returns_rate_limited_without_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                429, text="rate limited", headers={"Retry-After": "not-a-number"}
            )

        monkeypatch.setattr("scout.platforms.farcaster.NEYNAR_API_URL", "https://api.example")

        sleeps: list[float] = []

        async def fake_sleeper(delay: float) -> None:
            sleeps.append(delay)

        scanner = FarcasterScanner(
            api_key="test-key", max_results_per_query=10, sleeper=fake_sleeper
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            casts, ceiling, failure = await scanner._search_paginated(
                client, "hello", since=None, max_pages=10
            )

        assert sleeps == []
        assert len(requests) == 1
        assert not ceiling
        assert casts == []
        assert failure is not None
        assert failure.kind == "rate_limited"


class TestChannelFeedPagination:
    @pytest.mark.asyncio
    async def test_follows_cursor_across_two_pages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        page1 = [_cast("h1", "msg1", now)]
        page2 = [_cast("h2", "msg2", now - timedelta(minutes=1))]

        transport = _make_transport([
            httpx.Response(200, json={"casts": page1, "next": {"cursor": "c1"}}),
            httpx.Response(200, json={"casts": page2, "next": {"cursor": None}}),
        ])

        monkeypatch.setattr("scout.platforms.farcaster.NEYNAR_API_URL", "https://api.example")

        scanner = FarcasterScanner(api_key="test-key", max_results_per_query=10)
        async with httpx.AsyncClient(transport=transport) as client:
            casts, ceiling, failure = await scanner._channel_feed_paginated(
                client, "dev", max_pages=10
            )

        assert failure is None
        assert not ceiling
        assert len(casts) == 2
        assert [c["hash"] for c in casts] == ["h1", "h2"]

    @pytest.mark.asyncio
    async def test_old_item_then_newer_on_later_page_does_not_stop_pagination(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Channel feeds are not guaranteed chronological: an old item on an
        early page must not stop pagination before a newer page is seen.
        """
        now = datetime.now(UTC)

        page1 = [_cast("old1", "old", now - timedelta(hours=2))]
        page2 = [_cast("new1", "new", now)]

        transport = _make_transport([
            httpx.Response(200, json={"casts": page1, "next": {"cursor": "c1"}}),
            httpx.Response(200, json={"casts": page2, "next": {"cursor": None}}),
        ])

        monkeypatch.setattr("scout.platforms.farcaster.NEYNAR_API_URL", "https://api.example")

        scanner = FarcasterScanner(api_key="test-key", max_results_per_query=10)
        async with httpx.AsyncClient(transport=transport) as client:
            casts, ceiling, failure = await scanner._channel_feed_paginated(
                client, "dev", max_pages=10
            )

        assert failure is None
        assert not ceiling
        assert len(casts) == 2
        assert [c["hash"] for c in casts] == ["old1", "new1"]


# ---------------------------------------------------------------------------
# Error classification tests
# ---------------------------------------------------------------------------


class TestFetchMessagesErrors:
    @pytest.mark.asyncio
    async def test_429_retries_once_then_repeated_429_returns_rate_limited_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second consecutive 429 (after the one automatic retry) still
        surfaces as a rate_limited failure — retries are bounded to one."""
        transport = _make_transport([
            httpx.Response(429, text="rate limited", headers={"Retry-After": "60"}),
            httpx.Response(429, text="still rate limited", headers={"Retry-After": "5"}),
        ])
        _patch_http(monkeypatch, transport)
        monkeypatch.setattr("scout.platforms.farcaster.NEYNAR_API_URL", "https://api.example")

        sleeps: list[float] = []

        async def fake_sleeper(delay: float) -> None:
            sleeps.append(delay)

        scanner = FarcasterScanner(
            api_key="test-key", max_results_per_query=10, sleeper=fake_sleeper
        )
        result = await scanner.fetch_messages(queries=["hello"])

        assert isinstance(result, PlatformFetchSuccess)
        assert result.messages == []
        assert sleeps == [60.0]  # exactly one retry attempted
        assert len(result.failures) == 1
        failure = result.failures[0]
        assert failure.kind == "rate_limited"
        assert failure.http_status == 429
        assert failure.retry_after == "5"
        assert failure.retryable is True

    @pytest.mark.asyncio
    async def test_403_returns_auth_error_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transport = _make_transport([
            httpx.Response(403, text="forbidden"),
        ])
        _patch_http(monkeypatch, transport)
        monkeypatch.setattr("scout.platforms.farcaster.NEYNAR_API_URL", "https://api.example")

        scanner = FarcasterScanner(api_key="test-key", max_results_per_query=10)
        result = await scanner.fetch_messages(queries=["hello"])

        assert isinstance(result, PlatformFetchSuccess)
        assert result.messages == []
        assert len(result.failures) == 1
        failure = result.failures[0]
        assert failure.kind == "auth_error"
        assert failure.retryable is False


# ---------------------------------------------------------------------------
# fetch_messages integration: dedup, self-promo filtering, ceiling flag
# ---------------------------------------------------------------------------


class TestFetchMessagesIntegration:
    @pytest.mark.asyncio
    async def test_channel_feed_paginates_past_old_item_and_since_still_excludes_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A channel feed with an old cast on page1 must still fetch page2
        (not guaranteed chronological) — and the since filter, applied
        per-item in _collect, must still exclude the old cast from output.
        """
        now = datetime.now(UTC)
        since = now - timedelta(hours=1)

        old_cast = _cast("old1", "old", now - timedelta(hours=2))
        new_cast = _cast("new1", "new", now)

        transport = _make_transport([
            httpx.Response(200, json={"casts": [old_cast], "next": {"cursor": "c1"}}),
            httpx.Response(200, json={"casts": [new_cast], "next": {"cursor": None}}),
        ])
        _patch_http(monkeypatch, transport)
        monkeypatch.setattr("scout.platforms.farcaster.NEYNAR_API_URL", "https://api.example")

        scanner = FarcasterScanner(
            api_key="test-key", channel_ids=["dev"], max_results_per_query=10
        )
        result = await scanner.fetch_messages(since=since)

        assert isinstance(result, PlatformFetchSuccess)
        assert len(result.messages) == 1
        assert result.messages[0].platform_id == "new1"

    @pytest.mark.asyncio
    async def test_dedup_across_pages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        cast_a = _cast("ha", "unique a", now)
        cast_b = _cast("hb", "unique b", now - timedelta(minutes=1))

        transport = _make_transport([
            httpx.Response(
                200,
                json={"result": {"casts": [cast_a, cast_b], "next": {"cursor": "c1"}}},
            ),
            httpx.Response(200, json={"result": {"casts": [cast_a], "next": {"cursor": None}}}),
        ])
        _patch_http(monkeypatch, transport)
        monkeypatch.setattr("scout.platforms.farcaster.NEYNAR_API_URL", "https://api.example")

        scanner = FarcasterScanner(api_key="test-key", max_results_per_query=10)
        result = await scanner.fetch_messages(queries=["hello"])

        assert isinstance(result, PlatformFetchSuccess)
        hashes = [m.platform_id for m in result.messages]
        assert hashes.count("ha") == 1
        assert len(result.messages) == 2

    @pytest.mark.asyncio
    async def test_self_promo_filtered_across_pages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        good = _cast("hg", "interesting discussion about agents", now, username="alice")
        promo = _cast(
            "hp",
            "check out @mybot for tools",
            now - timedelta(minutes=1),
            username="mybot",
        )

        transport = _make_transport([
            httpx.Response(200, json={"result": {"casts": [good], "next": {"cursor": "c1"}}}),
            httpx.Response(200, json={"result": {"casts": [promo], "next": {"cursor": None}}}),
        ])
        _patch_http(monkeypatch, transport)
        monkeypatch.setattr("scout.platforms.farcaster.NEYNAR_API_URL", "https://api.example")

        scanner = FarcasterScanner(api_key="test-key", max_results_per_query=10)
        result = await scanner.fetch_messages(queries=["agents"])

        assert isinstance(result, PlatformFetchSuccess)
        assert len(result.messages) == 1
        assert result.messages[0].platform_id == "hg"

    @pytest.mark.asyncio
    async def test_page_ceiling_sets_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)

        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            cast_item = _cast(
                f"h{call_count}",
                f"msg{call_count}",
                now - timedelta(minutes=call_count),
            )
            return httpx.Response(
                200, json={"result": {"casts": [cast_item], "next": {"cursor": "next"}}}
            )

        _patch_http(monkeypatch, httpx.MockTransport(handler))
        monkeypatch.setattr("scout.platforms.farcaster.NEYNAR_API_URL", "https://api.example")
        monkeypatch.setattr("scout.platforms.farcaster.FARCASTER_MAX_PAGES", 2)

        scanner = FarcasterScanner(api_key="test-key", max_results_per_query=10)
        result = await scanner.fetch_messages(queries=["hello"])

        assert isinstance(result, PlatformFetchSuccess)
        assert result.page_ceiling_reached is True
        assert len(result.messages) == 2

    @pytest.mark.asyncio
    async def test_results_sorted_newest_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        old = _cast("old", "old cast", now - timedelta(hours=2))
        mid = _cast("mid", "mid cast", now - timedelta(hours=1))
        new = _cast("new", "new cast", now)

        transport = _make_transport([
            httpx.Response(
                200,
                json={"result": {"casts": [old, mid, new], "next": {"cursor": None}}},
            ),
        ])
        _patch_http(monkeypatch, transport)
        monkeypatch.setattr("scout.platforms.farcaster.NEYNAR_API_URL", "https://api.example")

        scanner = FarcasterScanner(api_key="test-key", max_results_per_query=10)
        result = await scanner.fetch_messages(queries=["hello"])

        assert isinstance(result, PlatformFetchSuccess)
        ids = [m.platform_id for m in result.messages]
        assert ids == ["new", "mid", "old"]
