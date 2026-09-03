"""Tests for the keyword prefilter.

The prefilter is recall-oriented: it must pick up every message that
contains any keyword from the registry, pairing each message with its
winning KeywordRoute.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scout.config import Message
from scout.registry import KeywordRoute
from scout.scanning.prefilter import RoutedMessage, keyword_prefilter


def _make_message(content: str, platform_id: str = "1") -> Message:
    return Message(
        platform="discord",
        platform_id=platform_id,
        channel_name="general",
        channel_id="123",
        author_name="alice",
        author_id="a1",
        content=content,
        created_at=datetime(2026, 4, 18, tzinfo=UTC),
    )


def _kw(
    id: int,
    keyword: str,
    project_key: str = "gateway",
    priority: int = 0,
    match_type: str = "substring",
) -> KeywordRoute:
    return KeywordRoute(
        id=id,
        project_key=project_key,
        keyword=keyword,
        evaluate_prompt=None,
        respond_prompt=None,
        critique_prompt=None,
        priority=priority,
        match_type=match_type,
    )


def test_keyword_match_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scout.scanning.prefilter.KEYWORD_PREFILTER", True)
    keywords = (_kw(1, "CAPTCHA"),)
    messages = [_make_message("looking for captcha alternatives")]
    filtered = keyword_prefilter(messages, keywords)
    assert len(filtered) == 1
    assert filtered[0].keyword_route is not None
    assert filtered[0].keyword_route.keyword == "CAPTCHA"


def test_phrase_match_normalizes_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scout.scanning.prefilter.KEYWORD_PREFILTER", True)
    keywords = (_kw(1, "agent reputation", match_type="phrase"),)
    messages = [_make_message("agent\n  reputation matters")]
    filtered = keyword_prefilter(messages, keywords)
    assert len(filtered) == 1
    assert filtered[0].keyword_route == keywords[0]


def test_exact_match_requires_token_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scout.scanning.prefilter.KEYWORD_PREFILTER", True)
    keywords = (_kw(1, "bot", match_type="exact"),)
    messages = [
        _make_message("robotics talk", platform_id="1"),
        _make_message("the bot is live", platform_id="2"),
    ]
    filtered = keyword_prefilter(messages, keywords)
    assert len(filtered) == 1
    assert filtered[0].message.platform_id == "2"


def test_regex_match_uses_ignorecase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scout.scanning.prefilter.KEYWORD_PREFILTER", True)
    keywords = (_kw(1, r"cap(tch)?a", match_type="regex"),)
    messages = [_make_message("CAPTCHA alternatives")]
    filtered = keyword_prefilter(messages, keywords)
    assert len(filtered) == 1
    assert filtered[0].keyword_route == keywords[0]


def test_invalid_regex_disables_route(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("scout.scanning.prefilter.KEYWORD_PREFILTER", True)
    keywords = (_kw(1, "(", match_type="regex"),)
    messages = [_make_message("anything")]
    with caplog.at_level("WARNING", logger="prefilter"):
        filtered = keyword_prefilter(messages, keywords)
    assert filtered == []
    assert any("Disabling regex keyword route" in r.getMessage() for r in caplog.records)


def test_multi_token_exact_route_is_disabled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("scout.scanning.prefilter.KEYWORD_PREFILTER", True)
    keywords = (_kw(1, "agent reputation", match_type="exact"),)
    messages = [_make_message("agent reputation matters")]
    with caplog.at_level("WARNING", logger="prefilter"):
        filtered = keyword_prefilter(messages, keywords)
    assert filtered == []
    assert any(
        "exact match_type requires a single normalized token" in r.getMessage()
        for r in caplog.records
    )


def test_messages_without_any_keyword_are_filtered_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("scout.scanning.prefilter.KEYWORD_PREFILTER", True)
    keywords = (_kw(1, "x402"),)
    messages = [
        _make_message("totally unrelated", platform_id="1"),
        _make_message("discussing x402 at the panel", platform_id="2"),
    ]
    filtered = keyword_prefilter(messages, keywords)
    assert len(filtered) == 1
    assert filtered[0].message.platform_id == "2"


def test_first_keyword_wins_on_multi_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scout.scanning.prefilter.KEYWORD_PREFILTER", True)
    # Pre-sorted: kw1 has longer length (higher priority on tie) comes first
    kw1 = _kw(1, "agent reputation", priority=0)
    kw2 = _kw(2, "agent", priority=0)
    keywords = (kw1, kw2)
    msg = _make_message("agent reputation matters")
    filtered = keyword_prefilter([msg], keywords)
    assert len(filtered) == 1
    assert filtered[0].keyword_route == kw1


def test_prefilter_false_keeps_all_messages_with_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("scout.scanning.prefilter.KEYWORD_PREFILTER", False)
    keywords = (_kw(1, "x402"),)
    messages = [
        _make_message("unrelated", platform_id="1"),
        _make_message("x402 payment", platform_id="2"),
    ]
    result = keyword_prefilter(messages, keywords)
    assert len(result) == 2
    assert result[0].keyword_route is None
    assert result[1].keyword_route is not None


def test_empty_keywords_drops_all_when_prefilter_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("scout.scanning.prefilter.KEYWORD_PREFILTER", True)
    messages = [_make_message("anything")]
    result = keyword_prefilter(messages, ())
    assert result == []


def test_empty_keywords_keeps_all_when_prefilter_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("scout.scanning.prefilter.KEYWORD_PREFILTER", False)
    messages = [_make_message("anything")]
    result = keyword_prefilter(messages, ())
    assert len(result) == 1
    assert result[0].keyword_route is None


def test_routed_message_type() -> None:
    msg = _make_message("hello")
    kw = _kw(1, "hello")
    rm = RoutedMessage(message=msg, keyword_route=kw)
    assert rm.message is msg
    assert rm.keyword_route is kw
