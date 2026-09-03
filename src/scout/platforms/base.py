"""Shared platform-scanning mechanics: cursor pagination, ISO timestamp
parsing, HTTP failure classification, and Retry-After parsing.

This is the sole module allowed to implement paginate_cursor, parse_platform_ts,
classify_http_failure, and parse_retry_after — Bluesky and Farcaster adapters
import these primitives rather than duplicating them locally.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from scout.errors import PlatformFetchFailure
from scout.result import Err, Ok, Result

logger = logging.getLogger(__name__)


def parse_platform_ts(value: str) -> datetime | None:
    """Parse an ISO-8601 platform timestamp into an aware datetime.

    A terminal "Z" is translated to "+00:00" before parsing. Returns None
    for empty, malformed, or timezone-naive values — callers must treat a
    rejected timestamp as unverifiable and never substitute the current time.
    """
    if not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt


def classify_http_failure(
    platform: str,
    e: httpx.HTTPStatusError,
    context: str | None = None,
) -> PlatformFetchFailure:
    """Classify an HTTP status error into a typed platform fetch failure.

    429 is retryable rate_limited, preserving Retry-After (or
    x-ratelimit-reset-after); 401/403 are non-retryable auth_error; every
    other status is retryable network_error.
    """
    status = e.response.status_code
    body = e.response.text or str(e)
    if status == 429:
        retry_after = e.response.headers.get(
            "Retry-After"
        ) or e.response.headers.get("x-ratelimit-reset-after")
        return PlatformFetchFailure(
            platform=platform,
            kind="rate_limited",
            message=body,
            http_status=status,
            context=context,
            retry_after=retry_after,
            retryable=True,
        )
    if status in (401, 403):
        return PlatformFetchFailure(
            platform=platform,
            kind="auth_error",
            message=body,
            http_status=status,
            context=context,
            retryable=False,
        )
    return PlatformFetchFailure(
        platform=platform,
        kind="network_error",
        message=body,
        http_status=status,
        context=context,
        retryable=True,
    )


def parse_retry_after(value: str, now: datetime) -> float | None:
    """Parse a Retry-After value (delta-seconds or HTTP-date) into a delay.

    A naive HTTP-date is treated as UTC. Returns None when the value is
    empty, unparseable, or resolves to a negative delay — callers treat
    None as "do not retry".
    """
    value = value.strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        pass
    else:
        return seconds if seconds >= 0 else None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    delay = (parsed - now).total_seconds()
    return delay if delay >= 0 else None


@dataclass(frozen=True, slots=True)
class PaginationResult[Item, Failure]:
    """Outcome of a paginate_cursor run.

    `items` accumulates across every page fetched, including the boundary
    page on early chronological termination and any pages fetched before a
    failure. `failure` is the original error from the page that failed, if
    any — its presence does not clear `items`.
    """

    items: list[Item]
    page_ceiling_reached: bool
    failure: Failure | None = None


async def paginate_cursor[Page, Item, Failure](
    *,
    fetch_page: Callable[..., Awaitable[Result[Page, Failure]]],
    extract: Callable[[Page], tuple[list[Item], str | None]],
    timestamp_of: Callable[[Item], datetime | None],
    since: datetime | None,
    max_pages: int,
    chronological: bool,
    platform: str,
    context: str | None = None,
) -> PaginationResult[Item, Failure]:
    """Paginate a cursor-based endpoint, accumulating items across pages.

    `fetch_page(cursor=..., page_number=...)` owns the request, auth, and
    the endpoint's own retry policy for a single page — paginate_cursor
    supplies the current cursor unchanged and never retries a fetch itself.
    `extract(page)` pulls the raw item list and next cursor out of a page.

    Pagination starts with cursor None, appends each non-empty page before
    evaluating the since boundary, and stops with page_ceiling_reached False
    on an empty page or an absent next cursor. On a page fetch failure, the
    items accumulated so far and the original failure are both returned.

    When `chronological` is True and `since` is set, pagination stops after
    a page if any item's `timestamp_of` value is an aware datetime at or
    before `since`; missing, malformed, or naive timestamps never terminate
    pagination early. `chronological=False` (relevance/feed ordering) never
    terminates early on `since` — pagination continues to cursor exhaustion
    or the page ceiling.

    The page ceiling is reached only when `max_pages` pages have been
    consumed while a next cursor still remains.
    """
    if max_pages < 1:
        raise ValueError(f"max_pages must be a positive integer, got {max_pages}")

    all_items: list[Item] = []
    cursor: str | None = None

    for page_number in range(1, max_pages + 1):
        match await fetch_page(cursor=cursor, page_number=page_number):
            case Ok(page):
                pass
            case Err(failure):
                return PaginationResult(
                    items=all_items, page_ceiling_reached=False, failure=failure
                )

        items, next_cursor = extract(page)
        if not items:
            return PaginationResult(items=all_items, page_ceiling_reached=False, failure=None)

        all_items.extend(items)
        cursor = next_cursor

        if chronological and since is not None:
            for item in items:
                ts = timestamp_of(item)
                if ts is not None and ts.tzinfo is not None and ts <= since:
                    return PaginationResult(
                        items=all_items, page_ceiling_reached=False, failure=None
                    )

        if not cursor:
            return PaginationResult(items=all_items, page_ceiling_reached=False, failure=None)
    else:
        if cursor:
            logger.warning(
                "Pagination page ceiling (%d) reached for platform=%s context=%s",
                max_pages,
                platform,
                context,
            )
            return PaginationResult(items=all_items, page_ceiling_reached=True, failure=None)

    return PaginationResult(items=all_items, page_ceiling_reached=False, failure=None)
