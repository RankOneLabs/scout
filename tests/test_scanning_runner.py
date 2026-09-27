"""Focused scan-start integration tests for the live Jev classifier."""

from __future__ import annotations

from argparse import Namespace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

import scout.scanning.runner as runner
from scout.registry import KeywordRoute, RuntimeRegistry
from scout.result import Err, Ok


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
