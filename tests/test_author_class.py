"""Author classifier: unit behaviour and the acceptance set.

The acceptance fixture is every active blocked author and every unblocked
author with a human-graded relevant post, as observed on 2026-09-11. The
rule is accepted when it recalls at least the agreed share of the
blocklist and flags none of the good authors beyond the four feeds that
were graded relevant but never blocked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

import pytest

from scout.scanning.author_class import (
    AUTHOR_CLASS_RULE_VERSION,
    classify_author,
    handle_from_url,
)

FIXTURE = Path(__file__).parent / "fixtures" / "author_classifier" / "acceptance.json"

# Blocked authors the rule must flag: 22 of 43 on 2026-09-11. The rest are
# brand accounts and individuals, which name and handle alone cannot teach.
BLOCKED_RECALL_FLOOR = 22

# Unblocked authors with a human-graded relevant post that the rule flags.
# All four are feeds that were graded relevant but never blocked; a new
# entry here is a regression to explain, not a number to bump.
EXPECTED_GOOD_HITS = frozenset(
    {
        ("bluesky", "bigearthdata.ai"),
        ("bluesky", "feed.igeek.gamer-geek-news.com.ap.brid.gy"),
        ("bluesky", "timxai.ai"),
        ("bluesky", "genainews.bsky.social"),
    }
)


class FixtureAuthor(TypedDict):
    platform: str
    author_id: str
    names: list[str]
    handles: list[str]


def _load() -> tuple[list[FixtureAuthor], list[FixtureAuthor]]:
    document = json.loads(FIXTURE.read_text())
    return document["blocked"], document["good"]


def _is_flagged(author: FixtureAuthor) -> bool:
    names: list[str | None] = list(author["names"]) or [None]
    handles: list[str | None] = list(author["handles"]) or [None]
    return any(
        classify_author(name, handle).author_class == "aggregator"
        for name in names
        for handle in handles
    )


def _first_handle(author: FixtureAuthor) -> str:
    return author["handles"][0] if author["handles"] else author["author_id"]


@pytest.mark.parametrize(
    ("name", "handle", "matched"),
    [
        ("AI & ML News", "ai-news.at.thenote.app", "News"),
        ("Software Engineering Daily", "softwaredaily.bsky.social", "Daily"),
        ("OpenAI {bot}", "openaibot.bsky.social", "{bot}"),
        ("OpenAI :bot:", "openai.zpravobot.news.ap.brid.gy", ":bot:"),
        ("askfred.be", "askfred.be", ".be"),
        ("Automation", "automation-wire.bsky.social", "wire"),
    ],
)
def test_lexicon_hit_reports_class_and_matched_text(
    name: str, handle: str, matched: str
) -> None:
    result = classify_author(name, handle)
    assert (result.author_class, result.matched_text) == ("aggregator", matched)


@pytest.mark.parametrize(
    ("name", "handle"),
    [
        ("Grafana", "grafana.bsky.social"),
        ("The New Stack", "thenewstack.io"),
        ("Robert Ta", "therobertta.bsky.social"),
        ("GitHub Fan", "github-fan.bsky.social"),
        ("", None),
    ],
)
def test_no_lexicon_term_is_unknown(name: str, handle: str | None) -> None:
    result = classify_author(name, handle)
    assert (result.author_class, result.matched_text) == ("unknown", None)


def test_default_handle_suffix_is_not_matched() -> None:
    # ".social" is not in the lexicon, but "social" must not be scanned as a
    # word from the default suffix either.
    assert classify_author("Alice", "alice.bsky.social").author_class == "unknown"


def test_classification_carries_rule_version() -> None:
    assert classify_author("Daily Feed", None).rule_version == AUTHOR_CLASS_RULE_VERSION


@pytest.mark.parametrize(
    ("url", "handle"),
    [
        ("https://bsky.app/profile/alice.bsky.social/post/xyz789", "alice.bsky.social"),
        ("https://bsky.app/profile/handle.invalid/post/xyz789", None),
        ("https://warpcast.com/alice/0xabc", None),
        ("", None),
        (None, None),
    ],
)
def test_handle_from_url(url: str | None, handle: str | None) -> None:
    assert handle_from_url(url) == handle


def test_acceptance_blocked_recall_meets_floor() -> None:
    blocked, _ = _load()
    flagged = sum(_is_flagged(author) for author in blocked)
    assert len(blocked) == 43
    assert flagged >= BLOCKED_RECALL_FLOOR


def test_acceptance_good_authors_flag_only_known_feeds() -> None:
    _, good = _load()
    flagged = {
        (author["platform"], _first_handle(author)) for author in good if _is_flagged(author)
    }
    assert len(good) == 114
    assert flagged == EXPECTED_GOOD_HITS
