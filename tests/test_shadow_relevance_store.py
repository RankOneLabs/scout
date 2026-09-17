from datetime import UTC, datetime

from scout.config import Account, Message, RelevanceResult
from scout.storage.shadow_relevance import ShadowRunWrite
from scout.storage.state import StateManager


def _parents(state: StateManager) -> tuple[int, int, Message]:
    scan_id = state.start_scan()
    msg = Message(
        "bluesky",
        "shadow-post",
        "feed",
        "feed",
        Account("bluesky", "author", "Author", "author.test"),
        "text",
        datetime.now(UTC),
    )
    return scan_id, state.save_post(msg, scan_id), msg


def test_record_is_idempotent_and_backfills_evaluation() -> None:
    with StateManager(":memory:") as state:
        scan_id, post_id, msg = _parents(state)
        write = ShadowRunWrite(
            scan_id=scan_id,
            post_id=post_id,
            backend="placeholder",
            model="fixture",
            catalogue_id="catalogue",
            catalogue_version="a" * 64,
            request_id="request-1",
            state={"post": {"id": "shadow-post"}},
            status="ok",
            answers={"answers": {}},
            decision={"eligible": True},
            eligible=True,
            p_eligible=0.9,
            uncertain=False,
        )
        first = state.shadow_relevance.record_shadow_run(write)
        second = state.shadow_relevance.record_shadow_run(write)
        assert first.id == second.id
        assert len(state.shadow_relevance.list_runs_for_scan(scan_id)) == 1

        evaluation_id = state.save_evaluation(
            RelevanceResult(msg, True, 0.9, "fixture"), post_id, scan_id
        )
        assert state.shadow_relevance.backfill_evaluation_id(first.id, evaluation_id)
        assert not state.shadow_relevance.backfill_evaluation_id(first.id, evaluation_id)
        since = state.shadow_relevance.list_runs_since(first.created_at)
        assert since[0].evaluation_id == evaluation_id
