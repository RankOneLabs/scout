from datetime import UTC, datetime

from scout.config import Account, Message, RelevanceResult
from scout.registry import ProjectTarget
from scout.storage.state import StateManager
from scout.typesafe.catalogue import load_catalogue
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


async def test_shadow_backend_exception_becomes_error_row(caplog) -> None:
    async def raising_backend(state, catalogue):
        del state, catalogue
        raise RuntimeError("backend down")

    catalogue = load_catalogue("tests/fixtures/typesafe/fixture-two-question.v1.yaml")
    runner = ShadowRelevanceRunner(catalogue, "placeholder", raising_backend)
    project = ProjectTarget("agent-ops", "Agent Ops", "desc", "https://example.test")
    with StateManager(":memory:") as state:
        scan_id = state.start_scan()
        message = _message("error-post")
        post_id = state.save_post(message, scan_id)
        assert await runner.run(
            state_manager=state,
            scan_id=scan_id,
            post_id=post_id,
            message=message,
            project=project,
        ) is None
        row = state.shadow_relevance.list_runs_for_scan(scan_id)[0]
        assert row.status == "error"
        assert "backend down" in (row.error_detail or "")
        assert "typesafe shadow evaluation failed" in caplog.text
