"""Tests for the environment lease primitive: acquire/renew/release/takeover,
startup reconciliation of abandoned canonical owners, cutover, and the
probe/recovery-audit evidence trail."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta

import pytest

from scout.result import Err, Ok
from scout.scanning.lease import acquire_lease, generate_owner_id, reconcile_abandoned_owners
from scout.storage.scans import EnvironmentLease, LeaseError
from scout.storage.state import StateManager


@pytest.fixture
def file_backed_state() -> StateManager:
    """acquire_lease's heartbeat handle opens its own connection to the
    same db_path — ":memory:" is per-connection isolated, so lease-handle
    tests need a real temp file to exercise the dedicated heartbeat
    connection against the same durable state."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "lease_test.db")
        with StateManager(db_path=db_path) as state:
            yield state, db_path


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


def _passed_probe(
    state: StateManager,
    fence: int,
    environment: str = "production",
    window_hours: float = 6.0,
) -> int:
    probe_id = state.start_probe_run(
        environment, source_count=1, window_hours=window_hours, limits_json="{}",
    )
    state.complete_probe_run(
        probe_id, environment=environment, owner_id="owner-a", fence=fence,
        passed=True, source_count=1, page_count=1, detail_json="{}",
    )
    return probe_id


def _cutover(state: StateManager, fence: int, **overrides):
    kwargs = dict(
        environment="production", owner_id="owner-a", fence=fence,
        operator="steve", rationale="accept the gap", policy="policy-v1",
        source_evidence="status page", expected_old_watermark=None,
        accepted_new_watermark=datetime.now(UTC) - timedelta(minutes=1),
        probe_max_age_seconds=3600, required_probe_limits={},
    )
    kwargs.update(overrides)
    return state.cutover_watermark(**kwargs)


def _audit_rows(state: StateManager) -> list[sqlite3.Row]:
    return state.db.execute(
        "SELECT operation, outcome, detail, probe_run_id FROM recovery_operations ORDER BY id"
    ).fetchall()


