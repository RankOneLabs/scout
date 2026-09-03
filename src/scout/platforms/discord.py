"""Discord API wrapper for fetching messages from monitored channels."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

import discord

from scout.config import DISCORD_MAX_PAGES, Message
from scout.errors import PlatformFetchFailure, PlatformFetchSuccess

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
    ) -> None:
        self.token = token
        self.server_id = server_id
        self.channel_ids = channel_ids
        self.max_messages = max_messages

    async def fetch_messages(
        self,
        since: datetime | None = None,
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

        max_total = self.max_messages * DISCORD_MAX_PAGES
        collected: list[Message] = []
        failures: list[PlatformFetchFailure] = []
        seen_ids: set[str] = set()
        fetch_error: BaseException | None = None
        page_ceiling_reached = False

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
                    if page_ceiling_reached:
                        break
                    try:
                        channel: object = guild.get_channel(ch_id)
                        if channel is None:
                            channel = await guild.fetch_channel(ch_id)

                        if not isinstance(channel, discord.TextChannel):
                            logger.warning("Channel %d is not a text channel, skipping", ch_id)
                            continue

                        count = 0
                        # limit=None removes discord.py's default 100-message
                        # truncation; after=since moves the strict boundary to
                        # Discord (an exclusive lower bound) while oldest_first=
                        # False keeps newest-first processing for the ceiling
                        # check below.
                        async for msg in channel.history(
                            limit=None, after=since, oldest_first=False
                        ):
                            if len(collected) >= max_total:
                                page_ceiling_reached = True
                                failures.append(PlatformFetchFailure(
                                    platform="discord",
                                    kind="page_ceiling",
                                    message=(
                                        "Page ceiling reached; fetched "
                                        f"{len(collected)} total messages so far "
                                        f"(cap {max_total})"
                                    ),
                                    context=f"channel:{ch_id}",
                                    retryable=True,
                                ))
                                logger.warning(
                                    "Discord channel %d hit page ceiling (%d messages)",
                                    ch_id,
                                    max_total,
                                )
                                break

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
                                    author_name=msg.author.display_name,
                                    author_id=str(msg.author.id),
                                    content=msg.content,
                                    created_at=msg.created_at.replace(tzinfo=UTC),
                                    url=msg.jump_url,
                                )
                            )
                            count += 1

                        logger.info("Fetched %d messages from #%s", count, channel.name)

                    except discord.Forbidden as e:
                        logger.warning("No access to channel %d, skipping", ch_id)
                        failures.append(PlatformFetchFailure(
                            platform="discord",
                            kind="auth_error",
                            message=str(e) or f"No access to channel {ch_id}",
                            context=f"channel:{ch_id}",
                            retryable=False,
                        ))
                    except discord.NotFound as e:
                        logger.warning("Channel %d not found, skipping", ch_id)
                        failures.append(PlatformFetchFailure(
                            platform="discord",
                            kind="config_error",
                            message=str(e) or f"Channel {ch_id} not found",
                            context=f"channel:{ch_id}",
                            retryable=False,
                        ))
                    except Exception as e:
                        logger.error("Error fetching channel %d: %s", ch_id, e)
                        failures.append(PlatformFetchFailure(
                            platform="discord",
                            kind="unexpected",
                            message=str(e),
                            context=f"channel:{ch_id}",
                            retryable=True,
                        ))

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
            )
        except discord.PrivilegedIntentsRequired as e:
            return PlatformFetchFailure(
                platform="discord",
                kind="auth_error",
                message=str(e),
                retryable=False,
            )
        except Exception as e:
            return PlatformFetchFailure(
                platform="discord",
                kind="unexpected",
                message=str(e),
            )

        if fetch_error:
            return PlatformFetchFailure(
                platform="discord",
                kind="unexpected",
                message=str(fetch_error),
            )

        # Sort newest-first (deterministic ordering for downstream scoring/tests)
        collected.sort(key=lambda m: m.created_at, reverse=True)
        logger.info("Total messages fetched: %d", len(collected))
        return PlatformFetchSuccess(
            platform="discord",
            messages=collected,
            page_ceiling_reached=page_ceiling_reached,
            failures=tuple(failures),
        )
