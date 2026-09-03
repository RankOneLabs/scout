"""Tests for BlueskyScanner.fetch_messages — pagination, error classification, and dedup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from scout.errors import PlatformFetchFailure, PlatformFetchSuccess
from scout.platforms.bluesky import BlueskyScanner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SESSION_RESP = {
    "accessJwt": "test-jwt",
    "did": "did:plc:test",
    "handle": "test.bsky.social",
}


def _post(uri: str, text: str, created_at: datetime, lang: str | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "text": text,
        "createdAt": created_at.isoformat().replace("+00:00", "Z"),
    }
    if lang:
        record["langs"] = [lang]
    return {
        "uri": f"at://did:plc:test/app.bsky.feed.post/{uri}",
        "author": {"did": "did:plc:test", "handle": "test.bsky.social", "displayName": "Test"},
        "record": record,
    }


def _make_transport(responses: list[httpx.Response]) -> httpx.MockTransport:
    """Return responses in order; session POST is always first."""
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if not queue:
            raise AssertionError(f"Unexpected request to {request.url}")
        return queue.pop(0)

    return httpx.MockTransport(handler)


def _patch_http(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    """Patch httpx.AsyncClient to inject *transport* into every instantiation."""
    _original = httpx.AsyncClient

    def factory(**kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return _original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("scout.platforms.bluesky.httpx.AsyncClient", factory)


# ---------------------------------------------------------------------------
# Pagination tests
# ---------------------------------------------------------------------------


class TestSearchPagination:
    @pytest.mark.asyncio
    async def test_follows_cursor_across_two_pages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        page1 = [_post("p1", "hello", now), _post("p2", "world", now - timedelta(minutes=1))]
        page2 = [_post("p3", "foo", now - timedelta(minutes=2))]

        # _search_paginated is called directly — no session request, so no SESSION_RESP
        transport = _make_transport([
            httpx.Response(200, json={"posts": page1, "cursor": "cursor1"}),
            httpx.Response(200, json={"posts": page2, "cursor": None}),
        ])

        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        scanner = BlueskyScanner(max_results_per_query=10)
        async with httpx.AsyncClient(transport=transport) as client:
            posts, ceiling, failure = await scanner._search_paginated(
                client, "hello", {}, since=None, max_pages=10
            )

        assert failure is None
        assert not ceiling
        assert len(posts) == 3
        assert [p["uri"].split("/")[-1] for p in posts] == ["p1", "p2", "p3"]

    @pytest.mark.asyncio
    async def test_stops_at_since_boundary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        since = now - timedelta(minutes=5)

        page1 = [
            _post("p1", "new", now),
            _post("p2", "old", now - timedelta(minutes=6)),  # older than since
        ]

        transport = _make_transport([
            httpx.Response(200, json={"posts": page1, "cursor": "cursor1"}),
            # page2 must NOT be fetched — boundary hit on page1
        ])

        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        scanner = BlueskyScanner(max_results_per_query=10)
        async with httpx.AsyncClient(transport=transport) as client:
            posts, ceiling, failure = await scanner._search_paginated(
                client, "hello", {}, since=since, max_pages=10
            )

        assert failure is None
        assert not ceiling
        # Both posts from page1 are returned (since filter applied in _collect later)
        assert len(posts) == 2

    @pytest.mark.asyncio
    async def test_naive_created_at_does_not_crash_since_boundary_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A naive createdAt (no timezone) in the early-stop boundary check
        must not raise TypeError from comparing naive to aware `since` — it
        should be treated as inconclusive and pagination should continue.
        """
        now = datetime.now(UTC)
        since = now - timedelta(minutes=5)

        naive_post = _post("naive", "no tz", now)
        naive_post["record"]["createdAt"] = "2026-01-01T00:00:00"  # no Z, no offset

        old_post = _post("old", "old", now - timedelta(minutes=6))  # older than since

        transport = _make_transport([
            httpx.Response(200, json={"posts": [naive_post], "cursor": "cursor1"}),
            httpx.Response(200, json={"posts": [old_post], "cursor": "cursor2"}),
        ])

        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        scanner = BlueskyScanner(max_results_per_query=10)
        async with httpx.AsyncClient(transport=transport) as client:
            posts, ceiling, failure = await scanner._search_paginated(
                client, "hello", {}, since=since, max_pages=10
            )

        assert failure is None
        assert not ceiling
        # page1's naive item didn't crash or trigger early-stop; page2's aware,
        # older-than-since item did — so page3 was never fetched.
        assert len(posts) == 2

    @pytest.mark.asyncio
    async def test_debug_log_includes_language(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        now = datetime.now(UTC)
        post = _post("p1", "msg", now)

        transport = _make_transport([
            httpx.Response(200, json={"posts": [post], "cursor": None}),
        ])
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        scanner = BlueskyScanner(max_results_per_query=10)
        with caplog.at_level("DEBUG", logger="scout.platforms.bluesky"):
            async with httpx.AsyncClient(transport=transport) as client:
                await scanner._search_paginated(
                    client, "hello", {}, since=None, max_pages=10, lang="en"
                )

        assert any("lang=en" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_page_ceiling_returns_ceiling_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        post = _post("p1", "msg", now)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"posts": [post], "cursor": "next"})

        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        scanner = BlueskyScanner(max_results_per_query=10)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            posts, ceiling, failure = await scanner._search_paginated(
                client, "hello", {}, since=None, max_pages=2
            )

        assert failure is None
        assert ceiling is True
        assert len(posts) == 2  # 2 pages, 1 post each

    @pytest.mark.asyncio
    async def test_no_cursor_on_last_page_not_ceiling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        posts_data = [_post("p1", "msg", now)]

        transport = _make_transport([
            httpx.Response(200, json={"posts": posts_data, "cursor": None}),
        ])

        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        scanner = BlueskyScanner(max_results_per_query=10)
        async with httpx.AsyncClient(transport=transport) as client:
            posts, ceiling, failure = await scanner._search_paginated(
                client, "hello", {}, since=None, max_pages=2
            )

        assert failure is None
        assert not ceiling
        assert len(posts) == 1


