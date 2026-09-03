"""Tests for build_search_queries keyword aggregation."""

from __future__ import annotations

from scout.config import build_search_queries
from scout.registry import KeywordRoute


def _kw(id: int, keyword: str, project_key: str = "alpha") -> KeywordRoute:
    return KeywordRoute(
        id=id,
        project_key=project_key,
        keyword=keyword,
        evaluate_prompt=None,
        respond_prompt=None,
        critique_prompt=None,
        priority=0,
    )


class TestBuildSearchQueries:
    def test_collects_keywords_from_routes(self) -> None:
        keywords = (
            _kw(1, "AI agent"),
            _kw(2, "micropayments"),
        )
        assert build_search_queries(keywords) == ["AI agent", "micropayments"]

    def test_deduplicates_across_routes(self) -> None:
        keywords = (
            _kw(1, "AI agent", "alpha"),
            _kw(2, "bot detection", "alpha"),
            _kw(3, "bot detection", "beta"),
            _kw(4, "sybil resistance", "beta"),
        )
        assert build_search_queries(keywords) == ["AI agent", "bot detection", "sybil resistance"]

    def test_case_insensitive_dedup(self) -> None:
        keywords = (
            _kw(1, "AI Agent", "alpha"),
            _kw(2, "ai agent", "beta"),
            _kw(3, "Bot Detection", "beta"),
        )
        result = build_search_queries(keywords)
        assert result == ["AI Agent", "Bot Detection"]

    def test_trims_keywords_and_drops_blank_entries(self) -> None:
        keywords = (
            _kw(1, "  AI agent  "),
            _kw(2, " "),
            _kw(3, ""),
            _kw(4, "ai AGENT"),
            _kw(5, "\tbot detection\n"),
        )
        result = build_search_queries(keywords)
        assert result == ["AI agent", "bot detection"]

    def test_empty_keywords_returns_empty(self) -> None:
        assert build_search_queries(()) == []
