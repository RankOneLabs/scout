"""Tests for DiscordScanner.fetch_messages — auth failures, pagination, ordering, dedup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import discord
import pytest

from scout.errors import PlatformFetchFailure, PlatformFetchSuccess
from scout.platforms.discord import DiscordScanner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_message(
    msg_id: str,
    content: str,
    created_at: datetime,
    author_bot: bool = False,
) -> MagicMock:
    msg = MagicMock()
    msg.id = int(msg_id)
    msg.content = content
    msg.created_at = created_at.replace(tzinfo=None)  # discord gives naive UTC
    msg.author.bot = author_bot
    msg.author.display_name = "User"
    msg.author.id = 999
    msg.jump_url = f"https://discord.com/channels/1/2/{msg_id}"
    return msg


def _fake_channel(name: str, channel_id: int, messages: list[MagicMock]) -> MagicMock:
    """Build a fake TextChannel whose history() mimics discord.py's real
    server-side `after` filtering (exclusive lower bound) so tests exercise
    the same boundary behavior production code relies on.
    """
    channel = MagicMock(spec=discord.TextChannel)
    channel.name = name
    channel.id = channel_id
    history_calls: list[dict[str, object]] = []
    channel.history_calls = history_calls

    def history_gen(
        *, limit: int | None = None, after: datetime | None = None, oldest_first: bool = True
    ) -> object:
        history_calls.append({"limit": limit, "after": after, "oldest_first": oldest_first})

        async def gen() -> object:
            for m in messages:
                if after is not None:
                    msg_ts = m.created_at.replace(tzinfo=UTC)
                    if msg_ts <= after:
                        continue
                yield m

        return gen()

    channel.history = history_gen
    return channel


def _fake_guild(channels: dict[int, MagicMock]) -> MagicMock:
    guild = MagicMock()

    def get_channel(ch_id: int) -> MagicMock | None:
        return channels.get(ch_id)

    guild.get_channel = get_channel
    guild.fetch_channel = AsyncMock(side_effect=lambda ch_id: channels[ch_id])
    return guild


# ---------------------------------------------------------------------------
# Auth / startup failure tests
# ---------------------------------------------------------------------------


class TestStartupFailures:
    @pytest.mark.asyncio
    async def test_login_failure_returns_auth_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scanner = DiscordScanner(
            token="bad-token",
            server_id=123,
            channel_ids=[456],
        )

        with patch("scout.platforms.discord.discord.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.start = AsyncMock(side_effect=discord.LoginFailure("Invalid token"))
            mock_client.event = lambda f: f
            mock_client_cls.return_value = mock_client

            result = await scanner.fetch_messages()

        assert isinstance(result, PlatformFetchFailure)
        assert result.kind == "auth_error"
        assert result.retryable is False
        assert result.platform == "discord"

    @pytest.mark.asyncio
    async def test_unexpected_startup_exception_returns_unexpected_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scanner = DiscordScanner(
            token="tok",
            server_id=123,
            channel_ids=[456],
        )

        with patch("scout.platforms.discord.discord.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.start = AsyncMock(side_effect=RuntimeError("connection refused"))
            mock_client.event = lambda f: f
            mock_client_cls.return_value = mock_client

            result = await scanner.fetch_messages()

        assert isinstance(result, PlatformFetchFailure)
        assert result.kind == "unexpected"
        assert result.platform == "discord"


# ---------------------------------------------------------------------------
# Integration: pagination, ordering, dedup
# ---------------------------------------------------------------------------


class TestFetchMessagesIntegration:
    @pytest.mark.asyncio
    async def test_returns_messages_newest_first(self) -> None:
        now = datetime.now(UTC)
        old_msg = _fake_message("100", "old message", now - timedelta(hours=2))
        new_msg = _fake_message("200", "new message", now)

        # newest-first iteration (oldest_first=False): new_msg appears first
        channel = _fake_channel("general", 456, [new_msg, old_msg])
        guild = _fake_guild({456: channel})

        scanner = DiscordScanner(token="tok", server_id=123, channel_ids=[456])

        with patch("scout.platforms.discord.discord.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.user = SimpleNamespace(name="Bot")

            async def start_side_effect(token: str) -> None:
                # Simulate on_ready firing immediately
                assert mock_client._on_ready_handler is not None
                await mock_client._on_ready_handler()

            def event_decorator(fn: object) -> object:
                if hasattr(fn, "__name__") and fn.__name__ == "on_ready":
                    mock_client._on_ready_handler = fn
                return fn

            mock_client.event = event_decorator
            mock_client.get_guild = Mock(return_value=guild)
            mock_client.close = AsyncMock()
            mock_client.start = AsyncMock(side_effect=start_side_effect)
            mock_client_cls.return_value = mock_client

            result = await scanner.fetch_messages()

        assert isinstance(result, PlatformFetchSuccess)
        assert len(result.messages) == 2
        assert result.messages[0].platform_id == "200"  # newest first
        assert result.messages[1].platform_id == "100"
        assert channel.history_calls == [
            {"limit": None, "after": None, "oldest_first": False}
        ]

    @pytest.mark.asyncio
    async def test_since_boundary_stops_iteration(self) -> None:
        now = datetime.now(UTC)
        since = now - timedelta(hours=1)

        new_msg = _fake_message("300", "new", now)
        old_msg = _fake_message("100", "old", now - timedelta(hours=2))
        boundary_msg = _fake_message("200", "at boundary", since)  # equals since → excluded

        channel = _fake_channel("general", 456, [new_msg, boundary_msg, old_msg])
        guild = _fake_guild({456: channel})

        scanner = DiscordScanner(token="tok", server_id=123, channel_ids=[456])

        with patch("scout.platforms.discord.discord.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.user = SimpleNamespace(name="Bot")

            async def start_side_effect(token: str) -> None:
                await mock_client._on_ready_handler()

            def event_decorator(fn: object) -> object:
                if hasattr(fn, "__name__") and fn.__name__ == "on_ready":
                    mock_client._on_ready_handler = fn
                return fn

            mock_client.event = event_decorator
            mock_client.get_guild = Mock(return_value=guild)
            mock_client.close = AsyncMock()
            mock_client.start = AsyncMock(side_effect=start_side_effect)
            mock_client_cls.return_value = mock_client

            result = await scanner.fetch_messages(since=since)

        assert isinstance(result, PlatformFetchSuccess)
        # Boundary message (created_at == since) and the older message are both
        # excluded; only the strictly-newer message survives.
        assert len(result.messages) == 1
        assert result.messages[0].platform_id == "300"
        assert channel.history_calls == [
            {"limit": None, "after": since, "oldest_first": False}
        ]

    @pytest.mark.asyncio
    async def test_no_implicit_cap_returns_all_messages_after_since(self) -> None:
        """Regression test for the implicit 100-message discord.py default:
        history() must be called with limit=None so more than 100 messages
        newer than since are returned in a single page, not silently truncated.
        """
        now = datetime.now(UTC)
        since = now - timedelta(hours=1)

        messages = [
            _fake_message(str(i), f"msg {i}", now - timedelta(seconds=i))
            for i in range(101)  # all newer than since
        ]

        channel = _fake_channel("general", 456, messages)
        guild = _fake_guild({456: channel})

        scanner = DiscordScanner(token="tok", server_id=123, channel_ids=[456])

        with patch("scout.platforms.discord.discord.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.user = SimpleNamespace(name="Bot")

            async def start_side_effect(token: str) -> None:
                await mock_client._on_ready_handler()

            def event_decorator(fn: object) -> object:
                if hasattr(fn, "__name__") and fn.__name__ == "on_ready":
                    mock_client._on_ready_handler = fn
                return fn

            mock_client.event = event_decorator
            mock_client.get_guild = Mock(return_value=guild)
            mock_client.close = AsyncMock()
            mock_client.start = AsyncMock(side_effect=start_side_effect)
            mock_client_cls.return_value = mock_client

            result = await scanner.fetch_messages(since=since)

        assert isinstance(result, PlatformFetchSuccess)
        assert not result.page_ceiling_reached
        assert len(result.messages) == 101
        assert channel.history_calls == [
            {"limit": None, "after": since, "oldest_first": False}
        ]

    @pytest.mark.asyncio
    async def test_page_ceiling_sets_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = datetime.now(UTC)
        # Create more messages than max_messages * DISCORD_MAX_PAGES
        monkeypatch.setattr("scout.platforms.discord.DISCORD_MAX_PAGES", 1)

        # max_messages=2, max_pages=1 → ceiling at 2 messages
        messages = [
            _fake_message(str(i), f"msg {i}", now - timedelta(minutes=i))
            for i in range(5)  # 5 messages, ceiling at 2
        ]
        channel = _fake_channel("general", 456, messages)
        guild = _fake_guild({456: channel})

        scanner = DiscordScanner(token="tok", server_id=123, channel_ids=[456], max_messages=2)

        with patch("scout.platforms.discord.discord.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.user = SimpleNamespace(name="Bot")

            async def start_side_effect(token: str) -> None:
                await mock_client._on_ready_handler()

            def event_decorator(fn: object) -> object:
                if hasattr(fn, "__name__") and fn.__name__ == "on_ready":
                    mock_client._on_ready_handler = fn
                return fn

            mock_client.event = event_decorator
            mock_client.get_guild = Mock(return_value=guild)
            mock_client.close = AsyncMock()
            mock_client.start = AsyncMock(side_effect=start_side_effect)
            mock_client_cls.return_value = mock_client

            result = await scanner.fetch_messages()

        assert isinstance(result, PlatformFetchSuccess)
        assert result.page_ceiling_reached is True
        assert len(result.messages) == 2  # stopped at ceiling
        assert result.failures
        assert result.failures[0].kind == "page_ceiling"
        assert result.failures[0].context == "channel:456"
        assert result.failures[0].message == (
            "Page ceiling reached; fetched 2 total messages so far (cap 2)"
        )

    @pytest.mark.asyncio
    async def test_channel_fetch_error_returns_partial_failure(self) -> None:
        guild = _fake_guild({})
        guild.fetch_channel = AsyncMock(side_effect=RuntimeError("channel API failed"))
        scanner = DiscordScanner(token="tok", server_id=123, channel_ids=[456])

        with patch("scout.platforms.discord.discord.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.user = SimpleNamespace(name="Bot")

            async def start_side_effect(token: str) -> None:
                await mock_client._on_ready_handler()

            def event_decorator(fn: object) -> object:
                if hasattr(fn, "__name__") and fn.__name__ == "on_ready":
                    mock_client._on_ready_handler = fn
                return fn

            mock_client.event = event_decorator
            mock_client.get_guild = Mock(return_value=guild)
            mock_client.close = AsyncMock()
            mock_client.start = AsyncMock(side_effect=start_side_effect)
            mock_client_cls.return_value = mock_client

            result = await scanner.fetch_messages()

        assert isinstance(result, PlatformFetchSuccess)
        assert result.messages == []
        assert len(result.failures) == 1
        assert result.failures[0].kind == "unexpected"
        assert result.failures[0].context == "channel:456"
        assert "channel API failed" in result.failures[0].message

    @pytest.mark.asyncio
    async def test_dedup_across_channels(self) -> None:
        now = datetime.now(UTC)
        # Same message ID in two channels (shouldn't happen in practice but guards regression)
        msg_a = _fake_message("100", "message A", now)
        msg_b = _fake_message("100", "message A duplicate", now)  # same ID

        channel1 = _fake_channel("ch1", 111, [msg_a])
        channel2 = _fake_channel("ch2", 222, [msg_b])
        guild = _fake_guild({111: channel1, 222: channel2})

        scanner = DiscordScanner(token="tok", server_id=123, channel_ids=[111, 222])

        with patch("scout.platforms.discord.discord.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.user = SimpleNamespace(name="Bot")

            async def start_side_effect(token: str) -> None:
                await mock_client._on_ready_handler()

            def event_decorator(fn: object) -> object:
                if hasattr(fn, "__name__") and fn.__name__ == "on_ready":
                    mock_client._on_ready_handler = fn
                return fn

            mock_client.event = event_decorator
            mock_client.get_guild = Mock(return_value=guild)
            mock_client.close = AsyncMock()
            mock_client.start = AsyncMock(side_effect=start_side_effect)
            mock_client_cls.return_value = mock_client

            result = await scanner.fetch_messages()

        assert isinstance(result, PlatformFetchSuccess)
        # ID "100" deduplicated
        assert len(result.messages) == 1

    @pytest.mark.asyncio
    async def test_bot_messages_excluded(self) -> None:
        now = datetime.now(UTC)
        human = _fake_message("1", "human message", now)
        bot = _fake_message("2", "bot message", now - timedelta(minutes=1), author_bot=True)

        channel = _fake_channel("general", 456, [human, bot])
        guild = _fake_guild({456: channel})

        scanner = DiscordScanner(token="tok", server_id=123, channel_ids=[456])

        with patch("scout.platforms.discord.discord.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.user = SimpleNamespace(name="Bot")

            async def start_side_effect(token: str) -> None:
                await mock_client._on_ready_handler()

            def event_decorator(fn: object) -> object:
                if hasattr(fn, "__name__") and fn.__name__ == "on_ready":
                    mock_client._on_ready_handler = fn
                return fn

            mock_client.event = event_decorator
            mock_client.get_guild = Mock(return_value=guild)
            mock_client.close = AsyncMock()
            mock_client.start = AsyncMock(side_effect=start_side_effect)
            mock_client_cls.return_value = mock_client

            result = await scanner.fetch_messages()

        assert isinstance(result, PlatformFetchSuccess)
        assert len(result.messages) == 1
        assert result.messages[0].platform_id == "1"
