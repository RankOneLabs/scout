"""Tests for the shared replay_runtime resource-lifetime context manager:
ownership of an injected StateManager, reverse-order cleanup, and the
in-flight-exception-versus-cleanup-exception contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from jig import SQLiteFeedbackLoop, SQLiteTracer

import scout.replay.runtime as replay_runtime
from scout.replay.runtime import replay_runtime as replay_runtime_cm
from scout.storage.state import StateManager


def _db_paths(tmp_path: Path) -> tuple[str, str, str]:
    return (
        str(tmp_path / "scout.db"),
        str(tmp_path / "traces.db"),
        str(tmp_path / "feedback.db"),
    )


class TestCliOwnedState:
    async def test_opens_and_closes_its_own_state(self, tmp_path: Path) -> None:
        db_path, trace_db_path, feedback_db_path = _db_paths(tmp_path)
        async with replay_runtime_cm(
            db_path=db_path, trace_db_path=trace_db_path, feedback_db_path=feedback_db_path
        ) as rt:
            assert isinstance(rt.state, StateManager)
            assert isinstance(rt.tracer, SQLiteTracer)
            assert isinstance(rt.feedback, SQLiteFeedbackLoop)
            # Schema was initialized — a query against a real table succeeds.
            rt.state.get_scan_stats()

        with pytest.raises(Exception, match="Cannot operate on a closed database"):
            # Connection is closed; any further use should fail.
            rt.state.get_scan_stats()

    async def test_domain_exception_propagates(self, tmp_path: Path) -> None:
        db_path, trace_db_path, feedback_db_path = _db_paths(tmp_path)
        with pytest.raises(ValueError, match="boom"):
            async with replay_runtime_cm(
                db_path=db_path, trace_db_path=trace_db_path, feedback_db_path=feedback_db_path
            ):
                raise ValueError("boom")


class TestInjectedState:
    async def test_does_not_close_injected_state(self, tmp_path: Path) -> None:
        db_path, trace_db_path, feedback_db_path = _db_paths(tmp_path)
        state = StateManager(db_path=db_path)
        try:
            async with replay_runtime_cm(
                trace_db_path=trace_db_path, feedback_db_path=feedback_db_path, state=state
            ) as rt:
                assert rt.state is state

            # Still usable after the context manager exits — ownership stayed
            # with the caller.
            state.get_scan_stats()
        finally:
            state.close()

    async def test_never_reinitializes_injected_state_schema(self, tmp_path: Path) -> None:
        db_path, trace_db_path, feedback_db_path = _db_paths(tmp_path)
        # Bootstrap schema once, then hand replay_runtime a connection opened
        # with init_schema=False, mirroring a caller that already ran the
        # DDL and migrations for this process.
        bootstrap = StateManager(db_path=db_path)
        bootstrap.close()
        state = StateManager(db_path=db_path, init_schema=False)
        try:
            async with replay_runtime_cm(
                trace_db_path=trace_db_path, feedback_db_path=feedback_db_path, state=state
            ) as rt:
                assert rt.state is state
                rt.state.get_scan_stats()
        finally:
            state.close()


class TestCleanupOrderAndErrors:
    async def test_closes_feedback_then_tracer_then_state_in_reverse_order(
        self, tmp_path: Path
    ) -> None:
        db_path, trace_db_path, feedback_db_path = _db_paths(tmp_path)
        order: list[str] = []

        class TrackingTracer(SQLiteTracer):
            async def close(self) -> None:
                order.append("tracer")
                await super().close()

        class TrackingFeedback(SQLiteFeedbackLoop):
            async def close(self) -> None:
                order.append("feedback")
                await super().close()

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(replay_runtime, "SQLiteTracer", TrackingTracer)
        monkeypatch.setattr(replay_runtime, "SQLiteFeedbackLoop", TrackingFeedback)
        try:
            async with replay_runtime_cm(
                db_path=db_path, trace_db_path=trace_db_path, feedback_db_path=feedback_db_path
            ):
                pass
        finally:
            monkeypatch.undo()

        assert order == ["feedback", "tracer"]

    async def test_cleanup_failure_does_not_replace_in_flight_exception(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        db_path, trace_db_path, feedback_db_path = _db_paths(tmp_path)

        class BrokenFeedback(SQLiteFeedbackLoop):
            async def close(self) -> None:
                raise RuntimeError("cleanup failed")

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(replay_runtime, "SQLiteFeedbackLoop", BrokenFeedback)
        try:
            with caplog.at_level("ERROR"), pytest.raises(ValueError, match="original failure"):
                async with replay_runtime_cm(
                    db_path=db_path,
                    trace_db_path=trace_db_path,
                    feedback_db_path=feedback_db_path,
                ):
                    raise ValueError("original failure")
            assert "feedback.close() failed" in caplog.text
        finally:
            monkeypatch.undo()

    async def test_cleanup_failure_propagates_when_block_exits_cleanly(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        db_path, trace_db_path, feedback_db_path = _db_paths(tmp_path)

        class BrokenFeedback(SQLiteFeedbackLoop):
            async def close(self) -> None:
                raise RuntimeError("cleanup failed")

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(replay_runtime, "SQLiteFeedbackLoop", BrokenFeedback)
        try:
            with caplog.at_level("ERROR"), pytest.raises(RuntimeError, match="cleanup failed"):
                async with replay_runtime_cm(
                    db_path=db_path,
                    trace_db_path=trace_db_path,
                    feedback_db_path=feedback_db_path,
                ):
                    pass
            assert "feedback.close() failed" in caplog.text
        finally:
            monkeypatch.undo()
