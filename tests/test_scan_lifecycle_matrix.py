"""Canonical-owner / linkage matrix and await-boundary transaction
instrumentation for main_loop under the dedicated-connection heartbeat.

Every case drives the real main_loop against a real file-backed
StateManager: scoring goes through the real score_messages with the model
boundary (run_pipeline) faked, so the scan/linkage/watermark invariants are
asserted from durable rows, not from mocks."""

from __future__ import annotations

import asyncio
import sqlite3
from argparse import Namespace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

import scout.config as config
import scout.scanning.runner as scan_runner
from scout.config import Message
from scout.errors import PlatformFetchFailure, SourceFetchOutcome
from scout.registry import KeywordRoute, RuntimeRegistry
from scout.result import Err, Ok
from scout.scanning.runner import PlatformsFetch
from scout.storage.state import StateManager


def _registry() -> RuntimeRegistry:
    route = KeywordRoute(
        id=1, project_key="gw", keyword="agent",
        evaluate_prompt=None, respond_prompt=None, critique_prompt=None, priority=0,
    )
    return RuntimeRegistry(projects={}, keywords=(route,), prompt_templates={})


def _message(platform_id: str) -> Message:
    return Message(
        platform="bluesky", platform_id=platform_id, channel_name="bluesky", channel_id="",
        author_name="author", author_id="author-id", content=f"agent message {platform_id}",
        created_at=datetime.now(UTC),
    )


def _outcome(source_key: str, *, covered: bool = True) -> SourceFetchOutcome:
    failure = None if covered else PlatformFetchFailure(
        platform="bluesky", kind="page_ceiling", message="ceiling", context=source_key,
        operation_phase="fetch", blocks_watermark_advance=True,
    )
    return SourceFetchOutcome(
        source_key=source_key, platform="bluesky", source_kind="search", provider_key="agent",
        page_count=2, termination="since_boundary" if covered else "page_ceiling",
        message_count=1, failure=failure,
    )


class _FakeTracer:
    def __init__(self) -> None:
        self.flush = AsyncMock()
        self.close = AsyncMock()


class _FakeFeedback:
    def __init__(self) -> None:
        self.close = AsyncMock()


def _scoring_error_pipeline_result() -> SimpleNamespace:
    # A non-blocking scoring failure: the post stays saved-but-unevaluated,
    # the scan degrades to 'partial', and coverage still finalizes complete.
    return SimpleNamespace(
        step_outputs={"score_and_draft": Err(SimpleNamespace(detail="boom", operation="score"))}
    )