class TestRetryOn429:
    @pytest.mark.asyncio
    async def test_retries_same_cursor_page_after_delta_seconds_delay(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        post = _post("p1", "recovered", now)

        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(
                    429, text="rate limited", headers={"Retry-After": "2.5"}
                )
            return httpx.Response(200, json={"posts": [post], "cursor": None})

        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        sleeps: list[float] = []

        async def fake_sleeper(delay: float) -> None:
            sleeps.append(delay)

        scanner = BlueskyScanner(max_results_per_query=10, sleeper=fake_sleeper)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            posts, ceiling, failure = await scanner._search_paginated(
                client, "hello", {}, since=None, max_pages=10
            )

        assert failure is None
        assert not ceiling
        assert sleeps == [2.5]
        assert len(posts) == 1
        # Retry hit the exact same cursor page (cursor param absent both times).
        assert len(requests) == 2
        assert requests[0].url.params.get("cursor") == requests[1].url.params.get("cursor")

    @pytest.mark.asyncio
    async def test_retries_after_http_date_delay_using_injected_clock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        post = _post("p1", "recovered", now)
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
            return httpx.Response(200, json={"posts": [post], "cursor": None})

        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        sleeps: list[float] = []

        async def fake_sleeper(delay: float) -> None:
            sleeps.append(delay)

        scanner = BlueskyScanner(
            max_results_per_query=10, sleeper=fake_sleeper, clock=lambda: clock_now
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            posts, ceiling, failure = await scanner._search_paginated(
                client, "hello", {}, since=None, max_pages=10
            )

        assert failure is None
        assert not ceiling
        assert sleeps == [7.0]  # 00:00:07 GMT minus the injected clock's 00:00:00
        assert len(posts) == 1

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

        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        sleeps: list[float] = []

        async def fake_sleeper(delay: float) -> None:
            sleeps.append(delay)

        scanner = BlueskyScanner(max_results_per_query=10, sleeper=fake_sleeper)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            posts, ceiling, failure = await scanner._search_paginated(
                client, "hello", {}, since=None, max_pages=10
            )

        assert sleeps == []  # no retry attempted
        assert len(requests) == 1
        assert not ceiling
        assert posts == []
        assert failure is not None
        assert failure.kind == "rate_limited"

    @pytest.mark.asyncio
    async def test_absent_retry_after_returns_rate_limited_without_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(429, text="rate limited")

        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        sleeps: list[float] = []

        async def fake_sleeper(delay: float) -> None:
            sleeps.append(delay)

        scanner = BlueskyScanner(max_results_per_query=10, sleeper=fake_sleeper)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            posts, ceiling, failure = await scanner._search_paginated(
                client, "hello", {}, since=None, max_pages=10
            )

        assert sleeps == []
        assert len(requests) == 1
        assert failure is not None
        assert failure.kind == "rate_limited"


class TestFeedPagination:
    @pytest.mark.asyncio
    async def test_follows_cursor_across_two_pages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        page1_items = [{"post": _post("p1", "msg1", now)}]
        page2_items = [{"post": _post("p2", "msg2", now - timedelta(minutes=1))}]

        # _feed_paginated called directly — no session request
        transport = _make_transport([
            httpx.Response(200, json={"feed": page1_items, "cursor": "cur1"}),
            httpx.Response(200, json={"feed": page2_items, "cursor": None}),
        ])

        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        scanner = BlueskyScanner(
            feed_uris=["at://did:plc:test/app.bsky.feed.generator/myfeed"],
            max_results_per_query=10,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            posts, ceiling, failure = await scanner._feed_paginated(
                client,
                "at://did:plc:test/app.bsky.feed.generator/myfeed",
                {},
                max_pages=10,
            )

        assert failure is None
        assert not ceiling
        assert len(posts) == 2

    @pytest.mark.asyncio
    async def test_old_item_then_newer_on_later_page_does_not_stop_pagination(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Custom feeds are relevance-ordered, not chronological: an old item
        on an early page must not stop pagination before a newer page is seen.
        """
        now = datetime.now(UTC)

        # page1 has an item far older than page2's — with a chronological
        # feed this would stop pagination; a relevance-ordered feed must not.
        page1_items = [{"post": _post("old1", "old", now - timedelta(hours=2))}]
        page2_items = [{"post": _post("new1", "new", now)}]

        transport = _make_transport([
            httpx.Response(200, json={"feed": page1_items, "cursor": "cur1"}),
            httpx.Response(200, json={"feed": page2_items, "cursor": None}),
        ])

        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        scanner = BlueskyScanner(
            feed_uris=["at://did:plc:test/app.bsky.feed.generator/myfeed"],
            max_results_per_query=10,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            posts, ceiling, failure = await scanner._feed_paginated(
                client,
                "at://did:plc:test/app.bsky.feed.generator/myfeed",
                {},
                max_pages=10,
            )

        assert failure is None
        assert not ceiling
        # Both pages fetched despite page1's item being older than `since` —
        # the since filter is applied later, in _collect, not during paging.
        assert len(posts) == 2
        assert [p["uri"].split("/")[-1] for p in posts] == ["old1", "new1"]


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
            httpx.Response(200, json=SESSION_RESP),
            httpx.Response(429, text="rate limited", headers={"Retry-After": "30"}),
            httpx.Response(429, text="still rate limited", headers={"Retry-After": "5"}),
        ])
        _patch_http(monkeypatch, transport)
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_IDENTIFIER", "user")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_APP_PASSWORD", "pass")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        sleeps: list[float] = []

        async def fake_sleeper(delay: float) -> None:
            sleeps.append(delay)

        scanner = BlueskyScanner(max_results_per_query=10, sleeper=fake_sleeper)
        result = await scanner.fetch_messages(queries=["hello"])

        assert isinstance(result, PlatformFetchSuccess)
        assert result.messages == []
        assert sleeps == [30.0]  # exactly one retry attempted
        assert len(result.failures) == 1
        failure = result.failures[0]
        assert failure.kind == "rate_limited"
        assert failure.http_status == 429
        assert failure.retry_after == "5"
        assert failure.retryable is True

    @pytest.mark.asyncio
    async def test_401_returns_auth_error_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transport = _make_transport([
            httpx.Response(200, json=SESSION_RESP),
            httpx.Response(401, text="unauthorized"),
        ])
        _patch_http(monkeypatch, transport)
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_IDENTIFIER", "user")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_APP_PASSWORD", "pass")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        scanner = BlueskyScanner(max_results_per_query=10)
        result = await scanner.fetch_messages(queries=["hello"])

        assert isinstance(result, PlatformFetchSuccess)
        assert result.messages == []
        assert len(result.failures) == 1
        failure = result.failures[0]
        assert failure.kind == "auth_error"
        assert failure.retryable is False

    @pytest.mark.asyncio
    async def test_missing_credentials_returns_auth_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_IDENTIFIER", "")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_APP_PASSWORD", "")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")
        # No network needed — missing creds short-circuits before any HTTP call
        _patch_http(monkeypatch, _make_transport([]))

        scanner = BlueskyScanner(max_results_per_query=10)
        result = await scanner.fetch_messages(queries=["hello"])

        assert isinstance(result, PlatformFetchFailure)
        assert result.kind == "auth_error"
        assert result.retryable is False

    @pytest.mark.asyncio
    async def test_no_queries_returns_empty_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transport = _make_transport([httpx.Response(200, json=SESSION_RESP)])
        _patch_http(monkeypatch, transport)
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_IDENTIFIER", "user")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_APP_PASSWORD", "pass")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        scanner = BlueskyScanner(max_results_per_query=10)
        result = await scanner.fetch_messages(queries=[])

        assert isinstance(result, PlatformFetchSuccess)
        assert result.messages == []
        assert not result.page_ceiling_reached


# ---------------------------------------------------------------------------
# fetch_messages integration: dedup, filtering, ceiling flag
# ---------------------------------------------------------------------------


class TestFetchMessagesIntegration:
    @pytest.mark.asyncio
    async def test_executes_cartesian_product_of_queries_and_languages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every (query, language) pair gets its own search request, sharing
        one AT-URI seen set across pairs so a repeated URI is deduped even
        when it surfaces under a different query/language combination.
        """
        now = datetime.now(UTC)
        shared_post = _post("shared", "shared post", now)

        seen_requests: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if "createSession" in str(request.url):
                return httpx.Response(200, json=SESSION_RESP)
            params = dict(request.url.params)
            seen_requests.append(params)
            q, lang = params["q"], params.get("lang", "")
            if (q, lang) == ("alpha", "en"):
                # Also appears under beta/fr below — cross-pair dedup target.
                return httpx.Response(200, json={"posts": [shared_post], "cursor": None})
            if (q, lang) == ("beta", "fr"):
                return httpx.Response(200, json={"posts": [shared_post], "cursor": None})
            unique_post = _post(f"{q}-{lang}", f"{q} {lang}", now)
            return httpx.Response(200, json={"posts": [unique_post], "cursor": None})

        _patch_http(monkeypatch, httpx.MockTransport(handler))
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_IDENTIFIER", "user")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_APP_PASSWORD", "pass")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        scanner = BlueskyScanner(max_results_per_query=10, languages=("en", "fr"))
        result = await scanner.fetch_messages(queries=["alpha", "beta"])

        assert isinstance(result, PlatformFetchSuccess)
        # 2 queries x 2 languages = 4 search requests.
        assert len(seen_requests) == 4
        assert {(r["q"], r["lang"]) for r in seen_requests} == {
            ("alpha", "en"), ("alpha", "fr"), ("beta", "en"), ("beta", "fr"),
        }
        # 4 pairs surface 4 posts total, but "shared" appears twice
        # (alpha/en and beta/fr) and collapses to one via the shared seen set.
        ids = {m.platform_id.split("/")[-1] for m in result.messages}
        assert ids == {"shared", "alpha-fr", "beta-en"}
        assert len(result.messages) == 3

    @pytest.mark.asyncio
    async def test_no_languages_configured_runs_each_query_once_without_lang(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen_requests: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if "createSession" in str(request.url):
                return httpx.Response(200, json=SESSION_RESP)
            seen_requests.append(dict(request.url.params))
            return httpx.Response(200, json={"posts": [], "cursor": None})

        _patch_http(monkeypatch, httpx.MockTransport(handler))
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_IDENTIFIER", "user")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_APP_PASSWORD", "pass")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        scanner = BlueskyScanner(max_results_per_query=10)
        result = await scanner.fetch_messages(queries=["alpha", "beta"])

        assert isinstance(result, PlatformFetchSuccess)
        assert len(seen_requests) == 2  # one request per query, no lang fan-out
        assert all("lang" not in r for r in seen_requests)

    @pytest.mark.asyncio
    async def test_each_query_language_pair_has_independent_page_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each (query, lang) pair paginates with its own cursor: page 2 of
        one pair must not be confused with page 1 of another."""
        now = datetime.now(UTC)

        def handler(request: httpx.Request) -> httpx.Response:
            if "createSession" in str(request.url):
                return httpx.Response(200, json=SESSION_RESP)
            params = dict(request.url.params)
            lang = params.get("lang", "")
            if not params.get("cursor"):
                post = _post(f"{lang}-p1", f"{lang} page1", now)
                return httpx.Response(
                    200, json={"posts": [post], "cursor": f"{lang}-cur"}
                )
            post = _post(f"{lang}-p2", f"{lang} page2", now - timedelta(minutes=1))
            return httpx.Response(200, json={"posts": [post], "cursor": None})

        _patch_http(monkeypatch, httpx.MockTransport(handler))
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_IDENTIFIER", "user")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_APP_PASSWORD", "pass")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        scanner = BlueskyScanner(max_results_per_query=10, languages=("en", "fr"))
        result = await scanner.fetch_messages(queries=["alpha"])

        assert isinstance(result, PlatformFetchSuccess)
        ids = {m.platform_id.split("/")[-1] for m in result.messages}
        assert ids == {"en-p1", "en-p2", "fr-p1", "fr-p2"}

    @pytest.mark.asyncio
    async def test_feed_paginates_past_old_item_and_since_still_excludes_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A custom feed with an old item on page1 must still fetch page2
        (relevance order, not chronological) — and the since filter, applied
        per-item in _collect, must still exclude the old item from output.
        """
        now = datetime.now(UTC)
        since = now - timedelta(hours=1)

        old_post = _post("old1", "old", now - timedelta(hours=2))
        new_post = _post("new1", "new", now)

        transport = _make_transport([
            httpx.Response(200, json=SESSION_RESP),
            httpx.Response(200, json={"feed": [{"post": old_post}], "cursor": "cur1"}),
            httpx.Response(200, json={"feed": [{"post": new_post}], "cursor": None}),
        ])
        _patch_http(monkeypatch, transport)
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_IDENTIFIER", "user")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_APP_PASSWORD", "pass")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        scanner = BlueskyScanner(
            feed_uris=["at://did:plc:test/app.bsky.feed.generator/myfeed"],
            max_results_per_query=10,
        )
        result = await scanner.fetch_messages(since=since)

        assert isinstance(result, PlatformFetchSuccess)
        # Both feed pages were consumed (transport queue empty, no assertion
        # error raised) even though page1's item predates `since`.
        assert len(result.messages) == 1
        assert result.messages[0].platform_id.split("/")[-1] == "new1"

    @pytest.mark.asyncio
    async def test_dedup_across_pages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        post_a = _post("pa", "unique a", now)
        post_b = _post("pb", "unique b", now - timedelta(minutes=1))

        transport = _make_transport([
            httpx.Response(200, json=SESSION_RESP),
            httpx.Response(200, json={"posts": [post_a, post_b], "cursor": "cur1"}),
            httpx.Response(200, json={"posts": [post_a], "cursor": None}),
        ])
        _patch_http(monkeypatch, transport)
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_IDENTIFIER", "user")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_APP_PASSWORD", "pass")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        scanner = BlueskyScanner(max_results_per_query=10)
        result = await scanner.fetch_messages(queries=["hello"])

        assert isinstance(result, PlatformFetchSuccess)
        ids = [m.platform_id.split("/")[-1] for m in result.messages]
        assert ids.count("pa") == 1  # deduped
        assert len(result.messages) == 2

    @pytest.mark.asyncio
    async def test_language_filter_applied_across_pages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        en_post = _post("en1", "english", now, lang="en")
        fr_post = _post("fr1", "french", now - timedelta(minutes=1), lang="fr")

        transport = _make_transport([
            httpx.Response(200, json=SESSION_RESP),
            httpx.Response(200, json={"posts": [en_post, fr_post], "cursor": None}),
        ])
        _patch_http(monkeypatch, transport)
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_IDENTIFIER", "user")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_APP_PASSWORD", "pass")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        scanner = BlueskyScanner(max_results_per_query=10, languages=("en",))
        result = await scanner.fetch_messages(queries=["hello"])

        assert isinstance(result, PlatformFetchSuccess)
        assert len(result.messages) == 1
        assert result.messages[0].content == "english"

    @pytest.mark.asyncio
    async def test_page_ceiling_sets_flag_on_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        post = _post("p1", "msg", now)

        def handler(request: httpx.Request) -> httpx.Response:
            if "createSession" in str(request.url):
                return httpx.Response(200, json=SESSION_RESP)
            return httpx.Response(200, json={"posts": [post], "cursor": "next"})

        _patch_http(monkeypatch, httpx.MockTransport(handler))
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_IDENTIFIER", "user")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_APP_PASSWORD", "pass")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_MAX_PAGES", 3)

        scanner = BlueskyScanner(max_results_per_query=10)
        result = await scanner.fetch_messages(queries=["hello"])

        assert isinstance(result, PlatformFetchSuccess)
        assert result.page_ceiling_reached is True
        # Same URI on every page — dedup collapses to 1 unique message
        assert len(result.messages) == 1

    @pytest.mark.asyncio
    async def test_results_sorted_newest_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        old = _post("old", "old post", now - timedelta(hours=2))
        mid = _post("mid", "mid post", now - timedelta(hours=1))
        new = _post("new", "new post", now)

        transport = _make_transport([
            httpx.Response(200, json=SESSION_RESP),
            httpx.Response(200, json={"posts": [old, mid, new], "cursor": None}),
        ])
        _patch_http(monkeypatch, transport)
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_IDENTIFIER", "user")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_APP_PASSWORD", "pass")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        scanner = BlueskyScanner(max_results_per_query=10)
        result = await scanner.fetch_messages(queries=["hello"])

        assert isinstance(result, PlatformFetchSuccess)
        ids = [m.platform_id.split("/")[-1] for m in result.messages]
        assert ids == ["new", "mid", "old"]


