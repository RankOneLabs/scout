"""Author classification: a cheap, annotating read of who posted.

Relevance labels judge content only. Whether the *account* is worth
engaging is a separate dimension, today expressed solely as the manual
blocklist. This node adds the first automatic signal on that dimension:
a lexicon over the author's display name and handle that flags link
aggregators, news feeds and bots. It annotates, never gates — the scan
pipeline still evaluates every unblocked post, and the class is shown in
the review UI next to the block button so a reviewer can act on it.

Pure transform: no I/O, no clock. `AUTHOR_CLASS_RULE_VERSION` is stored
with every classification so a future rule change is distinguishable
from a rerun of the same rule.

Acceptance: tests/fixtures/author_classifier/acceptance.json, scored by
tests/test_author_class.py. Brand accounts (Grafana, InfoQ, The New
Stack) and individuals are out of scope for this rule; a bio-aware rule
needs profile ingestion the platform adapters do not do yet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

AUTHOR_CLASS_RULE_VERSION = 1

AuthorClass = Literal["aggregator", "unknown"]

_BSKY_PROFILE_URL = re.compile(r"^https://bsky\.app/profile/([^/]+)/")
_DEFAULT_HANDLE_SUFFIX = ".bsky.social"
_INVALID_HANDLE = "handle.invalid"

# Whole-word terms in a name or handle that mark an automated feed, plus
# handle TLDs that in practice only aggregators use, plus the two
# bot-marker spellings seen on Bluesky bridges.
_AGGREGATOR_LEXICON = re.compile(
    r"\b(news|feed|daily|weekly|wire|bot|digest|hub|arxiv|compilator"
    r"|updates?|alerts?|headlines|tools|apps)\b"
    r"|\.(news|ai|art|be)$"
    r"|:bot:|\{bot\}",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class AuthorClassification:
    """Outcome of one classification: the class and the text that decided it."""

    author_class: AuthorClass
    rule_version: int
    matched_text: str | None


def handle_from_url(url: str | None) -> str | None:
    """Recover the author handle from a Bluesky post URL; None elsewhere."""
    if not url:
        return None
    match = _BSKY_PROFILE_URL.match(url)
    if match is None:
        return None
    handle = match.group(1)
    return None if handle == _INVALID_HANDLE else handle


def _normalize_handle(handle: str | None) -> str:
    if not handle or handle == _INVALID_HANDLE:
        return ""
    return handle.removesuffix(_DEFAULT_HANDLE_SUFFIX)


def classify_author(name: str | None, handle: str | None) -> AuthorClassification:
    """Classify an author from display name and handle alone.

    The default-suffix handle is stripped before matching so the lexicon
    sees the part the author chose. A hit in either field is enough.
    """
    for candidate in (name or "", _normalize_handle(handle)):
        match = _AGGREGATOR_LEXICON.search(candidate)
        if match is not None:
            return AuthorClassification(
                author_class="aggregator",
                rule_version=AUTHOR_CLASS_RULE_VERSION,
                matched_text=match.group(0),
            )
    return AuthorClassification(
        author_class="unknown",
        rule_version=AUTHOR_CLASS_RULE_VERSION,
        matched_text=None,
    )
