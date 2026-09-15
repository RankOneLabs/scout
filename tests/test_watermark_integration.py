"""Consolidated coverage/watermark integration evidence for the release gate.

Two things live here that don't belong split across the storage-level unit
tests in test_scan_store.py / test_scan_lease.py / test_watermark_recovery.py:

1. `TestLegacyProductionMigration` proves the actual checked-in
   tests/fixtures/watermark/legacy-production.db (a genuine v0-shaped
   production database, no environment/coverage/lease/checkpoint columns)
   migrates cleanly, that its history is correctly excluded from a
   'production' environment read (it has no environment column, so it
   becomes 'unknown'), and that grandfathering a legacy per-source cursor
   from tests/fixtures/watermark/source-coverage.json preserves the
   timestamp byte-for-byte.

2. `TestIntegrationMatrix` is the single place every named coverage/
   watermark scenario in the cohort's acceptance criteria is proven against
   a real StateManager in one file, even though most already have deeper
   unit coverage elsewhere — this file is the one a release review can
   point at as the full matrix in one place.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict

import pytest

from scout.result import Err, Ok
from scout.storage.state import StateManager

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "watermark"


class SourceCoverageEntry(TypedDict):
    source_key: str
    platform: str
    source_kind: str
    provider_key: str
    required: bool
    legacy_checkpoint_at: str


class SourceCoverageFixture(TypedDict):
    environment: str
    description: str
    sources: list[SourceCoverageEntry]


# --- Legacy production migration -------------------------------------------


def _fresh_bootstrap_schema(tmp_path: Path) -> tuple[dict[str, object], int]:
    from tests.legacy_schema_fixtures import schema_snapshot

    db_path = str(tmp_path / "fresh.db")
    with StateManager(db_path=db_path) as state:
        return schema_snapshot(state.conn)


def _migrated_legacy_copy(tmp_path: Path) -> Path:
    """A writable copy of the checked-in legacy fixture — tests mutate
    (bootstrap checkpoints, insert scans), so the checked-in original must
    stay untouched."""
    dest = tmp_path / "legacy-production.db"
    dest.write_bytes((FIXTURES_DIR / "legacy-production.db").read_bytes())
    return dest


@pytest.fixture
def source_coverage() -> SourceCoverageFixture:
    data: SourceCoverageFixture = json.loads((FIXTURES_DIR / "source-coverage.json").read_text())
    return data


class TestLegacyProductionMigration:
    def test_migrates_cleanly_and_converges_with_a_fresh_bootstrap(self, tmp_path: Path) -> None:
        from tests.legacy_schema_fixtures import schema_snapshot

        legacy_path = _migrated_legacy_copy(tmp_path)
        with StateManager(db_path=str(legacy_path)) as state:
            snapshot, user_version = schema_snapshot(state.conn)

        from scout.storage.schema import LATEST_SCHEMA_VERSION

        assert user_version == LATEST_SCHEMA_VERSION
        fresh_snapshot, fresh_version = _fresh_bootstrap_schema(tmp_path)
        assert user_version == fresh_version
        assert snapshot == fresh_snapshot, (
            "the checked-in legacy-production.db's migration path diverged "
            "from a fresh SCHEMA bootstrap"
        )

    def test_foreign_keys_are_clean_after_migration(self, tmp_path: Path) -> None:
        legacy_path = _migrated_legacy_copy(tmp_path)
        with StateManager(db_path=str(legacy_path)) as state:
            errors = state.conn.execute("PRAGMA foreign_key_check").fetchall()
        assert errors == []

    def test_legacy_history_has_no_environment_and_never_leaks_into_a_production_read(
        self, tmp_path: Path
    ) -> None:
        legacy_path = _migrated_legacy_copy(tmp_path)
        with StateManager(db_path=str(legacy_path)) as state:
            rows = state.conn.execute("SELECT environment FROM scans").fetchall()
            assert rows, "fixture must carry real legacy scan history"
            assert {r["environment"] for r in rows} == {"unknown"}

            # Exact-environment isolation (decision 6): 'production' has no
            # eligible cursor yet — none of the grandfathered history
            # counts, by construction of get_last_scan_timestamp's exact,
            # non-'unknown' environment match.
            assert state.get_last_scan_timestamp(environment="production") is None

    def test_pre_and_post_bootstrap_eligible_cursor_per_environment(
        self, tmp_path: Path, source_coverage: SourceCoverageFixture
    ) -> None:
        legacy_path = _migrated_legacy_copy(tmp_path)
        with StateManager(db_path=str(legacy_path)) as state:
            environment = source_coverage["environment"]
            assert isinstance(environment, str)

            # Pre: no eligible cursor for this environment.
            assert state.get_last_scan_timestamp(environment=environment) is None

            for source in source_coverage["sources"]:
                state.bootstrap_legacy_source_checkpoint(
                    source["source_key"],
                    platform=source["platform"],
                    source_kind=source["source_kind"],
                    provider_key=source["provider_key"],
                    legacy_checkpoint_at=datetime.fromisoformat(source["legacy_checkpoint_at"]),
                    required=source["required"],
                )
            state.commit()

            # Bootstrapping a per-source checkpoint is not itself a
            # watermark advance — it grandfathers a cursor, it does not
            # fabricate a scan. Still no eligible cursor until a real
            # canonical scan finalizes.
            assert state.get_last_scan_timestamp(environment=environment) is None

            lease = state.acquire_environment_lease(environment, "owner", ttl_seconds=300)
            assert isinstance(lease, Ok)
            fetch_started_at = datetime.now(UTC) - timedelta(minutes=1)
            scan_id = state.start_scan(
                fetch_started_at=fetch_started_at, environment=environment, run_kind="live",
            )
            state.complete_scan(scan_id, 3, 1, status="complete")
            result = state.finalize_scan_coverage(
                scan_id, environment=environment, advance_watermark=True, owner_id="owner",
                required_source_keys=frozenset(
                    source["source_key"]
                    for source in source_coverage["sources"]
                    if source["required"]
                ),
                covered_source_keys=frozenset(
                    source["source_key"]
                    for source in source_coverage["sources"]
                    if source["required"]
                ),
                coverage_classifier_version=1,
            )
            assert isinstance(result, Ok)

            # Post: the environment now has an eligible cursor, anchored to
            # the new scan's fetch_started_at — never the legacy history.
            assert state.get_last_scan_timestamp(environment=environment) == fetch_started_at

    def test_bootstrap_preserves_the_grandfathered_timestamp_byte_for_byte(
        self, tmp_path: Path, source_coverage: SourceCoverageFixture
    ) -> None:
        legacy_path = _migrated_legacy_copy(tmp_path)
        with StateManager(db_path=str(legacy_path)) as state:
            for source in source_coverage["sources"]:
                expected = datetime.fromisoformat(source["legacy_checkpoint_at"])
                result = state.bootstrap_legacy_source_checkpoint(
                    source["source_key"],
                    platform=source["platform"],
                    source_kind=source["source_kind"],
                    provider_key=source["provider_key"],
                    legacy_checkpoint_at=expected,
                    required=source["required"],
                )
                assert isinstance(result, Ok)
                assert result.value.checkpoint_at == expected
                assert result.value.bootstrapped_from_legacy is True

            state.commit()
            for source in source_coverage["sources"]:
                # The raw stored TEXT, not a parsed datetime: an equivalent
                # but re-spelled timestamp would round-trip through
                # fromisoformat unnoticed, which is exactly what this test
                # exists to rule out.
                raw = state.conn.execute(
                    "SELECT checkpoint_at FROM source_checkpoints WHERE source_key = ?",
                    (source["source_key"],),
                ).fetchone()["checkpoint_at"]
                assert raw == source["legacy_checkpoint_at"]

    def test_bootstrap_is_one_time_only(
        self, tmp_path: Path, source_coverage: SourceCoverageFixture
    ) -> None:
        legacy_path = _migrated_legacy_copy(tmp_path)
        source = source_coverage["sources"][0]
        with StateManager(db_path=str(legacy_path)) as state:
            first = state.bootstrap_legacy_source_checkpoint(
                source["source_key"], platform=source["platform"],
                source_kind=source["source_kind"], provider_key=source["provider_key"],
                legacy_checkpoint_at=datetime.fromisoformat(source["legacy_checkpoint_at"]),
                required=source["required"],
            )
            assert isinstance(first, Ok)
            state.commit()

            second = state.bootstrap_legacy_source_checkpoint(
                source["source_key"], platform=source["platform"],
                source_kind=source["source_kind"], provider_key=source["provider_key"],
                legacy_checkpoint_at=datetime.now(UTC),
                required=source["required"],
            )
            assert isinstance(second, Err)


# --- Integration matrix -----------------------------------------------------


def _eligible_scan(
    state: StateManager, *, environment: str = "production", owner_id: str = "owner",
    role: str = "canonical_live", status: str = "complete",
    fetch_started_at: datetime | None = None,
) -> int:
    scan_id = state.start_scan(
        fetch_started_at=fetch_started_at or (datetime.now(UTC) - timedelta(minutes=5)),
        environment=environment, run_kind="live", role=role,
    )
    state.complete_scan(scan_id, 5, 1, status=status)
    return scan_id


class TestIntegrationMatrix:
    def test_live_canonical_scan_advances_watermark(self, in_memory_state: StateManager) -> None:
        in_memory_state.acquire_environment_lease("production", "owner", ttl_seconds=300)
        scan_id = _eligible_scan(in_memory_state)
        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", advance_watermark=True, owner_id="owner",
            coverage_classifier_version=1,
        )
        assert isinstance(result, Ok)
        assert result.value.watermark_advanced is True

    def test_secondary_scan_never_advances(self, in_memory_state: StateManager) -> None:
        in_memory_state.acquire_environment_lease("production", "owner", ttl_seconds=300)
        canonical_id = _eligible_scan(in_memory_state)
        secondary_id = _eligible_scan(in_memory_state, role="secondary")
        in_memory_state.link_secondary_scan(secondary_id, canonical_scan_id=canonical_id)
        result = in_memory_state.finalize_scan_coverage(
            secondary_id, environment="production", advance_watermark=True, owner_id="owner",
            coverage_classifier_version=1,
        )
        assert isinstance(result, Err)
        assert "canonical-live" in result.error.detail

    def test_rescore_scan_never_advances(self, in_memory_state: StateManager) -> None:
        in_memory_state.acquire_environment_lease("production", "owner", ttl_seconds=300)
        scan_id = _eligible_scan(in_memory_state, role="rescore")
        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", advance_watermark=True, owner_id="owner",
            coverage_classifier_version=1,
        )
        assert isinstance(result, Err)

    def test_failed_scan_never_advances(self, in_memory_state: StateManager) -> None:
        in_memory_state.acquire_environment_lease("production", "owner", ttl_seconds=300)
        scan_id = _eligible_scan(in_memory_state, status="failed")
        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", advance_watermark=True, owner_id="owner",
            coverage_classifier_version=1,
        )
        assert isinstance(result, Err)

    def test_interrupted_scan_never_advances(self, in_memory_state: StateManager) -> None:
        in_memory_state.acquire_environment_lease("production", "owner", ttl_seconds=300)
        scan_id = _eligible_scan(in_memory_state, status="interrupted")
        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", advance_watermark=True, owner_id="owner",
            coverage_classifier_version=1,
        )
        assert isinstance(result, Err)

    def test_future_fetch_started_at_is_rejected(self, in_memory_state: StateManager) -> None:
        in_memory_state.acquire_environment_lease("production", "owner", ttl_seconds=300)
        scan_id = _eligible_scan(
            in_memory_state, fetch_started_at=datetime.now(UTC) + timedelta(hours=1),
        )
        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", advance_watermark=True, owner_id="owner",
            coverage_classifier_version=1,
        )
        assert isinstance(result, Err)
        assert "future" in result.error.detail

    def test_missing_classification_without_evidence_is_rejected(
        self, in_memory_state: StateManager
    ) -> None:
        in_memory_state.acquire_environment_lease("production", "owner", ttl_seconds=300)
        in_memory_state.ensure_source_checkpoint(
            "bluesky:search:missing", platform="bluesky",
            source_kind="search", provider_key="missing",
        )
        scan_id = _eligible_scan(in_memory_state)
        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", advance_watermark=True, owner_id="owner",
            required_source_keys=frozenset({"bluesky:search:missing"}),
            covered_source_keys=frozenset(),
            coverage_classifier_version=1,
        )
        assert isinstance(result, Err)
        assert "without failure evidence" in result.error.detail

    def test_cross_scan_blocker_is_structurally_impossible(
        self, in_memory_state: StateManager
    ) -> None:
        in_memory_state.acquire_environment_lease("production", "owner", ttl_seconds=300)
        scan_a = _eligible_scan(in_memory_state)
        scan_b = _eligible_scan(in_memory_state)
        failure_id = in_memory_state.save_fetch_failure(
            scan_id=scan_a, platform="discord", kind="page_ceiling", message="ceiling",
            operation_phase="fetch", blocks_watermark_advance=True,
        )
        in_memory_state.commit()
        # scan_watermark_blockers' composite FK is (scan_id, failure_id) ->
        # scan_fetch_failures(scan_id, id) — a blocker row can only ever
        # reference a failure whose own scan_id matches.
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "INSERT INTO scan_watermark_blockers (scan_id, failure_id, created_at) "
                "VALUES (?, ?, ?)",
                (scan_b, failure_id, datetime.now(UTC).isoformat()),
            )
            in_memory_state.commit()

    def test_stale_lease_fence_blocks_advancement(self, in_memory_state: StateManager) -> None:
        acquired = in_memory_state.acquire_environment_lease("production", "owner", ttl_seconds=300)
        assert isinstance(acquired, Ok)
        scan_id = _eligible_scan(in_memory_state)
        # A takeover after expiry bumps the fence, stranding the scan's
        # captured-at-start fence behind current.
        in_memory_state.conn.execute(
            "UPDATE environment_leases SET expires_at = ? WHERE environment = 'production'",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
        )
        in_memory_state.commit()
        takeover = in_memory_state.acquire_environment_lease(
            "production", "owner-2", ttl_seconds=300,
        )
        assert isinstance(takeover, Ok)
        assert takeover.value.fence != acquired.value.fence

        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", advance_watermark=True, owner_id="owner-2",
            coverage_classifier_version=1,
        )
        assert isinstance(result, Err)
        assert "stale lease fence" in result.error.detail

    def test_exact_environment_isolation(self, in_memory_state: StateManager) -> None:
        in_memory_state.acquire_environment_lease("production", "owner", ttl_seconds=300)
        in_memory_state.acquire_environment_lease("staging", "owner", ttl_seconds=300)
        prod_scan = _eligible_scan(
            in_memory_state, environment="production",
            fetch_started_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        staging_scan = _eligible_scan(
            in_memory_state, environment="staging",
            fetch_started_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        in_memory_state.finalize_scan_coverage(
            prod_scan, environment="production", advance_watermark=True, owner_id="owner",
            coverage_classifier_version=1,
        )
        in_memory_state.finalize_scan_coverage(
            staging_scan, environment="staging", advance_watermark=True, owner_id="owner",
            coverage_classifier_version=1,
        )
        assert in_memory_state.get_last_scan_timestamp(environment="production") == datetime(
            2026, 1, 1, tzinfo=UTC
        )
        assert in_memory_state.get_last_scan_timestamp(environment="staging") == datetime(
            2026, 6, 1, tzinfo=UTC
        )

    def test_new_source_starts_cold(self, in_memory_state: StateManager) -> None:
        checkpoint = in_memory_state.ensure_source_checkpoint(
            "bluesky:search:new-source", platform="bluesky", source_kind="search",
            provider_key="new-source",
        )
        assert checkpoint.checkpoint_at is None
        assert checkpoint.active is True
        assert checkpoint.bootstrapped_from_legacy is False

    def test_retired_source_excluded_then_reactivated_resumes(
        self, in_memory_state: StateManager
    ) -> None:
        in_memory_state.ensure_source_checkpoint(
            "farcaster:feed:seasonal", platform="farcaster", source_kind="feed",
            provider_key="seasonal",
        )
        checkpoint_at = datetime.now(UTC) - timedelta(days=1)
        in_memory_state.update_source_checkpoint(
            "farcaster:feed:seasonal", checkpoint_at=checkpoint_at,
        )
        assert in_memory_state.retire_source_checkpoint("farcaster:feed:seasonal") is True
        retired = in_memory_state.get_source_checkpoint("farcaster:feed:seasonal")
        assert retired is not None
        assert retired.active is False
        active_only = in_memory_state.list_source_checkpoints(active_only=True)
        assert "farcaster:feed:seasonal" not in {c.source_key for c in active_only}

        reactivated = in_memory_state.reactivate_source_checkpoint("farcaster:feed:seasonal")
        assert isinstance(reactivated, Ok)
        assert reactivated.value.active is True
        # Resumes from the retained cursor rather than restarting cold.
        assert reactivated.value.checkpoint_at == checkpoint_at

    def test_chronological_boundary_anchors_watermark_to_fetch_started_at(
        self, in_memory_state: StateManager
    ) -> None:
        in_memory_state.acquire_environment_lease("production", "owner", ttl_seconds=300)
        fetch_started_at = datetime.now(UTC) - timedelta(minutes=10)
        scan_id = _eligible_scan(in_memory_state, fetch_started_at=fetch_started_at)
        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", advance_watermark=True, owner_id="owner",
            coverage_classifier_version=1,
        )
        assert isinstance(result, Ok)
        # Not completed_at, not "now" — the pre-fetch boundary, so a
        # message that arrived mid-fetch is never silently skipped by the
        # next scan's window.
        assert result.value.safe_watermark_at == fetch_started_at

    def test_non_chronological_feed_ceiling_blocks_coverage(
        self, in_memory_state: StateManager
    ) -> None:
        in_memory_state.acquire_environment_lease("production", "owner", ttl_seconds=300)
        scan_id = _eligible_scan(in_memory_state)
        # farcaster/bluesky feeds are not strictly chronological — a page
        # ceiling reached before the since-boundary is a genuine coverage
        # gap, not merely a slow fetch, and must block.
        in_memory_state.save_fetch_failure(
            scan_id=scan_id, platform="farcaster", kind="page_ceiling",
            message="reached page ceiling before since-boundary",
            context="farcaster:feed:trending", operation_phase="fetch",
            blocks_watermark_advance=True,
        )
        in_memory_state.commit()
        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", advance_watermark=True, owner_id="owner",
            coverage_classifier_version=1,
        )
        assert isinstance(result, Ok)
        assert result.value.coverage_outcome == "blocked"
        assert result.value.watermark_advanced is False

    def test_six_hour_probe_evidence_is_recorded_and_retrievable(
        self, in_memory_state: StateManager
    ) -> None:
        acquired = in_memory_state.acquire_environment_lease(
            "production", "probe-owner", ttl_seconds=300
        )
        assert isinstance(acquired, Ok)
        probe_id = in_memory_state.start_probe_run(
            "production", source_count=3, window_hours=6.0, limits_json="{}",
        )
        in_memory_state.complete_probe_run(
            probe_id, environment="production", owner_id="probe-owner",
            fence=acquired.value.fence, passed=True, source_count=3,
            page_count=9, detail_json='{"sources": 3}',
        )
        probe = in_memory_state.get_latest_passed_probe("production", max_age_seconds=3600)
        assert probe is not None
        assert probe.window_hours == 6.0
        assert probe.passed is True

        # A probe with too short a window is not eligible collateral for a
        # cutover even if it passed.
        short_probe_id = in_memory_state.start_probe_run(
            "production", source_count=3, window_hours=1.0, limits_json="{}",
        )
        in_memory_state.complete_probe_run(
            short_probe_id, environment="production", owner_id="probe-owner",
            fence=acquired.value.fence, passed=True, source_count=3,
            page_count=3, detail_json="{}",
        )
        # get_latest_passed_probe itself doesn't filter on window_hours —
        # that gate lives in cutover_watermark — but the row is at least
        # readable for an operator/UI to inspect its evidence.
        latest = in_memory_state.get_probe_run(short_probe_id)
        assert latest is not None
        assert latest.window_hours == 1.0

    def test_expected_old_race_refuses_a_stale_expected_old(
        self, in_memory_state: StateManager
    ) -> None:
        acquired = in_memory_state.acquire_environment_lease("production", "owner", ttl_seconds=300)
        assert isinstance(acquired, Ok)
        probe_id = in_memory_state.start_probe_run(
            "production", source_count=1, window_hours=6.0, limits_json="{}",
        )
        in_memory_state.complete_probe_run(
            probe_id, environment="production", owner_id="owner",
            fence=acquired.value.fence, passed=True, source_count=1,
            page_count=1, detail_json="{}",
        )

        # Someone else's cutover already moved the cursor between when this
        # caller observed expected_old and when they call cutover.
        first = in_memory_state.cutover_watermark(
            environment="production", owner_id="owner", fence=acquired.value.fence,
            operator="steve", rationale="accept the gap", policy="policy-v1",
            source_evidence="status page", expected_old_watermark=None,
            accepted_new_watermark=datetime.now(UTC) - timedelta(hours=2),
            probe_max_age_seconds=3600, required_probe_limits={},
        )
        assert isinstance(first, Ok)

        stale_race = in_memory_state.cutover_watermark(
            environment="production", owner_id="owner", fence=acquired.value.fence,
            operator="steve", rationale="accept another gap", policy="policy-v1",
            source_evidence="status page",
            expected_old_watermark=datetime.now(UTC) - timedelta(days=1),
            accepted_new_watermark=datetime.now(UTC) - timedelta(minutes=1),
            probe_max_age_seconds=3600, required_probe_limits={},
        )
        assert isinstance(stale_race, Err)
        assert stale_race.error.reason == "stale_expected_old"

    def test_backfill_style_recovery_reconciles_and_advances(
        self, in_memory_state: StateManager
    ) -> None:
        # Simulates the storage-level shape of `scout watermark backfill`:
        # reconcile any abandoned canonical owner, then run a normal
        # canonical-owner pipeline through the same finalize path.
        acquired = in_memory_state.acquire_environment_lease("production", "owner", ttl_seconds=300)
        assert isinstance(acquired, Ok)
        # A scan started but never completed — the worker holding it died
        # or was fenced out mid-flight, leaving it truly abandoned rather
        # than terminally failed/interrupted (which reconciliation does
        # not touch — those are already resolved).
        abandoned_id = in_memory_state.start_scan(
            fetch_started_at=datetime.now(UTC) - timedelta(minutes=5),
            environment="production", run_kind="live",
        )

        takeover = in_memory_state.acquire_environment_lease(
            "production", "owner-2", ttl_seconds=-1,
        )
        assert isinstance(takeover, Err)  # still held and unexpired by owner
        in_memory_state.conn.execute(
            "UPDATE environment_leases SET expires_at = ? WHERE environment = 'production'",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
        )
        in_memory_state.commit()
        recovered = in_memory_state.acquire_environment_lease(
            "production", "recovery-owner", ttl_seconds=300,
        )
        assert isinstance(recovered, Ok)
        reconciled = in_memory_state.reconcile_abandoned_canonical_owners(
            "production", recovered.value.fence,
        )
        assert abandoned_id in reconciled

        backfill_scan = _eligible_scan(in_memory_state, owner_id="recovery-owner")
        result = in_memory_state.finalize_scan_coverage(
            backfill_scan, environment="production", advance_watermark=True,
            owner_id="recovery-owner", coverage_classifier_version=1,
        )
        assert isinstance(result, Ok)
        assert result.value.watermark_advanced is True
        record_id = in_memory_state.record_recovery_operation(
            environment="production", operation="backfill", operator="steve",
            rationale="recover from interrupted owner", outcome="accepted",
            detail=f"reconciled scan #{abandoned_id}, advanced via scan #{backfill_scan}",
        )
        assert record_id > 0

    def test_controlled_cutover_accepts_the_gap_with_a_durable_audit_row(
        self, in_memory_state: StateManager
    ) -> None:
        acquired = in_memory_state.acquire_environment_lease("production", "owner", ttl_seconds=300)
        assert isinstance(acquired, Ok)
        probe_id = in_memory_state.start_probe_run(
            "production", source_count=2, window_hours=6.0, limits_json="{}",
        )
        in_memory_state.complete_probe_run(
            probe_id, environment="production", owner_id="owner",
            fence=acquired.value.fence, passed=True, source_count=2,
            page_count=4, detail_json="{}",
        )

        accepted_new = datetime.now(UTC) - timedelta(minutes=5)
        result = in_memory_state.cutover_watermark(
            environment="production", owner_id="owner", fence=acquired.value.fence,
            operator="steve", rationale="platform outage, unrecoverable gap",
            policy="accept-gap-v1", source_evidence="platform status page incident #4821",
            expected_old_watermark=None, accepted_new_watermark=accepted_new,
            probe_max_age_seconds=3600, required_probe_limits={},
        )
        assert isinstance(result, Ok)
        assert result.value.accepted_new_watermark == accepted_new
        assert result.value.probe_run_id == probe_id
        assert in_memory_state.get_last_scan_timestamp(environment="production") == accepted_new

        audit_row = in_memory_state.conn.execute(
            "SELECT operation, outcome, probe_run_id, accepted_new_watermark "
            "FROM recovery_operations WHERE id = ?",
            (result.value.audit_id,),
        ).fetchone()
        assert audit_row["operation"] == "cutover"
        assert audit_row["outcome"] == "accepted"
        assert audit_row["probe_run_id"] == probe_id