class Harness:
    """Wires a real StateManager (primary) and a real heartbeat StateManager
    on the same temp file into main_loop, with every external boundary
    faked."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path, *, name: str) -> None:
        self.db_path = str(tmp_path / f"{name}.db")
        self.state = StateManager(db_path=self.db_path)
        self.state.load_runtime_registry = Mock(return_value=_registry())  # type: ignore[method-assign]
        state_cm = MagicMock()
        state_cm.__enter__ = Mock(return_value=self.state)
        state_cm.__exit__ = Mock(return_value=False)
        self.heartbeat_state = StateManager(db_path=self.db_path, init_schema=False)
        self.state_factory = Mock(side_effect=[state_cm, self.heartbeat_state])
        self.run_pipeline = AsyncMock(return_value=_scoring_error_pipeline_result())
        self.digest_calls: list[str] = []

        monkeypatch.setattr(scan_runner, "validate_config", lambda: [])
        monkeypatch.setattr(scan_runner, "StateManager", self.state_factory)
        monkeypatch.setattr(scan_runner, "SQLiteTracer", Mock(return_value=_FakeTracer()))
        monkeypatch.setattr(scan_runner, "SQLiteFeedbackLoop", Mock(return_value=_FakeFeedback()))
        monkeypatch.setattr(scan_runner, "run_pipeline", self.run_pipeline)
        monkeypatch.setattr(scan_runner, "build_scout_pipeline", Mock(return_value=Mock()))
        monkeypatch.setattr(scan_runner, "build_scout_phase_configs", Mock(return_value=Mock()))
        monkeypatch.setattr(scan_runner, "write_digest_header", Mock())
        monkeypatch.setattr(scan_runner, "append_to_digest", Mock())
        monkeypatch.setattr(scan_runner, "finalize_digest", Mock(return_value=""))
        monkeypatch.setattr(
            scan_runner, "MODES",
            {
                "lead_gen": {"evaluate": "e", "respond": "r", "critique": "c"},
                "temp_check": {"evaluate": "e2", "respond": "r2", "critique": "c2"},
            },
        )
        monkeypatch.setattr(config, "SCOUT_DOSSIER_ROOT", "")

    def fetch(self, fetched: PlatformsFetch, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scan_runner, "fetch_messages", AsyncMock(return_value=fetched))

    def scans(self) -> list:
        return self.state.conn.execute(
            "SELECT id, role, run_kind, status, canonical_scan_id, coverage_outcome, "
            "watermark_advanced, safe_watermark_at FROM scans ORDER BY id"
        ).fetchall()

    def close(self) -> None:
        self.state.close()
        # main_loop closed the heartbeat connection it was handed.
        with pytest.raises(sqlite3.ProgrammingError):
            self.heartbeat_state.conn.execute("SELECT 1")


def _args(**overrides) -> Namespace:
    base = dict(mode="lead_gen", rescore=None, rescore_failed=None, continuous=False)
    base.update(overrides)
    return Namespace(**base)


async def test_single_mode_with_candidates_has_exactly_one_canonical_owner_that_advances(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    h = Harness(monkeypatch, tmp_path, name="single")
    h.fetch(PlatformsFetch([_message("m1")], [], (_outcome("bluesky:search:agent"),)), monkeypatch)

    await scan_runner.main_loop(_args())

    rows = h.scans()
    assert len(rows) == 1
    (row,) = rows
    assert (row["role"], row["run_kind"], row["status"]) == ("canonical_live", "live", "partial")
    assert row["coverage_outcome"] == "complete"
    assert row["watermark_advanced"] == 1
    assert h.state.get_last_scan_timestamp(environment=config.SCOUT_ENVIRONMENT) is not None
    checkpoint = h.state.get_source_checkpoint("bluesky:search:agent")
    assert checkpoint is not None and checkpoint.checkpoint_at is not None
    h.close()


async def test_mode_both_links_a_non_advancing_secondary_to_the_canonical_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    h = Harness(monkeypatch, tmp_path, name="both")
    h.fetch(PlatformsFetch([_message("m1")], [], (_outcome("bluesky:search:agent"),)), monkeypatch)

    await scan_runner.main_loop(_args(mode="both"))

    rows = h.scans()
    assert [r["role"] for r in rows] == ["canonical_live", "secondary"]
    canonical, secondary = rows
    assert secondary["canonical_scan_id"] == canonical["id"]
    assert canonical["coverage_outcome"] == "complete" and canonical["watermark_advanced"] == 1
    assert secondary["coverage_outcome"] is None and secondary["watermark_advanced"] == 0
    assert secondary["safe_watermark_at"] is None
    h.close()


async def test_rescore_failed_pass_is_a_rescore_role_that_never_advances(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    h = Harness(monkeypatch, tmp_path, name="rescore_failed")
    h.state.load_unevaluated_posts = Mock(return_value=[_message("m1")])  # type: ignore[method-assign]
    fetch = AsyncMock()
    monkeypatch.setattr(scan_runner, "fetch_messages", fetch)

    await scan_runner.main_loop(_args(rescore_failed="all"))

    rows = h.scans()
    assert len(rows) == 1
    assert (rows[0]["role"], rows[0]["run_kind"]) == ("rescore", "rescore")
    assert rows[0]["watermark_advanced"] == 0
    fetch.assert_not_awaited()
    h.close()


async def test_partial_processing_with_uncovered_required_source_is_blocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    h = Harness(monkeypatch, tmp_path, name="partial")
    uncovered = _outcome("bluesky:search:agent", covered=False)
    assert uncovered.failure is not None
    h.fetch(PlatformsFetch([_message("m1")], [uncovered.failure], (uncovered,)), monkeypatch)

    await scan_runner.main_loop(_args())

    (row,) = h.scans()
    assert row["status"] == "partial"
    assert row["coverage_outcome"] == "blocked"
    assert row["watermark_advanced"] == 0
    checkpoint = h.state.get_source_checkpoint("bluesky:search:agent")
    assert checkpoint is not None and checkpoint.checkpoint_at is None
    h.close()


async def test_lease_lost_during_fetch_stops_the_scan_promptly_and_never_advances(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    h = Harness(monkeypatch, tmp_path, name="lost")
    monkeypatch.setattr(config, "SCOUT_LEASE_HEARTBEAT_SECONDS", 0.01)

    async def slow_fetch(*_a: object, **_k: object) -> PlatformsFetch:
        # Another worker takes the environment over while this fetch is in flight.
        taker = StateManager(db_path=h.db_path, init_schema=False)
        taker.db.execute(
            "UPDATE environment_leases SET expires_at = ? WHERE environment = ?",
            ("2000-01-01T00:00:00+00:00", config.SCOUT_ENVIRONMENT),
        )
        taker.db.commit()
        assert isinstance(
            taker.acquire_environment_lease(config.SCOUT_ENVIRONMENT, "other", ttl_seconds=60), Ok
        )
        taker.close()
        await asyncio.sleep(0.1)
        return PlatformsFetch([_message("m1")], [], (_outcome("bluesky:search:agent"),))

    monkeypatch.setattr(scan_runner, "fetch_messages", slow_fetch)

    with pytest.raises(Exception, match="lease"):
        await scan_runner.main_loop(_args())

    (row,) = h.scans()
    assert row["status"] == "failed"
    assert row["watermark_advanced"] == 0
    failures = h.state.get_scan_fetch_failures(row["id"])
    assert any("LeaseLostError" in (f["message"] or "") for f in failures)
    h.close()


async def test_no_transaction_is_open_across_any_await_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Platform fetch, model call, digest I/O, heartbeat renewal, and the
    continuous-mode sleep all observe the primary connection with no
    transaction open."""
    h = Harness(monkeypatch, tmp_path, name="instrumented")
    primary = h.state
    observed: dict[str, bool] = {}

    def _record(boundary: str) -> None:
        observed[boundary] = not primary.db.in_transaction and not primary.conn.in_transaction

    async def fetch(*_a: object, **_k: object) -> PlatformsFetch:
        _record("platform_fetch")
        return PlatformsFetch([_message("m1")], [], (_outcome("bluesky:search:agent"),))

    async def model(*_a: object, **_k: object) -> SimpleNamespace:
        _record("model_call")
        return _scoring_error_pipeline_result()

    def digest(*_a: object, **_k: object) -> str:
        _record("digest_io")
        return ""

    real_renew = h.heartbeat_state.renew_environment_lease

    def renew(*a: object, **k: object):
        _record("heartbeat_renewal")
        return real_renew(*a, **k)

    sleeps: list[float] = []

    async def continuous_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        _record("continuous_sleep")
        raise asyncio.CancelledError()

    monkeypatch.setattr(scan_runner, "fetch_messages", fetch)
    monkeypatch.setattr(scan_runner, "run_pipeline", model)
    monkeypatch.setattr(scan_runner, "write_digest_header", digest)
    monkeypatch.setattr(scan_runner, "finalize_digest", digest)
    monkeypatch.setattr(h.heartbeat_state, "renew_environment_lease", renew)
    monkeypatch.setattr(config, "SCOUT_LEASE_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(scan_runner.asyncio, "sleep", continuous_sleep)

    original_fetch = fetch

    async def fetch_with_heartbeat_window(*a: object, **k: object) -> PlatformsFetch:
        result = await original_fetch(*a, **k)
        # Let at least one heartbeat renewal land while the scan is mid-flight.
        deadline = asyncio.get_running_loop().time() + 1.0
        while "heartbeat_renewal" not in observed and asyncio.get_running_loop().time() < deadline:
            await _yield()
        return result

    async def _yield() -> None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        loop.call_later(0.02, fut.set_result, None)
        await fut

    monkeypatch.setattr(scan_runner, "fetch_messages", fetch_with_heartbeat_window)

    with pytest.raises(asyncio.CancelledError):
        await scan_runner.main_loop(_args(continuous=True))

    assert sleeps == [config.SCAN_INTERVAL_HOURS * 3600]
    assert observed == {
        "platform_fetch": True,
        "model_call": True,
        "digest_io": True,
        "heartbeat_renewal": True,
        "continuous_sleep": True,
    }
    h.close()
