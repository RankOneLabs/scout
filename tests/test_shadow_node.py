from __future__ import annotations

import asyncio
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any, Literal
from unittest.mock import AsyncMock, Mock

import pytest

import scout.config as config
import scout.scanning.runner as scan_runner
import scout.storage.evaluations as evaluation_store
import scout.typesafe.shadow as shadow_module
from scout.config import Account, Message, ModeConfig, RelevanceResult
from scout.errors import PlatformFetchFailure
from scout.registry import ProjectTarget
from scout.result import Ok, Result
from scout.scanning.prefilter import RoutedMessage
from scout.scanning.schemas import ReplyCandidate
from scout.storage.state import StateManager
from scout.typesafe.catalogue import Catalogue, load_catalogue
from scout.typesafe.models import Answers, BackendError
from scout.typesafe.placeholder import PlaceholderBackend
from scout.typesafe.shadow import ShadowRelevanceRunner


def _message(platform_id: str) -> Message:
    return Message(
        "bluesky",
        platform_id,
        "feed",
        "feed",
        Account("bluesky", "author", "Author", "author.test"),
        "agent operations",
        datetime.now(UTC),
    )


def _project(key: str = "agent-ops") -> ProjectTarget:
    return ProjectTarget(key, key.replace("-", " ").title(), "desc", "https://example.test")


def _enable_shadow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "TYPESAFE_SHADOW_MODE", True)
    monkeypatch.setattr(
        config,
        "TYPESAFE_CATALOGUE_PATH",
        "tests/fixtures/typesafe/fixture-two-question.v1.yaml",
    )
    monkeypatch.setattr(
        config,
        "TYPESAFE_PLACEHOLDER_ANSWERS_PATH",
        "tests/fixtures/typesafe/placeholder-answers.yaml",
    )


def _stub_pipeline_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scan_runner, "build_scout_pipeline", Mock(return_value=Mock()))
    monkeypatch.setattr(scan_runner, "build_scout_phase_configs", Mock(return_value=Mock()))
    monkeypatch.setattr(scan_runner, "write_digest_header", Mock())
    monkeypatch.setattr(scan_runner, "finalize_digest", Mock(return_value="fixture digest"))


async def _score(
    state: StateManager,
    scan_id: int,
    routed: list[RoutedMessage],
    projects: dict[str, ProjectTarget],
    digest_path: Path,
) -> tuple[str, int, bool, list[PlatformFetchFailure]]:
    base_mode: ModeConfig = {"evaluate": "e", "respond": "r", "critique": "c"}
    snapshot = state.record_feedback_snapshot(scan_id, mode="shadow")
    return await scan_runner.score_messages(
        routed,
        [item.message for item in routed],
        base_mode,
        projects,
        {},
        "model",
        "model",
        "model",
        Mock(),
        Mock(),
        state,
        scan_id,
        str(digest_path),
        feedback_snapshot=snapshot,
    )


def _rows(
    state: StateManager,
    table: Literal["evaluations", "evaluation_phase_runs", "shadow_relevance_runs"],
) -> list[tuple[object, ...]]:
    queries = {
        "evaluations": "SELECT * FROM evaluations ORDER BY id",
        "evaluation_phase_runs": "SELECT * FROM evaluation_phase_runs ORDER BY id",
        "shadow_relevance_runs": "SELECT * FROM shadow_relevance_runs ORDER BY id",
    }
    rows = state.conn.execute(queries[table]).fetchall()
    return [tuple(row) for row in rows]


