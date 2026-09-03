"""Tests for scan_runner live-scan zero-work behavior."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from argparse import Namespace
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from jig import (
    CompletionParams,
    LLMClient,
    LLMResponse,
    SQLiteFeedbackLoop,
    SQLiteTracer,
    ToolCall,
    Usage,
)

import scout.config as config
import scout.grading.feedback as ef
import scout.scanning.runner as scan_runner
from scout.config import (
    MODES,
    SCAN_INTERVAL_HOURS,
    GradeRecord,
    Message,
    ModeConfig,
    SourceAuthor,
    SourceParent,
)
from scout.dossiers.resolver import DossierFact, DossierResource, DossierSummary
from scout.errors import PlatformFetchFailure
from scout.grading.feedback import (
    PersistedFeedbackPhase,
    PersistedFeedbackSnapshot,
    legacy_feedback_bundle,
)
from scout.grading.service import format_grading_signals
from scout.registry import KeywordRoute, ProjectTarget, RuntimeRegistry
from scout.result import Err, Ok
from scout.scanning.prefilter import RoutedMessage
from scout.scanning.schemas import (
    DeclarativeSegment,
    ReplyCandidate,
    ResourceSegment,
    StructuredDraftOutput,
)
from scout.storage.state import StateManager, SurfaceRateLimitedError
from scout.verifier import VerifyResult
from tests.conftest import seed_phase_run_contributors

_EMPTY_FEEDBACK_BUNDLE = legacy_feedback_bundle("")


def _empty_registry() -> RuntimeRegistry:
    return RuntimeRegistry(projects={}, keywords=(), prompt_templates={})


def _registry_with_keywords() -> RuntimeRegistry:
    from scout.registry import KeywordRoute
    kw = KeywordRoute(
        id=1, project_key="gw", keyword="agent",
        evaluate_prompt=None, respond_prompt=None, critique_prompt=None, priority=0,
    )
    return RuntimeRegistry(projects={}, keywords=(kw,), prompt_templates={})


def _fake_feedback_snapshot(mode: str = "shadow") -> PersistedFeedbackSnapshot:
    phases = tuple(
        PersistedFeedbackPhase(
            phase=phase,  # type: ignore[arg-type]
            snapshot_phase_id=i,
            rendered_sha256="e" * 64,
            token_estimate=0,
            token_budget=800,
            aggregate_count=0,
            example_count=0,
            excluded_count=0,
        )
        for i, phase in enumerate(("relevance", "reply_draft", "critic"), start=1)
    )
    return PersistedFeedbackSnapshot(
        snapshot_id=1,
        scan_id=1,
        policy_version="evaluation-feedback/v1",
        mode=mode,  # type: ignore[arg-type]
        as_of="2025-01-01T00:00:00.000Z",
        population_count=0,
        eligible_count=0,
        excluded_count=0,
        phases=(phases[0], phases[1], phases[2]),
    )


class _FakeState(AbstractContextManager["_FakeState"]):
    def __init__(self, registry: RuntimeRegistry | None = None) -> None:
        self.load_runtime_registry = Mock(return_value=registry or _empty_registry())
        self.start_scan = Mock(side_effect=AssertionError("start_scan should not be called"))
        self.complete_scan = Mock()
        self.save_fetch_failure = Mock()
        self.commit = Mock()
        self.has_seen_message = Mock(return_value=False)
        self.get_last_scan_timestamp = Mock(return_value=None)
        self.load_posts = Mock()
        self.load_unevaluated_posts = Mock()
        self.get_recent_grading_signals = Mock()
        self.get_recent_critique_feedback = Mock()
        self.record_feedback_snapshot = Mock(return_value=_fake_feedback_snapshot())
        self.load_committed_feedback_bundle = Mock(
            side_effect=AssertionError(
                "load_committed_feedback_bundle should not be called in shadow mode"
            )
        )
        self.conn = Mock()
        self.conn.rollback = Mock()

    def __enter__(self) -> _FakeState:
        return self

    def __exit__(
        self, exc_type: object, exc: object, tb: object
    ) -> Literal[False]:
        return False


class _FakeTracer:
    def __init__(self) -> None:
        self.flush = AsyncMock()
        self.close = AsyncMock()


class _FakeFeedback:
    def __init__(self) -> None:
        self.close = AsyncMock()


def _message(platform_id: str, created_at: datetime) -> Message:
    return Message(
        platform="bluesky",
        platform_id=platform_id,
        channel_name="bluesky",
        channel_id="",
        author_name="author",
        author_id="author-id",
        content=f"message {platform_id}",
        created_at=created_at,
    )


def _configure_main_loop_score_failure(
    monkeypatch: pytest.MonkeyPatch,
    state: StateManager,
    score_messages: object,
) -> None:
    state.load_runtime_registry = Mock(  # type: ignore[method-assign]
        return_value=_registry_with_keywords()
    )
    state_cm = MagicMock()
    state_cm.__enter__ = Mock(return_value=state)
    state_cm.__exit__ = Mock(return_value=False)
    msg = _message("agent", datetime.now(UTC))

    monkeypatch.setattr(scan_runner, "validate_config", lambda: [])
    monkeypatch.setattr(
        scan_runner, "fetch_messages", AsyncMock(return_value=([msg], []))
    )
    monkeypatch.setattr(scan_runner, "score_messages", score_messages)
    monkeypatch.setattr(scan_runner, "StateManager", Mock(return_value=state_cm))
    monkeypatch.setattr(
        scan_runner, "SQLiteTracer", Mock(return_value=_FakeTracer())
    )
    monkeypatch.setattr(
        scan_runner, "SQLiteFeedbackLoop", Mock(return_value=_FakeFeedback())
    )
    monkeypatch.setattr(
        scan_runner,
        "MODES",
        {"default": {"evaluate": "e", "respond": "r", "critique": "c"}},
    )


def _route(evaluate: str | None = None) -> KeywordRoute:
    return KeywordRoute(
        id=1, project_key="gw", keyword="agent",
        evaluate_prompt=evaluate, respond_prompt=None, critique_prompt=None,
        priority=0,
    )


def test_required_prompt_names_covers_modes_and_route_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        scan_runner, "MODES",
        {"m": {"evaluate": "base_eval", "respond": "base_resp", "critique": "base_crit"}},
    )
    registry = RuntimeRegistry(
        projects={}, keywords=(_route(evaluate="custom_eval"),), prompt_templates={}
    )
    names = scan_runner._required_prompt_names(registry, ["m"])
    assert names == {"base_eval", "base_resp", "base_crit", "custom_eval"}


def test_log_prompt_diagnostics_warns_on_missing_prompt(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        scan_runner, "MODES",
        {"m": {"evaluate": "ghost_eval", "respond": "ghost_resp", "critique": "ghost_crit"}},
    )
    registry = RuntimeRegistry(projects={}, keywords=(), prompt_templates={})
    with caplog.at_level(logging.WARNING, logger="scout.scanning.runner"):
        scan_runner.log_prompt_diagnostics(registry, ["m"])
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings and "missing" in warnings[0].getMessage()
    assert "ghost_eval" in warnings[0].getMessage()


def test_log_prompt_diagnostics_silent_when_all_db_backed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        scan_runner, "MODES",
        {"m": {"evaluate": "e", "respond": "r", "critique": "c"}},
    )
    registry = RuntimeRegistry(
        projects={}, keywords=(),
        prompt_templates={"e": "E", "r": "R", "c": "C"},
    )
    with caplog.at_level(logging.WARNING, logger="scout.scanning.runner"):
        scan_runner.log_prompt_diagnostics(registry, ["m"])
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_cap_new_messages_sorts_newest_first_and_truncates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = datetime.now(UTC)
    messages = [
        _message("older", now - timedelta(minutes=2)),
        _message("newest", now),
        _message("middle", now - timedelta(minutes=1)),
    ]

    with caplog.at_level(logging.INFO, logger="scout.scanning.runner"):
        capped = scan_runner.cap_new_messages(messages, max_messages=2)

    assert [m.platform_id for m in capped] == ["newest", "middle"]
    assert "New messages capped: 3 -> 2" in caplog.text


def test_cap_new_messages_non_positive_limit_means_uncapped() -> None:
    now = datetime.now(UTC)
    messages = [
        _message("older", now - timedelta(minutes=1)),
        _message("newer", now),
    ]

    capped = scan_runner.cap_new_messages(messages, max_messages=0)

    assert [m.platform_id for m in capped] == ["newer", "older"]


def test_cap_new_messages_resolves_default_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    messages = [
        _message("oldest", now - timedelta(minutes=3)),
        _message("newest", now),
        _message("middle", now - timedelta(minutes=1)),
    ]
    monkeypatch.setattr(scan_runner, "SCAN_MAX_NEW_MESSAGES", 1)

    capped = scan_runner.cap_new_messages(messages)

    assert [m.platform_id for m in capped] == ["newest"]


def test_log_prompt_diagnostics_warns_when_route_override_threshold_is_met(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        scan_runner, "MODES",
        {"m": {"evaluate": "e", "respond": "r", "critique": "c"}},
    )
    monkeypatch.setattr(scan_runner, "PROMPT_DIAGNOSTIC_ROUTE_THRESHOLD", 1)
    registry = RuntimeRegistry(
        projects={},
        keywords=(
            _route(evaluate="missing_eval"),
            _route(evaluate=None),
        ),
        prompt_templates={"e": "E", "r": "R", "c": "C"},
    )
    with caplog.at_level(logging.WARNING, logger="scout.scanning.runner"):
        scan_runner.log_prompt_diagnostics(registry, ["m"])
    assert any("active keyword route(s) reference" in r.getMessage() for r in caplog.records)


def test_validate_config_requires_openrouter_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "openrouter/anthropic/claude-3.5-sonnet"
    monkeypatch.setattr(scan_runner, "RELEVANCE_MODEL", model)
    monkeypatch.setenv("RELEVANCE_MODEL", model)
    monkeypatch.setattr(scan_runner, "REPLY_DRAFT_MODEL", "ollama/reply")
    monkeypatch.setattr(scan_runner, "CRITIC_MODEL", "ollama/critic")
    monkeypatch.setattr(scan_runner, "DISCORD_BOT_TOKEN", "token")
    monkeypatch.setattr(scan_runner, "DISCORD_SERVER_ID", 1)
    monkeypatch.setattr(scan_runner, "DISCORD_CHANNEL_IDS", [1])
    monkeypatch.setattr("scout.config.get_env_errors", lambda: [])
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    errors = scan_runner.validate_config()

    assert errors == [
        "OPENROUTER_API_KEY not set (required when "
        "RELEVANCE_MODEL='openrouter/anthropic/claude-3.5-sonnet')"
    ]


def test_validate_config_accepts_openrouter_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        scan_runner,
        "RELEVANCE_MODEL",
        "openrouter/anthropic/claude-3.5-sonnet",
    )
    monkeypatch.setenv("RELEVANCE_MODEL", "openrouter/anthropic/claude-3.5-sonnet")
    monkeypatch.setattr(scan_runner, "REPLY_DRAFT_MODEL", "ollama/reply")
    monkeypatch.setattr(scan_runner, "CRITIC_MODEL", "ollama/critic")
    monkeypatch.setattr(scan_runner, "DISCORD_BOT_TOKEN", "token")
    monkeypatch.setattr(scan_runner, "DISCORD_SERVER_ID", 1)
    monkeypatch.setattr(scan_runner, "DISCORD_CHANNEL_IDS", [1])
    monkeypatch.setattr("scout.config.get_env_errors", lambda: [])
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    assert scan_runner.validate_config() == []


@pytest.mark.asyncio
async def test_main_loop_skips_zero_work_live_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_state = _FakeState(registry=_empty_registry())
    fetch_messages_mock = AsyncMock(return_value=([], []))

    monkeypatch.setattr(scan_runner, "validate_config", lambda: [])
    monkeypatch.setattr(scan_runner, "fetch_messages", fetch_messages_mock)
    monkeypatch.setattr(scan_runner, "StateManager", Mock(return_value=fake_state))
    monkeypatch.setattr(scan_runner, "SQLiteTracer", Mock())
    monkeypatch.setattr(scan_runner, "SQLiteFeedbackLoop", Mock())

    args = Namespace(mode="both", rescore=None, rescore_failed=None, continuous=False)

    await scan_runner.main_loop(args)

    fake_state.start_scan.assert_not_called()
    fake_state.complete_scan.assert_not_called()
    fake_state.commit.assert_not_called()
    fetch_messages_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_loop_anchors_watermark_to_pre_fetch_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    db_path = str(tmp_path / "pre_fetch_watermark.db")
    real_state = StateManager(db_path=db_path)
    state_cm = MagicMock()
    state_cm.__enter__ = Mock(return_value=real_state)
    state_cm.__exit__ = Mock(return_value=False)

    fetch_called_at: datetime | None = None

    async def fake_fetch(
        *_args: object,
        **_kwargs: object,
    ) -> tuple[list[Message], list[PlatformFetchFailure]]:
        nonlocal fetch_called_at
        fetch_called_at = datetime.now(UTC)
        return ([_message("during-fetch", fetch_called_at)], [])

    monkeypatch.setattr(scan_runner, "validate_config", lambda: [])
    monkeypatch.setattr(scan_runner, "fetch_messages", fake_fetch)
    monkeypatch.setattr(scan_runner, "StateManager", Mock(return_value=state_cm))
    monkeypatch.setattr(scan_runner, "SQLiteTracer", Mock(return_value=_FakeTracer()))
    monkeypatch.setattr(scan_runner, "SQLiteFeedbackLoop", Mock(return_value=_FakeFeedback()))
    monkeypatch.setattr(
        scan_runner,
        "MODES",
        {"default": {"evaluate": "e", "respond": "r", "critique": "c"}},
    )
    monkeypatch.setattr(scan_runner, "write_digest_header", Mock())
    monkeypatch.setattr(scan_runner, "finalize_digest", Mock(return_value=""))

    args = Namespace(mode="default", rescore=None, rescore_failed=None, continuous=False)
    await scan_runner.main_loop(args)

    assert fetch_called_at is not None
    row = real_state.conn.execute(
        "SELECT fetch_started_at, safe_watermark_at FROM scans ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    fetch_started_at = datetime.fromisoformat(row["fetch_started_at"])
    safe_watermark_at = datetime.fromisoformat(row["safe_watermark_at"])
    assert fetch_started_at <= fetch_called_at
    assert safe_watermark_at == fetch_started_at

    real_state.close()


@pytest.mark.asyncio
async def test_fetch_failure_without_messages_creates_partial_scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    db_path = str(tmp_path / "empty_failure.db")
    real_state = StateManager(db_path=db_path)
    state_cm = MagicMock()
    state_cm.__enter__ = Mock(return_value=real_state)
    state_cm.__exit__ = Mock(return_value=False)
    failure = PlatformFetchFailure(
        platform="discord",
        kind="auth_error",
        message="missing channel access",
        context="channel:123",
        retryable=False,
    )

    monkeypatch.setattr(scan_runner, "validate_config", lambda: [])
    monkeypatch.setattr(scan_runner, "fetch_messages", AsyncMock(return_value=([], [failure])))
    monkeypatch.setattr(scan_runner, "StateManager", Mock(return_value=state_cm))
    monkeypatch.setattr(scan_runner, "SQLiteTracer", Mock(return_value=_FakeTracer()))
    monkeypatch.setattr(scan_runner, "SQLiteFeedbackLoop", Mock(return_value=_FakeFeedback()))
    monkeypatch.setattr(
        scan_runner,
        "MODES",
        {"default": {"evaluate": "e", "respond": "r", "critique": "c"}},
    )
    monkeypatch.setattr(scan_runner, "write_digest_header", Mock())
    monkeypatch.setattr(scan_runner, "finalize_digest", Mock(return_value=""))

    args = Namespace(mode="default", rescore=None, rescore_failed=None, continuous=False)
    await scan_runner.main_loop(args)

    scan_row = real_state.conn.execute(
        "SELECT id, status, safe_watermark_at FROM scans ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert scan_row is not None
    assert scan_row["status"] == "partial"
    assert scan_row["safe_watermark_at"] is None
    failures = real_state.get_scan_fetch_failures(scan_row["id"])
    assert len(failures) == 1
    assert failures[0]["context"] == "channel:123"

    real_state.close()


@pytest.mark.asyncio
async def test_processing_failure_marks_main_loop_scan_partial(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    db_path = str(tmp_path / "processing_failure.db")
    real_state = StateManager(db_path=db_path)
    real_state.load_runtime_registry = Mock(return_value=_registry_with_keywords())  # type: ignore[method-assign]
    state_cm = MagicMock()
    state_cm.__enter__ = Mock(return_value=real_state)
    state_cm.__exit__ = Mock(return_value=False)
    msg = _message("agent", datetime.now(UTC))
    pipeline_result = Mock()
    pipeline_result.step_outputs = {
        "score_and_draft": Err(Mock(detail="model unavailable", operation="score"))
    }

    monkeypatch.setattr(scan_runner, "validate_config", lambda: [])
    monkeypatch.setattr(scan_runner, "fetch_messages", AsyncMock(return_value=([msg], [])))
    monkeypatch.setattr(scan_runner, "run_pipeline", AsyncMock(return_value=pipeline_result))
    monkeypatch.setattr(scan_runner, "build_scout_pipeline", Mock(return_value=Mock()))
    monkeypatch.setattr(scan_runner, "build_scout_phase_configs", Mock(return_value=Mock()))
    monkeypatch.setattr(scan_runner, "StateManager", Mock(return_value=state_cm))
    monkeypatch.setattr(scan_runner, "SQLiteTracer", Mock(return_value=_FakeTracer()))
    monkeypatch.setattr(scan_runner, "SQLiteFeedbackLoop", Mock(return_value=_FakeFeedback()))
    monkeypatch.setattr(
        scan_runner,
        "MODES",
        {"default": {"evaluate": "e", "respond": "r", "critique": "c"}},
    )
    monkeypatch.setattr(scan_runner, "write_digest_header", Mock())
    monkeypatch.setattr(scan_runner, "finalize_digest", Mock(return_value=""))

    args = Namespace(mode="default", rescore=None, rescore_failed=None, continuous=False)
    await scan_runner.main_loop(args)

    scan_row = real_state.conn.execute(
        "SELECT id, status, safe_watermark_at FROM scans ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert scan_row is not None
    assert scan_row["status"] == "partial"
    assert scan_row["safe_watermark_at"] is None
    failures = real_state.get_scan_fetch_failures(scan_row["id"])
    assert len(failures) == 1
    assert failures[0]["kind"] == "scoring_error"
    assert failures[0]["context"] == "bluesky:agent:score"

    real_state.close()


@pytest.mark.asyncio
async def test_main_loop_marks_unexpected_active_scan_failed_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_state = StateManager(db_path=str(tmp_path / "unexpected-failure.db"))
    _configure_main_loop_score_failure(
        monkeypatch,
        real_state,
        AsyncMock(side_effect=RuntimeError("unexpected scoring defect")),
    )

    args = Namespace(mode="default", rescore=None, rescore_failed=None, continuous=False)
    with pytest.raises(RuntimeError, match="unexpected scoring defect"):
        await scan_runner.main_loop(args)

    scan_row = real_state.conn.execute(
        "SELECT id, status, completed_at FROM scans ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert scan_row is not None
    assert scan_row["status"] == "failed"
    assert scan_row["completed_at"] is not None
    failures = real_state.get_scan_fetch_failures(scan_row["id"])
    assert len(failures) == 1
    assert failures[0]["kind"] == "scan_error"
    assert failures[0]["context"] is None
    assert "unexpected scoring defect" in str(failures[0]["message"])

    real_state.close()


@pytest.mark.asyncio
async def test_main_loop_propagates_previously_recorded_persistence_failure_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_state = StateManager(db_path=str(tmp_path / "persistence-failure.db"))

    async def persistence_failure(*args: object, **_kwargs: object) -> None:
        state = args[10]
        scan_id = args[11]
        assert isinstance(state, StateManager)
        assert isinstance(scan_id, int)
        state.fail_scan(
            scan_id,
            1,
            failure_post_id=123,
            error_kind="persistence_error",
            error_message="simulated write failure",
        )
        raise sqlite3.IntegrityError("simulated write failure")

    _configure_main_loop_score_failure(
        monkeypatch, real_state, persistence_failure
    )

    args = Namespace(mode="default", rescore=None, rescore_failed=None, continuous=False)
    with pytest.raises(sqlite3.IntegrityError, match="simulated write failure"):
        await scan_runner.main_loop(args)

    scan_row = real_state.conn.execute(
        "SELECT id, status FROM scans ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert scan_row is not None
    assert scan_row["status"] == "failed"
    failures = real_state.get_scan_fetch_failures(scan_row["id"])
    assert len(failures) == 1
    assert failures[0]["kind"] == "persistence_error"
    assert failures[0]["context"] == "post_id:123"

    real_state.close()


@pytest.mark.asyncio
async def test_keyboard_interrupt_marks_active_scan_interrupted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    db_path = str(tmp_path / "interrupted.db")
    real_state = StateManager(db_path=db_path)
    real_state.load_runtime_registry = Mock(return_value=_registry_with_keywords())  # type: ignore[method-assign]
    state_cm = MagicMock()
    state_cm.__enter__ = Mock(return_value=real_state)
    state_cm.__exit__ = Mock(return_value=False)
    msg = _message("agent", datetime.now(UTC))

    async def interrupting_score(
        *_args: object,
        **_kwargs: object,
    ) -> tuple[str, int, bool, list[PlatformFetchFailure]]:
        raise KeyboardInterrupt

    monkeypatch.setattr(scan_runner, "validate_config", lambda: [])
    monkeypatch.setattr(scan_runner, "fetch_messages", AsyncMock(return_value=([msg], [])))
    monkeypatch.setattr(scan_runner, "score_messages", interrupting_score)
    monkeypatch.setattr(scan_runner, "StateManager", Mock(return_value=state_cm))
    monkeypatch.setattr(scan_runner, "SQLiteTracer", Mock(return_value=_FakeTracer()))
    monkeypatch.setattr(scan_runner, "SQLiteFeedbackLoop", Mock(return_value=_FakeFeedback()))
    monkeypatch.setattr(
        scan_runner,
        "MODES",
        {"default": {"evaluate": "e", "respond": "r", "critique": "c"}},
    )

    args = Namespace(mode="default", rescore=None, rescore_failed=None, continuous=False)
    with pytest.raises(KeyboardInterrupt):
        await scan_runner.main_loop(args)

    scan_row = real_state.conn.execute(
        "SELECT status, safe_watermark_at, messages_scanned FROM scans ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert scan_row is not None
    assert scan_row["status"] == "interrupted"
    assert scan_row["safe_watermark_at"] is None
    assert scan_row["messages_scanned"] == 1

    real_state.close()


@pytest.mark.asyncio
async def test_main_loop_passes_configured_platform_query_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_state = _FakeState(registry=_empty_registry())
    fetch_messages_mock = AsyncMock(return_value=[])
    farcaster_ctor = Mock(return_value=object())
    bluesky_ctor = Mock(return_value=object())

    fetch_messages_mock = AsyncMock(return_value=([], []))

    monkeypatch.setattr(scan_runner, "validate_config", lambda: [])
    monkeypatch.setattr(scan_runner, "fetch_messages", fetch_messages_mock)
    monkeypatch.setattr(scan_runner, "StateManager", Mock(return_value=fake_state))
    monkeypatch.setattr(scan_runner, "FarcasterScanner", farcaster_ctor)
    monkeypatch.setattr(scan_runner, "BlueskyScanner", bluesky_ctor)
    monkeypatch.setattr(scan_runner, "NEYNAR_API_KEY", "neynar")
    monkeypatch.setattr(scan_runner, "NEYNAR_API_URL", "https://api.example")
    monkeypatch.setattr(scan_runner, "BLUESKY_API_URL", "https://bsky.example")
    monkeypatch.setattr(scan_runner, "BLUESKY_IDENTIFIER", "alice.example")
    monkeypatch.setattr(scan_runner, "BLUESKY_APP_PASSWORD", "password")
    monkeypatch.setattr(scan_runner, "DISCORD_BOT_TOKEN", "")
    monkeypatch.setattr(scan_runner, "DISCORD_SERVER_ID", 0)
    monkeypatch.setattr(scan_runner, "DISCORD_CHANNEL_IDS", [])
    monkeypatch.setattr(scan_runner, "FARCASTER_CHANNEL_IDS", [])
    monkeypatch.setattr(scan_runner, "BLUESKY_FEED_URIS", [])
    monkeypatch.setattr(scan_runner, "FARCASTER_MAX_RESULTS_PER_QUERY", 11)
    monkeypatch.setattr(scan_runner, "BLUESKY_MAX_RESULTS_PER_QUERY", 12)

    args = Namespace(mode="both", rescore=None, rescore_failed=None, continuous=False)

    await scan_runner.main_loop(args)

    farcaster_ctor.assert_called_once_with(
        api_key="neynar",
        channel_ids=None,
        max_results_per_query=11,
    )
    bluesky_ctor.assert_called_once_with(
        feed_uris=None,
        max_results_per_query=12,
    )


@pytest.mark.asyncio
async def test_main_loop_closes_feedback_and_tracer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_state = _FakeState(registry=_registry_with_keywords())
    fake_state.start_scan = Mock(return_value=1)
    fake_state.complete_scan = Mock()
    fake_state.commit = Mock()
    fake_state.get_recent_grading_signals = Mock(
        return_value=SimpleNamespace(
            total_graded=0,
            pass_count=0,
            false_positive_count=0,
            false_negative_count=0,
            fail_count=0,
            dimension_counts=[],
            posture_correction_count=0,
            factual_unsupported_count=0,
            factual_contradicted_count=0,
            recent_causal_examples=[],
        )
    )
    fake_state.get_recent_critique_feedback = Mock(return_value=[])

    fake_tracer = _FakeTracer()
    fake_feedback = _FakeFeedback()

    monkeypatch.setattr(scan_runner, "validate_config", lambda: [])
    monkeypatch.setattr(scan_runner, "fetch_messages", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(scan_runner, "StateManager", Mock(return_value=fake_state))
    monkeypatch.setattr(scan_runner, "SQLiteTracer", Mock(return_value=fake_tracer))
    monkeypatch.setattr(scan_runner, "SQLiteFeedbackLoop", Mock(return_value=fake_feedback))
    monkeypatch.setattr(
        scan_runner, "MODES",
        {"default": {"evaluate": "e", "respond": "r", "critique": "c"}},
    )

    args = Namespace(mode="default", rescore=None, rescore_failed=None, continuous=False)

    await scan_runner.main_loop(args)

    fake_feedback.close.assert_awaited_once()
    fake_tracer.flush.assert_awaited_once()
    fake_tracer.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_feedback_snapshot_recorded_before_score_messages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """evaluation-feedback/v1's shadow snapshot must be built and
    committed before the first model call — score_messages is the entry
    point into evaluation/drafting/critique, so record_feedback_snapshot
    must run first."""
    real_state = StateManager(db_path=str(tmp_path / "ordering.db"))
    call_order: list[str] = []

    real_record_feedback_snapshot = real_state.record_feedback_snapshot

    def spy_record_feedback_snapshot(scan_id: int, *, mode: str = "shadow"):
        call_order.append("record_feedback_snapshot")
        return real_record_feedback_snapshot(scan_id, mode=mode)

    monkeypatch.setattr(
        real_state, "record_feedback_snapshot", spy_record_feedback_snapshot
    )

    async def fake_score_messages(*args: object, **kwargs: object) -> tuple:
        call_order.append("score_messages")
        return "", 0, True, []

    _configure_main_loop_score_failure(monkeypatch, real_state, fake_score_messages)

    args = Namespace(mode="default", rescore=None, rescore_failed=None, continuous=False)
    await scan_runner.main_loop(args)

    assert call_order == ["record_feedback_snapshot", "score_messages"]

    snapshot_count = real_state.conn.execute(
        "SELECT COUNT(*) FROM feedback_snapshots"
    ).fetchone()[0]
    assert snapshot_count == 1

    real_state.close()


# ---------------------------------------------------------------------------
# Durability: overflow persistence, per-message transactions, digest isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overflow_messages_saved_as_unevaluated_posts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Messages beyond SCAN_MAX_NEW_MESSAGES must be persisted without evaluations.

    The cap must limit scoring work only, not message durability. Overflow
    posts saved without evaluations are recoverable via load_unevaluated_posts.
    """
    now = datetime.now(UTC)
    msgs = [
        _message("newest", now),
        _message("middle", now - timedelta(minutes=1)),
        _message("oldest", now - timedelta(minutes=2)),
    ]

    db_path = str(tmp_path / "overflow.db")

    # Build a context-manager shim that wraps the real StateManager but
    # does NOT close the connection on __exit__ — we need to query it after
    # main_loop returns.
    real_state = StateManager(db_path=db_path)
    state_cm = MagicMock()
    state_cm.__enter__ = Mock(return_value=real_state)
    state_cm.__exit__ = Mock(return_value=False)

    fake_tracer = _FakeTracer()
    fake_feedback = _FakeFeedback()

    monkeypatch.setattr(scan_runner, "SCAN_MAX_NEW_MESSAGES", 1)
    monkeypatch.setattr(scan_runner, "validate_config", lambda: [])
    monkeypatch.setattr(scan_runner, "fetch_messages", AsyncMock(return_value=(msgs, [])))
    monkeypatch.setattr(scan_runner, "StateManager", Mock(return_value=state_cm))
    monkeypatch.setattr(scan_runner, "SQLiteTracer", Mock(return_value=fake_tracer))
    monkeypatch.setattr(scan_runner, "SQLiteFeedbackLoop", Mock(return_value=fake_feedback))
    monkeypatch.setattr(
        scan_runner, "MODES",
        {"default": {"evaluate": "e", "respond": "r", "critique": "c"}},
    )
    monkeypatch.setattr(scan_runner, "write_digest_header", Mock())
    monkeypatch.setattr(scan_runner, "finalize_digest", Mock(return_value=""))

    args = Namespace(mode="default", rescore=None, rescore_failed=None, continuous=False)
    await scan_runner.main_loop(args)

    all_posts = real_state.load_posts()
    assert len(all_posts) == 3, (
        f"Expected 3 posts (1 scored + 2 overflow), got {len(all_posts)}: "
        f"{[p.platform_id for p in all_posts]}"
    )

    unevaluated = real_state.load_unevaluated_posts()
    unevaluated_ids = {p.platform_id for p in unevaluated}
    assert {"middle", "oldest"}.issubset(unevaluated_ids), (
        f"Overflow messages missing from unevaluated: {unevaluated_ids}"
    )

    real_state.close()


