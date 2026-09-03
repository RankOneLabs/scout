"""Tests for Farcaster dedup and self-promo filtering logic."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import httpx

from scout.platforms.farcaster import FarcasterScanner


class TestIsSelfPromo:
    def test_self_promo_detected(self) -> None:
        assert FarcasterScanner._is_self_promo(
            "consider using mybot.xyz for payments",
            "mybot",
        )

    def test_self_promo_with_at_mention(self) -> None:
        assert FarcasterScanner._is_self_promo(
            "check out @alice for great tools",
            "alice",
        )

    def test_long_text_not_promo(self) -> None:
        # Long text (>=200 chars) should never be flagged
        long_text = "consider using mybot.xyz " + "x" * 200
        assert not FarcasterScanner._is_self_promo(long_text, "mybot")

    def test_no_username_not_promo(self) -> None:
        assert not FarcasterScanner._is_self_promo("check out this tool", "")

    def test_no_promo_phrase_not_flagged(self) -> None:
        assert not FarcasterScanner._is_self_promo(
            "mybot.xyz is interesting",
            "mybot",
        )

    def test_other_user_mention_not_flagged(self) -> None:
        assert not FarcasterScanner._is_self_promo(
            "check out @bob for tools",
            "alice",
        )


class TestSearchGuards:
    async def test_empty_query_returns_without_network_call(self) -> None:
        scanner = FarcasterScanner(api_key="test")
        posts, ceiling, failure = await scanner._search_paginated(
            client=cast(httpx.AsyncClient, object()),
            query="   ",
            since=None,
            max_pages=10,
        )
        assert posts == []
        assert not ceiling
        assert failure is None


class TestCastToMessage:
    def test_basic_conversion(self) -> None:
        cast: dict[str, object] = {
            "hash": "0xabc123def4",
            "author": {
                "username": "alice",
                "display_name": "Alice W",
                "fid": 12345,
            },
            "channel": {"id": "dev"},
        }
        now = datetime.now(UTC)
        msg = FarcasterScanner._cast_to_message(cast, "Hello world", now)

        assert msg.platform == "farcaster"
        assert msg.platform_id == "0xabc123def4"
        assert msg.channel_name == "dev"
        assert msg.author_name == "Alice W"
        assert msg.content == "Hello world"
        assert msg.created_at == now
        assert "warpcast.com/alice/" in msg.url

    def test_missing_channel_defaults_to_home(self) -> None:
        cast: dict[str, object] = {
            "hash": "0xfff",
            "author": {"username": "bob", "fid": 1},
            "channel": None,
        }
        msg = FarcasterScanner._cast_to_message(cast, "test", datetime.now(UTC))
        assert msg.channel_name == "home"

    def test_missing_author_fields(self) -> None:
        cast: dict[str, object] = {
            "hash": "0xfff",
            "author": {},
        }
        msg = FarcasterScanner._cast_to_message(cast, "test", datetime.now(UTC))
        assert msg.author_name == "unknown"
        assert msg.url == ""  # no username → no URL
