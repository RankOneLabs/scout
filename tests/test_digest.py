"""Tests for digest formatting."""

from __future__ import annotations

from scout.scanning.digest import finalize_digest, write_digest_header


def test_finalize_digest_includes_page_ceiling_failures(tmp_path) -> None:
    digest_path = tmp_path / "digest.md"
    write_digest_header(str(digest_path), 123)

    content = finalize_digest(
        str(digest_path),
        messages_scanned=10,
        relevant_count=0,
        status="partial",
        failures=[
            {
                "platform": "bluesky",
                "context": "search: agent",
                "kind": "page_ceiling",
                "message": "Page ceiling reached; fetched 250 posts",
                "http_status": None,
                "retry_after": None,
                "retryable": True,
            }
        ],
    )

    assert "## Scan Failures" in content
    assert "`page_ceiling`" in content
    assert "search: agent" in content
