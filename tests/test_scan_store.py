"""Tests for ScanStore: scan lifecycle, fetch failures, author blocking."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scout.config import Message, RelevanceResult
from scout.storage.state import StateManager


def _make_discord_msg(platform_id: str = "m1") -> Message:
    return Message(
        platform="discord",
        platform_id=platform_id,
        channel_name="general",
        channel_id="c1",
        author_name="bob",
        author_id="u1",
        content="hello",
        created_at=datetime.now(UTC),
    )


def _make_relevance(msg: Message, relevant: bool = True) -> RelevanceResult:
    return RelevanceResult(
        message=msg,
        relevant=relevant,
        score=0.85,
        reason="fits topic",
        relevant_to=("gateway",),
    )



class TestScanLifecycle:
    def test_start_and_complete_scan(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()
        assert scan_id >= 1

        in_memory_state.complete_scan(scan_id, messages_scanned=10, relevant_found=3)

        row = in_memory_state.conn.execute(
            "SELECT * FROM scans WHERE id = ?",
            (scan_id,),
        ).fetchone()
        assert row["messages_scanned"] == 10
        assert row["relevant_found"] == 3
        assert row["completed_at"] is not None

    def test_get_last_scan_timestamp_returns_none_when_empty(
        self,
        in_memory_state: StateManager,
    ) -> None:
        assert in_memory_state.get_last_scan_timestamp() is None

    def test_get_last_scan_timestamp_after_completion(
        self,
        in_memory_state: StateManager,
    ) -> None:
        scan_id = in_memory_state.start_scan()
        in_memory_state.complete_scan(scan_id, 0, 0)

        ts = in_memory_state.get_last_scan_timestamp()
        assert ts is not None
        assert isinstance(ts, datetime)

class TestScanStats:
    def test_initial_stats_all_zero(self, in_memory_state: StateManager) -> None:
        stats = in_memory_state.get_scan_stats()
        assert stats.total_scans == 0
        assert stats.total_posts == 0
        assert stats.total_relevant == 0
        assert stats.total_drafts == 0

    def test_stats_after_scan(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()

        msg = Message(
            platform="test",
            platform_id="m1",
            channel_name="ch",
            channel_id="c1",
            author_name="a",
            author_id="u1",
            content="content",
            created_at=datetime.now(UTC),
        )
        post_id = in_memory_state.save_post(msg, scan_id)

        result = RelevanceResult(
            message=msg,
            relevant=True,
            score=0.9,
            reason="test",
            relevant_to=("gateway",),
        )
        eval_id = in_memory_state.save_evaluation(result, post_id, scan_id)
        in_memory_state.save_draft(
            post_id,
            eval_id,
            "gateway",
            "Draft text",
            scan_id,
        )
        in_memory_state.complete_scan(scan_id, 1, 1)

        stats = in_memory_state.get_scan_stats()
        assert stats.total_scans == 1
        assert stats.total_posts == 1
        assert stats.total_relevant == 1
        assert stats.total_drafts == 1

class TestGetLatestCompletedScanId:
    def test_returns_none_when_no_completed_scans(
        self,
        in_memory_state: StateManager,
    ) -> None:
        in_memory_state.start_scan()
        assert in_memory_state.get_latest_completed_scan_id() is None

    def test_returns_most_recent_completed_scan_id(
        self,
        in_memory_state: StateManager,
    ) -> None:
        first = in_memory_state.start_scan()
        in_memory_state.complete_scan(first, messages_scanned=1, relevant_found=0)
        second = in_memory_state.start_scan()
        in_memory_state.complete_scan(second, messages_scanned=2, relevant_found=1)
        in_memory_state.start_scan()  # still in-progress, should be ignored

        assert in_memory_state.get_latest_completed_scan_id() == second

class TestScanFetchFailures:
    def test_save_and_retrieve_fetch_failure(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()
        in_memory_state.save_fetch_failure(
            scan_id,
            platform="bluesky",
            kind="rate_limited",
            message="Too many requests",
            context="feed_fetch",
            http_status=429,
            retry_after="30",
            retryable=True,
        )
        in_memory_state.commit()

        failures = in_memory_state.get_scan_fetch_failures(scan_id)
        assert len(failures) == 1
        f = failures[0]
        assert f["platform"] == "bluesky"
        assert f["kind"] == "rate_limited"
        assert f["http_status"] == 429
        assert f["retry_after"] == "30"
        assert f["retryable"] is True
        assert f["context"] == "feed_fetch"

    def test_no_failures_returns_empty(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()
        assert in_memory_state.get_scan_fetch_failures(scan_id) == []

    def test_failures_scoped_to_scan(self, in_memory_state: StateManager) -> None:
        scan1 = in_memory_state.start_scan()
        scan2 = in_memory_state.start_scan()
        in_memory_state.save_fetch_failure(scan1, "farcaster", "network_error", "timeout")
        in_memory_state.commit()

        assert len(in_memory_state.get_scan_fetch_failures(scan1)) == 1
        assert len(in_memory_state.get_scan_fetch_failures(scan2)) == 0

class TestOverflowCount:
    def test_complete_scan_persists_overflow_count(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()
        in_memory_state.complete_scan(
            scan_id,
            messages_scanned=10,
            relevant_found=2,
            overflow_count=5,
        )
        in_memory_state.commit()

        row = in_memory_state.conn.execute(
            "SELECT overflow_count FROM scans WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row is not None
        assert row["overflow_count"] == 5

    def test_complete_scan_can_skip_watermark_advance(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()
        in_memory_state.complete_scan(
            scan_id,
            messages_scanned=10,
            relevant_found=2,
            advance_watermark=False,
        )
        in_memory_state.commit()

        row = in_memory_state.conn.execute(
            "SELECT status, safe_watermark_at FROM scans WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row is not None
        assert row["status"] == "complete"
        assert row["safe_watermark_at"] is None

    def test_overflow_count_defaults_to_zero(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()
        in_memory_state.complete_scan(scan_id, messages_scanned=5, relevant_found=1)
        in_memory_state.commit()

        row = in_memory_state.conn.execute(
            "SELECT overflow_count FROM scans WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row is not None
        assert row["overflow_count"] == 0

class TestScanStatusTransitions:
    def test_complete_status_sets_watermark(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()
        in_memory_state.complete_scan(scan_id, 10, 3, status="complete")
        in_memory_state.commit()

        row = in_memory_state.conn.execute(
            "SELECT status, safe_watermark_at FROM scans WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["status"] == "complete"
        assert row["safe_watermark_at"] is not None

    def test_partial_status_leaves_watermark_null(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()
        in_memory_state.complete_scan(scan_id, 5, 1, status="partial")
        in_memory_state.commit()

        row = in_memory_state.conn.execute(
            "SELECT status, safe_watermark_at FROM scans WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["status"] == "partial"
        assert row["safe_watermark_at"] is None

    def test_interrupted_status_is_valid(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()
        in_memory_state.complete_scan(scan_id, 0, 0, status="interrupted")
        in_memory_state.commit()

        row = in_memory_state.conn.execute(
            "SELECT status FROM scans WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["status"] == "interrupted"

class TestBlockedAuthors:
    def test_block_reactivate_and_platform_scope(self, in_memory_state: StateManager) -> None:
        block_id = in_memory_state.block_author(
            platform=" Bluesky ",
            author_id="did:plc:aggregator",
            author_name="Link Aggregator",
            reason="automated link feed",
        )

        assert in_memory_state.is_author_blocked(platform="bluesky", author_id="did:plc:aggregator")
        assert not in_memory_state.is_author_blocked(
            platform="farcaster", author_id="did:plc:aggregator"
        )
        assert in_memory_state.get_blocked_author_keys() == frozenset(
            {("bluesky", "did:plc:aggregator")}
        )

        assert in_memory_state.unblock_author(platform="bluesky", author_id="did:plc:aggregator")
        assert not in_memory_state.is_author_blocked(
            platform="bluesky", author_id="did:plc:aggregator"
        )

        reactivated_id = in_memory_state.block_author(
            platform="bluesky",
            author_id="did:plc:aggregator",
            author_name="Updated Name",
            reason="confirmed bot",
        )
        assert reactivated_id == block_id
        row = in_memory_state.conn.execute(
            "SELECT author_name, reason, active FROM blocked_authors WHERE id = ?",
            (block_id,),
        ).fetchone()
        assert row is not None
        assert dict(row) == {
            "author_name": "Updated Name",
            "reason": "confirmed bot",
            "active": 1,
        }

    def test_blocked_posts_are_not_requeued_for_failed_scan_recovery(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id = in_memory_state.start_scan()
        msg = _make_discord_msg("blocked-recovery")
        in_memory_state.save_post(msg, scan_id)
        assert [post.platform_id for post in in_memory_state.load_unevaluated_posts()] == [
            msg.platform_id
        ]

        in_memory_state.block_author(
            platform=msg.platform,
            author_id=msg.author_id,
            author_name=msg.author_name,
        )

        assert in_memory_state.load_unevaluated_posts() == []
        assert in_memory_state.load_unevaluated_posts(scan_id=scan_id) == []

    @pytest.mark.parametrize(
        ("platform", "author_id"),
        [("", "author"), ("bluesky", ""), ("   ", "author"), ("bluesky", "   ")],
    )
    def test_blank_identity_is_rejected(
        self,
        in_memory_state: StateManager,
        platform: str,
        author_id: str,
    ) -> None:
        with pytest.raises(ValueError):
            in_memory_state.block_author(platform=platform, author_id=author_id)