# ---------------------------------------------------------------------------
# Parent context helpers
# ---------------------------------------------------------------------------


def _reply_post(
    uri: str,
    text: str,
    created_at: datetime,
    parent_uri: str,
) -> dict[str, Any]:
    """Build a raw Bluesky post dict with a reply reference."""
    return {
        "uri": f"at://did:plc:test/app.bsky.feed.post/{uri}",
        "author": {"did": "did:plc:test", "handle": "test.bsky.social", "displayName": "Test"},
        "record": {
            "text": text,
            "createdAt": created_at.isoformat().replace("+00:00", "Z"),
            "reply": {
                "parent": {"uri": parent_uri, "cid": "bafycid001"},
                "root": {"uri": parent_uri, "cid": "bafycid001"},
            },
        },
    }


def _parent_view(uri: str, text: str, handle: str = "parent.bsky.social") -> dict[str, Any]:
    """Build a raw post-view dict as returned by app.bsky.feed.getPosts."""
    return {
        "uri": uri,
        "cid": "bafycidparent",
        "author": {
            "did": "did:plc:parentauthor",
            "handle": handle,
            "displayName": "Parent Author",
        },
        "record": {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": "2026-07-10T14:00:00.000Z",
        },
        "indexedAt": "2026-07-10T14:00:05.000Z",
    }