async def test_shadow_runner_records_ok_and_can_be_backfilled() -> None:
    catalogue = load_catalogue("tests/fixtures/typesafe/fixture-two-question.v1.yaml")
    backend = PlaceholderBackend("tests/fixtures/typesafe/placeholder-answers.yaml")
    runner = ShadowRelevanceRunner(catalogue, "placeholder", backend)
    project = ProjectTarget("agent-ops", "Agent Ops", "desc", "https://example.test")
    with StateManager(":memory:") as state:
        scan_id = state.start_scan()
        message = _message("known-post")
        post_id = state.save_post(message, scan_id)
        run_id = await runner.run(
            state_manager=state,
            scan_id=scan_id,
            post_id=post_id,
            message=message,
            project=project,
        )
        assert run_id is not None
        evaluation_id = state.save_evaluation(
            RelevanceResult(message, True, 0.9, "pipeline"), post_id, scan_id
        )
        assert state.shadow_relevance.backfill_evaluation_id(run_id, evaluation_id)
        row = state.shadow_relevance.list_runs_for_scan(scan_id)[0]
        assert row.status == "ok"
        assert row.evaluation_id == evaluation_id


async def test_shadow_backend_exception_becomes_error_row(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def raising_backend(
        state: dict[str, object], catalogue: Catalogue
    ) -> Result[Answers, BackendError]:
        del state, catalogue
        raise RuntimeError("backend down")

    catalogue = load_catalogue("tests/fixtures/typesafe/fixture-two-question.v1.yaml")
    runner = ShadowRelevanceRunner(catalogue, "placeholder", raising_backend)
    project = ProjectTarget("agent-ops", "Agent Ops", "desc", "https://example.test")
    with StateManager(":memory:") as state:
        scan_id = state.start_scan()
        message = _message("error-post")
        post_id = state.save_post(message, scan_id)
        run_id = await runner.run(
            state_manager=state,
            scan_id=scan_id,
            post_id=post_id,
            message=message,
            project=project,
        )
        assert run_id is not None
        evaluation_id = state.save_evaluation(
            RelevanceResult(message, False, 0.1, "pipeline"), post_id, scan_id
        )
        assert state.shadow_relevance.backfill_evaluation_id(run_id, evaluation_id)
        row = state.shadow_relevance.list_runs_for_scan(scan_id)[0]
        assert row.status == "error"
        assert row.evaluation_id == evaluation_id
        assert "backend down" in (row.error_detail or "")
        assert "typesafe shadow evaluation failed" in caplog.text


@pytest.mark.asyncio
async def test_score_messages_shadow_mode_is_non_gating_and_backfills_evaluations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The node adds only shadow rows; pipeline outcomes stay byte-identical."""
    _stub_pipeline_construction(monkeypatch)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> _FixedDateTime:
            del tz
            return cls(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    monkeypatch.setattr(evaluation_store, "datetime", _FixedDateTime)

    async def fake_run_pipeline(
        pipeline: object, *, input: RoutedMessage, context: dict[str, Any]
    ) -> Mock:
        del pipeline
        execution = context["execution_context"]
        phase_run_id = execution.state.insert_phase_run(
            scan_id=execution.scan_id,
            post_id=execution.post_id,
            snapshot_phase_id=execution.relevance.snapshot_phase_id,
            phase="relevance",
            trace_id=f"fixture-trace-{input.message.platform_id}",
            model="fixture-model",
            status="complete",
        )
        result = Mock()
        result.step_outputs = {
            "score_and_draft": Ok(
                ReplyCandidate(
                    relevant=False,
                    score=0.1,
                    reason="fixture irrelevant",
                    contributor_phase_run_ids=(phase_run_id,),
                )
            )
        }
        return result

    monkeypatch.setattr(scan_runner, "run_pipeline", fake_run_pipeline)
    messages = [_message("known-post"), _message("fixture-post-2")]

    async def run(shadow_mode: bool) -> tuple[
        tuple[str, int, bool, list[PlatformFetchFailure]],
        list[tuple[object, ...]],
        list[tuple[object, ...]],
        list[tuple[object, ...]],
    ]:
        monkeypatch.setattr(config, "TYPESAFE_SHADOW_MODE", shadow_mode)
        if shadow_mode:
            _enable_shadow(monkeypatch)
        with StateManager(":memory:") as state:
            state.registry.upsert_project("agent-ops", "Agent Ops", "desc", "https://example.test")
            state.registry.upsert_keyword("agent-ops", "agent")
            route = state.load_runtime_registry().keywords[0]
            scan_id = state.start_scan()
            outcome = await _score(
                state,
                scan_id,
                [RoutedMessage(message=message, keyword_route=route) for message in messages],
                {"agent-ops": _project()},
                tmp_path / f"digest-{shadow_mode}.md",
            )
            return (
                outcome,
                _rows(state, "evaluations"),
                _rows(state, "evaluation_phase_runs"),
                _rows(state, "shadow_relevance_runs"),
            )

    off_outcome, off_evaluations, off_phase_runs, off_shadow = await run(False)
    on_outcome, on_evaluations, on_phase_runs, on_shadow = await run(True)

    assert on_outcome == off_outcome
    assert on_evaluations == off_evaluations
    assert on_phase_runs == off_phase_runs
    assert off_shadow == []
    assert len(on_shadow) == len(messages)
    evaluation_ids = {row[0] for row in on_evaluations}
    assert all(row[3] in evaluation_ids for row in on_shadow)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked,project_key", [(True, "agent-ops"), (False, "other")])
async def test_score_messages_skips_shadow_for_blocked_or_non_agent_ops_posts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    blocked: bool,
    project_key: str,
) -> None:
    _enable_shadow(monkeypatch)
    _stub_pipeline_construction(monkeypatch)
    backend_call = AsyncMock(side_effect=AssertionError("backend must not be called"))
    monkeypatch.setattr(PlaceholderBackend, "__call__", backend_call)
    monkeypatch.setattr(
        scan_runner,
        "run_pipeline",
        AsyncMock(side_effect=RuntimeError("continue")),
    )

    with StateManager(":memory:") as state:
        state.registry.upsert_project(project_key, "Project", "desc", "https://example.test")
        state.registry.upsert_keyword(project_key, "agent")
        route = state.load_runtime_registry().keywords[0]
        scan_id = state.start_scan()
        message = _message(f"{project_key}-post")
        if blocked:
            state.block_author(platform=message.platform, author_id=message.author_id)

        await _score(
            state,
            scan_id,
            [RoutedMessage(message=message, keyword_route=route)],
            {project_key: _project(project_key)},
            tmp_path / "digest.md",
        )

        assert state.shadow_relevance.list_runs_for_scan(scan_id) == []
        backend_call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_path", ["continue", "raise"])
async def test_score_messages_leaves_no_shadow_tasks_on_early_exits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    exit_path: str,
) -> None:
    _enable_shadow(monkeypatch)
    _stub_pipeline_construction(monkeypatch)
    baseline = asyncio.all_tasks()

    async def fake_run_pipeline(
        pipeline: object, *, input: object, context: object
    ) -> Mock:
        del pipeline, input, context
        if exit_path == "continue":
            raise RuntimeError("continue")
        result = Mock()
        result.step_outputs = {
            "score_and_draft": Ok(ReplyCandidate(relevant=False, score=0.1, reason="fixture"))
        }
        return result

    monkeypatch.setattr(scan_runner, "run_pipeline", fake_run_pipeline)

    class FatalOutcome(BaseException):
        pass

    if exit_path == "raise":
        monkeypatch.setattr(scan_runner, "classify_outcome", Mock(side_effect=FatalOutcome))

    with StateManager(":memory:") as state:
        state.registry.upsert_project("agent-ops", "Agent Ops", "desc", "https://example.test")
        state.registry.upsert_keyword("agent-ops", "agent")
        route = state.load_runtime_registry().keywords[0]
        scan_id = state.start_scan()
        call = _score(
            state,
            scan_id,
            [RoutedMessage(message=_message("known-post"), keyword_route=route)],
            {"agent-ops": _project()},
            tmp_path / "digest.md",
        )
        if exit_path == "raise":
            with pytest.raises(FatalOutcome):
                await call
        else:
            await call

    assert asyncio.all_tasks() == baseline


@pytest.mark.asyncio
async def test_score_messages_bounds_and_cancels_slow_shadow_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _enable_shadow(monkeypatch)
    _stub_pipeline_construction(monkeypatch)
    shadow_cancelled = asyncio.Event()

    async def pending_shadow(
        self: ShadowRelevanceRunner, **kwargs: object
    ) -> int | None:
        del self, kwargs
        try:
            await asyncio.Future[None]()
        finally:
            shadow_cancelled.set()
        return None

    monkeypatch.setattr(ShadowRelevanceRunner, "run", pending_shadow)
    monkeypatch.setattr(
        scan_runner,
        "run_pipeline",
        AsyncMock(side_effect=RuntimeError("continue")),
    )

    with StateManager(":memory:") as state:
        state.registry.upsert_project("agent-ops", "Agent Ops", "desc", "https://example.test")
        state.registry.upsert_keyword("agent-ops", "agent")
        route = state.load_runtime_registry().keywords[0]
        scan_id = state.start_scan()
        await asyncio.wait_for(
            _score(
                state,
                scan_id,
                [RoutedMessage(message=_message("known-post"), keyword_route=route)],
                {"agent-ops": _project()},
                tmp_path / "digest.md",
            ),
            timeout=1.0,
        )

    assert shadow_cancelled.is_set()


@pytest.mark.asyncio
async def test_outer_cancellation_while_settling_shadow_task_propagates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _enable_shadow(monkeypatch)
    _stub_pipeline_construction(monkeypatch)
    shadow_started = asyncio.Event()

    async def pending_shadow(
        self: ShadowRelevanceRunner, **kwargs: object
    ) -> int | None:
        del self, kwargs
        shadow_started.set()
        await asyncio.Future[None]()
        return None

    result = Mock()
    result.step_outputs = {
        "score_and_draft": Ok(ReplyCandidate(relevant=False, score=0.1, reason="fixture"))
    }
    monkeypatch.setattr(ShadowRelevanceRunner, "run", pending_shadow)
    monkeypatch.setattr(scan_runner, "run_pipeline", AsyncMock(return_value=result))

    with StateManager(":memory:") as state:
        state.registry.upsert_project("agent-ops", "Agent Ops", "desc", "https://example.test")
        state.registry.upsert_keyword("agent-ops", "agent")
        route = state.load_runtime_registry().keywords[0]
        scan_id = state.start_scan()
        task = asyncio.create_task(
            _score(
                state,
                scan_id,
                [RoutedMessage(message=_message("known-post"), keyword_route=route)],
                {"agent-ops": _project()},
                tmp_path / "digest.md",
            )
        )
        await shadow_started.wait()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_shadow_objects_are_not_constructed_when_flag_is_unset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(config, "TYPESAFE_SHADOW_MODE", False)
    _stub_pipeline_construction(monkeypatch)
    runner_constructor = Mock(side_effect=AssertionError("runner constructed"))
    backend_constructor = Mock(side_effect=AssertionError("backend constructed"))
    catalogue_loader = Mock(side_effect=AssertionError("catalogue loaded"))
    monkeypatch.setattr(scan_runner, "ShadowRelevanceRunner", runner_constructor)
    monkeypatch.setattr(shadow_module, "PlaceholderBackend", backend_constructor)
    monkeypatch.setattr(shadow_module, "load_catalogue", catalogue_loader)
    monkeypatch.setattr(
        scan_runner,
        "run_pipeline",
        AsyncMock(side_effect=RuntimeError("continue")),
    )

    with StateManager(":memory:") as state:
        state.registry.upsert_project("agent-ops", "Agent Ops", "desc", "https://example.test")
        state.registry.upsert_keyword("agent-ops", "agent")
        route = state.load_runtime_registry().keywords[0]
        scan_id = state.start_scan()
        await _score(
            state,
            scan_id,
            [RoutedMessage(message=_message("known-post"), keyword_route=route)],
            {"agent-ops": _project()},
            tmp_path / "digest.md",
        )

    runner_constructor.assert_not_called()
    backend_constructor.assert_not_called()
    catalogue_loader.assert_not_called()
