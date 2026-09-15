"""Tests for the environment lease primitive: acquire/renew/release/takeover,
startup reconciliation of abandoned canonical owners, cutover, and the
probe/recovery-audit evidence trail."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from scout.result import Err, Ok
from scout.storage.scans import EnvironmentLease, LeaseError
from scout.storage.state import StateManager


def _acquire(state: StateManager, environment: str, owner_id: str, *, ttl_seconds: float = 30):
    return state.acquire_environment_lease(environment, owner_id, ttl_seconds=ttl_seconds)


@pytest.fixture
def in_memory_state() -> StateManager:
    with StateManager(db_path=":memory:") as state:
        yield state


class TestAcquireLease:
    def test_first_acquire_starts_at_fence_one(self, in_memory_state: StateManager) -> None:
        result = _acquire(in_memory_state, "production", "owner-a")
        assert isinstance(result, Ok)
        assert result.value == EnvironmentLease(
            environment="production", owner_id="owner-a", fence=1,
            expires_at=result.value.expires_at,
        )

    def test_second_acquire_while_unexpired_is_refused(self, in_memory_state: StateManager) -> None:
        _acquire(in_memory_state, "production", "owner-a")
        result = _acquire(in_memory_state, "production", "owner-b")
        assert isinstance(result, Err)
        assert isinstance(result.error, LeaseError)

    def test_same_owner_cannot_re_acquire_while_unexpired(
        self, in_memory_state: StateManager
    ) -> None:
        _acquire(in_memory_state, "production", "owner-a")
        result = _acquire(in_memory_state, "production", "owner-a")
        assert isinstance(result, Err)

    def test_acquire_after_expiry_takes_over_and_bumps_fence(
        self, in_memory_state: StateManager
    ) -> None:
        first = _acquire(in_memory_state, "production", "owner-a")
        assert isinstance(first, Ok)
        in_memory_state.db.execute(
            "UPDATE environment_leases SET expires_at = ? WHERE environment = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "production"),
        )
        in_memory_state.db.commit()
        second = _acquire(in_memory_state, "production", "owner-b")
        assert isinstance(second, Ok)
        assert second.value.owner_id == "owner-b"
        assert second.value.fence == first.value.fence + 1

    def test_different_environments_do_not_contend(self, in_memory_state: StateManager) -> None:
        r1 = _acquire(in_memory_state, "production", "owner-a")
        r2 = _acquire(in_memory_state, "development", "owner-b")
        assert isinstance(r1, Ok)
        assert isinstance(r2, Ok)


class TestRenewLease:
    def test_renew_extends_expiry_without_bumping_fence(
        self, in_memory_state: StateManager
    ) -> None:
        acquired = _acquire(in_memory_state, "production", "owner-a")
        assert isinstance(acquired, Ok)
        renewed = in_memory_state.renew_environment_lease(
            "production", "owner-a", acquired.value.fence, ttl_seconds=30
        )
        assert isinstance(renewed, Ok)
        assert renewed.value.fence == acquired.value.fence
        assert renewed.value.expires_at > acquired.value.expires_at

    def test_stale_generation_cannot_renew(self, in_memory_state: StateManager) -> None:
        acquired = _acquire(in_memory_state, "production", "owner-a")
        assert isinstance(acquired, Ok)
        result = in_memory_state.renew_environment_lease(
            "production", "owner-a", acquired.value.fence - 1, ttl_seconds=30
        )
        assert isinstance(result, Err)

    def test_wrong_owner_cannot_renew(self, in_memory_state: StateManager) -> None:
        acquired = _acquire(in_memory_state, "production", "owner-a")
        assert isinstance(acquired, Ok)
        result = in_memory_state.renew_environment_lease(
            "production", "owner-b", acquired.value.fence, ttl_seconds=30
        )
        assert isinstance(result, Err)

    def test_expired_owner_cannot_renew(self, in_memory_state: StateManager) -> None:
        acquired = _acquire(in_memory_state, "production", "owner-a")
        assert isinstance(acquired, Ok)
        in_memory_state.db.execute(
            "UPDATE environment_leases SET expires_at = ? WHERE environment = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "production"),
        )
        in_memory_state.db.commit()
        result = in_memory_state.renew_environment_lease(
            "production", "owner-a", acquired.value.fence, ttl_seconds=30
        )
        assert isinstance(result, Err)


class TestReleaseLease:
    def test_release_allows_immediate_reacquire_by_another_owner(
        self, in_memory_state: StateManager
    ) -> None:
        acquired = _acquire(in_memory_state, "production", "owner-a")
        assert isinstance(acquired, Ok)
        assert in_memory_state.release_environment_lease(
            "production", "owner-a", acquired.value.fence
        )
        result = _acquire(in_memory_state, "production", "owner-b")
        assert isinstance(result, Ok)
        assert result.value.fence == acquired.value.fence + 1

    def test_release_with_wrong_fence_is_a_noop(self, in_memory_state: StateManager) -> None:
        acquired = _acquire(in_memory_state, "production", "owner-a")
        assert isinstance(acquired, Ok)
        assert not in_memory_state.release_environment_lease(
            "production", "owner-a", acquired.value.fence + 1
        )


class TestReconcileAbandonedCanonicalOwners:
    def test_interrupts_only_stale_generation_scans_in_same_environment(
        self, in_memory_state: StateManager
    ) -> None:
        first = _acquire(in_memory_state, "production", "owner-a")
        assert isinstance(first, Ok)
        abandoned_id = in_memory_state.start_scan(environment="production", role="canonical_live")

        other_env_id = in_memory_state.start_scan(environment="development", role="canonical_live")

        in_memory_state.db.execute(
            "UPDATE environment_leases SET expires_at = ? WHERE environment = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "production"),
        )
        in_memory_state.db.commit()
        second = _acquire(in_memory_state, "production", "owner-b")
        assert isinstance(second, Ok)

        current_owner_scan_id = in_memory_state.start_scan(
            environment="production", role="canonical_live"
        )

        reconciled = in_memory_state.reconcile_abandoned_canonical_owners(
            "production", second.value.fence
        )

        assert reconciled == [abandoned_id]
        abandoned_row = in_memory_state.db.execute(
            "SELECT status FROM scans WHERE id = ?", (abandoned_id,)
        ).fetchone()
        assert abandoned_row["status"] == "interrupted"

        current_row = in_memory_state.db.execute(
            "SELECT status FROM scans WHERE id = ?", (current_owner_scan_id,)
        ).fetchone()
        assert current_row["status"] is None

        other_env_row = in_memory_state.db.execute(
            "SELECT status FROM scans WHERE id = ?", (other_env_id,)
        ).fetchone()
        assert other_env_row["status"] is None

    def test_completed_scans_are_never_touched(self, in_memory_state: StateManager) -> None:
        first = _acquire(in_memory_state, "production", "owner-a")
        assert isinstance(first, Ok)
        scan_id = in_memory_state.start_scan(environment="production", role="canonical_live")
        in_memory_state.complete_scan(scan_id, 0, 0, status="complete")

        in_memory_state.db.execute(
            "UPDATE environment_leases SET expires_at = ? WHERE environment = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "production"),
        )
        in_memory_state.db.commit()
        second = _acquire(in_memory_state, "production", "owner-b")
        assert isinstance(second, Ok)

        reconciled = in_memory_state.reconcile_abandoned_canonical_owners(
            "production", second.value.fence
        )
        assert reconciled == []

    def test_reconciled_scan_stays_fenced_out_of_finalization(
        self, in_memory_state: StateManager
    ) -> None:
        first = _acquire(in_memory_state, "production", "owner-a")
        assert isinstance(first, Ok)
        abandoned_id = in_memory_state.start_scan(environment="production", role="canonical_live")

        in_memory_state.db.execute(
            "UPDATE environment_leases SET expires_at = ? WHERE environment = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "production"),
        )
        in_memory_state.db.commit()
        second = _acquire(in_memory_state, "production", "owner-b")
        assert isinstance(second, Ok)
        in_memory_state.reconcile_abandoned_canonical_owners("production", second.value.fence)

        result = in_memory_state.finalize_scan_coverage(
            abandoned_id,
            environment="production",
            advance_watermark=True,
            coverage_classifier_version=1,
        )
        assert isinstance(result, Err)


class TestCutoverWatermark:
    def test_cutover_requires_current_lease(self, in_memory_state: StateManager) -> None:
        acquired = _acquire(in_memory_state, "production", "owner-a")
        assert isinstance(acquired, Ok)
        result = in_memory_state.cutover_watermark(
            environment="production",
            owner_id="owner-a",
            fence=acquired.value.fence + 1,
            expected_old_watermark=None,
            accepted_new_watermark=datetime.now(UTC),
        )
        assert isinstance(result, Err)

    def test_cutover_refuses_stale_expected_old(self, in_memory_state: StateManager) -> None:
        acquired = _acquire(in_memory_state, "production", "owner-a")
        assert isinstance(acquired, Ok)
        result = in_memory_state.cutover_watermark(
            environment="production",
            owner_id="owner-a",
            fence=acquired.value.fence,
            expected_old_watermark=datetime.now(UTC),
            accepted_new_watermark=datetime.now(UTC),
        )
        assert isinstance(result, Err)

    def test_successful_cutover_is_immediately_the_expected_cursor(
        self, in_memory_state: StateManager
    ) -> None:
        acquired = _acquire(in_memory_state, "production", "owner-a")
        assert isinstance(acquired, Ok)
        new_watermark = datetime.now(UTC)
        result = in_memory_state.cutover_watermark(
            environment="production",
            owner_id="owner-a",
            fence=acquired.value.fence,
            expected_old_watermark=None,
            accepted_new_watermark=new_watermark,
        )
        assert isinstance(result, Ok)
        assert in_memory_state.get_last_scan_timestamp(environment="production") == new_watermark


class TestProbeAndRecoveryAudit:
    def test_probe_run_lifecycle(self, in_memory_state: StateManager) -> None:
        probe_id = in_memory_state.start_probe_run("production", source_count=3)
        in_memory_state.complete_probe_run(
            probe_id, passed=True, page_count=5, detail_json='{"ok": true}'
        )
        run = in_memory_state.get_probe_run(probe_id)
        assert run is not None
        assert run.passed is True
        assert run.page_count == 5

    def test_get_latest_passed_probe_respects_max_age(self, in_memory_state: StateManager) -> None:
        probe_id = in_memory_state.start_probe_run("production", source_count=1)
        in_memory_state.complete_probe_run(probe_id, passed=True, page_count=1, detail_json="{}")
        assert in_memory_state.get_latest_passed_probe(
            "production", max_age_seconds=3600
        ) is not None
        assert in_memory_state.get_latest_passed_probe(
            "production", max_age_seconds=0
        ) is None

    def test_get_latest_passed_probe_ignores_failed_runs(
        self, in_memory_state: StateManager
    ) -> None:
        probe_id = in_memory_state.start_probe_run("production", source_count=1)
        in_memory_state.complete_probe_run(probe_id, passed=False, page_count=1, detail_json="{}")
        assert in_memory_state.get_latest_passed_probe(
            "production", max_age_seconds=3600
        ) is None

    def test_recovery_operation_is_recorded_and_immutable(
        self, in_memory_state: StateManager
    ) -> None:
        audit_id = in_memory_state.record_recovery_operation(
            environment="production",
            operation="backfill",
            operator="steve",
            rationale="page ceiling reached last night",
            outcome="accepted",
        )
        row = in_memory_state.db.execute(
            "SELECT outcome FROM recovery_operations WHERE id = ?", (audit_id,)
        ).fetchone()
        assert row["outcome"] == "accepted"
        with pytest.raises(sqlite3.Error):
            in_memory_state.db.execute(
                "UPDATE recovery_operations SET outcome = 'refused' WHERE id = ?", (audit_id,)
            )
            in_memory_state.db.commit()

    def test_refused_operation_is_also_recorded(self, in_memory_state: StateManager) -> None:
        audit_id = in_memory_state.record_recovery_operation(
            environment="production",
            operation="cutover",
            operator="steve",
            rationale="tried without a probe",
            outcome="refused",
            detail="no recent passed probe",
        )
        row = in_memory_state.db.execute(
            "SELECT outcome, detail FROM recovery_operations WHERE id = ?", (audit_id,)
        ).fetchone()
        assert row["outcome"] == "refused"
        assert row["detail"] == "no recent passed probe"
