"""Behavioral tests for the watermark recovery CLI: probe, stale-check,
bounded backfill, and controlled cutover, plus their audited recovery-lock
and exit-code contracts."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from argparse import Namespace
from datetime import UTC, datetime, timedelta

import pytest

import scout.cli.watermark as watermark_cli
from scout.config import Message
from scout.errors import SourceFetchOutcome
from scout.result import Err
from scout.scanning.runner import PlatformsFetch
from scout.storage.scans import LeaseError
from scout.storage.state import StateManager


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "watermark_recovery.db")
    with StateManager(db_path=path):
        pass
    return path


def _stale_check_args(db_path: str, *, environment: str = "production", hours=None) -> Namespace:
    return Namespace(
        watermark_command="stale-check", environment=environment, db_path=db_path, hours=hours,
    )


def _probe_args(db_path: str, *, environment: str = "production", hours: float = 6.0) -> Namespace:
    return Namespace(
        watermark_command="probe", environment=environment, db_path=db_path, hours=hours,
    )


def _backfill_args(
    db_path: str, *, environment: str = "production", operator: str = "steve",
    rationale: str = "recover from gap", hours: float = 24.0,
) -> Namespace:
    return Namespace(
        watermark_command="backfill", environment=environment, db_path=db_path,
        operator=operator, rationale=rationale, hours=hours,
    )


def _cutover_args(
    db_path: str, *, environment: str = "production", operator: str = "steve",
    rationale: str = "accept gap", policy: str = "policy-v1",
    source_evidence: str = "evidence", accepted_new: str, expected_old: str | None = None,
) -> Namespace:
    return Namespace(
        watermark_command="cutover", environment=environment, db_path=db_path,
        operator=operator, rationale=rationale, policy=policy,
        source_evidence=source_evidence, accepted_new=accepted_new, expected_old=expected_old,
    )


class TestStaleCheck:
    def test_no_watermark_is_stale(self, db_path: str, capsys: pytest.CaptureFixture) -> None:
        exit_code = watermark_cli.run_watermark(_stale_check_args(db_path))
        assert exit_code == watermark_cli.EXIT_STALE_WATERMARK
        payload = json.loads(capsys.readouterr().out)
        assert payload["stale"] is True
        assert payload["watermark"] is None

    def test_recent_watermark_is_not_stale(
        self, db_path: str, capsys: pytest.CaptureFixture
    ) -> None:
        with StateManager(db_path=db_path, init_schema=False) as state:
            accepted = datetime.now(UTC) - timedelta(minutes=1)
            acquired = state.acquire_environment_lease("production", "owner", ttl_seconds=30)
            assert acquired.value is not None
            probe_id = state.start_probe_run(
                "production", source_count=1, window_hours=6.0, limits_json="{}",
            )
            state.complete_probe_run(
                probe_id, environment="production", owner_id="owner",
                fence=acquired.value.fence, passed=True, source_count=1,
                page_count=1, detail_json="{}",
            )
            state.cutover_watermark(
                environment="production", owner_id="owner", fence=acquired.value.fence,
                operator="steve", rationale="seed", policy="p", source_evidence="e",
                expected_old_watermark=None, accepted_new_watermark=accepted,
                probe_max_age_seconds=3600, required_probe_limits={},
            )

        exit_code = watermark_cli.run_watermark(_stale_check_args(db_path))
        assert exit_code == watermark_cli.EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["stale"] is False

    def test_environment_scoping_never_reads_another_environment(
        self, db_path: str, capsys: pytest.CaptureFixture
    ) -> None:
        with StateManager(db_path=db_path, init_schema=False) as state:
            acquired = state.acquire_environment_lease("development", "owner", ttl_seconds=30)
            assert acquired.value is not None
            probe_id = state.start_probe_run(
                "development", source_count=1, window_hours=6.0, limits_json="{}",
            )
            state.complete_probe_run(
                probe_id, environment="development", owner_id="owner",
                fence=acquired.value.fence, passed=True, source_count=1,
                page_count=1, detail_json="{}",
            )
            state.cutover_watermark(
                environment="development", owner_id="owner", fence=acquired.value.fence,
                operator="steve", rationale="seed", policy="p", source_evidence="e",
                expected_old_watermark=None,
                accepted_new_watermark=datetime.now(UTC) - timedelta(minutes=1),
                probe_max_age_seconds=3600, required_probe_limits={},
            )

        exit_code = watermark_cli.run_watermark(_stale_check_args(db_path))
        assert exit_code == watermark_cli.EXIT_STALE_WATERMARK


class TestProbe:
    def test_probe_with_no_platforms_configured_passes_and_records_evidence(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setattr(
            watermark_cli, "build_platform_scanners", lambda **_kw: (None, None, None)
        )

        async def fake_fetch(*_args: object, **_kwargs: object):
            return PlatformsFetch([], [])

        monkeypatch.setattr(watermark_cli, "fetch_messages", fake_fetch)

        exit_code = watermark_cli.run_watermark(_probe_args(db_path))
        assert exit_code == watermark_cli.EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["probe_run_id"] == 1

        with StateManager(db_path=db_path, init_schema=False) as state:
            run = state.get_probe_run(1)
            assert run is not None
            assert run.passed is True

    def test_probe_records_actual_source_count_on_a_fresh_database(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            watermark_cli, "build_platform_scanners", lambda **_kw: (None, None, None)
        )
        outcome = SourceFetchOutcome(
            source_key="discord:channel:1", platform="discord",
            source_kind="channel", provider_key="1", page_count=1,
            termination="exhausted", message_count=0,
        )

        async def fake_fetch(*_args: object, **_kwargs: object):
            return PlatformsFetch([], [], (outcome,))

        monkeypatch.setattr(watermark_cli, "fetch_messages", fake_fetch)
        assert watermark_cli.run_watermark(_probe_args(db_path)) == watermark_cli.EXIT_OK

        with StateManager(db_path=db_path, init_schema=False) as state:
            run = state.get_probe_run(1)
            assert run is not None
            assert run.source_count == 1

    def test_probe_fetch_exception_is_completed_as_failed(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            watermark_cli, "build_platform_scanners", lambda **_kw: (None, None, None)
        )

        async def fail_fetch(*_args: object, **_kwargs: object):
            raise RuntimeError("provider unavailable")

        monkeypatch.setattr(watermark_cli, "fetch_messages", fail_fetch)
        assert (
            watermark_cli.run_watermark(_probe_args(db_path))
            == watermark_cli.EXIT_SOURCE_OR_PROBE_FAILURE
        )

        with StateManager(db_path=db_path, init_schema=False) as state:
            run = state.get_probe_run(1)
            assert run is not None
            assert run.completed_at is not None
            assert run.passed is False
            assert "provider unavailable" in run.detail_json

    def test_probe_cancellation_records_evidence_before_reraising(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            watermark_cli, "build_platform_scanners", lambda **_kw: (None, None, None)
        )

        async def cancel_fetch(*_args: object, **_kwargs: object):
            raise asyncio.CancelledError("operator cancelled")

        monkeypatch.setattr(watermark_cli, "fetch_messages", cancel_fetch)
        with pytest.raises(asyncio.CancelledError):
            watermark_cli.run_watermark(_probe_args(db_path))

        with StateManager(db_path=db_path, init_schema=False) as state:
            run = state.get_probe_run(1)
            assert run is not None
            assert run.completed_at is not None
            assert run.passed is False
            assert "cancelled" in run.detail_json

    def test_probe_never_touches_source_checkpoints(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            watermark_cli, "build_platform_scanners", lambda **_kw: (None, None, None)
        )

        async def fake_fetch(*_args: object, **_kwargs: object):
            return PlatformsFetch([], [])

        monkeypatch.setattr(watermark_cli, "fetch_messages", fake_fetch)

        with StateManager(db_path=db_path, init_schema=False) as state:
            state.ensure_source_checkpoint(
                "discord:channel:1", platform="discord", source_kind="channel",
                provider_key="1",
            )

        watermark_cli.run_watermark(_probe_args(db_path))

        with StateManager(db_path=db_path, init_schema=False) as state:
            checkpoint = state.get_source_checkpoint("discord:channel:1")
            assert checkpoint is not None
            assert checkpoint.checkpoint_at is None

    def test_probe_releases_lease_on_completion(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            watermark_cli, "build_platform_scanners", lambda **_kw: (None, None, None)
        )

        async def fake_fetch(*_args: object, **_kwargs: object):
            return PlatformsFetch([], [])

        monkeypatch.setattr(watermark_cli, "fetch_messages", fake_fetch)
        watermark_cli.run_watermark(_probe_args(db_path))

        with StateManager(db_path=db_path, init_schema=False) as state:
            other = state.acquire_environment_lease("production", "someone-else", ttl_seconds=30)
        from scout.result import Ok

        assert isinstance(other, Ok)


class TestBackfill:
    def test_backfill_advances_watermark_on_clean_fetch(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setattr(
            watermark_cli, "build_platform_scanners", lambda **_kw: (None, None, None)
        )

        async def fake_fetch(*_args: object, **_kwargs: object):
            return PlatformsFetch([], [])

        monkeypatch.setattr(watermark_cli, "fetch_messages", fake_fetch)

        exit_code = watermark_cli.run_watermark(_backfill_args(db_path))
        assert exit_code == watermark_cli.EXIT_OK

        with StateManager(db_path=db_path, init_schema=False) as state:
            assert state.get_last_scan_timestamp(environment="production") is not None

    def test_backfill_records_an_accepted_audit_row(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            watermark_cli, "build_platform_scanners", lambda **_kw: (None, None, None)
        )

        async def fake_fetch(*_args: object, **_kwargs: object):
            return PlatformsFetch([], [])

        monkeypatch.setattr(watermark_cli, "fetch_messages", fake_fetch)
        watermark_cli.run_watermark(_backfill_args(db_path, rationale="filling a known gap"))

        with StateManager(db_path=db_path, init_schema=False) as state:
            row = state.db.execute(
                "SELECT operation, operator, rationale, outcome FROM recovery_operations"
            ).fetchone()
            assert row["operation"] == "backfill"
            assert row["outcome"] == "accepted"
            assert row["rationale"] == "filling a known gap"

    def test_backfill_refuses_lock_contention_and_audits_the_refusal(
        self, db_path: str,
    ) -> None:
        with StateManager(db_path=db_path, init_schema=False) as state:
            held = state.acquire_environment_lease("production", "another-worker", ttl_seconds=60)
            assert held.value is not None

            exit_code = watermark_cli.run_watermark(_backfill_args(db_path))
            assert exit_code == watermark_cli.EXIT_LOCK_CONTENTION

            row = state.db.execute(
                "SELECT operation, outcome FROM recovery_operations"
            ).fetchone()
            assert row["operation"] == "backfill"
            assert row["outcome"] == "refused"

    def test_backfill_does_not_persist_recovery_data_after_lease_loss(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            watermark_cli, "build_platform_scanners", lambda **_kw: (None, None, None)
        )
        message = Message(
            platform="discord", platform_id="stale", channel_name="general",
            channel_id="1", author_name="alice", author_id="a1",
            content="stale worker data", created_at=datetime.now(UTC),
            url="https://example.test/stale",
        )

        async def fake_fetch(*_args: object, **_kwargs: object):
            return PlatformsFetch([message], [])

        def reject_lease(
            _state: StateManager, environment: str, _owner_id: str, _fence: int
        ) -> Err[LeaseError]:
            return Err(LeaseError(
                operation="validate_environment_lease",
                environment=environment,
                detail="lease taken over",
            ))

        monkeypatch.setattr(watermark_cli, "fetch_messages", fake_fetch)
        monkeypatch.setattr(StateManager, "validate_environment_lease", reject_lease)

        with pytest.raises(watermark_cli.lease_lifecycle.LeaseLostError):
            watermark_cli.run_watermark(_backfill_args(db_path))

        with StateManager(db_path=db_path, init_schema=False) as state:
            assert state.db.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
            assert state.db.execute(
                "SELECT COUNT(*) FROM scan_fetch_failures"
            ).fetchone()[0] == 0
            scan = state.db.execute("SELECT status FROM scans").fetchone()
            assert scan["status"] is None


class TestCutover:
    def test_cutover_without_a_recent_probe_is_refused(
        self, db_path: str, capsys: pytest.CaptureFixture,
    ) -> None:
        accepted = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        exit_code = watermark_cli.run_watermark(_cutover_args(db_path, accepted_new=accepted))
        assert exit_code == watermark_cli.EXIT_SOURCE_OR_PROBE_FAILURE
        payload = json.loads(capsys.readouterr().out)
        assert payload["reason"] == "missing_probe"

        with StateManager(db_path=db_path, init_schema=False) as state:
            row = state.db.execute(
                "SELECT operation, outcome FROM recovery_operations"
            ).fetchone()
            assert row["operation"] == "cutover"
            assert row["outcome"] == "refused"

    def test_cutover_succeeds_after_a_recent_passed_probe(
        self, db_path: str, capsys: pytest.CaptureFixture,
    ) -> None:
        with StateManager(db_path=db_path, init_schema=False) as state:
            lease = state.acquire_environment_lease(
                "production", "probe-seed", ttl_seconds=300
            )
            assert lease.value is not None
            probe_id = state.start_probe_run(
                "production", source_count=1, window_hours=6.0,
                limits_json=json.dumps(watermark_cli._production_limits(), sort_keys=True),
            )
            state.complete_probe_run(
                probe_id, environment="production", owner_id="probe-seed",
                fence=lease.value.fence, passed=True, source_count=1,
                page_count=1, detail_json="{}",
            )
            state.release_environment_lease(
                "production", "probe-seed", lease.value.fence
            )

        accepted = datetime.now(UTC) - timedelta(minutes=1)
        exit_code = watermark_cli.run_watermark(
            _cutover_args(db_path, accepted_new=accepted.isoformat())
        )
        assert exit_code == watermark_cli.EXIT_OK

        with StateManager(db_path=db_path, init_schema=False) as state:
            watermark = state.get_last_scan_timestamp(environment="production")
            assert watermark == accepted
            row = state.db.execute(
                "SELECT outcome, probe_run_id FROM recovery_operations "
                "WHERE operation = 'cutover'"
            ).fetchone()
            assert row["outcome"] == "accepted"
            assert row["probe_run_id"] == probe_id

    def test_cutover_refuses_stale_expected_old(self, db_path: str) -> None:
        with StateManager(db_path=db_path, init_schema=False) as state:
            lease = state.acquire_environment_lease(
                "production", "probe-seed", ttl_seconds=300
            )
            assert lease.value is not None
            probe_id = state.start_probe_run(
                "production", source_count=1, window_hours=6.0,
                limits_json=json.dumps(watermark_cli._production_limits(), sort_keys=True),
            )
            state.complete_probe_run(
                probe_id, environment="production", owner_id="probe-seed",
                fence=lease.value.fence, passed=True, source_count=1,
                page_count=1, detail_json="{}",
            )
            state.release_environment_lease(
                "production", "probe-seed", lease.value.fence
            )

        exit_code = watermark_cli.run_watermark(
            _cutover_args(
                db_path, accepted_new=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
                expected_old=datetime(2020, 1, 1, tzinfo=UTC).isoformat(),
            )
        )
        assert exit_code == watermark_cli.EXIT_STALE_EXPECTED_OLD

    def test_cutover_refuses_lock_contention(self, db_path: str) -> None:
        with StateManager(db_path=db_path, init_schema=False) as state:
            held = state.acquire_environment_lease("production", "another-worker", ttl_seconds=60)
            assert held.value is not None

            exit_code = watermark_cli.run_watermark(
                _cutover_args(
                    db_path, accepted_new=(datetime.now(UTC) - timedelta(minutes=1)).isoformat()
                )
            )
            assert exit_code == watermark_cli.EXIT_LOCK_CONTENTION


class TestBackfillFailureHandling:
    def test_fetch_exception_leaves_a_terminal_owner_and_a_refused_audit_row(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            watermark_cli, "build_platform_scanners", lambda **_kw: (None, None, None)
        )

        async def exploding_fetch(*_args: object, **_kwargs: object):
            raise RuntimeError("platform is on fire")

        monkeypatch.setattr(watermark_cli, "fetch_messages", exploding_fetch)

        with pytest.raises(RuntimeError, match="on fire"):
            watermark_cli.run_watermark(_backfill_args(db_path))

        with StateManager(db_path=db_path, init_schema=False) as state:
            scan = state.db.execute(
                "SELECT status, watermark_advanced FROM scans ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert scan["status"] == "failed"
            assert scan["watermark_advanced"] == 0
            audit = state.db.execute(
                "SELECT outcome, detail FROM recovery_operations ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert audit["outcome"] == "refused"
            assert "RuntimeError" in audit["detail"]
            # The lock was released on the way out.
            from scout.result import Ok

            assert isinstance(
                state.acquire_environment_lease("production", "next", ttl_seconds=30), Ok
            )

    def test_audit_insert_failure_rolls_back_the_advance(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            watermark_cli, "build_platform_scanners", lambda **_kw: (None, None, None)
        )

        async def fake_fetch(*_args: object, **_kwargs: object):
            return PlatformsFetch([], [])

        monkeypatch.setattr(watermark_cli, "fetch_messages", fake_fetch)
        original = StateManager.record_recovery_operation

        def exploding_audit(self: StateManager, **kwargs: object):
            if kwargs.get("outcome") == "accepted":
                raise sqlite3.OperationalError("audit table unavailable")
            return original(self, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(StateManager, "record_recovery_operation", exploding_audit)

        with pytest.raises(sqlite3.OperationalError):
            watermark_cli.run_watermark(_backfill_args(db_path))

        with StateManager(db_path=db_path, init_schema=False) as state:
            assert state.get_last_scan_timestamp(environment="production") is None
            scan = state.db.execute(
                "SELECT watermark_advanced, coverage_outcome FROM scans ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert scan["watermark_advanced"] == 0
            assert scan["coverage_outcome"] is None

    def test_unattempted_active_required_source_blocks_backfill(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setattr(
            watermark_cli, "build_platform_scanners", lambda **_kw: (None, None, None)
        )

        async def fake_fetch(*_args: object, **_kwargs: object):
            return PlatformsFetch([], [])

        monkeypatch.setattr(watermark_cli, "fetch_messages", fake_fetch)
        with StateManager(db_path=db_path, init_schema=False) as state:
            state.ensure_source_checkpoint(
                "discord:channel:9", platform="discord", source_kind="channel", provider_key="9",
            )

        exit_code = watermark_cli.run_watermark(_backfill_args(db_path))
        assert exit_code == watermark_cli.EXIT_SOURCE_OR_PROBE_FAILURE
        payload = json.loads(capsys.readouterr().out)
        assert payload["unattempted_active_sources"] == ["discord:channel:9"]

        with StateManager(db_path=db_path, init_schema=False) as state:
            assert state.get_last_scan_timestamp(environment="production") is None
            scan = state.db.execute(
                "SELECT coverage_outcome FROM scans ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert scan["coverage_outcome"] == "blocked"
            failures = state.get_scan_fetch_failures(1)
            assert any(f["kind"] == "source_unattempted" for f in failures)
