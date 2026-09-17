"""Discord API wrapper for fetching messages from monitored channels."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import discord

from scout.config import DISCORD_MAX_PAGES, Account, Message
from scout.errors import (
    PlatformFetchFailure,
    PlatformFetchSuccess,
    SourceFetchOutcome,
    SourceTermination,
)
from scout.platforms.base import SourceDescriptor, derive_source_key, source_since

# discord.py fetches channel history in 100-message requests.
_DISCORD_HISTORY_PAGE_SIZE = 100

logger = logging.getLogger(__name__)


class DiscordScanner:
    """Connects to Discord, fetches recent messages, and disconnects.

    Uses discord.py's Client in a connect-scan-disconnect pattern:
    on_ready fires -> fetch channel history newest-first -> close client.
    """

    def __init__(
        self,
        token: str,
        server_id: int,
        channel_ids: Sequence[int],
        max_messages: int = 200,
        max_pages: int | None = None,
    ) -> None:
        self.token = token
        self.server_id = server_id
        self.channel_ids = channel_ids
        self.max_messages = max_messages
        # None means "production's normal limit" (DISCORD_MAX_PAGES, read at
        # fetch time); a bounded backfill passes its own ceiling.
        self._max_pages_override = max_pages

    @property
    def max_pages(self) -> int:
        if self._max_pages_override is not None:
            return self._max_pages_override
        return DISCORD_MAX_PAGES

    async def fetch_messages(
        self,
        since: datetime | None = None,
        source_checkpoints: Mapping[str, datetime | None] | None = None,
    ) -> PlatformFetchSuccess | PlatformFetchFailure:
        """Fetch messages from all configured channels since the given timestamp.

        Paginates from newest toward older messages until since is exhausted or
        the page ceiling is reached. Returns messages deduplicated by platform_id
        in newest-first order.

        client.start() authentication/configuration exceptions are caught and
        converted to PlatformFetchFailure with auth_error or unexpected kind.
        """
        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)

        # Discord history uses fixed 100-item request pages. Clamp the legacy
        # per-page message setting to that protocol size, then enforce the
        # requested page ceiling independently for every channel/source.
        max_source_items = min(self.max_messages, _DISCORD_HISTORY_PAGE_SIZE) * self.max_pages
        collected: list[Message] = []
        failures: list[PlatformFetchFailure] = []
        outcomes: list[SourceFetchOutcome] = []
        seen_ids: set[str] = set()
        fetch_error: BaseException | None = None
        page_ceiling_reached = False

        def _outcome(
            ch_id: int,
            count: int,
            examined_count: int,
            termination: SourceTermination,
            failure: PlatformFetchFailure | None,
        ) -> SourceFetchOutcome:
            # discord.py's channel.history walks 100-message pages under
            # the hood; one "page" here is one such request-sized chunk.
            descriptor = SourceDescriptor("discord", "channel", str(ch_id))
            return SourceFetchOutcome(
                source_key=derive_source_key(descriptor),
                platform=descriptor.platform,
                source_kind=descriptor.source_kind,
                provider_key=descriptor.provider_key,
                page_count=max(1, -(-examined_count // _DISCORD_HISTORY_PAGE_SIZE)),
                termination=termination,
                message_count=count,
                failure=failure,
            )

        @client.event
        async def on_ready() -> None:
            nonlocal fetch_error, page_ceiling_reached
            try:
                assert client.user is not None
                logger.info(
                    "Connected to Discord as %s (scanning %d channels)",
                    client.user.name,
                    len(self.channel_ids),
                )

                guild = client.get_guild(self.server_id)
                if guild is None:
                    guild = await client.fetch_guild(self.server_id)

                for ch_id in self.channel_ids:
                    try:
                        channel: object = guild.get_channel(ch_id)
                        if channel is None:
                            channel = await guild.fetch_channel(ch_id)

                        if not isinstance(channel, discord.TextChannel):
                            logger.warning("Channel %d is not a text channel, skipping", ch_id)
                            failure = PlatformFetchFailure(
                                platform="discord",
                                kind="config_error",
                                message=f"Channel {ch_id} is not a text channel",
                                context=f"channel:{ch_id}",
                                retryable=False,
                                operation_phase="fetch",
                                blocks_watermark_advance=True,
                            )
                            failures.append(failure)
                            outcomes.append(_outcome(ch_id, 0, 0, "failure", failure))
                            continue

                        descriptor = SourceDescriptor("discord", "channel", str(ch_id))
                        channel_since = source_since(
                            descriptor, since, source_checkpoints
                        )
                        count = 0
                        examined_count = 0
                        channel_termination: SourceTermination = "exhausted"
                        # An explicit request-sized limit enforces max_pages;
                        # after moves this source's strict boundary to Discord
                        # while oldest_first=False keeps newest-first processing.
                        async for msg in channel.history(
                            limit=max_source_items,
                            after=channel_since,
                            oldest_first=False,
                        ):
                            if examined_count >= max_source_items:
                                break
                            examined_count += 1

                            if msg.author.bot:
                                continue
                            if not msg.content.strip():
                                continue

                            msg_id = str(msg.id)
                            if msg_id in seen_ids:
                                continue
                            seen_ids.add(msg_id)

                            collected.append(
                                Message(
                                    platform="discord",
                                    platform_id=msg_id,
                                    channel_name=channel.name,
                                    channel_id=str(channel.id),
                                    author=Account(
                                        platform="discord", id=str(msg.author.id),
                                        name=msg.author.display_name, handle=None,
                                    ),
                                    content=msg.content,
                                    created_at=msg.created_at.replace(tzinfo=UTC),
                                    url=msg.jump_url,
                                )
                            )
                            count += 1

                        ceiling_failure: PlatformFetchFailure | None = None
                        if examined_count >= max_source_items:
                            page_ceiling_reached = True
                            channel_termination = "page_ceiling"
                            ceiling_failure = PlatformFetchFailure(
                                platform="discord",
                                kind="page_ceiling",
                                message=(
                                    "Page ceiling reached for channel; examined "
                                    f"{examined_count} messages (cap {max_source_items})"
                                ),
                                context=f"channel:{ch_id}",
                                retryable=True,
                                operation_phase="fetch",
                                blocks_watermark_advance=True,
                            )
                            failures.append(ceiling_failure)
                            logger.warning(
                                "Discord channel %d hit page ceiling (%d examined messages)",
                                ch_id,
                                examined_count,
                            )

                        logger.info("Fetched %d messages from #%s", count, channel.name)
                        outcomes.append(_outcome(
                            ch_id, count, examined_count, channel_termination, ceiling_failure,
                        ))

                    except discord.Forbidden as e:
                        logger.warning("No access to channel %d, skipping", ch_id)
                        failure = PlatformFetchFailure(
                            platform="discord",
                            kind="auth_error",
                            message=str(e) or f"No access to channel {ch_id}",
                            context=f"channel:{ch_id}",
                            retryable=False,
                            operation_phase="fetch",
                            blocks_watermark_advance=True,
                        )
                        failures.append(failure)
                        outcomes.append(_outcome(ch_id, 0, 0, "failure", failure))
                    except discord.NotFound as e:
                        logger.warning("Channel %d not found, skipping", ch_id)
                        failure = PlatformFetchFailure(
                            platform="discord",
                            kind="config_error",
                            message=str(e) or f"Channel {ch_id} not found",
                            context=f"channel:{ch_id}",
                            retryable=False,
                            operation_phase="fetch",
                            blocks_watermark_advance=True,
                        )
                        failures.append(failure)
                        outcomes.append(_outcome(ch_id, 0, 0, "failure", failure))
                    except Exception as e:
                        logger.error("Error fetching channel %d: %s", ch_id, e)
                        failure = PlatformFetchFailure(
                            platform="discord",
                            kind="unexpected",
                            message=str(e),
                            context=f"channel:{ch_id}",
                            retryable=True,
                            operation_phase="fetch",
                            blocks_watermark_advance=True,
                        )
                        failures.append(failure)
                        outcomes.append(_outcome(ch_id, 0, 0, "failure", failure))

            except Exception as e:
                fetch_error = e
                logger.error("Fatal error during scan: %s", e)
            finally:
                await client.close()

        try:
            await client.start(self.token)
        except discord.LoginFailure as e:
            return PlatformFetchFailure(
                platform="discord",
                kind="auth_error",
                message=str(e),
                retryable=False,
                operation_phase="fetch",
                blocks_watermark_advance=True,
            )
        except discord.PrivilegedIntentsRequired as e:
            return PlatformFetchFailure(
                platform="discord",
                kind="auth_error",
                message=str(e),
                retryable=False,
                operation_phase="fetch",
                blocks_watermark_advance=True,
            )
        except Exception as e:
            return PlatformFetchFailure(
                platform="discord",
                kind="unexpected",
                message=str(e),
                operation_phase="fetch",
                blocks_watermark_advance=True,
            )

        if fetch_error:
            return PlatformFetchFailure(
                platform="discord",
                kind="unexpected",
                message=str(fetch_error),
                operation_phase="fetch",
                blocks_watermark_advance=True,
            )

        # Sort newest-first (deterministic ordering for downstream scoring/tests)
        collected.sort(key=lambda m: m.created_at, reverse=True)
        logger.info("Total messages fetched: %d", len(collected))
        return PlatformFetchSuccess(
            platform="discord",
            messages=collected,
            page_ceiling_reached=page_ceiling_reached,
            failures=tuple(failures),
            source_outcomes=tuple(outcomes),
        )
