"""Tests for ScanStore: scan lifecycle, fetch failures, author blocking."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from scout.config import Message, RelevanceResult
from scout.result import Err, Ok
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


class TestSaveFetchFailureClassification:
    def test_defaults_to_fail_closed_unknown_and_blocking(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id = in_memory_state.start_scan()
        failure_id = in_memory_state.save_fetch_failure(
            scan_id=scan_id, platform="discord", kind="unexpected", message="boom",
        )
        in_memory_state.commit()

        row = in_memory_state.conn.execute(
            "SELECT operation_phase, blocks_watermark_advance FROM scan_fetch_failures "
            "WHERE id = ?",
            (failure_id,),
        ).fetchone()
        assert row["operation_phase"] == "unknown"
        assert row["blocks_watermark_advance"] == 1

    def test_persists_explicit_classification(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()
        failure_id = in_memory_state.save_fetch_failure(
            scan_id=scan_id,
            platform="bluesky",
            kind="parent_missing",
            message="parent unavailable",
            operation_phase="parent_lookup",
            blocks_watermark_advance=False,
        )
        in_memory_state.commit()

        failures = in_memory_state.get_scan_fetch_failures(scan_id)
        assert failures[0]["operation_phase"] == "parent_lookup"
        assert failures[0]["blocks_watermark_advance"] is False
        _ = failure_id


class TestAdvanceWatermark:
    def test_advances_from_stored_fetch_started_at(self, in_memory_state: StateManager) -> None:
        explicit_fsa = datetime(2026, 3, 1, 8, 0, 0, tzinfo=UTC)
        scan_id = in_memory_state.start_scan(fetch_started_at=explicit_fsa)

        result = in_memory_state.advance_watermark(scan_id)

        assert isinstance(result, Ok)
        assert result.value == explicit_fsa
        row = in_memory_state.conn.execute(
            "SELECT safe_watermark_at, watermark_advanced FROM scans WHERE id = ?", (scan_id,)
        ).fetchone()
        assert datetime.fromisoformat(row["safe_watermark_at"]) == explicit_fsa
        assert row["watermark_advanced"] == 1

    def test_errors_for_a_missing_scan(self, in_memory_state: StateManager) -> None:
        result = in_memory_state.advance_watermark(999)
        assert isinstance(result, Err)
        assert result.error.scan_id == 999

    def test_errors_when_fetch_started_at_is_missing_rather_than_defaulting_to_now(
        self, in_memory_state: StateManager
    ) -> None:
        # A scan row with no stored fetch_started_at is not producible through
        # the public start_scan() surface; construct one directly to exercise
        # the "never fall back to now" guard (decision 3).
        now = datetime.now(UTC).isoformat()
        cursor = in_memory_state.conn.execute(
            "INSERT INTO scans (started_at, fetch_started_at, environment, run_kind) "
            "VALUES (?, NULL, 'production', 'live')",
            (now,),
        )
        in_memory_state.commit()
        scan_id = cursor.lastrowid
        assert scan_id is not None

        result = in_memory_state.advance_watermark(scan_id)

        assert isinstance(result, Err)
        assert "fetch_started_at" in result.error.detail
        row = in_memory_state.conn.execute(
            "SELECT safe_watermark_at FROM scans WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["safe_watermark_at"] is None


class TestGetLastScanTimestampByEnvironment:
    def test_none_preserves_legacy_any_environment_behavior(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id = in_memory_state.start_scan(environment="production")
        in_memory_state.complete_scan(scan_id, 0, 0, status="complete")

        assert in_memory_state.get_last_scan_timestamp() is not None

    def test_exact_environment_match_only(self, in_memory_state: StateManager) -> None:
        prod_scan = in_memory_state.start_scan(
            environment="production", fetch_started_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        in_memory_state.complete_scan(prod_scan, 0, 0, status="complete")
        dev_scan = in_memory_state.start_scan(
            environment="development", fetch_started_at=datetime(2026, 2, 1, tzinfo=UTC)
        )
        in_memory_state.complete_scan(dev_scan, 0, 0, status="complete")

        assert in_memory_state.get_last_scan_timestamp(
            environment="production"
        ) == datetime(2026, 1, 1, tzinfo=UTC)
        assert in_memory_state.get_last_scan_timestamp(
            environment="development"
        ) == datetime(2026, 2, 1, tzinfo=UTC)

    def test_unknown_environment_scans_never_influence_a_production_read(
        self, in_memory_state: StateManager
    ) -> None:
        unknown_scan = in_memory_state.start_scan(
            environment="unknown", fetch_started_at=datetime(2026, 5, 1, tzinfo=UTC)
        )
        in_memory_state.complete_scan(unknown_scan, 0, 0, status="complete")

        assert in_memory_state.get_last_scan_timestamp(environment="production") is None


class TestFinalizeScanCoverage:
    def _start_eligible_scan(
        self,
        state: StateManager,
        *,
        environment: str = "production",
        fetch_started_at: datetime | None = None,
        status: str = "complete",
    ) -> int:
        scan_id = state.start_scan(
            fetch_started_at=fetch_started_at or datetime.now(UTC) - timedelta(minutes=5),
            environment=environment,
            run_kind="live",
        )
        state.complete_scan(scan_id, 5, 1, status=status, advance_watermark=False)
        return scan_id

    def test_no_failures_advances_watermark_and_records_complete_outcome(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id = self._start_eligible_scan(in_memory_state)

        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", coverage_classifier_version=1,
        )

        assert isinstance(result, Ok)
        assert result.value.coverage_outcome == "complete"
        assert result.value.watermark_advanced is True
        assert result.value.blocking_failure_ids == ()
        row = in_memory_state.conn.execute(
            "SELECT safe_watermark_at, watermark_advanced, coverage_outcome, "
            "coverage_classifier_version FROM scans WHERE id = ?",
            (scan_id,),
        ).fetchone()
        assert row["safe_watermark_at"] is not None
        assert row["watermark_advanced"] == 1
        assert row["coverage_outcome"] == "complete"
        assert row["coverage_classifier_version"] == 1

    def test_blocking_failure_marks_outcome_blocked_and_withholds_watermark(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id = self._start_eligible_scan(in_memory_state)
        in_memory_state.save_fetch_failure(
            scan_id=scan_id, platform="discord", kind="page_ceiling", message="ceiling",
            operation_phase="fetch", blocks_watermark_advance=True,
        )
        in_memory_state.commit()

        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", coverage_classifier_version=1,
        )

        assert isinstance(result, Ok)
        assert result.value.coverage_outcome == "blocked"
        assert result.value.watermark_advanced is False
        assert len(result.value.blocking_failure_ids) == 1
        row = in_memory_state.conn.execute(
            "SELECT safe_watermark_at FROM scans WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["safe_watermark_at"] is None

    def test_unclassified_failure_is_always_treated_as_blocking(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id = self._start_eligible_scan(in_memory_state)
        # Even if a bug elsewhere ever passed blocks_watermark_advance=False
        # alongside operation_phase='unknown', finalization must still block.
        in_memory_state.save_fetch_failure(
            scan_id=scan_id, platform="discord", kind="unexpected", message="mystery",
            operation_phase="unknown", blocks_watermark_advance=False,
        )
        in_memory_state.commit()

        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", coverage_classifier_version=1,
        )

        assert isinstance(result, Ok)
        assert result.value.coverage_outcome == "blocked"
        assert result.value.watermark_advanced is False

    def test_non_blocking_failure_does_not_block(self, in_memory_state: StateManager) -> None:
        scan_id = self._start_eligible_scan(in_memory_state)
        in_memory_state.save_fetch_failure(
            scan_id=scan_id, platform="bluesky", kind="parent_missing", message="gone",
            operation_phase="parent_lookup", blocks_watermark_advance=False,
        )
        in_memory_state.commit()

        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", coverage_classifier_version=1,
        )

        assert isinstance(result, Ok)
        assert result.value.coverage_outcome == "complete"
        assert result.value.watermark_advanced is True

    def test_partial_processing_status_with_only_nonblocking_evidence_still_advances(
        self, in_memory_state: StateManager
    ) -> None:
        """Decision 4: coverage_outcome is independent of processing status —
        a scan degraded to status='partial' by non-primary enrichment can
        still advance the watermark."""
        scan_id = self._start_eligible_scan(in_memory_state, status="partial")
        in_memory_state.save_fetch_failure(
            scan_id=scan_id, platform="bluesky", kind="parent_missing", message="gone",
            operation_phase="parent_lookup", blocks_watermark_advance=False,
        )
        in_memory_state.commit()

        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", coverage_classifier_version=1,
        )

        assert isinstance(result, Ok)
        assert result.value.coverage_outcome == "complete"
        assert result.value.watermark_advanced is True
        row = in_memory_state.conn.execute(
            "SELECT status FROM scans WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["status"] == "partial"

    def test_rejects_failed_status(self, in_memory_state: StateManager) -> None:
        scan_id = self._start_eligible_scan(in_memory_state, status="failed")
        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", coverage_classifier_version=1,
        )
        assert isinstance(result, Err)
        assert "status" in result.error.detail

    def test_rejects_interrupted_status(self, in_memory_state: StateManager) -> None:
        scan_id = self._start_eligible_scan(in_memory_state, status="interrupted")
        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", coverage_classifier_version=1,
        )
        assert isinstance(result, Err)

    def test_rejects_secondary_role(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan(
            environment="production", run_kind="live", role="secondary",
        )
        in_memory_state.complete_scan(scan_id, 1, 0, status="complete", advance_watermark=False)
        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", coverage_classifier_version=1,
        )
        assert isinstance(result, Err)
        assert "role" in result.error.detail

    def test_rejects_rescore_role(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan(
            environment="production", run_kind="live", role="rescore",
        )
        in_memory_state.complete_scan(scan_id, 1, 0, status="complete", advance_watermark=False)
        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", coverage_classifier_version=1,
        )
        assert isinstance(result, Err)

    def test_rejects_non_live_run_kind(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan(environment="production", run_kind="human_positive")
        in_memory_state.complete_scan(scan_id, 1, 0, status="complete", advance_watermark=False)
        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", coverage_classifier_version=1,
        )
        assert isinstance(result, Err)
        assert "run kind" in result.error.detail

    def test_rejects_unknown_environment(self, in_memory_state: StateManager) -> None:
        scan_id = self._start_eligible_scan(in_memory_state, environment="unknown")
        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="unknown", coverage_classifier_version=1,
        )
        assert isinstance(result, Err)
        assert "environment" in result.error.detail

    def test_rejects_environment_mismatch(self, in_memory_state: StateManager) -> None:
        scan_id = self._start_eligible_scan(in_memory_state, environment="production")
        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="development", coverage_classifier_version=1,
        )
        assert isinstance(result, Err)

    def test_rejects_stale_lease_fence(self, in_memory_state: StateManager) -> None:
        scan_id = self._start_eligible_scan(in_memory_state, environment="production")
        # A new canonical owner takes over after this scan started.
        in_memory_state.bump_environment_lease_fence("production")

        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", coverage_classifier_version=1,
        )
        assert isinstance(result, Err)
        assert "lease fence" in result.error.detail

    def test_rejects_future_fetch_started_at(self, in_memory_state: StateManager) -> None:
        future = datetime.now(UTC) + timedelta(hours=1)
        scan_id = self._start_eligible_scan(in_memory_state, fetch_started_at=future)
        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", coverage_classifier_version=1,
        )
        assert isinstance(result, Err)
        assert "future" in result.error.detail

    def test_rejects_incomplete_required_source_coverage(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id = self._start_eligible_scan(in_memory_state)
        result = in_memory_state.finalize_scan_coverage(
            scan_id,
            environment="production",
            required_source_keys=frozenset({"discord:channel:1", "discord:channel:2"}),
            covered_source_keys=frozenset({"discord:channel:1"}),
            coverage_classifier_version=1,
        )
        assert isinstance(result, Err)
        assert "required-source" in result.error.detail

    def test_complete_required_source_coverage_is_accepted(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id = self._start_eligible_scan(in_memory_state)
        result = in_memory_state.finalize_scan_coverage(
            scan_id,
            environment="production",
            required_source_keys=frozenset({"discord:channel:1"}),
            covered_source_keys=frozenset({"discord:channel:1", "discord:channel:2"}),
            coverage_classifier_version=1,
        )
        assert isinstance(result, Ok)
        assert result.value.coverage_outcome == "complete"

    def test_rejects_caller_expected_outcome_disagreement(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id = self._start_eligible_scan(in_memory_state)
        result = in_memory_state.finalize_scan_coverage(
            scan_id,
            environment="production",
            coverage_classifier_version=1,
            expected_coverage_outcome="blocked",
        )
        assert isinstance(result, Err)
        assert "disagreement" in result.error.detail

    def test_materializes_exactly_the_blocking_failures_as_blocker_rows(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id = self._start_eligible_scan(in_memory_state)
        blocking_id = in_memory_state.save_fetch_failure(
            scan_id=scan_id, platform="discord", kind="page_ceiling", message="ceiling",
            operation_phase="fetch", blocks_watermark_advance=True,
        )
        in_memory_state.save_fetch_failure(
            scan_id=scan_id, platform="bluesky", kind="parent_missing", message="gone",
            operation_phase="parent_lookup", blocks_watermark_advance=False,
        )
        in_memory_state.commit()

        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", coverage_classifier_version=1,
        )
        assert isinstance(result, Ok)

        rows = in_memory_state.conn.execute(
            "SELECT failure_id FROM scan_watermark_blockers WHERE scan_id = ?", (scan_id,)
        ).fetchall()
        assert {r["failure_id"] for r in rows} == {blocking_id}

    def test_is_idempotent_on_rerun(self, in_memory_state: StateManager) -> None:
        scan_id = self._start_eligible_scan(in_memory_state)
        in_memory_state.save_fetch_failure(
            scan_id=scan_id, platform="discord", kind="page_ceiling", message="ceiling",
            operation_phase="fetch", blocks_watermark_advance=True,
        )
        in_memory_state.commit()

        first = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", coverage_classifier_version=1,
        )
        second = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", coverage_classifier_version=1,
        )
        assert isinstance(first, Ok) and isinstance(second, Ok)
        rows = in_memory_state.conn.execute(
            "SELECT COUNT(*) AS n FROM scan_watermark_blockers WHERE scan_id = ?", (scan_id,)
        ).fetchone()
        assert rows["n"] == 1


class TestScanWatermarkBlockersCompositeForeignKey:
    def test_a_blocker_row_cannot_reference_a_failure_from_another_scan(
        self, in_memory_state: StateManager
    ) -> None:
        scan1 = in_memory_state.start_scan(environment="production")
        scan2 = in_memory_state.start_scan(environment="production")
        other_scan_failure_id = in_memory_state.save_fetch_failure(
            scan_id=scan2, platform="discord", kind="unexpected", message="boom",
        )
        in_memory_state.commit()

        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "INSERT INTO scan_watermark_blockers (scan_id, failure_id, created_at) "
                "VALUES (?, ?, ?)",
                (scan1, other_scan_failure_id, datetime.now(UTC).isoformat()),
            )


class TestEnvironmentLeaseFence:
    def test_defaults_to_zero_for_a_never_leased_environment(
        self, in_memory_state: StateManager
    ) -> None:
        assert in_memory_state.get_environment_lease_fence("production") == 0

    def test_bump_increments_monotonically(self, in_memory_state: StateManager) -> None:
        assert in_memory_state.bump_environment_lease_fence("production") == 1
        assert in_memory_state.bump_environment_lease_fence("production") == 2
        assert in_memory_state.get_environment_lease_fence("production") == 2

    def test_new_scan_captures_the_fence_current_at_start_time(
        self, in_memory_state: StateManager
    ) -> None:
        in_memory_state.bump_environment_lease_fence("production")
        scan_id = in_memory_state.start_scan(environment="production")
        row = in_memory_state.conn.execute(
            "SELECT lease_fence FROM scans WHERE id = ?", (scan_id,)
        ).fetchone()
        assert row["lease_fence"] == 1


class TestSourceCheckpoints:
    def test_new_source_starts_uninitialized(self, in_memory_state: StateManager) -> None:
        checkpoint = in_memory_state.ensure_source_checkpoint(
            "discord:channel:111", platform="discord", source_kind="channel",
            provider_key="111",
        )
        assert checkpoint.checkpoint_at is None
        assert checkpoint.active is True
        assert checkpoint.bootstrapped_from_legacy is False

    def test_ensure_is_idempotent_and_does_not_overwrite_an_advanced_checkpoint(
        self, in_memory_state: StateManager
    ) -> None:
        in_memory_state.ensure_source_checkpoint(
            "discord:channel:111", platform="discord", source_kind="channel",
            provider_key="111",
        )
        advanced_at = datetime(2026, 4, 1, tzinfo=UTC)
        in_memory_state.update_source_checkpoint("discord:channel:111", checkpoint_at=advanced_at)

        checkpoint = in_memory_state.ensure_source_checkpoint(
            "discord:channel:111", platform="discord", source_kind="channel",
            provider_key="111",
        )
        assert checkpoint.checkpoint_at == advanced_at

    def test_independent_checkpoints_do_not_interfere(self, in_memory_state: StateManager) -> None:
        in_memory_state.ensure_source_checkpoint(
            "discord:channel:111", platform="discord", source_kind="channel",
            provider_key="111",
        )
        in_memory_state.ensure_source_checkpoint(
            "discord:channel:222", platform="discord", source_kind="channel",
            provider_key="222",
        )
        in_memory_state.update_source_checkpoint(
            "discord:channel:111", checkpoint_at=datetime(2026, 1, 1, tzinfo=UTC)
        )

        untouched = in_memory_state.get_source_checkpoint("discord:channel:222")
        assert untouched is not None
        assert untouched.checkpoint_at is None

    def test_bootstrap_legacy_records_the_legacy_cursor_and_the_audit_flag(
        self, in_memory_state: StateManager
    ) -> None:
        legacy_cursor = datetime(2026, 6, 1, tzinfo=UTC)
        result = in_memory_state.bootstrap_legacy_source_checkpoint(
            "bluesky:search:gateway",
            platform="bluesky",
            source_kind="search",
            provider_key="gateway",
            legacy_checkpoint_at=legacy_cursor,
        )
        assert isinstance(result, Ok)
        assert result.value.checkpoint_at == legacy_cursor
        assert result.value.bootstrapped_from_legacy is True

    def test_bootstrap_legacy_is_one_time_only(self, in_memory_state: StateManager) -> None:
        in_memory_state.bootstrap_legacy_source_checkpoint(
            "bluesky:search:gateway",
            platform="bluesky",
            source_kind="search",
            provider_key="gateway",
            legacy_checkpoint_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        second = in_memory_state.bootstrap_legacy_source_checkpoint(
            "bluesky:search:gateway",
            platform="bluesky",
            source_kind="search",
            provider_key="gateway",
            legacy_checkpoint_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        assert isinstance(second, Err)

    def test_retirement_retains_the_checkpoint_and_reactivation_resumes_it(
        self, in_memory_state: StateManager
    ) -> None:
        in_memory_state.ensure_source_checkpoint(
            "farcaster:feed:ai", platform="farcaster", source_kind="feed", provider_key="ai",
        )
        checkpoint_value = datetime(2026, 3, 15, tzinfo=UTC)
        in_memory_state.update_source_checkpoint(
            "farcaster:feed:ai", checkpoint_at=checkpoint_value
        )

        assert in_memory_state.retire_source_checkpoint("farcaster:feed:ai") is True
        retired = in_memory_state.get_source_checkpoint("farcaster:feed:ai")
        assert retired is not None
        assert retired.active is False
        assert retired.checkpoint_at == checkpoint_value

        reactivated = in_memory_state.reactivate_source_checkpoint("farcaster:feed:ai")
        assert isinstance(reactivated, Ok)
        assert reactivated.value.active is True
        assert reactivated.value.checkpoint_at == checkpoint_value

    def test_reactivation_with_reset_clears_the_checkpoint(
        self, in_memory_state: StateManager
    ) -> None:
        in_memory_state.ensure_source_checkpoint(
            "farcaster:feed:ai", platform="farcaster", source_kind="feed", provider_key="ai",
        )
        in_memory_state.update_source_checkpoint(
            "farcaster:feed:ai", checkpoint_at=datetime(2026, 3, 15, tzinfo=UTC)
        )
        in_memory_state.retire_source_checkpoint("farcaster:feed:ai")

        reactivated = in_memory_state.reactivate_source_checkpoint(
            "farcaster:feed:ai", reset=True
        )
        assert isinstance(reactivated, Ok)
        assert reactivated.value.checkpoint_at is None

    def test_reactivating_an_unknown_source_is_an_error(
        self, in_memory_state: StateManager
    ) -> None:
        result = in_memory_state.reactivate_source_checkpoint("nonexistent:source:x")
        assert isinstance(result, Err)
