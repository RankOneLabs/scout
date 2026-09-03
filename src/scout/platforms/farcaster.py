"""Farcaster scanner using Neynar search API."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import cast

import httpx

from scout.config import (
    FARCASTER_MAX_PAGES,
    FARCASTER_SIGNER_UUID,
    NEYNAR_API_URL,
    Message,
    PublishedPost,
)
from scout.errors import NetworkError, PlatformFetchFailure, PlatformFetchSuccess
from scout.platforms.base import (
    classify_http_failure,
    paginate_cursor,
    parse_platform_ts,
    parse_retry_after,
)
from scout.platforms.dedupe import dedupe_and_filter
from scout.result import Err, Ok, Result
from scout.scanning.schemas import validate_farcaster_byte_length

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class FarcasterScanner:
    """Fetches relevant casts from Farcaster via Neynar's search API.

    Two modes:
    - Keyword search: searches all of Farcaster (or within specific channels)
      for project-related terms
    - Channel monitor: fetches recent casts from specific channels
    """

    def __init__(
        self,
        api_key: str,
        channel_ids: Sequence[str] | None = None,
        max_results_per_query: int = 50,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.api_key = api_key
        self.channel_ids = channel_ids or []
        self.max_results = max_results_per_query
        self._headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json",
        }
        self._sleeper = sleeper
        self._clock = clock

    async def fetch_messages(
        self,
        since: datetime | None = None,
        queries: list[str] | None = None,
    ) -> PlatformFetchSuccess | PlatformFetchFailure:
        """Fetch casts matching the given queries from Farcaster.

        Follows Neynar pagination tokens for each query and channel feed until
        the since boundary is reached, no next page remains, or the page
        ceiling is hit. Returns PlatformFetchSuccess (possibly with
        page_ceiling_reached=True) or PlatformFetchFailure.
        """
        seen_hashes: set[str] = set()
        collected: list[Message] = []
        failures: list[PlatformFetchFailure] = []
        page_ceiling_reached = False

        active_queries = queries or []
        for q in active_queries:
            logger.info("Search query: %s", q)

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                for query in active_queries:
                    if self.channel_ids:
                        for ch_id in self.channel_ids:
                            casts, ceiling, failure = await self._search_paginated(
                                client, query, since, FARCASTER_MAX_PAGES, channel_id=ch_id
                            )
                            self._collect(casts, seen_hashes, collected, since)
                            if failure is not None:
                                failures.append(failure)
                                continue
                            if ceiling:
                                page_ceiling_reached = True
                                failures.append(PlatformFetchFailure(
                                    platform="farcaster",
                                    kind="page_ceiling",
                                    message=f"Page ceiling reached; fetched {len(casts)} casts",
                                    context=(
                                        f"search: {query[:40]} in /{ch_id}"
                                    ),
                                    retryable=True,
                                ))
                    else:
                        casts, ceiling, failure = await self._search_paginated(
                            client, query, since, FARCASTER_MAX_PAGES
                        )
                        self._collect(casts, seen_hashes, collected, since)
                        if failure is not None:
                            failures.append(failure)
                            continue
                        if ceiling:
                            page_ceiling_reached = True
                            failures.append(PlatformFetchFailure(
                                platform="farcaster",
                                kind="page_ceiling",
                                message=f"Page ceiling reached; fetched {len(casts)} casts",
                                context=f"search: {query[:40]}",
                                retryable=True,
                            ))

                for ch_id in self.channel_ids:
                    casts, ceiling, failure = await self._channel_feed_paginated(
                        client, ch_id, FARCASTER_MAX_PAGES
                    )
                    self._collect(casts, seen_hashes, collected, since)
                    if failure is not None:
                        failures.append(failure)
                        continue
                    if ceiling:
                        page_ceiling_reached = True
                        failures.append(PlatformFetchFailure(
                            platform="farcaster",
                            kind="page_ceiling",
                            message=f"Page ceiling reached; fetched {len(casts)} casts",
                            context=f"feed: /{ch_id}",
                            retryable=True,
                        ))

        except Exception as e:
            logger.error("Farcaster fetch failed: %s", e)
            return PlatformFetchFailure(
                platform="farcaster", kind="unexpected", message=str(e)
            )

        collected.sort(key=lambda m: m.created_at, reverse=True)
        logger.info("Total Farcaster casts fetched: %d", len(collected))
        return PlatformFetchSuccess(
            platform="farcaster",
            messages=collected,
            page_ceiling_reached=page_ceiling_reached,
            failures=tuple(failures),
        )

    async def _get_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        params: dict[str, str | int],
        context: str,
    ) -> Result[dict[str, object], PlatformFetchFailure]:
        """GET a page with at most one automatic retry on HTTP 429.

        Retries the exact same request once when Retry-After (or
        x-ratelimit-reset-after) yields a valid nonnegative delay; a second
        429 or an invalid/absent delay returns the classified failure
        instead. The sleeper and clock are constructor-injected so tests can
        assert the exact requested delay without wall-clock waiting.
        """
        try:
            resp = await client.get(url, headers=self._headers, params=params)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 429:
                return Err(classify_http_failure("farcaster", e, context=context))
            retry_after = e.response.headers.get(
                "Retry-After"
            ) or e.response.headers.get("x-ratelimit-reset-after")
            delay = parse_retry_after(retry_after, self._clock()) if retry_after else None
            if delay is None:
                return Err(classify_http_failure("farcaster", e, context=context))
            await self._sleeper(delay)
            try:
                resp = await client.get(url, headers=self._headers, params=params)
                resp.raise_for_status()
            except httpx.HTTPStatusError as e2:
                return Err(classify_http_failure("farcaster", e2, context=context))
            except Exception as e2:
                return Err(PlatformFetchFailure(
                    platform="farcaster", kind="unexpected", message=str(e2), context=context,
                ))
        except Exception as e:
            return Err(PlatformFetchFailure(
                platform="farcaster", kind="unexpected", message=str(e), context=context,
            ))

        try:
            return Ok(cast(dict[str, object], resp.json()))
        except Exception as e:
            return Err(PlatformFetchFailure(
                platform="farcaster", kind="unexpected", message=str(e), context=context,
            ))

    @staticmethod
    def _cast_boundary_ts(cast_item: dict[str, object]) -> datetime | None:
        """Timestamp used only for the chronological since-boundary check.

        Never logs — a missing, malformed, or naive value is inconclusive
        and must not terminate pagination early.
        """
        return parse_platform_ts(str(cast_item.get("timestamp", "")))

    async def _search_paginated(
        self,
        client: httpx.AsyncClient,
        query: str,
        since: datetime | None,
        max_pages: int,
        channel_id: str | None = None,
    ) -> tuple[list[dict[str, object]], bool, PlatformFetchFailure | None]:
        """Paginate Neynar cast search to the since boundary or token exhaustion.

        Returns (casts, page_ceiling_reached, failure_or_None).
        """
        if not query.strip():
            logger.warning("Skipping empty Farcaster search query")
            return [], False, None

        ctx = f"search: {query[:40]}" + (f" in /{channel_id}" if channel_id else "")

        async def fetch_page(
            *, cursor: str | None, page_number: int
        ) -> Result[dict[str, object], PlatformFetchFailure]:
            params: dict[str, str | int] = {
                "q": query,
                "limit": min(self.max_results, 100),
                "sort_type": "desc_chron",
            }
            if channel_id:
                params["channel_id"] = channel_id
            if cursor:
                params["cursor"] = cursor
            result = await self._get_with_retry(
                client, f"{NEYNAR_API_URL}/cast/search", params, context=ctx
            )
            match result:
                case Ok(data):
                    result_obj = cast(dict[str, object], data.get("result", {}))
                    casts = cast(list[dict[str, object]], result_obj.get("casts", []))
                    next_obj = cast(dict[str, object], result_obj.get("next") or {})
                    next_cursor = cast(str | None, next_obj.get("cursor"))
                    logger.debug(
                        "Search '%s'%s page %d: %d casts, cursor=%s",
                        query[:40],
                        f" in /{channel_id}" if channel_id else "",
                        page_number,
                        len(casts),
                        bool(next_cursor),
                    )
                case Err(_):
                    pass
            return result

        def extract(
            data: dict[str, object],
        ) -> tuple[list[dict[str, object]], str | None]:
            result_obj = cast(dict[str, object], data.get("result", {}))
            casts = cast(list[dict[str, object]], result_obj.get("casts", []))
            next_obj = cast(dict[str, object], result_obj.get("next") or {})
            next_cursor = cast(str | None, next_obj.get("cursor"))
            return casts, next_cursor

        result = await paginate_cursor(
            fetch_page=fetch_page,
            extract=extract,
            timestamp_of=self._cast_boundary_ts,
            since=since,
            max_pages=max_pages,
            chronological=True,
            platform="farcaster",
            context=ctx,
        )
        return result.items, result.page_ceiling_reached, result.failure

    async def _channel_feed_paginated(
        self,
        client: httpx.AsyncClient,
        channel_id: str,
        max_pages: int,
    ) -> tuple[list[dict[str, object]], bool, PlatformFetchFailure | None]:
        """Paginate Neynar channel feed to token exhaustion or the page ceiling.

        Channel feeds are not guaranteed reverse-chronological, so an item at
        or before `since` cannot prove later pages hold nothing new —
        pagination never stops early on that basis. The since filter is
        applied independently to every parsed item by the caller's _collect.

        Returns (casts, page_ceiling_reached, failure_or_None).
        """
        ctx = f"feed: /{channel_id}"

        async def fetch_page(
            *, cursor: str | None, page_number: int
        ) -> Result[dict[str, object], PlatformFetchFailure]:
            params: dict[str, str | int] = {
                "feed_type": "filter",
                "filter_type": "channel_id",
                "channel_id": channel_id,
                "limit": min(self.max_results, 100),
            }
            if cursor:
                params["cursor"] = cursor
            result = await self._get_with_retry(
                client, f"{NEYNAR_API_URL}/feed", params, context=ctx
            )
            match result:
                case Ok(data):
                    casts = cast(list[dict[str, object]], data.get("casts", []))
                    next_obj = cast(dict[str, object], data.get("next") or {})
                    next_cursor = cast(str | None, next_obj.get("cursor"))
                    logger.debug(
                        "Channel /%s page %d: %d casts, cursor=%s",
                        channel_id, page_number, len(casts), bool(next_cursor),
                    )
                case Err(_):
                    pass
            return result

        def extract(
            data: dict[str, object],
        ) -> tuple[list[dict[str, object]], str | None]:
            casts = cast(list[dict[str, object]], data.get("casts", []))
            next_obj = cast(dict[str, object], data.get("next") or {})
            next_cursor = cast(str | None, next_obj.get("cursor"))
            return casts, next_cursor

        result = await paginate_cursor(
            fetch_page=fetch_page,
            extract=extract,
            timestamp_of=self._cast_boundary_ts,
            since=None,
            max_pages=max_pages,
            chronological=False,
            platform="farcaster",
            context=ctx,
        )
        return result.items, result.page_ceiling_reached, result.failure

    async def publish(
        self,
        text: str,
        *,
        idempotency_key: str | None = None,
    ) -> Result[PublishedPost, NetworkError]:
        """Cast `text` to Farcaster via Neynar's managed-signer flow.

        Requires `FARCASTER_SIGNER_UUID` to be set (one-time signer
        approval handled outside this codebase via Neynar's flow).
        UTF-8 byte length is validated locally before any HTTP call;
        texts exceeding FARCASTER_POST_MAX_BYTES return Err immediately.
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await self._publish_with_client(
                client,
                text,
                idempotency_key=idempotency_key,
            )

    async def _publish_with_client(
        self,
        client: httpx.AsyncClient,
        text: str,
        *,
        idempotency_key: str | None = None,
    ) -> Result[PublishedPost, NetworkError]:
        """Inner publish — accepts a client so tests can inject a mock transport."""
        cast_url = f"{NEYNAR_API_URL}/cast"

        byte_error = validate_farcaster_byte_length(text)
        if byte_error:
            return Err(NetworkError(url=cast_url, status=400, detail=byte_error))

        if not FARCASTER_SIGNER_UUID:
            return Err(
                NetworkError(
                    url=cast_url,
                    detail="FARCASTER_SIGNER_UUID required (one-time Neynar signer approval)",
                )
            )

        now = datetime.now(UTC)
        payload = {
            "signer_uuid": FARCASTER_SIGNER_UUID,
            "text": text,
        }
        if idempotency_key:
            payload["idem"] = idempotency_key
        try:
            resp = await client.post(
                cast_url,
                headers=self._headers,
                json=payload,
            )
            resp.raise_for_status()
            try:
                data = resp.json()
            except ValueError as e:
                return Err(
                    NetworkError(
                        url=cast_url,
                        status=resp.status_code,
                        detail=f"non-JSON cast response: {resp.text[:500]!r} ({e})",
                    )
                )
            cast_data = data.get("cast") or {}
            cast_hash = str(cast_data.get("hash") or "")
            if not cast_hash:
                return Err(
                    NetworkError(
                        url=cast_url,
                        status=resp.status_code,
                        detail="cast response missing 'cast.hash'",
                    )
                )
            author = cast_data.get("author") or {}
            username = str(author.get("username") or "")
            url = f"https://warpcast.com/{username}/{cast_hash[:10]}" if username else ""
            logger.info("Published to Farcaster: %s", cast_hash)
            return Ok(
                PublishedPost(
                    platform="farcaster",
                    platform_post_id=cast_hash,
                    published_at=now,
                    url=url,
                )
            )
        except httpx.HTTPStatusError as e:
            return Err(
                NetworkError(
                    url=cast_url,
                    status=e.response.status_code,
                    detail=e.response.text or str(e),
                )
            )
        except Exception as e:
            return Err(NetworkError(url=cast_url, detail=str(e)))

    @staticmethod
    def _is_self_promo(text: str, username: str) -> bool:
        """Detect self-promotional replies (short messages plugging own service)."""
        if len(text) >= 200 or not username:
            return False
        text_lower = text.lower()
        username_lower = username.lower()
        has_self_ref = f"{username_lower}." in text_lower or f"@{username_lower}" in text_lower
        promo_phrases = (
            "consider using",
            "consider integrating",
            "check out",
            "try using",
        )
        return has_self_ref and any(w in text_lower for w in promo_phrases)

    @staticmethod
    def _cast_to_message(
        cast: dict[str, object],
        text: str,
        created_at: datetime,
    ) -> Message:
        """Convert a Neynar cast dict to a Message."""
        cast_hash = str(cast.get("hash", ""))
        author: dict[str, object] = cast.get("author", {})  # type: ignore[assignment]
        channel: dict[str, object] = cast.get("channel") or {}  # type: ignore[assignment]
        username = str(author.get("username", ""))
        url = f"https://warpcast.com/{username}/{cast_hash[:10]}" if username else ""

        return Message(
            platform="farcaster",
            platform_id=cast_hash,
            channel_name=str(channel.get("id", "home")),
            channel_id=str(channel.get("id", "")),
            author_name=str(
                author.get("display_name", author.get("username", "unknown")),
            ),
            author_id=str(author.get("fid", "")),
            content=text,
            created_at=created_at,
            url=url,
        )

    def _collect(
        self,
        casts: Sequence[dict[str, object]],
        seen_hashes: set[str],
        out: list[Message],
        since: datetime | None,
    ) -> None:
        """Convert Neynar cast dicts to Messages, deduplicating by cast hash."""

        def _id(cast: dict[str, object]) -> str:
            return str(cast.get("hash", ""))

        def _text(cast: dict[str, object]) -> str:
            return str(cast.get("text", ""))

        def _ts(cast: dict[str, object]) -> datetime | None:
            cast_hash = str(cast.get("hash", "<unknown>"))
            raw = cast.get("timestamp")
            if not isinstance(raw, str) or not raw:
                logger.warning(
                    "Skipping Farcaster cast with missing/non-string timestamp: hash=%s",
                    cast_hash,
                )
                return None
            dt = parse_platform_ts(raw)
            if dt is None:
                logger.warning(
                    "Skipping Farcaster cast with unparseable or naive timestamp %r: hash=%s",
                    raw, cast_hash,
                )
            return dt

        def _skip(cast: dict[str, object], text: str) -> bool:
            author: dict[str, object] = cast.get("author", {})  # type: ignore[assignment]
            username = str(author.get("username", ""))
            if self._is_self_promo(text, username):
                logger.debug("Skipping self-promo from @%s", username)
                return True
            return False

        out.extend(
            dedupe_and_filter(
                casts,
                id_of=_id,
                text_of=_text,
                created_at_of=_ts,
                to_message=self._cast_to_message,
                since=since,
                seen_ids=seen_hashes,
                skip=_skip,
            )
        )