# ---------------------------------------------------------------------------
# Parent context tests
# ---------------------------------------------------------------------------


class TestParentContext:
    @pytest.mark.asyncio
    async def test_reply_resolves_parent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = datetime.now(UTC)
        parent_uri = "at://did:plc:parentauthor/app.bsky.feed.post/parent001"
        child = _reply_post("child001", "Great point!", now, parent_uri)

        transport = _make_transport([
            httpx.Response(200, json=SESSION_RESP),
            httpx.Response(200, json={"posts": [child], "cursor": None}),
            httpx.Response(200, json={"posts": [_parent_view(parent_uri, "Hello world")]}),
        ])
        _patch_http(monkeypatch, transport)
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_IDENTIFIER", "user")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_APP_PASSWORD", "pass")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        scanner = BlueskyScanner(max_results_per_query=10)
        result = await scanner.fetch_messages(queries=["hello"])

        assert isinstance(result, PlatformFetchSuccess)
        assert len(result.messages) == 1
        msg = result.messages[0]
        assert msg.parent_lookup_status == "resolved"
        assert msg.parent is not None
        assert msg.parent.text == "Hello world"
        assert msg.parent.author.id == "did:plc:parentauthor"

    @pytest.mark.asyncio
    async def test_non_reply_has_not_applicable_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        child = _post("nonreply001", "standalone post", now)

        transport = _make_transport([
            httpx.Response(200, json=SESSION_RESP),
            httpx.Response(200, json={"posts": [child], "cursor": None}),
            # No getPosts call expected for non-replies
        ])
        _patch_http(monkeypatch, transport)
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_IDENTIFIER", "user")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_APP_PASSWORD", "pass")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        scanner = BlueskyScanner(max_results_per_query=10)
        result = await scanner.fetch_messages(queries=["hello"])

        assert isinstance(result, PlatformFetchSuccess)
        assert len(result.messages) == 1
        msg = result.messages[0]
        assert msg.parent_lookup_status == "not_applicable"
        assert msg.parent is None

    @pytest.mark.asyncio
    async def test_missing_parent_emits_failure_and_marks_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        parent_uri = "at://did:plc:gone/app.bsky.feed.post/deleted001"
        child = _reply_post("child002", "replying to deleted post", now, parent_uri)

        transport = _make_transport([
            httpx.Response(200, json=SESSION_RESP),
            httpx.Response(200, json={"posts": [child], "cursor": None}),
            # getPosts returns empty list (parent deleted/unavailable)
            httpx.Response(200, json={"posts": []}),
        ])
        _patch_http(monkeypatch, transport)
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_IDENTIFIER", "user")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_APP_PASSWORD", "pass")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        scanner = BlueskyScanner(max_results_per_query=10)
        result = await scanner.fetch_messages(queries=["hello"])

        assert isinstance(result, PlatformFetchSuccess)
        assert len(result.messages) == 1
        msg = result.messages[0]
        assert msg.parent_lookup_status == "failed"
        assert msg.parent is None
        # Child preserved; non-fatal failure emitted
        parent_failures = [f for f in result.failures if f.kind == "parent_context"]
        assert len(parent_failures) == 1

    @pytest.mark.asyncio
    async def test_parent_dedup_across_shared_parent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two children sharing the same parent URI trigger only one getPosts request."""
        now = datetime.now(UTC)
        parent_uri = "at://did:plc:parentauthor/app.bsky.feed.post/shared001"
        child1 = _reply_post("child_a", "first reply", now, parent_uri)
        child2 = _reply_post("child_b", "second reply", now - timedelta(minutes=1), parent_uri)

        transport = _make_transport([
            httpx.Response(200, json=SESSION_RESP),
            httpx.Response(200, json={"posts": [child1, child2], "cursor": None}),
            # Only one getPosts call for the shared parent
            httpx.Response(200, json={"posts": [_parent_view(parent_uri, "The original post")]}),
        ])
        _patch_http(monkeypatch, transport)
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_IDENTIFIER", "user")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_APP_PASSWORD", "pass")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        scanner = BlueskyScanner(max_results_per_query=10)
        result = await scanner.fetch_messages(queries=["hello"])

        assert isinstance(result, PlatformFetchSuccess)
        assert len(result.messages) == 2
        for msg in result.messages:
            assert msg.parent_lookup_status == "resolved"
            assert msg.parent is not None
            assert msg.parent.text == "The original post"

    @pytest.mark.asyncio
    async def test_parent_fetch_http_error_marks_failed_non_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        parent_uri = "at://did:plc:parentauthor/app.bsky.feed.post/err001"
        child = _reply_post("errchild", "replying", now, parent_uri)

        transport = _make_transport([
            httpx.Response(200, json=SESSION_RESP),
            httpx.Response(200, json={"posts": [child], "cursor": None}),
            httpx.Response(500, text="internal server error"),
        ])
        _patch_http(monkeypatch, transport)
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_IDENTIFIER", "user")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_APP_PASSWORD", "pass")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        scanner = BlueskyScanner(max_results_per_query=10)
        result = await scanner.fetch_messages(queries=["hello"])

        assert isinstance(result, PlatformFetchSuccess)
        # Child always included even when parent fetch fails
        assert len(result.messages) == 1
        assert result.messages[0].parent_lookup_status == "failed"
        parent_failures = [f for f in result.failures if f.kind == "parent_context"]
        assert len(parent_failures) >= 1

    @pytest.mark.asyncio
    async def test_malformed_parent_view_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        parent_uri = "at://did:plc:parentauthor/app.bsky.feed.post/malformed001"
        child = _reply_post("child_malformed", "replying", now, parent_uri)
        # Malformed: missing author did and empty text
        malformed_parent = {
            "uri": parent_uri,
            "author": {"did": "", "handle": "nobody"},
            "record": {"text": ""},
        }

        transport = _make_transport([
            httpx.Response(200, json=SESSION_RESP),
            httpx.Response(200, json={"posts": [child], "cursor": None}),
            httpx.Response(200, json={"posts": [malformed_parent]}),
        ])
        _patch_http(monkeypatch, transport)
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_IDENTIFIER", "user")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_APP_PASSWORD", "pass")
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")

        scanner = BlueskyScanner(max_results_per_query=10)
        result = await scanner.fetch_messages(queries=["hello"])

        assert isinstance(result, PlatformFetchSuccess)
        assert len(result.messages) == 1
        # Malformed parent treated as unresolved
        assert result.messages[0].parent_lookup_status == "failed"
