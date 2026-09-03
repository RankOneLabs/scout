"""Tests for Bluesky dedup and post conversion logic."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import httpx
import pytest

from scout.config import Message
from scout.platforms.bluesky import BlueskyScanner


class TestPostToMessage:
    def test_basic_conversion(self) -> None:
        post: dict[str, object] = {
            "uri": "at://did:plc:abc123/app.bsky.feed.post/xyz789",
            "author": {
                "did": "did:plc:abc123",
                "handle": "alice.bsky.social",
                "displayName": "Alice W",
            },
            "record": {
                "text": "Hello world",
                "createdAt": "2026-03-18T12:00:00.000Z",
            },
        }
        now = datetime.now(UTC)
        msg = BlueskyScanner._post_to_message(post, "Hello world", now)

        assert msg.platform == "bluesky"
        assert msg.platform_id == "at://did:plc:abc123/app.bsky.feed.post/xyz789"
        assert msg.channel_name == "bluesky"
        assert msg.author_name == "Alice W"
        assert msg.author_id == "did:plc:abc123"
        assert msg.content == "Hello world"
        assert msg.created_at == now
        assert msg.url == "https://bsky.app/profile/alice.bsky.social/post/xyz789"

    def test_missing_display_name_falls_back_to_handle(self) -> None:
        post: dict[str, object] = {
            "uri": "at://did:plc:abc/app.bsky.feed.post/123",
            "author": {
                "did": "did:plc:abc",
                "handle": "bob.bsky.social",
            },
        }
        msg = BlueskyScanner._post_to_message(post, "test", datetime.now(UTC))
        assert msg.author_name == "bob.bsky.social"

    def test_missing_author_fields(self) -> None:
        post: dict[str, object] = {
            "uri": "",
            "author": {},
        }
        msg = BlueskyScanner._post_to_message(post, "test", datetime.now(UTC))
        assert msg.author_name == "unknown"
        assert msg.url == ""

    def test_url_construction_from_uri(self) -> None:
        post: dict[str, object] = {
            "uri": "at://did:plc:xyz/app.bsky.feed.post/rkey456",
            "author": {
                "handle": "carol.bsky.social",
            },
        }
        msg = BlueskyScanner._post_to_message(post, "test", datetime.now(UTC))
        assert msg.url == "https://bsky.app/profile/carol.bsky.social/post/rkey456"


class TestSearchGuards:
    async def test_empty_query_returns_without_network_call(self) -> None:
        scanner = BlueskyScanner()
        posts, ceiling, failure = await scanner._search_paginated(
            client=cast(httpx.AsyncClient, object()),
            query=" \n ",
            headers={},
            since=None,
            max_pages=10,
        )
        assert posts == []
        assert not ceiling
        assert failure is None

    async def test_explicit_lang_is_sent_to_search(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen_params: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_params.update(dict(request.url.params))
            return httpx.Response(200, json={"posts": []})

        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")
        scanner = BlueskyScanner(languages=("en",))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            posts, ceiling, failure = await scanner._search_paginated(
                client, "agent eval", headers={}, since=None, max_pages=10, lang="en"
            )

        assert posts == []
        assert not ceiling
        assert failure is None
        assert seen_params["lang"] == "en"

    async def test_configured_language_values_are_normalized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")
        scanner = BlueskyScanner(languages=(" EN ", ""))

        assert scanner.languages == ("en",)

    async def test_no_lang_omits_lang_param(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen_params: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_params.update(dict(request.url.params))
            return httpx.Response(200, json={"posts": []})

        monkeypatch.setattr("scout.platforms.bluesky.BLUESKY_API_URL", "https://bsky.example/xrpc")
        scanner = BlueskyScanner()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            posts, ceiling, failure = await scanner._search_paginated(
                client, "agent eval", headers={}, since=None, max_pages=10
            )

        assert posts == []
        assert not ceiling
        assert failure is None
        assert "lang" not in seen_params


class TestLanguageFiltering:
    def test_collect_keeps_matching_and_missing_langs(self) -> None:
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        posts: list[dict[str, object]] = [
            {
                "uri": "at://did:plc:abc/app.bsky.feed.post/en",
                "author": {"handle": "alice.bsky.social"},
                "record": {"text": "hello", "createdAt": now, "langs": ["en"]},
            },
            {
                "uri": "at://did:plc:abc/app.bsky.feed.post/missing",
                "author": {"handle": "alice.bsky.social"},
                "record": {"text": "no metadata", "createdAt": now},
            },
            {
                "uri": "at://did:plc:abc/app.bsky.feed.post/fr",
                "author": {"handle": "alice.bsky.social"},
                "record": {"text": "bonjour", "createdAt": now, "langs": ["fr"]},
            },
        ]
        out: list[Message] = []

        BlueskyScanner(languages=("en",))._collect(
            posts,
            seen_uris=set(),
            out=out,
            since=None,
        )

        assert [msg.platform_id.rsplit("/", 1)[-1] for msg in out] == ["en", "missing"]

    def test_language_filter_matches_primary_subtag(self) -> None:
        scanner = BlueskyScanner(languages=("en",))
        post = {"record": {"langs": ["en-US"]}}

        assert scanner._allows_language(post)
