"""Keyword-based prefilter: fast recall-oriented message filter.

Runs before the LLM agent so scans don't pay a tool-use round-trip for
messages that have zero keyword overlap with any loaded project.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from scout.config import KEYWORD_PREFILTER, Message
from scout.registry import KeywordRoute

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RoutedMessage:
    """A message paired with the winning keyword route (or None if unmatched)."""

    message: Message
    keyword_route: KeywordRoute | None


@dataclass(frozen=True, slots=True)
class _MatchContext:
    raw: str
    casefolded: str
    normalized: str | None = None
    tokens: tuple[str, ...] | None = None


def _normalize_whitespace(value: str) -> str:
    return " ".join(value.split())


def _tokenize_exact(value: str) -> tuple[str, ...]:
    return tuple(token for token in re.split(r"[^0-9A-Za-z]+", value.casefold()) if token)


def _build_route_matcher(route: KeywordRoute) -> Callable[[_MatchContext], bool] | None:
    keyword = route.keyword.strip()
    if not keyword:
        return None

    match_type = route.match_type.strip().lower() or "substring"

    if match_type == "substring":
        needle = keyword.casefold()
        return lambda ctx: needle in ctx.casefolded

    if match_type == "phrase":
        needle = _normalize_whitespace(keyword.casefold())

        def _matches_phrase(ctx: _MatchContext) -> bool:
            assert ctx.normalized is not None
            return needle in ctx.normalized

        return _matches_phrase

    if match_type == "exact":
        keyword_tokens = _tokenize_exact(keyword)
        if len(keyword_tokens) != 1:
            logger.warning(
                "Disabling exact keyword route %s (project=%s keyword=%r): "
                "exact match_type requires a single normalized token",
                route.id,
                route.project_key,
                route.keyword,
            )
            return None
        needle = keyword_tokens[0]

        def _matches_exact(ctx: _MatchContext) -> bool:
            assert ctx.tokens is not None
            return needle in ctx.tokens

        return _matches_exact

    if match_type == "regex":
        try:
            pattern = re.compile(keyword, re.IGNORECASE)
        except re.error as exc:
            logger.warning(
                "Disabling regex keyword route %s (project=%s keyword=%r): %s",
                route.id,
                route.project_key,
                route.keyword,
                exc,
            )
            return None
        return lambda ctx: bool(pattern.search(ctx.raw))

    needle = keyword.casefold()
    return lambda ctx: needle in ctx.casefolded


def keyword_prefilter(
    messages: Sequence[Message],
    keywords: tuple[KeywordRoute, ...],
) -> list[RoutedMessage]:
    """Return RoutedMessage list with winning route per message.

    Keywords are assumed pre-sorted (priority ASC, length DESC, id ASC) by
    load_runtime_registry — first matching keyword wins.

    When KEYWORD_PREFILTER is true, drops messages with no matching keyword.
    When false, keeps all messages; keyword_route is None only when nothing matched.
    """
    result: list[RoutedMessage] = []
    matchers = [
        (kw_route, _build_route_matcher(kw_route))
        for kw_route in keywords
    ]
    needs_normalized = any(
        route.match_type.strip().lower() == "phrase" for route, matcher in matchers if matcher
    )
    needs_tokens = any(
        route.match_type.strip().lower() == "exact" for route, matcher in matchers if matcher
    )
    for msg in messages:
        casefolded = msg.content.casefold()
        match_ctx = _MatchContext(
            raw=msg.content,
            casefolded=casefolded,
            normalized=_normalize_whitespace(casefolded) if needs_normalized else None,
            tokens=_tokenize_exact(msg.content) if needs_tokens else None,
        )
        winning_route: KeywordRoute | None = None
        for kw_route, matcher in matchers:
            if matcher is not None and matcher(match_ctx):
                winning_route = kw_route
                break

        if KEYWORD_PREFILTER and winning_route is None:
            continue

        result.append(RoutedMessage(message=msg, keyword_route=winning_route))

    matched = sum(1 for r in result if r.keyword_route is not None)
    logger.info(
        "Keyword prefilter: %d/%d messages passed (%d matched keyword)",
        len(result),
        len(messages),
        matched,
    )
    return result