@pytest.mark.asyncio
async def test_per_message_exception_preserves_prior_posts(
    monkeypatch: pytest.MonkeyPatch,
    in_memory_state: StateManager,
    tmp_path,
) -> None:
    """A scoring exception for message N must not undo persisted posts 1..N-1.

    score_messages must catch per-message exceptions internally so prior
    committed posts survive and the scan can continue.
    """
    now = datetime.now(UTC)
    msg1 = _message("msg1", now)
    msg2 = _message("msg2", now - timedelta(minutes=1))

    scan_id = in_memory_state.start_scan()
    in_memory_state.commit()

    call_count = 0

    async def fake_run_pipeline(pipeline, *, input, context):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            result = Mock()
            result.step_outputs = {
                "score_and_draft": Err(Mock(detail="scoring_fail", operation="test"))
            }
            return result
        raise RuntimeError("pipeline crashed for msg2")

    monkeypatch.setattr(scan_runner, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(scan_runner, "build_scout_pipeline", Mock(return_value=Mock()))
    monkeypatch.setattr(scan_runner, "build_scout_phase_configs", Mock(return_value=Mock()))
    monkeypatch.setattr(scan_runner, "write_digest_header", Mock())
    monkeypatch.setattr(scan_runner, "finalize_digest", Mock(return_value=""))

    base_mode: ModeConfig = {"evaluate": "e", "respond": "r", "critique": "c"}
    routed = [
        RoutedMessage(message=msg1, keyword_route=None),
        RoutedMessage(message=msg2, keyword_route=None),
    ]

    # After fix: score_messages must complete without re-raising the per-message exception.
    _, _, _, processing_failures = await scan_runner.score_messages(
        routed,
        [msg1, msg2],
        base_mode,
        {}, {},
        "model", "model", "model",
        Mock(), Mock(),
        in_memory_state,
        scan_id,
        str(tmp_path / "digest.md"),
        feedback_snapshot=in_memory_state.record_feedback_snapshot(scan_id, mode="shadow"),
    )

    all_posts = in_memory_state.load_posts()
    assert any(p.platform_id == "msg1" for p in all_posts), (
        "msg1 must be persisted even though msg2 raised a scoring exception"
    )
    assert [f.kind for f in processing_failures] == ["scoring_error", "scoring_error"]


@pytest.mark.asyncio
async def test_blocked_author_is_persisted_but_never_sent_to_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    in_memory_state: StateManager,
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    msg = _message("blocked-poster", now)
    scan_id = in_memory_state.start_scan()
    in_memory_state.block_author(
        platform=msg.platform,
        author_id=msg.author_id,
        author_name=msg.author_name,
        reason="link aggregator",
    )
    run_mock = AsyncMock()

    monkeypatch.setattr(scan_runner, "run_pipeline", run_mock)
    monkeypatch.setattr(scan_runner, "build_scout_pipeline", Mock(return_value=Mock()))
    monkeypatch.setattr(scan_runner, "build_scout_phase_configs", Mock(return_value=Mock()))
    monkeypatch.setattr(scan_runner, "write_digest_header", Mock())
    monkeypatch.setattr(scan_runner, "finalize_digest", Mock(return_value=""))

    routed = [RoutedMessage(message=msg, keyword_route=None)]
    _, relevant_count, _, processing_failures = await scan_runner.score_messages(
        routed,
        [msg],
        {"evaluate": "e", "respond": "r", "critique": "c"},
        {},
        {},
        "model",
        "model",
        "model",
        Mock(),
        Mock(),
        in_memory_state,
        scan_id,
        str(tmp_path / "digest.md"),
        feedback_snapshot=in_memory_state.record_feedback_snapshot(scan_id, mode="shadow"),
    )

    assert run_mock.await_count == 0
    assert relevant_count == 0
    assert processing_failures == []
    post_row = in_memory_state.conn.execute(
        "SELECT id FROM posts WHERE platform_msg_id = ?", (msg.platform_id,)
    ).fetchone()
    assert post_row is not None
    evaluation_count = in_memory_state.conn.execute(
        "SELECT COUNT(*) FROM evaluations WHERE post_id = ?", (post_row["id"],)
    ).fetchone()[0]
    assert evaluation_count == 0


@pytest.mark.asyncio
async def test_digest_failure_preserves_committed_posts(
    monkeypatch: pytest.MonkeyPatch,
    in_memory_state: StateManager,
    tmp_path,
) -> None:
    """A finalize_digest exception must not destroy committed posts.

    Database commits must occur before digest finalization so that a digest
    write failure leaves all scored posts durable and recoverable.
    """
    now = datetime.now(UTC)
    msg = _message("persist-me", now)

    scan_id = in_memory_state.start_scan()
    in_memory_state.commit()

    pipeline_result = Mock()
    pipeline_result.step_outputs = {
        "score_and_draft": Err(Mock(detail="test", operation="op"))
    }

    monkeypatch.setattr(scan_runner, "run_pipeline", AsyncMock(return_value=pipeline_result))
    monkeypatch.setattr(scan_runner, "build_scout_pipeline", Mock(return_value=Mock()))
    monkeypatch.setattr(scan_runner, "build_scout_phase_configs", Mock(return_value=Mock()))
    monkeypatch.setattr(scan_runner, "write_digest_header", Mock())
    monkeypatch.setattr(
        scan_runner, "finalize_digest", Mock(side_effect=OSError("disk full"))
    )

    base_mode: ModeConfig = {"evaluate": "e", "respond": "r", "critique": "c"}
    routed = [RoutedMessage(message=msg, keyword_route=None)]

    # After fix: score_messages must complete (catches digest exception) and return.
    digest, relevant_count, digest_ok, processing_failures = await scan_runner.score_messages(
        routed,
        [msg],
        base_mode,
        {}, {},
        "model", "model", "model",
        Mock(), Mock(),
        in_memory_state,
        scan_id,
        str(tmp_path / "digest.md"),
        feedback_snapshot=in_memory_state.record_feedback_snapshot(scan_id, mode="shadow"),
    )

    posts = in_memory_state.load_posts()
    assert any(p.platform_id == "persist-me" for p in posts), (
        "Post must survive a digest finalization failure"
    )
    assert not digest_ok, "digest_ok must be False when finalize_digest raised"
    assert any(f.kind == "digest_error" for f in processing_failures)


@pytest.mark.asyncio
async def test_surfaced_draft_persists_and_digests_exact_verifier_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """validated_text must be the verifier's exact bytes at persistence and digest.

    A fake verify_draft_content returns unicode text (an em dash and a
    multibyte emoji). scan_runner must never reformat, re-assemble, or fall
    back to a pre-verification draft artifact — the gate's
    assembled_text is the one artifact byte-for-byte identical at the
    persist_surfaced_outcome call and the digest block, and the gate runs
    exactly once (inside classify_outcome).
    """
    unicode_text = "Check out the gateway — a full 🔒 rollout awaits you."

    now = datetime.now(UTC)
    msg = _message("unicode-msg", now)

    candidate = ReplyCandidate(
        relevant=True,
        score=0.95,
        reason="direct fit",
        relevant_to=["gw"],
        project_key="gw",
        structured_draft=StructuredDraftOutput(
            posture="answer",
            segments=[
                DeclarativeSegment(type="declarative", fact_id="fact-1", text="placeholder")
            ],
            claims=["placeholder"],
            resources_used=[],
        ),
    )

    async def fake_run_pipeline(
        pipeline: object, *, input: object, context: dict[str, object]
    ) -> Mock:
        assert context["dossier_summaries"] == {"gw": dossier}
        result = Mock()
        result.step_outputs = {"score_and_draft": Ok(candidate)}
        return result

    verify_calls: list[dict[str, object]] = []

    def fake_verify_draft_content(**kwargs: object) -> VerifyResult:
        verify_calls.append(kwargs)
        return VerifyResult(ok=True, violations=[], assembled_text=unicode_text)

    dossier = DossierSummary(
        project_key="gw",
        last_reviewed=now.date(),
        reviewer="tester",
        facts=[],
        resources=[],
        prohibitions=[],
    )

    fake_state = Mock()
    fake_state.save_post = Mock(return_value=1)
    fake_state.persist_surfaced_outcome = Mock(return_value=(1, 1, 1))
    fake_state.persist_terminal_outcome = Mock()

    monkeypatch.setattr(scan_runner, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(scan_runner, "build_scout_pipeline", Mock(return_value=Mock()))
    monkeypatch.setattr(scan_runner, "build_scout_phase_configs", Mock(return_value=Mock()))
    monkeypatch.setattr(scan_runner, "verify_draft_content", fake_verify_draft_content)

    base_mode: ModeConfig = {"evaluate": "e", "respond": "r", "critique": "c"}
    routed = [RoutedMessage(message=msg, keyword_route=None)]

    digest, relevant_count, digest_ok, processing_failures = await scan_runner.score_messages(
        routed,
        [msg],
        base_mode,
        {}, {},
        "model", "model", "model",
        Mock(), Mock(),
        fake_state,
        1,
        str(tmp_path / "digest.md"),
        dossier_summaries={"gw": dossier},
        feedback_snapshot=_fake_feedback_snapshot(),
    )

    assert digest_ok
    assert relevant_count == 1
    assert processing_failures == []
    assert len(verify_calls) == 1, "the content gate must run exactly once per draft"

    fake_state.persist_terminal_outcome.assert_not_called()
    fake_state.persist_surfaced_outcome.assert_called_once()
    persisted_kwargs = fake_state.persist_surfaced_outcome.call_args.kwargs
    assert persisted_kwargs["comment_text"].encode("utf-8") == unicode_text.encode("utf-8")

    assert unicode_text.encode("utf-8") in digest.encode("utf-8")


@pytest.mark.asyncio
async def test_main_loop_cleanup_emits_no_runtime_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main_loop must not emit RuntimeWarning: coroutine was never awaited.

    The bug is observable even when functional output looks correct: feedback.close
    and tracer.close return coroutines that must be awaited, not called synchronously.
    """
    import warnings

    fake_state = _FakeState(registry=_registry_with_keywords())
    fake_state.start_scan = Mock(return_value=1)
    fake_state.complete_scan = Mock()
    fake_state.commit = Mock()
    fake_state.get_recent_grading_signals = Mock(
        return_value=SimpleNamespace(
            total_graded=0,
            pass_count=0,
            false_positive_count=0,
            false_negative_count=0,
            fail_count=0,
            dimension_counts=[],
            posture_correction_count=0,
            factual_unsupported_count=0,
            factual_contradicted_count=0,
            recent_causal_examples=[],
        )
    )
    fake_state.get_recent_critique_feedback = Mock(return_value=[])

    fake_tracer = _FakeTracer()
    fake_feedback = _FakeFeedback()

    monkeypatch.setattr(scan_runner, "validate_config", lambda: [])
    monkeypatch.setattr(scan_runner, "fetch_messages", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(scan_runner, "StateManager", Mock(return_value=fake_state))
    monkeypatch.setattr(scan_runner, "SQLiteTracer", Mock(return_value=fake_tracer))
    monkeypatch.setattr(scan_runner, "SQLiteFeedbackLoop", Mock(return_value=fake_feedback))
    monkeypatch.setattr(
        scan_runner, "MODES",
        {"default": {"evaluate": "e", "respond": "r", "critique": "c"}},
    )

    args = Namespace(mode="default", rescore=None, rescore_failed=None, continuous=False)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await scan_runner.main_loop(args)

    runtime_warnings = [
        w for w in caught
        if issubclass(w.category, RuntimeWarning)
        and "coroutine" in str(w.message).lower()
        and "never awaited" in str(w.message).lower()
    ]
    assert runtime_warnings == [], (
        f"main_loop cleanup emitted unawaited-coroutine warnings: "
        f"{[str(w.message) for w in runtime_warnings]}"
    )


# ---------------------------------------------------------------------------
# Operator-visible scan outcomes: failures, partial status, overflow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_failure_recorded_as_partial_scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Platform fetch failures must be saved to scan_fetch_failures and set status=partial."""
    from scout.errors import PlatformFetchFailure

    db_path = str(tmp_path / "failure.db")
    real_state = StateManager(db_path=db_path)
    state_cm = MagicMock()
    state_cm.__enter__ = Mock(return_value=real_state)
    state_cm.__exit__ = Mock(return_value=False)

    failure = PlatformFetchFailure(
        platform="bluesky",
        kind="rate_limited",
        message="Too many requests",
        http_status=429,
        retry_after="30",
        retryable=True,
    )

    fake_tracer = _FakeTracer()
    fake_feedback = _FakeFeedback()

    now = datetime.now(UTC)
    monkeypatch.setattr(scan_runner, "validate_config", lambda: [])
    # Provide one message so the scan proceeds (not skipped as zero-work).
    monkeypatch.setattr(
        scan_runner, "fetch_messages",
        AsyncMock(return_value=([_message("m1", now)], [failure]))
    )
    monkeypatch.setattr(scan_runner, "StateManager", Mock(return_value=state_cm))
    monkeypatch.setattr(scan_runner, "SQLiteTracer", Mock(return_value=fake_tracer))
    monkeypatch.setattr(scan_runner, "SQLiteFeedbackLoop", Mock(return_value=fake_feedback))
    monkeypatch.setattr(
        scan_runner, "MODES",
        {"default": {"evaluate": "e", "respond": "r", "critique": "c"}},
    )
    monkeypatch.setattr(scan_runner, "write_digest_header", Mock())
    monkeypatch.setattr(scan_runner, "finalize_digest", Mock(return_value=""))

    args = Namespace(mode="default", rescore=None, rescore_failed=None, continuous=False)
    await scan_runner.main_loop(args)

    scan_row = real_state.conn.execute(
        "SELECT status FROM scans ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert scan_row is not None
    assert scan_row["status"] == "partial", (
        f"Expected status=partial for scan with fetch failure, got {scan_row['status']}"
    )

    failures = real_state.conn.execute(
        "SELECT platform, kind, http_status FROM scan_fetch_failures"
    ).fetchall()
    assert len(failures) == 1
    assert failures[0]["platform"] == "bluesky"
    assert failures[0]["kind"] == "rate_limited"
    assert failures[0]["http_status"] == 429

    real_state.close()


@pytest.mark.asyncio
async def test_overflow_count_persisted_in_scan_row(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Overflow count must be written to the scans.overflow_count column."""
    now = datetime.now(UTC)
    msgs = [
        _message("newest", now),
        _message("middle", now - timedelta(minutes=1)),
        _message("oldest", now - timedelta(minutes=2)),
    ]

    db_path = str(tmp_path / "overflow_count.db")
    real_state = StateManager(db_path=db_path)
    state_cm = MagicMock()
    state_cm.__enter__ = Mock(return_value=real_state)
    state_cm.__exit__ = Mock(return_value=False)

    fake_tracer = _FakeTracer()
    fake_feedback = _FakeFeedback()

    monkeypatch.setattr(scan_runner, "SCAN_MAX_NEW_MESSAGES", 1)
    monkeypatch.setattr(scan_runner, "validate_config", lambda: [])
    monkeypatch.setattr(scan_runner, "fetch_messages", AsyncMock(return_value=(msgs, [])))
    monkeypatch.setattr(scan_runner, "StateManager", Mock(return_value=state_cm))
    monkeypatch.setattr(scan_runner, "SQLiteTracer", Mock(return_value=fake_tracer))
    monkeypatch.setattr(scan_runner, "SQLiteFeedbackLoop", Mock(return_value=fake_feedback))
    monkeypatch.setattr(
        scan_runner, "MODES",
        {"default": {"evaluate": "e", "respond": "r", "critique": "c"}},
    )
    monkeypatch.setattr(scan_runner, "write_digest_header", Mock())
    monkeypatch.setattr(scan_runner, "finalize_digest", Mock(return_value=""))

    args = Namespace(mode="default", rescore=None, rescore_failed=None, continuous=False)
    await scan_runner.main_loop(args)

    scan_row = real_state.conn.execute(
        "SELECT overflow_count FROM scans ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert scan_row is not None
    assert scan_row["overflow_count"] == 2, (
        f"Expected overflow_count=2 (3 msgs - cap 1), got {scan_row['overflow_count']}"
    )

    real_state.close()


@pytest.mark.asyncio
async def test_digest_failure_recorded_as_partial_scan(
    monkeypatch: pytest.MonkeyPatch,
    in_memory_state: StateManager,
    tmp_path,
) -> None:
    """A finalize_digest failure must set status=partial and save a digest_error failure."""
    now = datetime.now(UTC)
    msg = _message("digest-fail-msg", now)

    scan_id = in_memory_state.start_scan()
    in_memory_state.commit()

    pipeline_result = Mock()
    pipeline_result.step_outputs = {
        "score_and_draft": Err(Mock(detail="test", operation="op"))
    }

    monkeypatch.setattr(scan_runner, "run_pipeline", AsyncMock(return_value=pipeline_result))
    monkeypatch.setattr(scan_runner, "build_scout_pipeline", Mock(return_value=Mock()))
    monkeypatch.setattr(scan_runner, "build_scout_phase_configs", Mock(return_value=Mock()))
    monkeypatch.setattr(scan_runner, "write_digest_header", Mock())
    monkeypatch.setattr(
        scan_runner, "finalize_digest", Mock(side_effect=OSError("disk full"))
    )

    base_mode: ModeConfig = {"evaluate": "e", "respond": "r", "critique": "c"}
    routed = [RoutedMessage(message=msg, keyword_route=None)]

    _, _, digest_ok, processing_failures = await scan_runner.score_messages(
        routed, [msg], base_mode, {}, {},
        "model", "model", "model",
        Mock(), Mock(), in_memory_state, scan_id,
        str(tmp_path / "digest.md"),
        feedback_snapshot=in_memory_state.record_feedback_snapshot(scan_id, mode="shadow"),
    )

    assert not digest_ok, "digest_ok must be False when finalize_digest raised"
    assert any(f.kind == "digest_error" for f in processing_failures)


def _registry_with_unready_project() -> RuntimeRegistry:
    """One active project with a dossier_summary_id set but SCOUT_DOSSIER_ROOT
    unconfigured — the cheapest way to force a deterministic, single readiness
    error without building a real dossier-source checkout."""
    project = ProjectTarget(
        key="gw", name="Gateway", description="d", link="l", dossier_summary_id="gw-dossier"
    )
    return RuntimeRegistry(projects={"gw": project}, keywords=(), prompt_templates={})


@pytest.mark.asyncio
async def test_main_loop_one_shot_exits_on_dossier_readiness_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One-shot mode preserves the existing terminate-before-scanning behavior."""
    fake_state = _FakeState(registry=_registry_with_unready_project())
    fetch_messages_mock = AsyncMock(return_value=([], []))

    monkeypatch.setattr(scan_runner, "validate_config", lambda: [])
    monkeypatch.setattr(scan_runner, "fetch_messages", fetch_messages_mock)
    monkeypatch.setattr(scan_runner, "StateManager", Mock(return_value=fake_state))
    monkeypatch.setattr(scan_runner, "SQLiteTracer", Mock())
    monkeypatch.setattr(scan_runner, "SQLiteFeedbackLoop", Mock())
    monkeypatch.setattr(config, "SCOUT_DOSSIER_ROOT", "")

    args = Namespace(mode="both", rescore=None, rescore_failed=None, continuous=False)

    with (
        caplog.at_level(logging.ERROR, logger="scout.scanning.runner"),
        pytest.raises(SystemExit) as exc_info,
    ):
        await scan_runner.main_loop(args)

    assert exc_info.value.code == 1
    fetch_messages_mock.assert_not_awaited()
    fake_state.start_scan.assert_not_called()
    assert any("dossier_readiness_failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_main_loop_continuous_retries_after_backoff_on_dossier_readiness_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Continuous mode must survive a readiness failure: log it, sleep once
    for the normal scan interval, and take a fresh registry/readiness pass
    (S-008.a — it previously exited the daemon outright)."""
    fake_state = _FakeState(registry=_registry_with_unready_project())
    fake_state.load_runtime_registry = Mock(
        side_effect=[_registry_with_unready_project(), _empty_registry()]
    )
    fetch_messages_mock = AsyncMock(return_value=([], []))

    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr(scan_runner, "validate_config", lambda: [])
    monkeypatch.setattr(scan_runner, "fetch_messages", fetch_messages_mock)
    monkeypatch.setattr(scan_runner, "StateManager", Mock(return_value=fake_state))
    monkeypatch.setattr(scan_runner, "SQLiteTracer", Mock())
    monkeypatch.setattr(scan_runner, "SQLiteFeedbackLoop", Mock())
    monkeypatch.setattr(config, "SCOUT_DOSSIER_ROOT", "")
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    args = Namespace(mode="both", rescore=None, rescore_failed=None, continuous=True)

    with (
        caplog.at_level(logging.ERROR, logger="scout.scanning.runner"),
        pytest.raises(asyncio.CancelledError),
    ):
        await scan_runner.main_loop(args)

    # Exactly one backoff delay before the retry, at the normal scan cadence.
    assert sleep_calls[0] == SCAN_INTERVAL_HOURS * 3600
    # A fresh registry/readiness pass was taken after the delay.
    assert fake_state.load_runtime_registry.call_count == 2
    # No scan row was created on the failed iteration.
    fake_state.start_scan.assert_not_called()
    assert any("dossier_readiness_failed" in r.message for r in caplog.records)

    # Second iteration's readiness (now repaired to an empty registry) makes
    # progress — exactly one platform fetch, from the second iteration only —
    # and the loop reaches the normal end-of-scan sleep instead of failing
    # readiness again.
    fetch_messages_mock.assert_awaited_once()
    assert len(sleep_calls) == 2


# ---------------------------------------------------------------------------
# classify_outcome / persist_outcome integration regressions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keyword_prefilter_false_unrouted_relevant_gate_blocked(
    monkeypatch: pytest.MonkeyPatch,
    in_memory_state: StateManager,
    tmp_path: Path,
) -> None:
    """KEYWORD_PREFILTER=false: an unrouted relevant message (identified only
    via relevant_to) must classify through relevant_to[0], not raise.

    The draft is deliberately made to fail the content gate so this test
    stays independent of the still-broken S-001 surfaced-persistence
    transaction. It proves the supported KEYWORD_PREFILTER=false
    configuration reaches a terminal gate_blocked evaluation with
    project_key='agent-ops' instead of turning an unrouted relevant
    candidate into a permanent scoring_error poison message.
    """
    now = datetime.now(UTC)
    msg = _message("unrouted-agent-ops", now)

    scan_id = in_memory_state.start_scan()
    post_id = in_memory_state.save_post(msg, scan_id)
    in_memory_state.commit()
    feedback_snapshot = in_memory_state.record_feedback_snapshot(scan_id, mode="shadow")
    contributor_ids = tuple(
        in_memory_state.insert_phase_run(
            scan_id=scan_id, post_id=post_id, snapshot_phase_id=phase.snapshot_phase_id,
            phase=phase.phase, trace_id=f"test-trace-{phase.phase}", model="test-model",
            status="complete",
        )
        for phase in feedback_snapshot.phases
    )

    candidate = ReplyCandidate(
        relevant=True,
        score=0.9,
        reason="direct fit for agent-ops",
        relevant_to=["agent-ops"],
        project_key=None,
        structured_draft=StructuredDraftOutput(
            posture="answer",
            segments=[
                DeclarativeSegment(
                    type="declarative", fact_id="unknown-fact", text="placeholder"
                )
            ],
            claims=["placeholder"],
            resources_used=[],
        ),
        contributor_phase_run_ids=contributor_ids,
    )

    async def fake_run_pipeline(pipeline: object, *, input: object, context: object) -> Mock:
        result = Mock()
        result.step_outputs = {"score_and_draft": Ok(candidate)}
        return result

    dossier = DossierSummary(
        project_key="agent-ops",
        last_reviewed=now.date(),
        reviewer="tester",
        facts=[],
        resources=[],
        prohibitions=[],
    )

    monkeypatch.setattr(scan_runner, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(scan_runner, "build_scout_pipeline", Mock(return_value=Mock()))
    monkeypatch.setattr(scan_runner, "build_scout_phase_configs", Mock(return_value=Mock()))
    monkeypatch.setattr(scan_runner, "write_digest_header", Mock())
    monkeypatch.setattr(scan_runner, "finalize_digest", Mock(return_value=""))

    base_mode: ModeConfig = {"evaluate": "e", "respond": "r", "critique": "c"}
    # keyword_route=None models the KEYWORD_PREFILTER=false path: nothing
    # matched a keyword, but the message still reached scoring.
    routed = [RoutedMessage(message=msg, keyword_route=None)]

    digest, relevant_count, digest_ok, processing_failures = await scan_runner.score_messages(
        routed,
        [msg],
        base_mode,
        {}, {},
        "model", "model", "model",
        Mock(), Mock(),
        in_memory_state,
        scan_id,
        str(tmp_path / "digest.md"),
        dossier_summaries={"agent-ops": dossier},
        feedback_snapshot=feedback_snapshot,
    )

    assert relevant_count == 0
    assert processing_failures == [], (
        "an unrouted relevant candidate must not produce a retryable scoring_error"
    )

    row = in_memory_state.conn.execute(
        "SELECT surface_status, project_key FROM evaluations ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row["surface_status"] == "gate_blocked"
    assert row["project_key"] == "agent-ops"


@pytest.mark.asyncio
async def test_page_ceiling_only_failure_scan_stays_partial_and_reuses_watermark(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """A fetch_failures list containing only a page_ceiling entry must keep
    the scan partial with no watermark advance, and the following scan must
    still ask 'since' the prior safe boundary rather than skip past
    whatever rows the ceiling hid."""
    db_path = str(tmp_path / "page_ceiling_watermark.db")
    real_state = StateManager(db_path=db_path)
    state_cm = MagicMock()
    state_cm.__enter__ = Mock(return_value=real_state)
    state_cm.__exit__ = Mock(return_value=False)

    fake_tracer = _FakeTracer()
    fake_feedback = _FakeFeedback()

    monkeypatch.setattr(scan_runner, "validate_config", lambda: [])
    monkeypatch.setattr(scan_runner, "StateManager", Mock(return_value=state_cm))
    monkeypatch.setattr(scan_runner, "SQLiteTracer", Mock(return_value=fake_tracer))
    monkeypatch.setattr(scan_runner, "SQLiteFeedbackLoop", Mock(return_value=fake_feedback))
    monkeypatch.setattr(
        scan_runner, "MODES",
        {"default": {"evaluate": "e", "respond": "r", "critique": "c"}},
    )
    monkeypatch.setattr(scan_runner, "write_digest_header", Mock())
    monkeypatch.setattr(scan_runner, "finalize_digest", Mock(return_value=""))

    args = Namespace(mode="default", rescore=None, rescore_failed=None, continuous=False)

    # First scan: one message, no failures — a real complete scan that
    # establishes a safe watermark to anchor against.
    monkeypatch.setattr(
        scan_runner, "fetch_messages",
        AsyncMock(return_value=([_message("m1", datetime.now(UTC))], [])),
    )
    await scan_runner.main_loop(args)

    first_watermark = real_state.get_last_scan_timestamp()
    assert first_watermark is not None

    # Second scan: only a page_ceiling failure, no messages — some upstream
    # rows may be beyond the fetched pages, so the scan must stay partial.
    page_ceiling_failure = PlatformFetchFailure(
        platform="discord",
        kind="page_ceiling",
        message="Page ceiling reached; fetched 50 messages",
        context="channel_history",
        retryable=True,
    )
    monkeypatch.setattr(
        scan_runner, "fetch_messages",
        AsyncMock(return_value=([], [page_ceiling_failure])),
    )
    await scan_runner.main_loop(args)

    scan_row = real_state.conn.execute(
        "SELECT id, status, safe_watermark_at FROM scans ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert scan_row is not None
    assert scan_row["status"] == "partial"
    assert scan_row["safe_watermark_at"] is None

    failures = real_state.get_scan_fetch_failures(scan_row["id"])
    assert len(failures) == 1
    assert failures[0]["kind"] == "page_ceiling"

    # The next scan reuses the first scan's safe boundary — the page-ceiling
    # scan's null watermark is skipped entirely by get_last_scan_timestamp.
    assert real_state.get_last_scan_timestamp() == first_watermark

    real_state.close()


# ---------------------------------------------------------------------------
# T-001: classify_outcome -> persist_outcome full-transaction integration
# matrix, real file-backed StateManager, real dossier. No mocks of
# StateManager, persist_surfaced_outcome, SQLite, verifier assembly, or the
# outcome adapter.
# ---------------------------------------------------------------------------

_T001_PROJECT_KEY = "gw"
_T001_FACT_ID = "t001-fact"
_T001_RESOURCE_ID = "t001-resource"
_T001_RESOURCE_LABEL = "Gateway Docs"
_T001_RESOURCE_URL = "https://gateway.example.com/docs"
# Unicode (multibyte emoji + em dash) so byte-for-byte assertions are load-bearing.
_T001_SAFE_PHRASING = "🔒 Our gateway keeps agent signups secure — full stop."


def _t001_dossier() -> DossierSummary:
    return DossierSummary(
        project_key=_T001_PROJECT_KEY,
        last_reviewed=date.today(),
        reviewer="tester",
        facts=[DossierFact(
            id=_T001_FACT_ID,
            text=f"Background: {_T001_SAFE_PHRASING}",
            safe_phrasings=[_T001_SAFE_PHRASING],
            immutable_evidence=["https://source.example.com/evidence"],
        )],
        resources=[DossierResource(
            id=_T001_RESOURCE_ID,
            label=_T001_RESOURCE_LABEL,
            canonical_url=_T001_RESOURCE_URL,
            immutable_evidence=["https://source.example.com/evidence"],
        )],
        prohibitions=[],
    )


def _t001_candidate(
    *, with_critique: bool = False, contributor_phase_run_ids: tuple[int, ...] = ()
) -> ReplyCandidate:
    """A resource-bearing, unicode-text candidate that passes every content gate."""
    return ReplyCandidate(
        relevant=True,
        score=0.95,
        reason="direct fit",
        relevant_to=[_T001_PROJECT_KEY],
        project_key=_T001_PROJECT_KEY,
        critique_verdict="approve" if with_critique else None,
        critique_feedback="Solid, on-topic reply." if with_critique else None,
        structured_draft=StructuredDraftOutput(
            posture="answer",
            segments=[
                DeclarativeSegment(
                    type="declarative", fact_id=_T001_FACT_ID, text=_T001_SAFE_PHRASING
                ),
                ResourceSegment(type="resource", resource_id=_T001_RESOURCE_ID),
            ],
            claims=[_T001_SAFE_PHRASING],
            resources_used=[_T001_RESOURCE_ID],
        ),
        contributor_phase_run_ids=contributor_phase_run_ids,
    )


def _t001_context(post_id: int, scan_id: int, msg: Message) -> scan_runner.PersistenceContext:
    return scan_runner.PersistenceContext(
        post_id=post_id,
        scan_id=scan_id,
        keyword_route_id=None,
        dossier_revision="rev-1",
        dossier_summary_id="gw-dossier",
        surfaced_at=msg.created_at.isoformat(),
    )


def _assert_t001_surfaced_outcome(
    state: StateManager,
    evaluation_id: int,
    post_id: int,
    scan_id: int,
    decision: scan_runner.OutcomeDecision,
    *,
    expect_critique: bool,
) -> None:
    """Assert the exact happy-path row shape for one surfaced T-001 outcome."""
    assert decision.validated_text is not None
    expected_bytes = (
        f"{_T001_SAFE_PHRASING} Resource: {_T001_RESOURCE_LABEL} — {_T001_RESOURCE_URL}"
    ).encode()
    assert decision.validated_text.encode("utf-8") == expected_bytes

    eval_rows = state.conn.execute(
        "SELECT * FROM evaluations WHERE id = ?", (evaluation_id,)
    ).fetchall()
    assert len(eval_rows) == 1
    eval_row = eval_rows[0]
    assert eval_row["surface_status"] == "surfaced"
    assert eval_row["post_id"] == post_id
    assert eval_row["scan_id"] == scan_id
    assert eval_row["project_key"] == _T001_PROJECT_KEY
    assert eval_row["dossier_revision"] == "rev-1"
    assert eval_row["dossier_summary_id"] == "gw-dossier"

    draft_rows = state.conn.execute(
        "SELECT * FROM draft_comments WHERE evaluation_id = ?", (evaluation_id,)
    ).fetchall()
    assert len(draft_rows) == 1
    draft_row = draft_rows[0]
    assert draft_row["post_id"] == post_id
    assert draft_row["comment_text"].encode("utf-8") == expected_bytes
    assert draft_row["comment_text"].encode("utf-8") == decision.validated_text.encode("utf-8")

    event_rows = state.conn.execute(
        "SELECT * FROM surfaced_events WHERE evaluation_id = ?", (evaluation_id,)
    ).fetchall()
    assert len(event_rows) == 1
    event_row = event_rows[0]
    assert event_row["post_id"] == post_id
    assert event_row["draft_id"] == draft_row["id"]

    critique_rows = state.conn.execute(
        "SELECT * FROM critiques WHERE evaluation_id = ?", (evaluation_id,)
    ).fetchall()
    if expect_critique:
        assert len(critique_rows) == 1
        assert critique_rows[0]["draft_id"] == draft_row["id"]
        assert critique_rows[0]["verdict"] == "approve"
    else:
        assert critique_rows == []

    gate_block_count = state.conn.execute(
        "SELECT COUNT(*) FROM gate_blocks WHERE evaluation_id = ?", (evaluation_id,)
    ).fetchone()[0]
    assert gate_block_count == 0

    scoring_error_count = state.conn.execute(
        "SELECT COUNT(*) FROM scan_fetch_failures WHERE kind = 'scoring_error'"
    ).fetchone()[0]
    assert scoring_error_count == 0


def _post_id_by_platform_msg_id(state: StateManager, platform_msg_id: str) -> int:
    row = state.conn.execute(
        "SELECT id FROM posts WHERE platform_msg_id = ?", (platform_msg_id,)
    ).fetchone()
    assert row is not None
    return int(row["id"])


def _t001_projects() -> dict[str, ProjectTarget]:
    return {
        _T001_PROJECT_KEY: ProjectTarget(
            key=_T001_PROJECT_KEY,
            name="Gateway",
            description="desc",
            link="https://gateway.example.com",
            dossier_summary_id="gw-dossier",
        )
    }


@pytest.mark.asyncio
async def test_outcome_persistence_failure_marks_scan_failed_preserves_prior_post_reraises(
    monkeypatch: pytest.MonkeyPatch,
    in_memory_state: StateManager,
    tmp_path: Path,
) -> None:
    """Post A/B durability: A's outcome persists fully; B's outcome
    persistence fails, so B keeps only its saved post (no evaluation,
    draft, or event rows). The scan is marked status='failed' with B's
    post id and the error, and the failure re-raises out of
    score_messages rather than degrading to a "partial" success.
    """
    now = datetime.now(UTC)
    msg_a = _message("t001-durab-a", now)
    msg_b = _message("t001-durab-b", now - timedelta(minutes=1))

    scan_id = in_memory_state.start_scan()
    post_a_id_seed = in_memory_state.save_post(msg_a, scan_id)
    in_memory_state.commit()
    feedback_snapshot = in_memory_state.record_feedback_snapshot(scan_id, mode="shadow")
    contributor_ids = tuple(
        in_memory_state.insert_phase_run(
            scan_id=scan_id, post_id=post_a_id_seed, snapshot_phase_id=phase.snapshot_phase_id,
            phase=phase.phase, trace_id=f"test-trace-a-{phase.phase}", model="test-model",
            status="complete",
        )
        for phase in feedback_snapshot.phases
    )
    a_candidate = _t001_candidate(contributor_phase_run_ids=contributor_ids)

    async def fake_run_pipeline(pipeline: object, *, input: object, context: object) -> Mock:
        result = Mock()
        result.step_outputs = {"score_and_draft": Ok(a_candidate)}
        return result

    original_persist_outcome = scan_runner.persist_outcome
    call_count = 0

    def fake_persist_outcome(state: StateManager, decision: object, context: object) -> int:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return original_persist_outcome(state, decision, context)
        raise sqlite3.IntegrityError("simulated persist_outcome failure")

    monkeypatch.setattr(scan_runner, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(scan_runner, "build_scout_pipeline", Mock(return_value=Mock()))
    monkeypatch.setattr(scan_runner, "build_scout_phase_configs", Mock(return_value=Mock()))
    monkeypatch.setattr(scan_runner, "persist_outcome", fake_persist_outcome)

    base_mode: ModeConfig = {"evaluate": "e", "respond": "r", "critique": "c"}
    routed = [
        RoutedMessage(message=msg_a, keyword_route=None),
        RoutedMessage(message=msg_b, keyword_route=None),
    ]

    with pytest.raises(sqlite3.IntegrityError, match="simulated persist_outcome failure"):
        await scan_runner.score_messages(
            routed,
            [msg_a, msg_b],
            base_mode,
            _t001_projects(), {},
            "model", "model", "model",
            Mock(), Mock(),
            in_memory_state,
            scan_id,
            str(tmp_path / "digest.md"),
            dossier_summaries={_T001_PROJECT_KEY: _t001_dossier()},
            dossier_revision="rev-1",
            feedback_snapshot=feedback_snapshot,
        )

    post_a_id = _post_id_by_platform_msg_id(in_memory_state, "t001-durab-a")
    post_b_id = _post_id_by_platform_msg_id(in_memory_state, "t001-durab-b")

    a_eval_count = in_memory_state.conn.execute(
        "SELECT COUNT(*) FROM evaluations WHERE post_id = ?", (post_a_id,)
    ).fetchone()[0]
    assert a_eval_count == 1, "post A's outcome must survive post B's failure"
    a_draft_count = in_memory_state.conn.execute(
        "SELECT COUNT(*) FROM draft_comments WHERE post_id = ?", (post_a_id,)
    ).fetchone()[0]
    assert a_draft_count == 1
    a_event_count = in_memory_state.conn.execute(
        "SELECT COUNT(*) FROM surfaced_events WHERE post_id = ?", (post_a_id,)
    ).fetchone()[0]
    assert a_event_count == 1

    b_eval_count = in_memory_state.conn.execute(
        "SELECT COUNT(*) FROM evaluations WHERE post_id = ?", (post_b_id,)
    ).fetchone()[0]
    assert b_eval_count == 0, "post B must have no evaluation row after its outcome rolled back"
    b_draft_count = in_memory_state.conn.execute(
        "SELECT COUNT(*) FROM draft_comments WHERE post_id = ?", (post_b_id,)
    ).fetchone()[0]
    assert b_draft_count == 0
    b_event_count = in_memory_state.conn.execute(
        "SELECT COUNT(*) FROM surfaced_events WHERE post_id = ?", (post_b_id,)
    ).fetchone()[0]
    assert b_event_count == 0

    scan_row = in_memory_state.conn.execute(
        "SELECT status FROM scans WHERE id = ?", (scan_id,)
    ).fetchone()
    assert scan_row["status"] == "failed"

    failure_row = in_memory_state.conn.execute(
        "SELECT context, kind, message FROM scan_fetch_failures "
        "WHERE scan_id = ? AND kind = 'persistence_error'",
        (scan_id,),
    ).fetchone()
    assert failure_row is not None
    assert failure_row["context"] == f"post_id:{post_b_id}"
    assert "simulated persist_outcome failure" in failure_row["message"]
    assert not in_memory_state.db.in_transaction

    unevaluated_ids = {p.platform_id for p in in_memory_state.load_unevaluated_posts()}
    assert unevaluated_ids == {"t001-durab-b"}, (
        "only the incomplete post B should be recoverable via load_unevaluated_posts"
    )


@pytest.mark.asyncio
async def test_cancellation_during_evaluation_marks_scan_failed_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
    in_memory_state: StateManager,
    tmp_path: Path,
) -> None:
    """Cancellation mid-evaluation (before B's outcome transaction ever
    begins) leaves exactly the saved post with no evaluation/draft/event
    rows, marks the scan status='failed', and propagates
    asyncio.CancelledError rather than swallowing it or continuing.
    """
    now = datetime.now(UTC)
    msg_a = _message("t001-cancel-a", now)
    msg_b = _message("t001-cancel-b", now - timedelta(minutes=1))

    scan_id = in_memory_state.start_scan()
    post_a_id_seed = in_memory_state.save_post(msg_a, scan_id)
    in_memory_state.commit()
    feedback_snapshot = in_memory_state.record_feedback_snapshot(scan_id, mode="shadow")
    contributor_ids = tuple(
        in_memory_state.insert_phase_run(
            scan_id=scan_id, post_id=post_a_id_seed, snapshot_phase_id=phase.snapshot_phase_id,
            phase=phase.phase, trace_id=f"test-trace-cancel-a-{phase.phase}", model="test-model",
            status="complete",
        )
        for phase in feedback_snapshot.phases
    )
    a_candidate = _t001_candidate(contributor_phase_run_ids=contributor_ids)

    call_count = 0

    async def fake_run_pipeline(pipeline: object, *, input: object, context: object) -> Mock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            result = Mock()
            result.step_outputs = {"score_and_draft": Ok(a_candidate)}
            return result
        raise asyncio.CancelledError()

    monkeypatch.setattr(scan_runner, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(scan_runner, "build_scout_pipeline", Mock(return_value=Mock()))
    monkeypatch.setattr(scan_runner, "build_scout_phase_configs", Mock(return_value=Mock()))

    base_mode: ModeConfig = {"evaluate": "e", "respond": "r", "critique": "c"}
    routed = [
        RoutedMessage(message=msg_a, keyword_route=None),
        RoutedMessage(message=msg_b, keyword_route=None),
    ]

    with pytest.raises(asyncio.CancelledError):
        await scan_runner.score_messages(
            routed,
            [msg_a, msg_b],
            base_mode,
            _t001_projects(), {},
            "model", "model", "model",
            Mock(), Mock(),
            in_memory_state,
            scan_id,
            str(tmp_path / "digest.md"),
            dossier_summaries={_T001_PROJECT_KEY: _t001_dossier()},
            dossier_revision="rev-1",
            feedback_snapshot=feedback_snapshot,
        )

    post_a_id = _post_id_by_platform_msg_id(in_memory_state, "t001-cancel-a")
    post_b_id = _post_id_by_platform_msg_id(in_memory_state, "t001-cancel-b")

    a_eval_count = in_memory_state.conn.execute(
        "SELECT COUNT(*) FROM evaluations WHERE post_id = ?", (post_a_id,)
    ).fetchone()[0]
    assert a_eval_count == 1

    b_eval_count = in_memory_state.conn.execute(
        "SELECT COUNT(*) FROM evaluations WHERE post_id = ?", (post_b_id,)
    ).fetchone()[0]
    assert b_eval_count == 0, "cancelled post must have no evaluation row"

    scan_row = in_memory_state.conn.execute(
        "SELECT status FROM scans WHERE id = ?", (scan_id,)
    ).fetchone()
    assert scan_row["status"] == "failed"

    failure_row = in_memory_state.conn.execute(
        "SELECT context, kind FROM scan_fetch_failures WHERE scan_id = ? AND kind = 'cancelled'",
        (scan_id,),
    ).fetchone()
    assert failure_row is not None
    assert failure_row["context"] == f"post_id:{post_b_id}"
    assert not in_memory_state.db.in_transaction

    unevaluated_ids = {p.platform_id for p in in_memory_state.load_unevaluated_posts()}
    assert unevaluated_ids == {"t001-cancel-b"}


def test_t001_surfaced_persists_when_post_committed_before_classification(
    tmp_path: Path,
) -> None:
    """Entry state 1: the post was saved and committed before classification.

    No implicit transaction is open when persist_outcome runs — the simplest
    of the three transaction shapes.
    """
    state = StateManager(db_path=str(tmp_path / "t001_committed.db"))
    try:
        scan_id = state.start_scan()
        msg = _message("t001-committed", datetime.now(UTC))
        post_id = state.save_post(msg, scan_id)
        state.commit()
        assert not state.conn.in_transaction
        contributor_ids = seed_phase_run_contributors(state, scan_id, post_id)

        dossier = _t001_dossier()
        candidate = _t001_candidate(with_critique=True, contributor_phase_run_ids=contributor_ids)
        decision = scan_runner.classify_outcome(candidate, msg, {_T001_PROJECT_KEY: dossier})
        assert decision.status == "surfaced"

        evaluation_id = scan_runner.persist_outcome(
            state, decision, _t001_context(post_id, scan_id, msg)
        )

        _assert_t001_surfaced_outcome(
            state, evaluation_id, post_id, scan_id, decision, expect_critique=True
        )
    finally:
        state.close()


def test_t001_surfaced_persists_when_save_post_commits_before_evaluation(
    tmp_path: Path,
) -> None:
    """Entry state 2: save_post durably commits its own INSERT before
    evaluation runs — no lingering implicit transaction for
    persist_outcome's begin_immediate() to contend with.

    Historically (before per-post durability), save_post could leave the
    connection in SQLite's implicit transaction, and
    persist_surfaced_outcome's unconditional ``BEGIN IMMEDIATE`` had to
    defensively flush it. save_post now opens its own
    ``Db.transaction()``, so no flush is ever needed here.
    """
    state = StateManager(db_path=str(tmp_path / "t001_implicit.db"))
    try:
        scan_id = state.start_scan()
        msg = _message("t001-implicit", datetime.now(UTC))
        post_id = state.save_post(msg, scan_id)
        assert not state.conn.in_transaction
        contributor_ids = seed_phase_run_contributors(state, scan_id, post_id)

        dossier = _t001_dossier()
        candidate = _t001_candidate(with_critique=False, contributor_phase_run_ids=contributor_ids)
        decision = scan_runner.classify_outcome(candidate, msg, {_T001_PROJECT_KEY: dossier})
        assert decision.status == "surfaced"

        evaluation_id = scan_runner.persist_outcome(
            state, decision, _t001_context(post_id, scan_id, msg)
        )

        _assert_t001_surfaced_outcome(
            state, evaluation_id, post_id, scan_id, decision, expect_critique=False
        )
    finally:
        state.close()


def test_t001_surfaced_persists_when_duplicate_post_upgrades_parent(
    tmp_path: Path,
) -> None:
    """Entry state 3: a previously committed post is re-saved as a duplicate
    with a now-resolved parent, upgrading the previously unresolved parent
    fields via save_post's healing UPDATE. The failed duplicate INSERT plus
    the healing UPDATE run inside save_post's own transaction and commit
    together — no implicit transaction is left open for persist_outcome to
    contend with.
    """
    state = StateManager(db_path=str(tmp_path / "t001_dup_parent.db"))
    try:
        scan_id = state.start_scan()
        base_msg = _message("t001-dup-parent", datetime.now(UTC))
        assert base_msg.parent_lookup_status == "not_applicable"
        post_id = state.save_post(base_msg, scan_id)
        assert not state.conn.in_transaction

        parent = SourceParent(
            id="parent-1",
            author=SourceAuthor(id="parent-author", name="Parent Author"),
            text="original parent text",
            url="https://example.com/parent",
        )
        resolved_msg = replace(base_msg, parent=parent, parent_lookup_status="resolved")

        dup_post_id = state.save_post(resolved_msg, scan_id)
        assert dup_post_id == post_id
        # The failed duplicate INSERT plus the parent-upgrade UPDATE commit
        # together inside save_post's own transaction.
        assert not state.conn.in_transaction
        contributor_ids = seed_phase_run_contributors(state, scan_id, post_id)

        dossier = _t001_dossier()
        candidate = _t001_candidate(with_critique=False, contributor_phase_run_ids=contributor_ids)
        decision = scan_runner.classify_outcome(
            candidate, resolved_msg, {_T001_PROJECT_KEY: dossier}
        )
        assert decision.status == "surfaced"

        evaluation_id = scan_runner.persist_outcome(
            state, decision, _t001_context(post_id, scan_id, resolved_msg)
        )

        _assert_t001_surfaced_outcome(
            state, evaluation_id, post_id, scan_id, decision, expect_critique=False
        )

        parent_row = state.conn.execute(
            "SELECT parent_lookup_status, parent_id, parent_author_id, "
            "parent_author_name, parent_text, parent_url FROM posts WHERE id = ?",
            (post_id,),
        ).fetchone()
        assert parent_row["parent_lookup_status"] == "resolved"
        assert parent_row["parent_id"] == "parent-1"
        assert parent_row["parent_author_id"] == "parent-author"
        assert parent_row["parent_author_name"] == "Parent Author"
        assert parent_row["parent_text"] == "original parent text"
        assert parent_row["parent_url"] == "https://example.com/parent"
    finally:
        state.close()


# ---------------------------------------------------------------------------
# T-001: author-rate limit — the losing candidate's gate_blocked outcome must
# be durable, non-partial, and must not disturb the winner already surfaced.
# ---------------------------------------------------------------------------


def test_t001_rate_limited_second_candidate_persists_durable_gate_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "SCOUT_AUTHOR_WEEKLY_CAP", 1)

    state = StateManager(db_path=str(tmp_path / "t001_rate_limit.db"))
    try:
        scan_id = state.start_scan()
        dossier = _t001_dossier()

        winner_msg = _message("t001-rate-winner", datetime.now(UTC))
        winner_post_id = state.save_post(winner_msg, scan_id)
        state.commit()
        winner_contributor_ids = seed_phase_run_contributors(state, scan_id, winner_post_id)
        winner_decision = scan_runner.classify_outcome(
            _t001_candidate(contributor_phase_run_ids=winner_contributor_ids),
            winner_msg, {_T001_PROJECT_KEY: dossier},
        )
        assert winner_decision.status == "surfaced"
        winner_evaluation_id = scan_runner.persist_outcome(
            state, winner_decision, _t001_context(winner_post_id, scan_id, winner_msg)
        )
        _assert_t001_surfaced_outcome(
            state, winner_evaluation_id, winner_post_id, scan_id, winner_decision,
            expect_critique=False,
        )

        loser_msg = _message("t001-rate-loser", datetime.now(UTC))
        loser_post_id = state.save_post(loser_msg, scan_id)
        state.commit()
        loser_contributor_ids = seed_phase_run_contributors(state, scan_id, loser_post_id)
        loser_decision = scan_runner.classify_outcome(
            _t001_candidate(contributor_phase_run_ids=loser_contributor_ids),
            loser_msg, {_T001_PROJECT_KEY: dossier},
        )
        assert loser_decision.status == "surfaced"

        with pytest.raises(SurfaceRateLimitedError) as excinfo:
            scan_runner.persist_outcome(
                state, loser_decision, _t001_context(loser_post_id, scan_id, loser_msg)
            )
        error = excinfo.value
        assert error.count == 1
        assert error.cap == 1

        loser_eval_rows = state.conn.execute(
            "SELECT * FROM evaluations WHERE id = ?", (error.persisted_evaluation_id,)
        ).fetchall()
        assert len(loser_eval_rows) == 1
        assert loser_eval_rows[0]["surface_status"] == "gate_blocked"
        assert loser_eval_rows[0]["post_id"] == loser_post_id

        assert len(error.gate_block_ids) == 1
        block_row = state.conn.execute(
            "SELECT * FROM gate_blocks WHERE id = ?", (error.gate_block_ids[0],)
        ).fetchone()
        assert block_row["reason_code"] == "author_rate"
        assert block_row["offending_text"] == "1 events in last 7 days (cap 1)"
        assert block_row["segment_index"] is None
        assert block_row["project_key"] == _T001_PROJECT_KEY
        assert block_row["dossier_summary_id"] == "gw-dossier"
        assert block_row["dossier_revision"] == "rev-1"
        assert block_row["scan_id"] == scan_id
        assert block_row["post_id"] == loser_post_id
        assert block_row["evaluation_id"] == error.persisted_evaluation_id
        assert block_row["context"] == f"{loser_msg.platform}:{loser_msg.platform_id}"

        assert state.conn.execute(
            "SELECT COUNT(*) FROM draft_comments WHERE evaluation_id = ?",
            (error.persisted_evaluation_id,),
        ).fetchone()[0] == 0
        assert state.conn.execute(
            "SELECT COUNT(*) FROM surfaced_events WHERE evaluation_id = ?",
            (error.persisted_evaluation_id,),
        ).fetchone()[0] == 0
        assert state.conn.execute(
            "SELECT COUNT(*) FROM critiques WHERE evaluation_id = ?",
            (error.persisted_evaluation_id,),
        ).fetchone()[0] == 0

        # The winner remains untouched by the loser's rate-limited attempt.
        _assert_t001_surfaced_outcome(
            state, winner_evaluation_id, winner_post_id, scan_id, winner_decision,
            expect_critique=False,
        )

        scoring_error_count = state.conn.execute(
            "SELECT COUNT(*) FROM scan_fetch_failures WHERE kind = 'scoring_error'"
        ).fetchone()[0]
        assert scoring_error_count == 0
    finally:
        state.close()


def test_author_rate_evaluator_version_is_the_state_manager_constant() -> None:
    """scan_runner consumes state_manager's own AUTHOR_RATE_EVALUATOR_VERSION
    (the PAA author_rate producer version) rather than a second copy.

    Equality alone wouldn't catch a regression here: both values are the
    single-character string "1", which CPython always interns, so even an
    `is` comparison would spuriously pass if scan_runner.py were edited to
    define its own local AUTHOR_RATE_EVALUATOR_VERSION instead of importing
    it. The AST check below directly proves the name is imported from
    state_manager and not (re)assigned at module level in scan_runner.py.
    """
    import ast
    import inspect

    import scout.storage.state as state_manager

    assert scan_runner.AUTHOR_RATE_EVALUATOR_VERSION == state_manager.AUTHOR_RATE_EVALUATOR_VERSION

    tree = ast.parse(inspect.getsource(scan_runner))
    module_level = tree.body

    imported_from_state_manager = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "scout.storage.state"
        and any(alias.name == "AUTHOR_RATE_EVALUATOR_VERSION" for alias in node.names)
        for node in module_level
    )
    assert imported_from_state_manager, (
        "scan_runner.py must import AUTHOR_RATE_EVALUATOR_VERSION from state_manager"
    )

    locally_assigned = any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "AUTHOR_RATE_EVALUATOR_VERSION"
            for target in node.targets
        )
        for node in module_level
    )
    assert not locally_assigned, (
        "scan_runner.py must not define its own AUTHOR_RATE_EVALUATOR_VERSION literal"
    )


# ---------------------------------------------------------------------------
# T-001: atomic rollback matrix — an injected failure at each of the four
# mutable-artifact boundaries inside persist_surfaced_outcome must leave the
# post durable, contribute zero rows to any of the five mutable artifacts,
# and must not disturb an earlier terminal outcome already committed in the
# same batch (the secondary batch-rollback regression).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fail_at", ["evaluation", "draft", "critique", "event"])
def test_t001_surfaced_persist_rollback_matrix(
    tmp_path: Path, fail_at: str
) -> None:
    state = StateManager(db_path=str(tmp_path / f"t001_rollback_{fail_at}.db"))
    try:
        scan_id = state.start_scan()
        dossier = _t001_dossier()

        # An earlier terminal outcome, committed via a savepoint-based unit
        # (persist_terminal_outcome), must survive the next surfaced
        # attempt's rollback untouched.
        prior_msg = _message("t001-rollback-prior", datetime.now(UTC))
        prior_post_id = state.save_post(prior_msg, scan_id)
        prior_contributor_ids = seed_phase_run_contributors(
            state, scan_id, prior_post_id, count=1
        )
        prior_decision = scan_runner.classify_outcome(
            ReplyCandidate(
                relevant=False, score=0.1, reason="not relevant", relevant_to=[],
                contributor_phase_run_ids=prior_contributor_ids,
            ),
            prior_msg,
            {_T001_PROJECT_KEY: dossier},
        )
        assert prior_decision.status == "not_relevant"
        prior_evaluation_id = scan_runner.persist_outcome(
            state, prior_decision, _t001_context(prior_post_id, scan_id, prior_msg)
        )
        state.commit()

        target_msg = _message("t001-rollback-target", datetime.now(UTC))
        target_post_id = state.save_post(target_msg, scan_id)
        state.commit()
        target_contributor_ids = seed_phase_run_contributors(state, scan_id, target_post_id)
        target_decision = scan_runner.classify_outcome(
            _t001_candidate(with_critique=True, contributor_phase_run_ids=target_contributor_ids),
            target_msg, {_T001_PROJECT_KEY: dossier},
        )
        assert target_decision.status == "surfaced"
        assert target_decision.critique is not None
        assert target_decision.validated_text is not None
        assert target_decision.project_key is not None
        critique_pair = (target_decision.critique.verdict, target_decision.critique.feedback)

        with pytest.raises(sqlite3.IntegrityError):
            state.persist_surfaced_outcome(
                target_decision.evaluation,
                target_post_id,
                scan_id,
                project_key=target_decision.project_key,
                author_id=target_msg.author_id,
                platform=target_msg.platform,
                comment_text=target_decision.validated_text,
                structured_output="{}",
                contributor_phase_run_ids=target_decision.contributor_phase_run_ids,
                critique=critique_pair,
                dossier_revision="rev-1",
                dossier_summary_id="gw-dossier",
                surfaced_at=target_msg.created_at.isoformat(),
                fail_at=fail_at,
            )

        # The post itself survives — it is a retry candidate, never rolled
        # back by a surfaced-attempt failure.
        assert state.conn.execute(
            "SELECT COUNT(*) FROM posts WHERE id = ?", (target_post_id,)
        ).fetchone()[0] == 1

        # Zero rows contributed by the failed attempt across all five
        # mutable artifacts.
        assert state.conn.execute(
            "SELECT COUNT(*) FROM evaluations WHERE post_id = ?", (target_post_id,)
        ).fetchone()[0] == 0
        assert state.conn.execute(
            "SELECT COUNT(*) FROM draft_comments WHERE post_id = ?", (target_post_id,)
        ).fetchone()[0] == 0
        assert state.conn.execute(
            "SELECT COUNT(*) FROM surfaced_events WHERE post_id = ?", (target_post_id,)
        ).fetchone()[0] == 0
        assert state.conn.execute(
            "SELECT COUNT(*) FROM critiques c JOIN draft_comments d ON d.id = c.draft_id "
            "WHERE d.post_id = ?", (target_post_id,)
        ).fetchone()[0] == 0
        assert state.conn.execute(
            "SELECT COUNT(*) FROM gate_blocks WHERE post_id = ?", (target_post_id,)
        ).fetchone()[0] == 0

        # The earlier terminal outcome in the same batch is untouched.
        prior_row = state.conn.execute(
            "SELECT surface_status, post_id FROM evaluations WHERE id = ?",
            (prior_evaluation_id,),
        ).fetchone()
        assert prior_row is not None
        assert prior_row["surface_status"] == "not_relevant"
        assert prior_row["post_id"] == prior_post_id
    finally:
        state.close()


# ---------------------------------------------------------------------------
# T-002: end-to-end v2 causal grading feedback propagation
# ---------------------------------------------------------------------------


class _ScriptedLLMClient(LLMClient):
    """Replay one scripted LLMResponse per `.complete` call."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[CompletionParams] = []

    async def complete(self, params: CompletionParams) -> LLMResponse:
        self.calls.append(params)
        return self._responses.pop(0)


def _t002_not_relevant_response() -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id="call-submit",
                name="submit_output",
                arguments={
                    "relevant": False,
                    "score": 0.1,
                    "reason": "off-topic",
                    "relevant_to": [],
                },
            )
        ],
        usage=Usage(input_tokens=10, output_tokens=5, cost=0.0),
        latency_ms=1.0,
        model="scripted",
    )


def _t002_message(platform_id: str, when: datetime) -> Message:
    return Message(
        platform="discord",
        platform_id=platform_id,
        channel_name="general",
        channel_id="c1",
        author_name="alice",
        author_id="a1",
        content=f"message {platform_id}",
        created_at=when,
    )


@pytest.mark.asyncio
async def test_t002_v2_grading_signals_propagate_into_all_phase_prompts(
    tmp_path: Path,
) -> None:
    """A completed prior scan's real, persisted v2 grades — covering a false
    positive, a false negative, causal failure dimensions, a failure note,
    contradicted and unsupported factual dispositions, and a non-no-op
    posture correction — must reach the relevance, reply-draft, and critic
    system prompts of the following scan's phase configs.

    This drives the real production path: StateManager persistence,
    get_recent_grading_signals recent-scan selection,
    format_grading_signals formatting, score_messages, and
    build_scout_phase_configs / build_*_system_prompt assembly. Only the
    LLM boundary is scripted — no model call is real.
    """
    db_path = str(tmp_path / "scout.db")
    now = datetime(2026, 4, 18, tzinfo=UTC)

    with StateManager(db_path=db_path) as state:
        # --- Prior completed scan with real evaluations and v2 grades ---
        prior_scan_id = state.start_scan(fetch_started_at=now - timedelta(hours=2))

        def _grade_post(
            platform_id: str,
            *,
            graded_at: datetime,
            posture: str | None,
            relevance_judgment: str,
            action_judgment: str,
            dimensions: list[str] | None,
            failure_note: str | None,
            factual_offending_claim: str | None = None,
            factual_disposition: str | None = None,
            factual_contradicting_evidence: str | None = None,
            posture_should_have_been: str | None = None,
        ) -> None:
            msg = _t002_message(platform_id, now - timedelta(hours=2))
            post_id = state.save_post(msg, prior_scan_id)
            result = config.RelevanceResult(
                message=msg, relevant=True, score=0.8,
                reason="prior scan evaluation", relevant_to=("gw",),
            )
            evaluation_id = state.save_evaluation(
                result, post_id, prior_scan_id,
                project_key="gw", posture=posture,
                surface_status="surfaced",
            )
            state.save_grade(
                GradeRecord(
                    post_id=post_id,
                    evaluation_id=evaluation_id,
                    scan_id=prior_scan_id,
                    source="cli",
                    graded_at=graded_at,
                    relevance_judgment=relevance_judgment,
                    action_judgment=action_judgment,
                    dimensions=dimensions,
                    failure_note=failure_note,
                    factual_offending_claim=factual_offending_claim,
                    factual_disposition=factual_disposition,
                    factual_contradicting_evidence=factual_contradicting_evidence,
                    posture_should_have_been=posture_should_have_been,
                )
            )

        # A pass, contributing to pass_count.
        _grade_post(
            "prior-pass",
            graded_at=now - timedelta(hours=2),
            posture="answer",
            relevance_judgment="correct",
            action_judgment="accept",
            dimensions=None,
            failure_note=None,
        )
        # A false positive with an unsupported factual claim.
        _grade_post(
            "prior-fp",
            graded_at=now - timedelta(hours=1, minutes=50),
            posture="answer",
            relevance_judgment="false_positive",
            action_judgment="fail",
            dimensions=["factual_support"],
            failure_note="Claimed dossier support that does not exist",
            factual_offending_claim="Product X already shipped GA",
            factual_disposition="unsupported",
        )
        # A false negative with a contradicted factual claim.
        _grade_post(
            "prior-fn",
            graded_at=now - timedelta(hours=1, minutes=40),
            posture="answer",
            relevance_judgment="false_negative",
            action_judgment="fail",
            dimensions=["factual_support"],
            failure_note="Missed evidence directly contradicting the stated claim",
            factual_offending_claim="Feature already shipped",
            factual_disposition="contradicted",
            factual_contradicting_evidence="Changelog shows the feature is still in beta",
        )
        # A non-no-op posture correction.
        _grade_post(
            "prior-posture",
            graded_at=now - timedelta(hours=1, minutes=30),
            posture="answer",
            relevance_judgment="correct",
            action_judgment="fail",
            dimensions=["posture"],
            failure_note="Should have asked a clarifying question instead of answering directly",
            posture_should_have_been="ask",
        )
        state.complete_scan(prior_scan_id, 4, 3)
        state.commit()

        # --- Production recent-scan selection + formatting, exactly once ---
        grading_signal = state.get_recent_grading_signals(limit_scans=3)
        grading_signals_text = format_grading_signals(grading_signal)

        assert grading_signals_text  # non-empty: real signal must format to text
        assert "1 of 4 passed" in grading_signals_text
        assert "1 false positives" in grading_signals_text
        assert "1 false negatives" in grading_signals_text
        assert "factual_support (2)" in grading_signals_text
        assert "posture (1)" in grading_signals_text
        assert "1 posture corrections" in grading_signals_text
        assert "1 unsupported, 1 contradicted" in grading_signals_text
        assert "Should have asked a clarifying question" in grading_signals_text
        assert "Missed evidence directly contradicting" in grading_signals_text

        # --- Spy on phase-config construction to capture the real prompts ---
        built: dict[str, object] = {}
        real_build = scan_runner.build_scout_phase_configs

        def spy_build(
            *,
            relevance_model,
            reply_draft_model,
            critic_model,
            mode_cfg,
            projects,
            templates,
            tracer,
            feedback,
            lessons=None,
            feedback_bundle=_EMPTY_FEEDBACK_BUNDLE,
        ):
            configs = real_build(
                relevance_model=relevance_model,
                reply_draft_model=reply_draft_model,
                critic_model=critic_model,
                mode_cfg=mode_cfg,
                projects=projects,
                templates=templates,
                tracer=tracer,
                feedback=feedback,
                lessons=lessons,
                feedback_bundle=feedback_bundle,
            )
            built["relevance"] = configs.relevance.system_prompt
            built["reply_draft"] = configs.reply_draft.system_prompt
            built["critic"] = configs.critic.system_prompt
            llm = _ScriptedLLMClient([_t002_not_relevant_response()])
            return configs.__class__(
                relevance=configs.relevance.with_(llm=llm),
                reply_draft=configs.reply_draft,
                critic=configs.critic,
            )

        # --- Assemble the following scan through production orchestration ---
        next_scan_id = state.start_scan(fetch_started_at=now)
        next_feedback_snapshot = state.record_feedback_snapshot(next_scan_id, mode="shadow")
        next_msg = _t002_message("next-scan-msg", now)
        routed = [RoutedMessage(message=next_msg, keyword_route=None)]

        tracer = SQLiteTracer(db_path=str(tmp_path / "traces.db"))
        feedback = SQLiteFeedbackLoop(db_path=str(tmp_path / "feedback.db"))
        original_build = scan_runner.build_scout_phase_configs
        scan_runner.build_scout_phase_configs = spy_build  # type: ignore[assignment]
        try:
            await scan_runner.score_messages(
                routed,
                [next_msg],
                MODES["lead_gen"],
                {},
                {},
                "claude-haiku-4-5-20251001",
                "claude-haiku-4-5-20251001",
                "claude-haiku-4-5-20251001",
                tracer,
                feedback,
                state,
                next_scan_id,
                str(tmp_path / "digest.md"),
                feedback_bundle=legacy_feedback_bundle(grading_signals_text),
                feedback_snapshot=next_feedback_snapshot,
            )
        finally:
            scan_runner.build_scout_phase_configs = original_build  # type: ignore[assignment]
            await tracer.flush()
            await tracer.close()
            await feedback.close()

        # --- All three phase prompts carry the v2 causal signal ---
        for phase in ("relevance", "reply_draft", "critic"):
            prompt = built[phase]
            assert "Recent Human Grading Feedback" in prompt, phase
            assert "1 of 4 passed" in prompt, phase
            assert "1 false positives" in prompt, phase
            assert "1 false negatives" in prompt, phase
            assert "factual_support (2)" in prompt, phase
            assert "posture (1)" in prompt, phase
            assert "1 posture corrections" in prompt, phase
            assert "1 unsupported, 1 contradicted" in prompt, phase

        # --- No test write ever touched the dead v1 columns ---
        # relevance_note and comment_note no longer exist as columns at all
        # (dropped by migration 021); rejection_reason, comment_quality, and
        # comment_issue are retained for audit compatibility but must stay
        # untouched by every v2 write this test performed.
        grade_cols = {
            row["name"] for row in state.conn.execute("PRAGMA table_info(grades)")
        }
        assert "relevance_note" not in grade_cols
        assert "comment_note" not in grade_cols

        rows = state.conn.execute(
            "SELECT rejection_reason, comment_quality, comment_issue FROM grades"
        ).fetchall()
        assert rows
        for row in rows:
            assert row["rejection_reason"] is None
            assert row["comment_quality"] is None
            assert row["comment_issue"] is None


# ---------------------------------------------------------------------------
# evaluation-feedback/v1 activation: FEEDBACK_PROMPT_ENABLED active mode
# ---------------------------------------------------------------------------

_ADVERSARIAL_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "prompts" / "adversarial_active_mode_grades.json"
)


def _seed_adversarial_fixture_grades(state: StateManager, *, scan_id: int) -> dict[str, str]:
    """Seed one grade per entry in adversarial_active_mode_grades.json.

    Returns {sentinel: exclusion_kind} so the test can assert each
    sentinel's expected fate without hardcoding the fixture's contents.
    """
    entries = json.loads(_ADVERSARIAL_FIXTURE_PATH.read_text())["grades"]
    sentinel_kinds: dict[str, str] = {}
    now = datetime.now(UTC)

    for entry in entries:
        sentinel = entry["sentinel"]
        kind = entry["exclusion_kind"]
        sentinel_kinds[sentinel] = kind

        msg = _t002_message(entry["platform_id"], now)
        post_id = state.save_post(msg, scan_id)
        eval_project_key = entry.get("evaluation_project_key", "gw")
        relevance_judgment = entry["relevance_judgment"]
        action_judgment = entry["action_judgment"]
        dimensions = entry["dimensions"]
        relevant = relevance_judgment != "false_positive"

        if kind == "missing_evaluation_linkage":
            grade = GradeRecord(
                post_id=post_id, evaluation_id=None, scan_id=scan_id, source="migration",
                graded_at=now, relevance_judgment=relevance_judgment,
                action_judgment=action_judgment, dimensions=dimensions,
                failure_note=sentinel, schema_version=3,
            )
            state.save_grade_for_migration(
                grade, migration_reason="fixture: broken evaluation link"
            )
            continue

        result = config.RelevanceResult(
            message=msg, relevant=relevant, score=0.8, reason="fixture",
            relevant_to=(eval_project_key,),
        )
        evaluation_id = state.save_evaluation(
            result, post_id, scan_id, project_key=eval_project_key, posture="answer",
            surface_status="surfaced" if relevant else "not_relevant",
        )

        if kind == "shared_contract_invalid":
            state.save_draft(
                post_id, evaluation_id, entry["draft_project_key"], "draft text",
                scan_id, posture="answer",
            )
        elif kind in ("draft_quality", "needs_regrade", "manual_exclude"):
            state.save_draft(
                post_id, evaluation_id, eval_project_key, "draft text",
                scan_id, posture="answer",
            )
        # "relevance_only" and "schema_version" entries get no draft.

        if kind == "schema_version":
            grade = GradeRecord(
                post_id=post_id, evaluation_id=evaluation_id, scan_id=scan_id,
                source="migration", graded_at=now, relevance_judgment=relevance_judgment,
                action_judgment=action_judgment, dimensions=dimensions,
                failure_note=sentinel, schema_version=1,
            )
            state.save_grade_for_migration(
                grade, migration_reason="fixture: legacy schema v1 grade"
            )
            continue

        grade = GradeRecord(
            post_id=post_id, evaluation_id=evaluation_id, scan_id=scan_id, source="cli",
            graded_at=now, relevance_judgment=relevance_judgment,
            action_judgment=action_judgment, dimensions=dimensions,
            failure_note=sentinel, schema_version=3,
            needs_regrade=(kind == "needs_regrade"),
        )
        grade_id = state.save_grade(grade)

        if kind == "manual_exclude":
            state.save_grade_usage_override(
                grade_id, mode="exclude", reason="fixture manual exclude"
            )

    return sentinel_kinds


@pytest.mark.asyncio
async def test_active_mode_adversarial_grades_excluded_and_phase_isolated(
    tmp_path: Path,
) -> None:
    """Active-mode production path: schema-v1, needs_regrade,
    contract-invalid, broken-link, and manually excluded grades must never
    reach any of the three phase prompts, while each valid sentinel
    appears only in its intended phase(s) — proving the eligibility gates
    and phase boundary directly, not just via aggregate counts."""
    db_path = str(tmp_path / "scout.db")
    with StateManager(db_path=db_path) as state:
        seed_scan_id = state.start_scan()
        sentinel_kinds = _seed_adversarial_fixture_grades(state, scan_id=seed_scan_id)
        state.complete_scan(seed_scan_id, len(sentinel_kinds), 0)
        state.commit()

        next_scan_id = state.start_scan()
        snapshot = state.record_feedback_snapshot(next_scan_id, mode="active")
        state.commit()
        assert snapshot.mode == "active"

        feedback_bundle = state.load_committed_feedback_bundle(
            snapshot.snapshot_id, expected_mode="active"
        )

        built: dict[str, str] = {}
        real_build = scan_runner.build_scout_phase_configs

        def spy_build(*, lessons=None, feedback_bundle, **kwargs):
            configs = real_build(lessons=lessons, feedback_bundle=feedback_bundle, **kwargs)
            built["relevance"] = configs.relevance.system_prompt
            built["reply_draft"] = configs.reply_draft.system_prompt
            built["critic"] = configs.critic.system_prompt
            llm = _ScriptedLLMClient([_t002_not_relevant_response()])
            return configs.__class__(
                relevance=configs.relevance.with_(llm=llm),
                reply_draft=configs.reply_draft,
                critic=configs.critic,
            )

        next_msg = _t002_message("next-scan-msg", datetime.now(UTC))
        routed = [RoutedMessage(message=next_msg, keyword_route=None)]
        tracer = SQLiteTracer(db_path=str(tmp_path / "traces.db"))
        feedback = SQLiteFeedbackLoop(db_path=str(tmp_path / "feedback.db"))
        original_build = scan_runner.build_scout_phase_configs
        scan_runner.build_scout_phase_configs = spy_build  # type: ignore[assignment]
        try:
            await scan_runner.score_messages(
                routed, [next_msg], MODES["lead_gen"], {}, {},
                "claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001",
                "claude-haiku-4-5-20251001", tracer, feedback, state,
                next_scan_id, str(tmp_path / "digest.md"),
                feedback_bundle=feedback_bundle,
                feedback_snapshot=snapshot,
            )
        finally:
            scan_runner.build_scout_phase_configs = original_build  # type: ignore[assignment]
            await tracer.flush()
            await tracer.close()
            await feedback.close()

        excluded_sentinels = [
            s for s, kind in sentinel_kinds.items()
            if kind not in ("relevance_only", "draft_quality")
        ]
        assert len(excluded_sentinels) == 5

        for sentinel in excluded_sentinels:
            for phase, prompt in built.items():
                assert sentinel not in prompt, f"{sentinel} leaked into {phase}"

        relevance_only_sentinel = next(
            s for s, kind in sentinel_kinds.items() if kind == "relevance_only"
        )
        draft_quality_sentinel = next(
            s for s, kind in sentinel_kinds.items() if kind == "draft_quality"
        )

        assert relevance_only_sentinel in built["relevance"]
        assert relevance_only_sentinel not in built["reply_draft"]
        assert relevance_only_sentinel not in built["critic"]

        assert draft_quality_sentinel not in built["relevance"]
        assert draft_quality_sentinel in built["reply_draft"]
        assert draft_quality_sentinel in built["critic"]


@pytest.mark.asyncio
async def test_active_mode_rollback_to_disabled_on_next_scan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Flipping FEEDBACK_PROMPT_ENABLED off must change only the next
    scan's prompt behavior — the prior active snapshot stays durable and
    untouched, and rollback is a pure configuration change."""
    db_path = str(tmp_path / "scout.db")
    real_state = StateManager(db_path=db_path)

    async def fake_score_messages(*args: object, **kwargs: object) -> tuple:
        return "", 0, True, []

    _configure_main_loop_score_failure(monkeypatch, real_state, fake_score_messages)
    args = Namespace(mode="default", rescore=None, rescore_failed=None, continuous=False)

    monkeypatch.setattr(scan_runner, "FEEDBACK_PROMPT_ENABLED", True)
    await scan_runner.main_loop(args)

    active_snapshot_row = real_state.conn.execute(
        "SELECT id, mode FROM feedback_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert active_snapshot_row["mode"] == "active"
    active_snapshot_id = active_snapshot_row["id"]

    monkeypatch.setattr(scan_runner, "FEEDBACK_PROMPT_ENABLED", False)
    await scan_runner.main_loop(args)

    rows = real_state.conn.execute(
        "SELECT id, mode FROM feedback_snapshots ORDER BY id"
    ).fetchall()
    assert [r["mode"] for r in rows] == ["active", "shadow"]
    # The first (active) snapshot's row is untouched — no deletion or
    # mutation on rollback, only future prompt selection changes.
    assert rows[0]["id"] == active_snapshot_id

    real_state.close()


@pytest.mark.asyncio
async def test_metadata_only_feedback_log_event_excludes_rendered_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The per-scan feedback-snapshot log line must carry only
    correlating metadata — never rendered text, grade notes, posts, or
    drafts."""
    db_path = str(tmp_path / "scout.db")
    real_state = StateManager(db_path=db_path)

    seed_scan_id = real_state.start_scan()
    sentinel_kinds = _seed_adversarial_fixture_grades(real_state, scan_id=seed_scan_id)
    real_state.complete_scan(seed_scan_id, len(sentinel_kinds), 0)
    real_state.commit()

    async def fake_score_messages(*args: object, **kwargs: object) -> tuple:
        return "", 0, True, []

    _configure_main_loop_score_failure(monkeypatch, real_state, fake_score_messages)
    monkeypatch.setattr(scan_runner, "FEEDBACK_PROMPT_ENABLED", True)
    args = Namespace(mode="default", rescore=None, rescore_failed=None, continuous=False)

    with caplog.at_level(logging.INFO, logger="scout.scanning.runner"):
        await scan_runner.main_loop(args)

    metadata_records = [
        r for r in caplog.records if r.getMessage().startswith("Feedback snapshot metadata:")
    ]
    assert len(metadata_records) == 1
    message = metadata_records[0].getMessage()
    payload = json.loads(message.removeprefix("Feedback snapshot metadata: "))

    assert payload["mode"] == "active"
    assert payload["policy_version"] == "evaluation-feedback/v1"
    assert isinstance(payload["snapshot_id"], int)
    assert {p["phase"] for p in payload["phases"]} == {"relevance", "reply_draft", "critic"}
    for phase_payload in payload["phases"]:
        assert set(phase_payload) == {
            "phase", "snapshot_phase_id", "rendered_sha256", "token_estimate", "token_budget",
        }

    # None of the seeded sentinel note text (grade notes) appears anywhere
    # in this log line, nor does any rendered_text body.
    for sentinel in sentinel_kinds:
        assert sentinel not in message

    real_state.close()


@pytest.mark.asyncio
async def test_active_mode_committed_phase_integrity_failure_fails_scan_before_model_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A corrupted or unreadable committed snapshot must fail the scan
    before any model call — there is no legacy fallback for active mode."""
    db_path = str(tmp_path / "scout.db")
    real_state = StateManager(db_path=db_path)

    score_messages_called = False

    async def spy_score_messages(*args: object, **kwargs: object) -> tuple:
        nonlocal score_messages_called
        score_messages_called = True
        return "", 0, True, []

    _configure_main_loop_score_failure(monkeypatch, real_state, spy_score_messages)
    monkeypatch.setattr(scan_runner, "FEEDBACK_PROMPT_ENABLED", True)

    def _broken_load_committed_feedback_bundle(*args: object, **kwargs: object) -> object:
        raise ef.FeedbackBundleIntegrityError("simulated committed-phase corruption")

    monkeypatch.setattr(
        real_state,
        "load_committed_feedback_bundle",
        _broken_load_committed_feedback_bundle,
    )

    args = Namespace(mode="default", rescore=None, rescore_failed=None, continuous=False)
    with pytest.raises(ef.FeedbackBundleIntegrityError):
        await scan_runner.main_loop(args)

    assert score_messages_called is False

    scan_row = real_state.conn.execute(
        "SELECT status FROM scans ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert scan_row["status"] == "failed"

    real_state.close()
