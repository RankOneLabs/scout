"""Zero-shot holdout selection and atomic persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from scout.config import Account, Message, RelevanceResult
from scout.registry import KeywordRoute, ProjectTarget, RuntimeRegistry
from scout.relevance.holdout import draw_relevance_holdout
from scout.relevance.models import RelevanceAction, ZeroShotRelevanceOutput
from scout.result import Ok
from scout.scanning.pipeline import PhaseExecution, score_and_draft_step
from scout.scanning.prefilter import RoutedMessage
from scout.scanning.runner import PersistenceContext, classify_outcome, persist_outcome
from scout.scanning.schemas import ReplyCandidate
from scout.storage.relevance_holdouts import RelevanceHoldoutStore
from scout.storage.state import StateManager


@dataclass
class StubRng:
    value: float
    calls: int = 0

    def random(self) -> float:
        self.calls += 1
        return self.value


def _message(platform_id: str = "held-post") -> Message:
    return Message(
        platform="bluesky",
        platform_id=platform_id,
        channel_name="agents",
        channel_id="agents",
        author=Account(platform="bluesky", id="did:example:ada", name="Ada", handle="ada.test"),
        content="How should an agent evaluation be operated?",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        url=f"https://example.test/{platform_id}",
    )


def _registry() -> RuntimeRegistry:
    project = ProjectTarget(
        key="agent-ops",
        name="Agent Ops",
        description="Operations for agent systems",
        link="https://example.test/agent-ops",
        dossier_summary_id="agent-ops-dossier",
    )
    route = KeywordRoute(
        id=7,
        project_key="agent-ops",
        keyword="agent",
        evaluate_prompt=None,
        respond_prompt=None,
        critique_prompt=None,
        priority=0,
    )
    return RuntimeRegistry(
        projects={project.key: project},
        keywords=(route,),
        prompt_templates={"evaluate": "classify"},
    )


def _relevance_phase_run(state: StateManager, scan_id: int, post_id: int) -> int:
    snapshot = state.record_feedback_snapshot(scan_id, mode="shadow")
    relevance = next(phase for phase in snapshot.phases if phase.phase == "relevance")
    return state.insert_phase_run(
        scan_id=scan_id,
        post_id=post_id,
        snapshot_phase_id=relevance.snapshot_phase_id,
        phase="relevance",
        trace_id=f"trace-{post_id}",
        model="zeroshot:test",
        status="complete",
    )


@pytest.mark.parametrize("action", ["respond", "review", "drop"])
def test_draw_covers_every_zeroshot_action(action: RelevanceAction) -> None:
    rng = StubRng(0.24)

    held = draw_relevance_holdout(
        classifier="zeroshot",
        project_key="agent-ops",
        production_action=action,
        rate=0.25,
        rng=rng,
    )

    assert held is True
    assert rng.calls == 1


@pytest.mark.parametrize(
    ("classifier", "project_key"),
    [("llm", "agent-ops"), ("zeroshot", "gateway"), ("zeroshot", None)],
)
def test_ineligible_posts_are_never_drawn(classifier: str, project_key: str | None) -> None:
    rng = StubRng(0.0)

    held = draw_relevance_holdout(
        classifier=classifier,  # type: ignore[arg-type]
        project_key=project_key,
        production_action="respond",
        rate=1.0,
        rng=rng,
    )

    assert held is False
    assert rng.calls == 0


def test_above_rate_is_not_held() -> None:
    assert not draw_relevance_holdout(
        classifier="zeroshot",
        project_key="agent-evals",
        production_action="respond",
        rate=0.25,
        rng=StubRng(0.25),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["respond", "drop"])
async def test_selected_zeroshot_post_returns_held_before_drafting(
    monkeypatch: pytest.MonkeyPatch,
    action: RelevanceAction,
) -> None:
    import scout.scanning.pipeline as pipeline

    relevance = ZeroShotRelevanceOutput(
        relevant=action == "respond",
        score=1.0,
        reason="in_post",
        relevant_to=["agent-ops"],
        action=action,
        answers={"q1": 1.0},
        line="in_post",
        margin=(),
        exclusion=None,
    )
    monkeypatch.setattr(
        pipeline,
        "_run_phase",
        AsyncMock(
            return_value=Ok(
                PhaseExecution(
                    parsed=relevance,
                    trace_id="trace-held",
                    phase_run_id=91,
                    phase="relevance",
                    model="zeroshot:test",
                )
            )
        ),
    )
    draft = AsyncMock()
    monkeypatch.setattr(pipeline, "_draft_and_critic", draft)
    monkeypatch.setattr(pipeline, "build_state", Mock(return_value={}))
    monkeypatch.setattr(pipeline._config, "RELEVANCE_CLASSIFIER", "zeroshot")
    project = _registry().projects["agent-ops"]
    context = {
        "input": RoutedMessage(_message(), _registry().keywords[0]),
        "phase_configs": SimpleNamespace(relevance=object()),
        "dossier_summaries": {},
        "execution_context": SimpleNamespace(
            state=object(),
            scan_id=1,
            post_id=2,
            relevance=SimpleNamespace(snapshot_phase_id=3, model="zeroshot:test"),
        ),
        "zeroshot_context": SimpleNamespace(
            client=SimpleNamespace(model="test"),
            catalogue=object(),
            questions=(),
            projects={"agent-ops": project},
        ),
        "holdout_draw": lambda classifier, project_key, action: draw_relevance_holdout(
            classifier=classifier,
            project_key=project_key,
            production_action=action,
            rate=0.25,
            rng=StubRng(0.24),
        ),
    }

    result = await score_and_draft_step(context)

    assert isinstance(result, Ok)
    assert result.value.held is True
    assert result.value.structured_draft is None
    assert result.value.relevance_action == action
    draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_unselected_zeroshot_post_drafts_normally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scout.scanning.pipeline as pipeline

    relevance = ZeroShotRelevanceOutput(
        relevant=True,
        score=1.0,
        reason="in_post",
        relevant_to=["agent-ops"],
        action="respond",
        answers={"q1": 1.0},
        line="in_post",
        margin=(),
        exclusion=None,
    )
    monkeypatch.setattr(
        pipeline,
        "_run_phase",
        AsyncMock(
            return_value=Ok(
                PhaseExecution(
                    parsed=relevance,
                    trace_id="trace-normal",
                    phase_run_id=92,
                    phase="relevance",
                    model="zeroshot:test",
                )
            )
        ),
    )
    expected = ReplyCandidate(
        relevant=True,
        score=1.0,
        reason="in_post",
        relevant_to=["agent-ops"],
        project_key="agent-ops",
        relevance_action="respond",
    )
    draft = AsyncMock(return_value=Ok(expected))
    monkeypatch.setattr(pipeline, "_draft_and_critic", draft)
    monkeypatch.setattr(pipeline, "build_state", Mock(return_value={}))
    monkeypatch.setattr(pipeline._config, "RELEVANCE_CLASSIFIER", "zeroshot")
    project = _registry().projects["agent-ops"]

    result = await score_and_draft_step(
        {
            "input": RoutedMessage(_message(), _registry().keywords[0]),
            "phase_configs": SimpleNamespace(relevance=object()),
            "dossier_summaries": {},
            "execution_context": SimpleNamespace(
                state=object(),
                scan_id=1,
                post_id=2,
                relevance=SimpleNamespace(snapshot_phase_id=3, model="zeroshot:test"),
            ),
            "zeroshot_context": SimpleNamespace(
                client=SimpleNamespace(model="test"),
                catalogue=object(),
                questions=(),
                projects={"agent-ops": project},
            ),
            "holdout_draw": lambda classifier, project_key, action: draw_relevance_holdout(
                classifier=classifier,
                project_key=project_key,
                production_action=action,
                rate=0.25,
                rng=StubRng(0.25),
            ),
        }
    )

    assert result == Ok(expected)
    draft.assert_awaited_once()


def test_held_evaluation_and_holdout_are_atomic_and_freeze_context(
    in_memory_state: StateManager,
) -> None:
    registry = _registry()
    message = _message()
    scan_id = in_memory_state.start_scan(environment="test")
    post_id = in_memory_state.save_post(message, scan_id)
    phase_run_id = _relevance_phase_run(in_memory_state, scan_id, post_id)
    candidate = ReplyCandidate(
        relevant=True,
        score=1.0,
        reason="in post",
        relevant_to=["agent-ops"],
        project_key="agent-ops",
        relevance_classifier="zeroshot:test",
        relevance_action="respond",
        held=True,
        contributor_phase_run_ids=(phase_run_id,),
    )
    decision = classify_outcome(candidate, message, {})

    evaluation_id = persist_outcome(
        in_memory_state,
        decision,
        PersistenceContext(
            post_id=post_id,
            scan_id=scan_id,
            keyword_route_id=None,
            dossier_revision="d" * 40,
            dossier_summary_id="agent-ops-dossier",
            surfaced_at=message.created_at.isoformat(),
            registry=registry,
        ),
    )
    holdout = in_memory_state.relevance_holdouts.get_for_evaluation(evaluation_id)

    evaluation = in_memory_state.conn.execute(
        "SELECT relevant, surface_status FROM evaluations WHERE id = ?",
        (evaluation_id,),
    ).fetchone()
    assert evaluation is not None
    assert holdout is not None
    assert (evaluation["relevant"], evaluation["surface_status"]) == (1, "not_relevant")
    assert holdout.held is True
    assert holdout.production_action == "respond"
    assert holdout.dossier_revision == "d" * 40
    assert holdout.registry_state["projects"] == {
        "agent-ops": {
            "key": "agent-ops",
            "name": "Agent Ops",
            "description": "Operations for agent systems",
            "link": "https://example.test/agent-ops",
            "dossier_summary_id": "agent-ops-dossier",
        }
    }
    assert holdout.batch_id is None
    assert holdout.exported_at is None


def test_failure_after_evaluation_write_rolls_back_both_rows_and_post_retries(
    in_memory_state: StateManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = _message("retryable-post")
    scan_id = in_memory_state.start_scan(environment="test")
    post_id = in_memory_state.save_post(message, scan_id)
    phase_run_id = _relevance_phase_run(in_memory_state, scan_id, post_id)
    result = RelevanceResult(
        message=message,
        relevant=False,
        score=0.0,
        reason="exclusion",
    )

    def fail_between_rows(self: RelevanceHoldoutStore, **kwargs: object) -> None:
        raise sqlite_error

    sqlite_error = RuntimeError("simulated holdout insert failure")
    monkeypatch.setattr(RelevanceHoldoutStore, "record_holdout", fail_between_rows)

    with pytest.raises(RuntimeError, match="simulated holdout insert failure"):
        in_memory_state.relevance_holdouts.persist_held_evaluation(
            result,
            post_id,
            scan_id,
            production_action="drop",
            registry=_registry(),
            contributor_phase_run_ids=(phase_run_id,),
            keyword_route_id=None,
            project_key="agent-ops",
            dossier_revision="d" * 40,
            dossier_summary_id="agent-ops-dossier",
        )

    assert in_memory_state.conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 0
    assert (
        in_memory_state.conn.execute("SELECT COUNT(*) FROM relevance_holdouts").fetchone()[0] == 0
    )
    assert [post.platform_id for post in in_memory_state.load_unevaluated_posts()] == [
        "retryable-post"
    ]