class TestCutoverWatermark:
    def test_refuses_without_the_current_lease_and_audits_it(
        self, in_memory_state: StateManager
    ) -> None:
        acquired = _acquire(in_memory_state, "production", "owner-a")
        assert isinstance(acquired, Ok)
        _passed_probe(in_memory_state, acquired.value.fence)
        result = _cutover(in_memory_state, acquired.value.fence + 1)
        assert isinstance(result, Err)
        assert result.error.reason == "lock_not_held"
        rows = _audit_rows(in_memory_state)
        assert [r["outcome"] for r in rows] == ["refused"]

    def test_refuses_blank_metadata(self, in_memory_state: StateManager) -> None:
        acquired = _acquire(in_memory_state, "production", "owner-a")
        assert isinstance(acquired, Ok)
        _passed_probe(in_memory_state, acquired.value.fence)
        result = _cutover(in_memory_state, acquired.value.fence, rationale="   ")
        assert isinstance(result, Err)
        assert result.error.reason == "missing_metadata"

    def test_refuses_naive_or_future_accepted_new(self, in_memory_state: StateManager) -> None:
        acquired = _acquire(in_memory_state, "production", "owner-a")
        assert isinstance(acquired, Ok)
        _passed_probe(in_memory_state, acquired.value.fence)
        naive = _cutover(
            in_memory_state, acquired.value.fence,
            accepted_new_watermark=datetime.now().replace(tzinfo=None),
        )
        assert isinstance(naive, Err) and naive.error.reason == "invalid_accepted_new"
        future = _cutover(
            in_memory_state, acquired.value.fence,
            accepted_new_watermark=datetime.now(UTC) + timedelta(hours=1),
        )
        assert isinstance(future, Err) and future.error.reason == "invalid_accepted_new"

    def test_refuses_without_a_recent_six_hour_probe(self, in_memory_state: StateManager) -> None:
        acquired = _acquire(in_memory_state, "production", "owner-a")
        assert isinstance(acquired, Ok)
        _passed_probe(in_memory_state, acquired.value.fence, window_hours=1.0)
        result = _cutover(in_memory_state, acquired.value.fence)
        assert isinstance(result, Err)
        assert result.error.reason == "missing_probe"

    def test_refuses_probe_with_nonproduction_limits(
        self, in_memory_state: StateManager
    ) -> None:
        acquired = _acquire(in_memory_state, "production", "owner-a")
        assert isinstance(acquired, Ok)
        probe_id = in_memory_state.start_probe_run(
            "production", source_count=1, window_hours=6.0,
            limits_json='{"discord_max_pages": 999}',
        )
        completion = in_memory_state.complete_probe_run(
            probe_id, environment="production", owner_id="owner-a",
            fence=acquired.value.fence, passed=True, source_count=1,
            page_count=1, detail_json="{}",
        )
        assert isinstance(completion, Ok)

        result = _cutover(
            in_memory_state, acquired.value.fence,
            required_probe_limits={"discord_max_pages": 5},
        )
        assert isinstance(result, Err)
        assert result.error.reason == "missing_probe"

    def test_refuses_stale_expected_old_and_moves_nothing(
        self, in_memory_state: StateManager
    ) -> None:
        acquired = _acquire(in_memory_state, "production", "owner-a")
        assert isinstance(acquired, Ok)
        _passed_probe(in_memory_state, acquired.value.fence)
        result = _cutover(
            in_memory_state, acquired.value.fence,
            expected_old_watermark=datetime(2020, 1, 1, tzinfo=UTC),
        )
        assert isinstance(result, Err)
        assert result.error.reason == "stale_expected_old"
        assert in_memory_state.get_last_scan_timestamp(environment="production") is None

    def test_success_is_immediately_the_cursor_with_probe_linked_audit(
        self, in_memory_state: StateManager
    ) -> None:
        acquired = _acquire(in_memory_state, "production", "owner-a")
        assert isinstance(acquired, Ok)
        probe_id = _passed_probe(in_memory_state, acquired.value.fence)
        accepted = datetime.now(UTC) - timedelta(minutes=1)
        result = _cutover(in_memory_state, acquired.value.fence, accepted_new_watermark=accepted)
        assert isinstance(result, Ok)
        assert result.value.probe_run_id == probe_id
        assert in_memory_state.get_last_scan_timestamp(environment="production") == accepted
        rows = _audit_rows(in_memory_state)
        assert [(r["operation"], r["outcome"], r["probe_run_id"]) for r in rows] == [
            ("cutover", "accepted", probe_id)
        ]

    def test_accepted_new_must_move_forward_past_expected_old(
        self, in_memory_state: StateManager
    ) -> None:
        acquired = _acquire(in_memory_state, "production", "owner-a")
        assert isinstance(acquired, Ok)
        _passed_probe(in_memory_state, acquired.value.fence)
        first = _cutover(
            in_memory_state, acquired.value.fence,
            accepted_new_watermark=datetime.now(UTC) - timedelta(hours=2),
        )
        assert isinstance(first, Ok)
        backwards = _cutover(
            in_memory_state, acquired.value.fence,
            expected_old_watermark=first.value.accepted_new_watermark,
            accepted_new_watermark=first.value.accepted_new_watermark - timedelta(hours=1),
        )
        assert isinstance(backwards, Err)
        assert backwards.error.reason == "invalid_accepted_new"


