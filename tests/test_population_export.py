"""Population export transforms and stable JSON-lines rendering."""

from __future__ import annotations

import pytest

from scout.scanning.author_class import handle_from_post_url


@pytest.mark.parametrize(
    ("platform", "url", "expected"),
    [
        (
            "bluesky",
            "https://bsky.app/profile/alice.bsky.social/post/3abc",
            "alice.bsky.social",
        ),
        ("farcaster", "https://warpcast.com/alice/0xabc", "alice"),
        ("farcaster", "https://farcaster.xyz/@bob/0xdef", "bob"),
        ("discord", "https://discord.com/channels/1/2/3", None),
    ],
)
def test_handle_from_post_url(platform: str, url: str, expected: str | None) -> None:
    assert handle_from_post_url(platform, url) == expected
