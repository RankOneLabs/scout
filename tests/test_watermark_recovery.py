"""Behavioral tests for the watermark recovery CLI: probe, stale-check,
bounded backfill, and controlled cutover, plus their audited recovery-lock
and exit-code contracts."""

from __future__ import annotations

import json
from argparse import Namespace
from datetime import UTC, datetime, timedelta

import pytest

import scout.cli.watermark as watermark_cli
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
            accepted = datetime.now(UTC)
            acquired = state.acquire_environment_lease("production", "owner", ttl_seconds=30)
            assert acquired.value is not None
            state.cutover_watermark(
                environment="production", owner_id="owner", fence=acquired.value.fence,
                expected_old_watermark=None, accepted_new_watermark=accepted,
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
            state.cutover_watermark(
                environment="development", owner_id="owner", fence=acquired.value.fence,
                expected_old_watermark=None, accepted_new_watermark=datetime.now(UTC),
            )

        exit_code = watermark_cli.run_watermark(_stale_check_args(db_path))
        assert exit_code == watermark_cli.EXIT_STALE_WATERMARK


class TestProbe:
    def test_probe_with_no_platforms_configured_passes_and_records_evidence(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setattr(watermark_cli, "build_platform_scanners", lambda: (None, None, None))

        async def fake_fetch(*_args: object, **_kwargs: object):
            return [], []

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

    def test_probe_never_touches_source_checkpoints(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(watermark_cli, "build_platform_scanners", lambda: (None, None, None))

        async def fake_fetch(*_args: object, **_kwargs: object):
            return [], []

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
        monkeypatch.setattr(watermark_cli, "build_platform_scanners", lambda: (None, None, None))

        async def fake_fetch(*_args: object, **_kwargs: object):
            return [], []

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
        monkeypatch.setattr(watermark_cli, "build_platform_scanners", lambda: (None, None, None))

        async def fake_fetch(*_args: object, **_kwargs: object):
            return [], []

        monkeypatch.setattr(watermark_cli, "fetch_messages", fake_fetch)

        exit_code = watermark_cli.run_watermark(_backfill_args(db_path))
        assert exit_code == watermark_cli.EXIT_OK

        with StateManager(db_path=db_path, init_schema=False) as state:
            assert state.get_last_scan_timestamp(environment="production") is not None

    def test_backfill_records_an_accepted_audit_row(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(watermark_cli, "build_platform_scanners", lambda: (None, None, None))

        async def fake_fetch(*_args: object, **_kwargs: object):
            return [], []

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


class TestCutover:
    def test_cutover_without_a_recent_probe_is_refused(
        self, db_path: str, capsys: pytest.CaptureFixture,
    ) -> None:
        accepted = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
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
            probe_id = state.start_probe_run("production", source_count=1)
            state.complete_probe_run(probe_id, passed=True, page_count=1, detail_json="{}")

        accepted = datetime.now(UTC)
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
            probe_id = state.start_probe_run("production", source_count=1)
            state.complete_probe_run(probe_id, passed=True, page_count=1, detail_json="{}")

        exit_code = watermark_cli.run_watermark(
            _cutover_args(
                db_path, accepted_new=datetime.now(UTC).isoformat(),
                expected_old=datetime(2020, 1, 1, tzinfo=UTC).isoformat(),
            )
        )
        assert exit_code == watermark_cli.EXIT_STALE_EXPECTED_OLD

    def test_cutover_refuses_lock_contention(self, db_path: str) -> None:
        with StateManager(db_path=db_path, init_schema=False) as state:
            held = state.acquire_environment_lease("production", "another-worker", ttl_seconds=60)
            assert held.value is not None

            exit_code = watermark_cli.run_watermark(
                _cutover_args(db_path, accepted_new=datetime.now(UTC).isoformat())
            )
            assert exit_code == watermark_cli.EXIT_LOCK_CONTENTION
