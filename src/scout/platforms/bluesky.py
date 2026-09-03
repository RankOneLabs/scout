"""Bluesky scanner using the AT Protocol API."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

import httpx

from scout.config import (
    BLUESKY_API_URL,
    BLUESKY_APP_PASSWORD,
    BLUESKY_IDENTIFIER,
    BLUESKY_LANGS,
    BLUESKY_MAX_PAGES,
    Message,
    PublishedPost,
    SourceAuthor,
    SourceParent,
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

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


# --- Rich text facet helpers ---

# Conservative http(s) URL matcher; trailing sentence punctuation is stripped.
_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_TRAILING_PUNCT = frozenset(".,;:!?)'\"")

# Hashtag: # followed by Unicode word characters, not preceded by a word char.
_HASHTAG_RE = re.compile(r"(?<!\w)#(\w+)", re.UNICODE)

# Mention: @handle with at least one dot in the handle (e.g. alice.bsky.social).
# Negative lookbehind avoids matching @ embedded inside a word.
_MENTION_RE = re.compile(
    r"(?<!\w)@([a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z][a-zA-Z0-9.-]*)"
)


def _rkey_from_idempotency_key(idempotency_key: str) -> str:
    """Return an AT Protocol-safe deterministic record key."""
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]


def _char_to_byte_map(text: str) -> list[int]:
    """Return a list mapping character index → UTF-8 byte offset.

    Index i gives the byte offset of text[i]; index len(text) gives the
    total byte length (sentinel for end-of-string facet computation).
    """
    offsets: list[int] = []
    pos = 0
    for ch in text:
        offsets.append(pos)
        pos += len(ch.encode("utf-8"))
    offsets.append(pos)
    return offsets


def _overlaps(start: int, end: int, occupied: list[tuple[int, int]]) -> bool:
    return any(s < end and start < e for s, e in occupied)


def build_facets(
    text: str,
    did_map: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    """Build app.bsky.richtext.facet entries for URLs, hashtags, and resolved mentions.

    Byte offsets are UTF-8, matching the AT Protocol facet schema.
    Priority: URL ranges accepted first, then mentions, then hashtags.
    Any candidate whose byte range overlaps an already-accepted facet is skipped,
    preventing nested facets inside URLs (e.g., # or @ inside a URL).

    Mention facets are only emitted when the handle appears in did_map with a
    resolved DID; unresolved handles remain as plain text.
    """
    byte_map = _char_to_byte_map(text)
    occupied: list[tuple[int, int]] = []
    facets: list[dict[str, object]] = []

    # 1. URLs — highest priority
    for m in _URL_RE.finditer(text):
        url_text = m.group()
        while url_text and url_text[-1] in _TRAILING_PUNCT:
            url_text = url_text[:-1]
        if not url_text:
            continue
        start_char = m.start()
        end_char = m.start() + len(url_text)
        start_byte = byte_map[start_char]
        end_byte = byte_map[end_char]
        if _overlaps(start_byte, end_byte, occupied):
            continue
        occupied.append((start_byte, end_byte))
        facets.append({
            "index": {"byteStart": start_byte, "byteEnd": end_byte},
            "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url_text}],
        })

    # 2. Mentions — only when DID is known
    if did_map:
        for m in _MENTION_RE.finditer(text):
            handle = m.group(1)
            did = did_map.get(handle)
            if did is None:
                continue
            start_byte = byte_map[m.start()]
            end_byte = byte_map[m.end()]
            if _overlaps(start_byte, end_byte, occupied):
                continue
            occupied.append((start_byte, end_byte))
            facets.append({
                "index": {"byteStart": start_byte, "byteEnd": end_byte},
                "features": [{"$type": "app.bsky.richtext.facet#mention", "did": did}],
            })

    # 3. Hashtags — lowest priority
    for m in _HASHTAG_RE.finditer(text):
        tag = m.group(1)
        start_byte = byte_map[m.start()]
        end_byte = byte_map[m.end()]
        if _overlaps(start_byte, end_byte, occupied):
            continue
        occupied.append((start_byte, end_byte))
        facets.append({
            "index": {"byteStart": start_byte, "byteEnd": end_byte},
            "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": tag}],
        })

    return sorted(facets, key=lambda f: cast(dict[str, int], f["index"])["byteStart"])


@dataclass(frozen=True, slots=True)
class BlueskySession:
    """Authenticated AT Protocol session: bearer token + repo identity."""

    access_jwt: str
    did: str
    handle: str


class BlueskyScanner:
    """Fetches relevant posts from Bluesky via the AT Protocol API.

    Two modes:
    - Keyword search: searches all of Bluesky for project-related terms
    - Feed monitor: fetches recent posts from specific feed generators

    Authenticates via app password to avoid 403 on search endpoints.
    """

    def __init__(
        self,
        feed_uris: Sequence[str] | None = None,
        max_results_per_query: int = 50,
        languages: Sequence[str] | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.feed_uris = feed_uris or []
        self.max_results = max_results_per_query
        configured_languages = BLUESKY_LANGS if languages is None else languages
        self.languages = tuple(
            normalized
            for lang in configured_languages
            if (normalized := lang.strip().lower())
        )
        self._sleeper = sleeper
        self._clock = clock

    async def _create_session(
        self,
        client: httpx.AsyncClient,
    ) -> Result[BlueskySession, NetworkError]:
        """Create a session and return the bearer token plus repo identity.

        `did` and `handle` are needed for publishing (the createRecord
        endpoint requires the repo did, and the public post URL is built
        from the handle). Returns NetworkError so HTTP status survives
        to callers that surface auth failures as publish errors.
        """
        auth_url = f"{BLUESKY_API_URL}/com.atproto.server.createSession"
        if not BLUESKY_IDENTIFIER or not BLUESKY_APP_PASSWORD:
            return Err(
                NetworkError(
                    url=auth_url,
                    detail="BLUESKY_IDENTIFIER and BLUESKY_APP_PASSWORD required",
                )
            )

        try:
            resp = await client.post(
                auth_url,
                json={
                    "identifier": BLUESKY_IDENTIFIER,
                    "password": BLUESKY_APP_PASSWORD,
                },
            )
            resp.raise_for_status()
            try:
                data = resp.json()
            except ValueError as e:
                return Err(
                    NetworkError(
                        url=auth_url,
                        status=resp.status_code,
                        detail=f"non-JSON session response: {resp.text[:500]!r} ({e})",
                    )
                )
            token = data.get("accessJwt", "")
            did = data.get("did", "")
            handle = data.get("handle", "")
            if not token or not did:
                return Err(
                    NetworkError(
                        url=auth_url,
                        status=resp.status_code,
                        detail="session response missing accessJwt or did",
                    )
                )
            logger.info("Bluesky authentication successful (did=%s)", did)
            return Ok(BlueskySession(access_jwt=token, did=did, handle=handle))
        except httpx.HTTPStatusError as e:
            return Err(
                NetworkError(
                    url=auth_url,
                    status=e.response.status_code,
                    detail=e.response.text or str(e),
                )
            )
        except Exception as e:
            return Err(NetworkError(url=auth_url, detail=f"auth error: {e}"))

    def _auth_failure(self, net_err: NetworkError) -> PlatformFetchFailure:
        if not BLUESKY_IDENTIFIER or not BLUESKY_APP_PASSWORD:
            return PlatformFetchFailure(
                platform="bluesky",
                kind="auth_error",
                message=net_err.detail,
                retryable=False,
            )
        if net_err.status in (401, 403):
            return PlatformFetchFailure(
                platform="bluesky",
                kind="auth_error",
                message=net_err.detail,
                http_status=net_err.status,
                retryable=False,
            )
        return PlatformFetchFailure(
            platform="bluesky",
            kind="unexpected",
            message=net_err.detail,
            http_status=net_err.status,
            retryable=True,
        )

    # Maximum number of URIs per app.bsky.feed.getPosts request.
    _GETPOSTS_CHUNK_SIZE = 25

    async def fetch_messages(
        self,
        since: datetime | None = None,
        queries: list[str] | None = None,
    ) -> PlatformFetchSuccess | PlatformFetchFailure:
        """Fetch posts matching the given queries from Bluesky.

        Follows cursor pagination for each query and feed until the since
        boundary is reached, the cursor is exhausted, or the page ceiling
        is hit. After collection, resolves immediate-parent context for reply
        posts via batched app.bsky.feed.getPosts calls.

        Returns PlatformFetchSuccess (possibly with page_ceiling_reached=True)
        or PlatformFetchFailure — never a bare list or swallowed exception.
        """
        seen_uris: set[str] = set()
        collected: list[Message] = []
        raw_by_uri: dict[str, dict[str, object]] = {}
        failures: list[PlatformFetchFailure] = []
        page_ceiling_reached = False

        active_queries = queries or []
        for q in active_queries:
            logger.info("Bluesky search query: %s", q)

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                match await self._create_session(client):
                    case Ok(session):
                        headers = {"Authorization": f"Bearer {session.access_jwt}"}
                    case Err(net_err):
                        return self._auth_failure(net_err)

                # Cartesian product: every query against every configured
                # language (or a single unfiltered pass when none are
                # configured). Each query-language pair gets its own cursor
                # and page budget; all results share seen_uris.
                langs_to_use: tuple[str | None, ...] = self.languages if self.languages else (None,)
                for query in active_queries:
                    for lang in langs_to_use:
                        posts, ceiling, failure = await self._search_paginated(
                            client, query, headers, since, BLUESKY_MAX_PAGES, lang=lang
                        )
                        self._collect(posts, seen_uris, collected, since)
                        for p in posts:
                            uri = str(p.get("uri", ""))
                            if uri:
                                raw_by_uri[uri] = p
                        if failure is not None:
                            failures.append(failure)
                            continue
                        if ceiling:
                            page_ceiling_reached = True
                            failures.append(PlatformFetchFailure(
                                platform="bluesky",
                                kind="page_ceiling",
                                message=f"Page ceiling reached; fetched {len(posts)} posts",
                                context=f"search: {query[:40]} lang={lang or 'none'}",
                                retryable=True,
                            ))

                for feed_uri in self.feed_uris:
                    posts, ceiling, failure = await self._feed_paginated(
                        client, feed_uri, headers, BLUESKY_MAX_PAGES
                    )
                    self._collect(posts, seen_uris, collected, since)
                    for p in posts:
                        uri = str(p.get("uri", ""))
                        if uri:
                            raw_by_uri[uri] = p
                    if failure is not None:
                        failures.append(failure)
                        continue
                    if ceiling:
                        page_ceiling_reached = True
                        failures.append(PlatformFetchFailure(
                            platform="bluesky",
                            kind="page_ceiling",
                            message=f"Page ceiling reached; fetched {len(posts)} posts",
                            context=f"feed: {feed_uri[:50]}",
                            retryable=True,
                        ))

                # Resolve immediate-parent context for reply posts.
                if collected:
                    collected, parent_failures = await self._attach_parent_context(
                        client, headers, collected, raw_by_uri
                    )
                    failures.extend(parent_failures)

        except Exception as e:
            logger.error("Bluesky fetch failed: %s", e)
            return PlatformFetchFailure(
                platform="bluesky", kind="unexpected", message=str(e)
            )

        collected.sort(key=lambda m: m.created_at, reverse=True)
        logger.info("Total Bluesky posts fetched: %d", len(collected))
        return PlatformFetchSuccess(
            platform="bluesky",
            messages=collected,
            page_ceiling_reached=page_ceiling_reached,
            failures=tuple(failures),
        )

    async def _attach_parent_context(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        messages: list[Message],
        raw_by_uri: dict[str, dict[str, object]],
    ) -> tuple[list[Message], list[PlatformFetchFailure]]:
        """Resolve immediate parent posts for reply messages and return updated list.

        Collects unique parent URIs from all accepted child posts, fetches them in
        chunks via app.bsky.feed.getPosts, then rebuilds each reply message with
        the resolved SourceParent (or a failed status if the parent is unavailable).
        Non-reply posts receive parent_lookup_status=not_applicable.
        """
        failures: list[PlatformFetchFailure] = []

        # Build: child_uri -> parent_uri for each reply among collected messages.
        child_to_parent_uri: dict[str, str] = {}
        for msg in messages:
            raw = raw_by_uri.get(msg.platform_id)
            if raw is None:
                continue
            record = raw.get("record", {})
            if not isinstance(record, dict):
                continue
            reply_ref = record.get("reply")
            if not isinstance(reply_ref, dict):
                continue
            parent_ref = reply_ref.get("parent")
            if not isinstance(parent_ref, dict):
                continue
            parent_uri = str(parent_ref.get("uri", ""))
            if parent_uri:
                child_to_parent_uri[msg.platform_id] = parent_uri

        if not child_to_parent_uri:
            return messages, failures

        # Deduplicate parent URIs and fetch in chunks.
        unique_parent_uris = list(dict.fromkeys(child_to_parent_uri.values()))
        resolved_parents: dict[str, SourceParent] = {}

        for i in range(0, len(unique_parent_uris), self._GETPOSTS_CHUNK_SIZE):
            chunk = unique_parent_uris[i : i + self._GETPOSTS_CHUNK_SIZE]
            chunk_resolved, chunk_failures = await self._fetch_parent_chunk(
                client, headers, chunk
            )
            resolved_parents.update(chunk_resolved)
            failures.extend(chunk_failures)

        # Rebuild messages with parent context.
        updated: list[Message] = []
        for msg in messages:
            if msg.platform_id not in child_to_parent_uri:
                updated.append(msg)
                continue
            parent_uri = child_to_parent_uri[msg.platform_id]
            parent = resolved_parents.get(parent_uri)
            if parent is not None:
                new_msg = Message(
                    platform=msg.platform,
                    platform_id=msg.platform_id,
                    channel_name=msg.channel_name,
                    channel_id=msg.channel_id,
                    author_name=msg.author_name,
                    author_id=msg.author_id,
                    content=msg.content,
                    created_at=msg.created_at,
                    url=msg.url,
                    parent=parent,
                    parent_lookup_status="resolved",
                )
            else:
                new_msg = Message(
                    platform=msg.platform,
                    platform_id=msg.platform_id,
                    channel_name=msg.channel_name,
                    channel_id=msg.channel_id,
                    author_name=msg.author_name,
                    author_id=msg.author_id,
                    content=msg.content,
                    created_at=msg.created_at,
                    url=msg.url,
                    parent=None,
                    parent_lookup_status="failed",
                )
                # Record a non-fatal failure for each child whose parent could not be resolved.
                failures.append(PlatformFetchFailure(
                    platform="bluesky",
                    kind="parent_context",
                    message=f"Parent post unavailable for child {msg.platform_id}",
                    context=f"child:{msg.platform_id} parent:{parent_uri}",
                    retryable=True,
                ))
            updated.append(new_msg)

        resolved_count = sum(
            1 for uri in child_to_parent_uri.values() if uri in resolved_parents
        )
        logger.info(
            "Parent context: %d replies found, %d resolved, %d failed",
            len(child_to_parent_uri),
            resolved_count,
            len(child_to_parent_uri) - resolved_count,
        )
        return updated, failures

    async def _fetch_parent_chunk(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        uris: list[str],
    ) -> tuple[dict[str, SourceParent], list[PlatformFetchFailure]]:
        """Fetch a chunk of parent posts and return a mapping uri -> SourceParent.

        Posts that are missing, deleted, or malformed are silently omitted from the
        returned dict; callers treat absence as a failed lookup.
        """
        resolved: dict[str, SourceParent] = {}
        failures: list[PlatformFetchFailure] = []

        try:
            resp = await client.get(
                f"{BLUESKY_API_URL}/app.bsky.feed.getPosts",
                params={"uris": uris},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            failure = classify_http_failure("bluesky", e, context="parent_context")
            failures.append(failure)
            return resolved, failures
        except Exception as e:
            failures.append(PlatformFetchFailure(
                platform="bluesky",
                kind="parent_context",
                message=str(e),
                context="parent_context",
                retryable=True,
            ))
            return resolved, failures

        posts: list[dict[str, object]] = cast(
            list[dict[str, object]], data.get("posts", [])
        )
        for post in posts:
            uri = str(post.get("uri", ""))
            if not uri:
                continue
            author: dict[str, object] = post.get("author", {})  # type: ignore[assignment]
            author_did = str(author.get("did", ""))
            author_handle = str(author.get("handle", ""))
            author_name = str(author.get("displayName") or author.get("handle", ""))
            record: dict[str, object] = post.get("record", {})  # type: ignore[assignment]
            text = str(record.get("text", ""))
            url = self._build_bsky_url(author_handle, uri)

            if not author_did or not text:
                logger.debug("Skipping malformed parent post view: uri=%s", uri)
                continue

            resolved[uri] = SourceParent(
                id=uri,
                author=SourceAuthor(id=author_did, name=author_name),
                text=text,
                url=url,
            )

        return resolved, failures

    async def _get_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        params: dict[str, str | int],
        headers: dict[str, str],
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
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 429:
                return Err(classify_http_failure("bluesky", e, context=context))
            retry_after = e.response.headers.get(
                "Retry-After"
            ) or e.response.headers.get("x-ratelimit-reset-after")
            delay = parse_retry_after(retry_after, self._clock()) if retry_after else None
            if delay is None:
                return Err(classify_http_failure("bluesky", e, context=context))
            await self._sleeper(delay)
            try:
                resp = await client.get(url, params=params, headers=headers)
                resp.raise_for_status()
            except httpx.HTTPStatusError as e2:
                return Err(classify_http_failure("bluesky", e2, context=context))
            except Exception as e2:
                return Err(PlatformFetchFailure(
                    platform="bluesky", kind="unexpected", message=str(e2), context=context,
                ))
        except Exception as e:
            return Err(PlatformFetchFailure(
                platform="bluesky", kind="unexpected", message=str(e), context=context,
            ))

        try:
            return Ok(cast(dict[str, object], resp.json()))
        except Exception as e:
            return Err(PlatformFetchFailure(
                platform="bluesky", kind="unexpected", message=str(e), context=context,
            ))

    @staticmethod
    def _post_boundary_ts(post: dict[str, object]) -> datetime | None:
        """Timestamp used only for the chronological since-boundary check.

        Never logs — a missing, malformed, or naive value is inconclusive
        and must not terminate pagination early.
        """
        record = post.get("record")
        if not isinstance(record, dict):
            return None
        return parse_platform_ts(str(record.get("createdAt", "")))

    async def _search_paginated(
        self,
        client: httpx.AsyncClient,
        query: str,
        headers: dict[str, str],
        since: datetime | None,
        max_pages: int,
        lang: str | None = None,
    ) -> tuple[list[dict[str, object]], bool, PlatformFetchFailure | None]:
        """Paginate searchPosts for one query-language pair to the since
        boundary or cursor exhaustion.

        Returns (posts, page_ceiling_reached, failure_or_None).
        """
        if not query.strip():
            logger.warning("Skipping empty Bluesky search query")
            return [], False, None

        context = f"search: {query[:40]} lang={lang or 'none'}"

        async def fetch_page(
            *, cursor: str | None, page_number: int
        ) -> Result[dict[str, object], PlatformFetchFailure]:
            params: dict[str, str | int] = {
                "q": query,
                "limit": min(self.max_results, 100),
                "sort": "latest",
            }
            if lang:
                params["lang"] = lang
            if cursor:
                params["cursor"] = cursor
            result = await self._get_with_retry(
                client,
                f"{BLUESKY_API_URL}/app.bsky.feed.searchPosts",
                params,
                headers,
                context=context,
            )
            match result:
                case Ok(data):
                    posts = cast(list[dict[str, object]], data.get("posts", []))
                    next_cursor = cast(str | None, data.get("cursor"))
                    logger.debug(
                        "Search '%s' lang=%s page %d: %d posts, cursor=%s",
                        query[:40], lang or "none", page_number, len(posts), bool(next_cursor),
                    )
                case Err(_):
                    pass
            return result

        def extract(
            data: dict[str, object],
        ) -> tuple[list[dict[str, object]], str | None]:
            posts = cast(list[dict[str, object]], data.get("posts", []))
            next_cursor = cast(str | None, data.get("cursor"))
            return posts, next_cursor

        result = await paginate_cursor(
            fetch_page=fetch_page,
            extract=extract,
            timestamp_of=self._post_boundary_ts,
            since=since,
            max_pages=max_pages,
            chronological=True,
            platform="bluesky",
            context=context,
        )
        return result.items, result.page_ceiling_reached, result.failure

    async def _feed_paginated(
        self,
        client: httpx.AsyncClient,
        feed_uri: str,
        headers: dict[str, str],
        max_pages: int,
    ) -> tuple[list[dict[str, object]], bool, PlatformFetchFailure | None]:
        """Paginate getFeed to cursor exhaustion or the page ceiling.

        Custom/hot/relevance feeds are not guaranteed reverse-chronological,
        so an item at or before `since` cannot prove later pages hold nothing
        new — pagination never stops early on that basis. The since filter is
        applied independently to every parsed item by the caller's _collect.

        Returns (posts, page_ceiling_reached, failure_or_None).
        """
        context = f"feed: {feed_uri[:50]}"

        async def fetch_page(
            *, cursor: str | None, page_number: int
        ) -> Result[dict[str, object], PlatformFetchFailure]:
            params: dict[str, str | int] = {
                "feed": feed_uri,
                "limit": min(self.max_results, 100),
            }
            if cursor:
                params["cursor"] = cursor
            result = await self._get_with_retry(
                client,
                f"{BLUESKY_API_URL}/app.bsky.feed.getFeed",
                params,
                headers,
                context=context,
            )
            match result:
                case Ok(data):
                    feed_items = cast(list[dict[str, object]], data.get("feed", []))
                    posts = [
                        cast(dict[str, object], item["post"])
                        for item in feed_items
                        if "post" in item
                    ]
                    next_cursor = cast(str | None, data.get("cursor"))
                    logger.debug(
                        "Feed %s page %d: %d posts, cursor=%s",
                        feed_uri[:50], page_number, len(posts), bool(next_cursor),
                    )
                case Err(_):
                    pass
            return result

        def extract(
            data: dict[str, object],
        ) -> tuple[list[dict[str, object]], str | None]:
            feed_items = cast(list[dict[str, object]], data.get("feed", []))
            posts = [
                cast(dict[str, object], item["post"])
                for item in feed_items
                if "post" in item
            ]
            next_cursor = cast(str | None, data.get("cursor"))
            return posts, next_cursor

        result = await paginate_cursor(
            fetch_page=fetch_page,
            extract=extract,
            timestamp_of=self._post_boundary_ts,
            since=None,
            max_pages=max_pages,
            chronological=False,
            platform="bluesky",
            context=context,
        )
        return result.items, result.page_ceiling_reached, result.failure

    async def publish(
        self,
        text: str,
        *,
        idempotency_key: str | None = None,
    ) -> Result[PublishedPost, NetworkError]:
        """Post `text` to Bluesky as the configured account.

        Returns the canonical AT URI on success. Length and content
        validation are deferred to Bluesky (a >300-grapheme post returns
        a 400, which surfaces here as NetworkError with the response body).
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
        match await self._create_session(client):
            case Ok(session):
                pass
            case Err(net_err):
                return Err(net_err)  # status preserved from auth path
        return await self._publish_with_session(
            client,
            session,
            text,
            idempotency_key=idempotency_key,
        )

    async def _publish_with_session(
        self,
        client: httpx.AsyncClient,
        session: BlueskySession,
        text: str,
        *,
        idempotency_key: str | None = None,
    ) -> Result[PublishedPost, NetworkError]:
        """Publish text using a pre-authenticated session.

        Builds rich text facets (URLs, hashtags) with UTF-8 byte offsets
        and includes them in the createRecord payload. Mention facets are
        not emitted here because build_facets is called without a did_map;
        callers that can supply resolved DIDs should pass did_map directly
        to build_facets and construct the record themselves.
        Used by both _publish_with_client and BlueskyBatchPublisher.
        """
        publish_url = f"{BLUESKY_API_URL}/com.atproto.repo.createRecord"

        now = datetime.now(UTC)
        facets = build_facets(text)
        record: dict[str, object] = {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": now.isoformat().replace("+00:00", "Z"),
        }
        if facets:
            record["facets"] = facets
        if self.languages:
            record["langs"] = list(self.languages)

        payload: dict[str, object] = {
            "repo": session.did,
            "collection": "app.bsky.feed.post",
            "record": record,
        }
        if idempotency_key:
            payload["rkey"] = _rkey_from_idempotency_key(idempotency_key)

        try:
            resp = await client.post(
                publish_url,
                headers={"Authorization": f"Bearer {session.access_jwt}"},
                json=payload,
            )
            resp.raise_for_status()
            try:
                data = resp.json()
            except ValueError as e:
                return Err(
                    NetworkError(
                        url=publish_url,
                        status=resp.status_code,
                        detail=f"non-JSON createRecord response: {resp.text[:500]!r} ({e})",
                    )
                )
            uri = data.get("uri", "")
            if not uri:
                return Err(
                    NetworkError(
                        url=publish_url,
                        status=resp.status_code,
                        detail="createRecord response missing 'uri'",
                    )
                )

            rkey = uri.rsplit("/", 1)[-1]
            url = (
                f"https://bsky.app/profile/{session.handle}/post/{rkey}"
                if session.handle and rkey
                else ""
            )
            logger.info("Published to Bluesky: %s", uri)
            return Ok(
                PublishedPost(
                    platform="bluesky",
                    platform_post_id=uri,
                    published_at=now,
                    url=url,
                )
            )
        except httpx.HTTPStatusError as e:
            return Err(
                NetworkError(
                    url=publish_url,
                    status=e.response.status_code,
                    detail=e.response.text or str(e),
                )
            )
        except Exception as e:
            return Err(NetworkError(url=publish_url, detail=str(e)))

    @staticmethod
    def _post_langs(post: dict[str, object]) -> tuple[str, ...]:
        record: dict[str, object] = post.get("record", {})  # type: ignore[assignment]
        raw_langs = record.get("langs")
        if not isinstance(raw_langs, list):
            return ()
        return tuple(lang.lower() for lang in raw_langs if isinstance(lang, str))

    @staticmethod
    def _lang_matches(post_lang: str, allowed_lang: str) -> bool:
        return (
            post_lang == allowed_lang
            or post_lang.split("-", 1)[0] == allowed_lang.split("-", 1)[0]
        )

    def _allows_language(self, post: dict[str, object]) -> bool:
        if not self.languages:
            return True
        post_langs = self._post_langs(post)
        if not post_langs:
            return True
        return any(
            self._lang_matches(post_lang, allowed_lang)
            for post_lang in post_langs
            for allowed_lang in self.languages
        )

    @staticmethod
    def _build_bsky_url(handle: str, uri: str) -> str:
        """Build a canonical Bluesky post URL from handle and AT URI."""
        rkey = uri.rsplit("/", 1)[-1] if uri else ""
        return f"https://bsky.app/profile/{handle}/post/{rkey}" if handle and rkey else ""

    @staticmethod
    def _post_to_message(
        post: dict[str, object],
        text: str,
        created_at: datetime,
        parent: SourceParent | None = None,
        parent_lookup_status: str = "not_applicable",
    ) -> Message:
        """Convert an AT Protocol post view to a Message."""
        uri = str(post.get("uri", ""))
        author: dict[str, object] = post.get("author", {})  # type: ignore[assignment]
        handle = str(author.get("handle", ""))
        display_name = str(
            author.get("displayName", author.get("handle", "unknown")),
        )

        url = BlueskyScanner._build_bsky_url(handle, uri)

        return Message(
            platform="bluesky",
            platform_id=uri,
            channel_name="bluesky",
            channel_id="",
            author_name=display_name,
            author_id=str(author.get("did", "")),
            content=text,
            created_at=created_at,
            url=url,
            parent=parent,
            parent_lookup_status=parent_lookup_status,
        )

    def _collect(
        self,
        posts: Sequence[dict[str, object]],
        seen_uris: set[str],
        out: list[Message],
        since: datetime | None,
    ) -> None:
        """Convert AT Protocol post views to Messages, deduplicating by AT URI."""

        def _id(post: dict[str, object]) -> str:
            return str(post.get("uri", ""))

        def _text(post: dict[str, object]) -> str:
            record: dict[str, object] = post.get("record", {})  # type: ignore[assignment]
            return str(record.get("text", ""))

        def _ts(post: dict[str, object]) -> datetime | None:
            uri = str(post.get("uri", "<unknown>"))
            record: dict[str, object] = post.get("record", {})  # type: ignore[assignment]
            raw = record.get("createdAt")
            if not isinstance(raw, str) or not raw:
                logger.warning(
                    "Skipping Bluesky post with missing/non-string timestamp: uri=%s", uri
                )
                return None
            dt = parse_platform_ts(raw)
            if dt is None:
                logger.warning(
                    "Skipping Bluesky post with unparseable or naive timestamp %r: uri=%s",
                    raw, uri,
                )
            return dt

        def _skip(post: dict[str, object], text: str) -> bool:
            if self._allows_language(post):
                return False
            logger.debug("Skipping Bluesky post with non-matching language")
            return True

        out.extend(
            dedupe_and_filter(
                posts,
                id_of=_id,
                text_of=_text,
                created_at_of=_ts,
                to_message=self._post_to_message,
                since=since,
                seen_ids=seen_uris,
                skip=_skip,
            )
        )


class BlueskyBatchPublisher:
    """Publisher that reuses one authenticated session across a publish batch.

    Create one instance per batch run (e.g., one per publish_candidate call).
    The session is created lazily on the first publish() call and reused for
    all subsequent calls in the same batch, avoiding repeated authentication
    round-trips that can trigger rate limits or temporary lockouts.

    A 401 or 403 response from createRecord triggers a one-time session
    refresh and retry.  Persistent auth failures are returned as Err without
    further retries so the caller can surface them clearly.

    Credentials (access tokens, app passwords) are never written to logs.
    """

    def __init__(self, scanner: BlueskyScanner) -> None:
        self._scanner = scanner
        self._session: BlueskySession | None = None

    async def publish(
        self,
        text: str,
        *,
        idempotency_key: str | None = None,
    ) -> Result[PublishedPost, NetworkError]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if self._session is None:
                match await self._scanner._create_session(client):
                    case Ok(session):
                        self._session = session
                    case Err(net_err):
                        return Err(net_err)

            result = await self._scanner._publish_with_session(
                client,
                self._session,
                text,
                idempotency_key=idempotency_key,
            )

            if isinstance(result, Err) and result.error.status in (401, 403):
                logger.info("Bluesky session expired mid-batch; refreshing once")
                match await self._scanner._create_session(client):
                    case Ok(fresh):
                        self._session = fresh
                        result = await self._scanner._publish_with_session(
                            client,
                            fresh,
                            text,
                            idempotency_key=idempotency_key,
                        )
                    case Err(net_err):
                        self._session = None
                        return Err(net_err)

            return result
