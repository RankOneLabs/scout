"""Focused scan-start integration tests for the live Jev classifier."""

from __future__ import annotations

from argparse import Namespace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

import scout.scanning.runner as runner
from scout.config import Account, Message
from scout.errors import LLMError
from scout.registry import KeywordRoute, ProjectTarget, RuntimeRegistry
from scout.relevance.setup import setup_jev_scan
from scout.result import Err, Ok
from scout.scanning.prefilter import RoutedMessage
from scout.storage.state import StateManager


def _args() -> Namespace:
    return Namespace(mode="both", rescore=None, rescore_failed=None, continuous=False)


def _route(project_key: str) -> KeywordRoute:
    return KeywordRoute(
        id=1,
        project_key=project_key,
        keyword="agent",
        evaluate_prompt=None,
        respond_prompt=None,
        critique_prompt=None,
        priority=0,
    )


def _message(platform_id: str) -> Message:
    return Message(
        platform="bluesky",
        platform_id=platform_id,
        channel_name="agents",
        channel_id="agents",
        author=Account(platform="bluesky", id=f"author-{platform_id}", name="Ada", handle="ada"),
        content="agent evaluation question",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        url=f"https://example.test/{platform_id}",
    )


def _main_loop_shell(
    monkeypatch: pytest.MonkeyPatch, registry: RuntimeRegistry
) -> tuple[MagicMock, SimpleNamespace]:
    state = MagicMock()
    state.__enter__ = Mock(return_value=state)
    state.__exit__ = Mock(return_value=False)
    state.load_runtime_registry.return_value = registry
    heartbeat = MagicMock()
    lease = SimpleNamespace(
        fence=1,
        lost=False,
        check=Mock(),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(runner, "validate_config", lambda: [])
    monkeypatch.setattr(runner, "build_platform_scanners", lambda: (Mock(), Mock(), Mock()))
    monkeypatch.setattr(runner, "StateManager", Mock(side_effect=(state, heartbeat)))
    monkeypatch.setattr(runner, "_acquire_scan_lease", Mock(return_value=lease))
    monkeypatch.setattr(runner._config, "RELEVANCE_CLASSIFIER", "jev")
    return state, lease


@pytest.mark.asyncio
async def test_unreadable_jev_catalogue_refuses_scan_before_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    missing = tmp_path / "missing-catalogue.yaml"
    _main_loop_shell(monkeypatch, RuntimeRegistry({}, (), {}))
    fetch = AsyncMock()
    monkeypatch.setattr(runner._config, "RELEVANCE_JEV_CATALOGUE_PATH", str(missing))
    monkeypatch.setattr(runner, "fetch_messages", fetch)

    with pytest.raises(RuntimeError, match=str(missing)):
        await runner.main_loop(_args())

    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_jev_client_closes_when_scan_body_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _main_loop_shell(monkeypatch, RuntimeRegistry({}, (), {}))
    client = SimpleNamespace(aclose=AsyncMock())
    context = SimpleNamespace(client=client)
    monkeypatch.setattr(runner, "setup_jev_scan", Mock(return_value=Ok(context)))
    monkeypatch.setattr(
        runner,
        "build_search_queries",
        Mock(side_effect=RuntimeError("scan body failed")),
    )

    with pytest.raises(RuntimeError, match="scan body failed"):
        await runner.main_loop(_args())

    client.aclose.assert_awaited_once()


def test_missing_routed_jev_project_is_a_setup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalogue = SimpleNamespace(questions=(object(),))
    monkeypatch.setattr("scout.relevance.setup.load_catalogue", Mock(return_value=catalogue))
    monkeypatch.setattr(runner._config, "RELEVANCE_JEV_CATALOGUE_PATH", "/catalogue.yaml")
    registry = RuntimeRegistry({}, (_route("agent-evals"),), {})

    result = runner.setup_jev_scan(registry)

    assert isinstance(result, Err)
    assert result.error.operation == "resolve_relevance_project"
    assert result.error.entity == "agent-evals"


@pytest.mark.asyncio
async def test_one_setup_is_reused_across_posts_and_jev_errors_are_retryable(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    project = ProjectTarget("agent-ops", "Agent Ops", "desc", "")
    registry = RuntimeRegistry({"agent-ops": project}, (_route("agent-ops"),), {})
    catalogue = SimpleNamespace(questions=(object(),))
    client = SimpleNamespace(model="jev-latest", aclose=AsyncMock())
    load = Mock(return_value=catalogue)
    client_constructor = Mock(return_value=client)
    monkeypatch.setattr("scout.relevance.setup.load_catalogue", load)
    monkeypatch.setattr("scout.relevance.setup.JevClient", client_constructor)
    monkeypatch.setattr(runner._config, "RELEVANCE_JEV_CATALOGUE_PATH", "/catalogue.yaml")

    setup = setup_jev_scan(registry)
    assert isinstance(setup, Ok)

    state = StateManager(db_path=str(tmp_path / "state.db"))
    scan_id = state.start_scan(environment="test")
    snapshot = state.record_feedback_snapshot(scan_id, mode="shadow")
    messages = [_message("post-1"), _message("post-2")]
    routed = [
        RoutedMessage(message=message, keyword_route=_route("agent-ops"))
        for message in messages
    ]
    pipeline_result = SimpleNamespace(
        step_outputs={
            "score_and_draft": Err(
                LLMError(
                    operation="jev.evaluate",
                    message_id="post",
                    detail="provider unavailable",
                )
            )
        }
    )
    run = AsyncMock(return_value=pipeline_result)
    monkeypatch.setattr(runner, "run_pipeline", run)
    monkeypatch.setattr(runner, "build_scout_pipeline", Mock(return_value=Mock()))
    monkeypatch.setattr(runner, "build_scout_phase_configs", Mock(return_value=Mock()))
    monkeypatch.setattr(runner, "write_digest_header", Mock())
    monkeypatch.setattr(runner, "finalize_digest", Mock(return_value=""))

    _, _, _, failures = await runner.score_messages(
        routed,
        messages,
        {"evaluate": "e", "respond": "r", "critique": "c"},
        registry.projects,
        {},
        "llm-model",
        "llm-model",
        "llm-model",
        Mock(),
        Mock(),
        state,
        scan_id,
        str(tmp_path / "digest.md"),
        feedback_snapshot=snapshot,
        jev_context=setup.value,
    )

    load.assert_called_once_with("/catalogue.yaml")
    client_constructor.assert_called_once()
    assert run.await_count == 2
    assert [failure.kind for failure in failures] == ["scoring_error", "scoring_error"]
    assert all(failure.retryable for failure in failures)
    assert all(failure.message == "provider unavailable" for failure in failures)
    assert all(failure.context.endswith(":jev.evaluate") for failure in failures if failure.context)
    state.close()