class TestLeaseBoundOwnerCreationAndAdvancement:
    def test_canonical_owner_creation_requires_the_held_lease(
        self, in_memory_state: StateManager
    ) -> None:
        acquired = _acquire(in_memory_state, "production", "owner-a")
        assert isinstance(acquired, Ok)
        wrong_owner = in_memory_state.start_canonical_owner_scan(
            environment="production", owner_id="owner-b", fence=acquired.value.fence,
            fetch_started_at=datetime.now(UTC),
        )
        assert isinstance(wrong_owner, Err)
        stale_fence = in_memory_state.start_canonical_owner_scan(
            environment="production", owner_id="owner-a", fence=acquired.value.fence - 1,
            fetch_started_at=datetime.now(UTC),
        )
        assert isinstance(stale_fence, Err)
        ok = in_memory_state.start_canonical_owner_scan(
            environment="production", owner_id="owner-a", fence=acquired.value.fence,
            fetch_started_at=datetime.now(UTC),
        )
        assert isinstance(ok, Ok)
        row = in_memory_state.db.execute(
            "SELECT role, lease_fence FROM scans WHERE id = ?", (ok.value,)
        ).fetchone()
        assert (row["role"], row["lease_fence"]) == ("canonical_live", acquired.value.fence)

    def test_expired_owner_cannot_create_a_canonical_owner(
        self, in_memory_state: StateManager
    ) -> None:
        acquired = _acquire(in_memory_state, "production", "owner-a")
        assert isinstance(acquired, Ok)
        in_memory_state.db.execute(
            "UPDATE environment_leases SET expires_at = ? WHERE environment = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "production"),
        )
        in_memory_state.db.commit()
        result = in_memory_state.start_canonical_owner_scan(
            environment="production", owner_id="owner-a", fence=acquired.value.fence,
            fetch_started_at=datetime.now(UTC),
        )
        assert isinstance(result, Err)

    def _owned_complete_scan(self, state: StateManager) -> tuple[int, int]:
        acquired = _acquire(state, "production", "owner-a")
        assert isinstance(acquired, Ok)
        created = state.start_canonical_owner_scan(
            environment="production", owner_id="owner-a", fence=acquired.value.fence,
            fetch_started_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        assert isinstance(created, Ok)
        state.complete_scan(created.value, 0, 0, status="complete")
        return created.value, acquired.value.fence

    def test_advancement_refused_for_a_matching_fence_whose_lease_expired(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, _fence = self._owned_complete_scan(in_memory_state)
        in_memory_state.db.execute(
            "UPDATE environment_leases SET expires_at = ? WHERE environment = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "production"),
        )
        in_memory_state.db.commit()
        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", advance_watermark=True, owner_id="owner-a",
            coverage_classifier_version=1,
        )
        assert isinstance(result, Err)
        assert "lease" in result.error.detail

    def test_advancement_refused_without_an_owner(self, in_memory_state: StateManager) -> None:
        scan_id, _fence = self._owned_complete_scan(in_memory_state)
        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", advance_watermark=True,
            coverage_classifier_version=1,
        )
        assert isinstance(result, Err)

    def test_coverage_read_without_advancement_needs_no_owner(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, _fence = self._owned_complete_scan(in_memory_state)
        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", advance_watermark=False,
            coverage_classifier_version=1,
        )
        assert isinstance(result, Ok)
        assert result.value.watermark_advanced is False

    def test_finalization_cannot_omit_a_persisted_required_source(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, _fence = self._owned_complete_scan(in_memory_state)
        in_memory_state.ensure_source_checkpoint(
            "discord:channel:1", platform="discord",
            source_kind="channel", provider_key="1",
        )

        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", advance_watermark=True,
            owner_id="owner-a", coverage_classifier_version=1,
        )
        assert isinstance(result, Err)
        assert "persisted active requirements" in result.error.detail

    def test_covered_source_checkpoints_advance_atomically_with_the_watermark(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, _fence = self._owned_complete_scan(in_memory_state)
        for key in ("discord:channel:1", "discord:channel:2"):
            in_memory_state.ensure_source_checkpoint(
                key, platform="discord", source_kind="channel", provider_key=key[-1],
            )
        in_memory_state.retire_source_checkpoint("discord:channel:2")
        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", advance_watermark=True, owner_id="owner-a",
            required_source_keys=frozenset({"discord:channel:1"}),
            covered_source_keys=frozenset({"discord:channel:1", "discord:channel:2"}),
            coverage_classifier_version=1,
        )
        assert isinstance(result, Ok)
        assert result.value.advanced_source_keys == ("discord:channel:1",)
        active = in_memory_state.get_source_checkpoint("discord:channel:1")
        retired = in_memory_state.get_source_checkpoint("discord:channel:2")
        assert active is not None and active.checkpoint_at == result.value.safe_watermark_at
        assert retired is not None and retired.checkpoint_at is None

    def test_blocked_outcome_never_advances_source_checkpoints(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, _fence = self._owned_complete_scan(in_memory_state)
        in_memory_state.ensure_source_checkpoint(
            "discord:channel:1", platform="discord", source_kind="channel", provider_key="1",
        )
        in_memory_state.save_fetch_failure(
            scan_id, platform="discord", kind="page_ceiling", message="ceiling",
            operation_phase="fetch", blocks_watermark_advance=True,
        )
        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", advance_watermark=True, owner_id="owner-a",
            required_source_keys=frozenset({"discord:channel:1"}),
            covered_source_keys=frozenset(),
            coverage_classifier_version=1,
        )
        assert isinstance(result, Ok)
        assert result.value.coverage_outcome == "blocked"
        checkpoint = in_memory_state.get_source_checkpoint("discord:channel:1")
        assert checkpoint is not None and checkpoint.checkpoint_at is None

    def test_missing_required_source_without_evidence_is_refused(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id, _fence = self._owned_complete_scan(in_memory_state)
        in_memory_state.ensure_source_checkpoint(
            "discord:channel:1", platform="discord",
            source_kind="channel", provider_key="1",
        )
        result = in_memory_state.finalize_scan_coverage(
            scan_id, environment="production", advance_watermark=True, owner_id="owner-a",
            required_source_keys=frozenset({"discord:channel:1"}),
            covered_source_keys=frozenset(),
            coverage_classifier_version=1,
        )
        assert isinstance(result, Err)
        assert "required-source" in result.error.detail


class TestProbeAndRecoveryAudit:
    def test_probe_run_lifecycle(self, in_memory_state: StateManager) -> None:
        acquired = _acquire(in_memory_state, "production", "probe-owner")
        assert isinstance(acquired, Ok)
        probe_id = in_memory_state.start_probe_run(
            "production", window_hours=6.0, limits_json="{}", source_count=3,
        )
        in_memory_state.complete_probe_run(
            probe_id, environment="production", owner_id="probe-owner",
            fence=acquired.value.fence, passed=True, source_count=3,
            page_count=5, detail_json='{"ok": true}',
        )
        run = in_memory_state.get_probe_run(probe_id)
        assert run is not None
        assert run.passed is True
        assert run.page_count == 5

    def test_get_latest_passed_probe_respects_max_age(self, in_memory_state: StateManager) -> None:
        acquired = _acquire(in_memory_state, "production", "probe-owner")
        assert isinstance(acquired, Ok)
        probe_id = in_memory_state.start_probe_run(
            "production", window_hours=6.0, limits_json="{}", source_count=1,
        )
        in_memory_state.complete_probe_run(
            probe_id, environment="production", owner_id="probe-owner",
            fence=acquired.value.fence, passed=True, source_count=1,
            page_count=1, detail_json="{}",
        )
        assert in_memory_state.get_latest_passed_probe(
            "production", max_age_seconds=3600
        ) is not None
        assert in_memory_state.get_latest_passed_probe(
            "production", max_age_seconds=0
        ) is None

    def test_get_latest_passed_probe_ignores_failed_runs(
        self, in_memory_state: StateManager
    ) -> None:
        acquired = _acquire(in_memory_state, "production", "probe-owner")
        assert isinstance(acquired, Ok)
        probe_id = in_memory_state.start_probe_run(
            "production", window_hours=6.0, limits_json="{}", source_count=1,
        )
        in_memory_state.complete_probe_run(
            probe_id, environment="production", owner_id="probe-owner",
            fence=acquired.value.fence, passed=False, source_count=1,
            page_count=1, detail_json="{}",
        )
        assert in_memory_state.get_latest_passed_probe(
            "production", max_age_seconds=3600
        ) is None

    def test_stale_owner_cannot_complete_probe_run(
        self, in_memory_state: StateManager
    ) -> None:
        acquired = _acquire(in_memory_state, "production", "owner-a")
        assert isinstance(acquired, Ok)
        probe_id = in_memory_state.start_probe_run(
            "production", window_hours=6.0, limits_json="{}", source_count=1,
        )
        in_memory_state.db.execute(
            "UPDATE environment_leases SET expires_at = ? WHERE environment = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "production"),
        )
        in_memory_state.db.commit()

        completion = in_memory_state.complete_probe_run(
            probe_id, environment="production", owner_id="owner-a",
            fence=acquired.value.fence, passed=True, source_count=1,
            page_count=1, detail_json="{}",
        )
        assert isinstance(completion, Err)
        assert in_memory_state.get_probe_run(probe_id).completed_at is None

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


class TestEnvironmentLeaseHandle:
    async def test_heartbeat_renews_before_expiry(self, file_backed_state) -> None:
        state, db_path = file_backed_state
        owner_id = generate_owner_id()
        result = acquire_lease(
            state, environment="production", owner_id=owner_id,
            heartbeat_state=StateManager(db_path=db_path, init_schema=False),
            ttl_seconds=0.6, heartbeat_interval_seconds=0.2,
        )
        assert isinstance(result, Ok)
        handle = result.value
        handle.start_heartbeat()
        try:
            import asyncio

            await asyncio.sleep(1.0)
            assert not handle.lost
            assert handle.fence == 1
        finally:
            await handle.stop(release=True)

    async def test_stop_without_release_leaves_lease_held(self, file_backed_state) -> None:
        state, db_path = file_backed_state
        owner_id = generate_owner_id()
        result = acquire_lease(
            state, environment="production", owner_id=owner_id,
            heartbeat_state=StateManager(db_path=db_path, init_schema=False),
            ttl_seconds=30, heartbeat_interval_seconds=10,
        )
        assert isinstance(result, Ok)
        handle = result.value
        await handle.stop(release=False)

        other = state.acquire_environment_lease("production", "someone-else", ttl_seconds=30)
        assert isinstance(other, Err)

    async def test_stop_with_release_allows_reacquire(self, file_backed_state) -> None:
        state, db_path = file_backed_state
        owner_id = generate_owner_id()
        result = acquire_lease(
            state, environment="production", owner_id=owner_id,
            heartbeat_state=StateManager(db_path=db_path, init_schema=False),
            ttl_seconds=30, heartbeat_interval_seconds=10,
        )
        assert isinstance(result, Ok)
        handle = result.value
        await handle.stop(release=True)

        other = state.acquire_environment_lease("production", "someone-else", ttl_seconds=30)
        assert isinstance(other, Ok)

    async def test_reconcile_abandoned_owners_helper_delegates(self, file_backed_state) -> None:
        state, db_path = file_backed_state
        first_owner = generate_owner_id()
        first = acquire_lease(
            state, environment="production", owner_id=first_owner,
            heartbeat_state=StateManager(db_path=db_path, init_schema=False),
            ttl_seconds=30, heartbeat_interval_seconds=10,
        )
        assert isinstance(first, Ok)
        abandoned_id = state.start_scan(environment="production", role="canonical_live")
        await first.value.stop(release=False)

        state.db.execute(
            "UPDATE environment_leases SET expires_at = ? WHERE environment = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "production"),
        )
        state.db.commit()

        second_owner = generate_owner_id()
        second = acquire_lease(
            state, environment="production", owner_id=second_owner,
            heartbeat_state=StateManager(db_path=db_path, init_schema=False),
            ttl_seconds=30, heartbeat_interval_seconds=10,
        )
        assert isinstance(second, Ok)
        reconciled = reconcile_abandoned_owners(state, second.value)
        assert reconciled == [abandoned_id]
        await second.value.stop(release=True)


class TestHeartbeatFailureIsLeaseLoss:
    async def test_renewal_exception_marks_the_lease_lost(self, file_backed_state) -> None:
        state, db_path = file_backed_state
        heartbeat_state = StateManager(db_path=db_path, init_schema=False)
        result = acquire_lease(
            state, environment="production", owner_id=generate_owner_id(),
            heartbeat_state=heartbeat_state, ttl_seconds=30, heartbeat_interval_seconds=0.01,
        )
        assert isinstance(result, Ok)
        handle = result.value

        def explode(*_a: object, **_k: object):
            raise sqlite3.OperationalError("disk I/O error")

        heartbeat_state.renew_environment_lease = explode  # type: ignore[method-assign]
        lost_callbacks: list[bool] = []
        handle.start_heartbeat(on_lost=lambda: lost_callbacks.append(True))
        import asyncio

        for _ in range(50):
            if handle.lost:
                break
            await asyncio.sleep(0.01)
        assert handle.lost
        assert lost_callbacks == [True]
        with pytest.raises(Exception, match="lost"):
            handle.check()
        await handle.stop(release=False)
        heartbeat_state.close()


class TestRecoveryOperationsCannotBeReplaced:
    def test_insert_or_replace_on_an_existing_id_is_refused(
        self, in_memory_state: StateManager
    ) -> None:
        audit_id = in_memory_state.record_recovery_operation(
            environment="production", operation="backfill", operator="steve",
            rationale="r", outcome="accepted",
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            in_memory_state.db.execute(
                "INSERT OR REPLACE INTO recovery_operations "
                "(id, environment, operation, operator, rationale, outcome, created_at) "
                "VALUES (?, 'production', 'backfill', 'mallory', 'rewritten', 'refused', 'now')",
                (audit_id,),
            )
        row = in_memory_state.db.execute(
            "SELECT operator FROM recovery_operations WHERE id = ?", (audit_id,)
        ).fetchone()
        assert row["operator"] == "steve"
