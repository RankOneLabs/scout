"""Holdout sampling at the relevance boundary: the draw, and what it stops.

The draw itself is a pure transform and is tested as one. The boundary it
sits on is tested through the real pipeline step with spied phase runners,
because "no draft call was made" is a claim about the step's control flow,
not about a value it returns.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any

import pytest

import scout.scanning.runner as scan_runner
from scout.config import Account, Message
from scout.result import Err, Ok
from scout.scanning.pipeline import (
    build_holdout_sampler,
    holdout_decision_key,
    score_and_draft_step,
    stable_holdout_draw,
)
from scout.scanning.prefilter import RoutedMessage
from scout.scanning.relevance import PhaseExecution
from scout.scanning.schemas import (
    CritiquePhaseOutput,
    HoldoutDraw,
    RecordedRelevanceDecision,
    RelevancePhaseOutput,
    ReplyCandidate,
    StructuredDraftOutput,
)
from scout.storage.holdouts import HoldoutStorageError, HoldoutWrite
from scout.storage.state import StateManager
from tests.conftest import seed_phase_run_contributors


def _sm() -> Generator[StateManager, None, None]:
    state = StateManager(db_path=":memory:")
    yield state
    state.commit()
    state.close()


sm = pytest.fixture(_sm)


def _message(platform_id: str = "0xabc") -> Message:
    return Message(
        platform="farcaster",
        platform_id=platform_id,
        channel_name="agents",
        channel_id="agents",
        author=Account(platform="farcaster", id="42", name="Ada", handle="ada"),
        content="our planner retries every failed step twice",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        url=f"https://warpcast.com/ada/{platform_id}",
    )


# --- the draw ---------------------------------------------------------------


def test_rate_zero_selects_nothing_across_the_whole_key_space() -> None:
    keys = [f"farcaster:{index}" for index in range(500)]
    assert not any(stable_holdout_draw(decision_key=key, rate=0.0).selected for key in keys)


def test_rate_one_selects_everything_across_the_whole_key_space() -> None:
    keys = [f"farcaster:{index}" for index in range(500)]
    assert all(stable_holdout_draw(decision_key=key, rate=1.0).selected for key in keys)


def test_an_intermediate_rate_lands_near_it_over_many_keys() -> None:
    keys = [f"farcaster:{index}" for index in range(2000)]
    selected = sum(stable_holdout_draw(decision_key=key, rate=0.1).selected for key in keys)
    assert 0.07 <= selected / len(keys) <= 0.13


def test_the_same_key_draws_the_same_value_every_time() -> None:
    first = stable_holdout_draw(decision_key="farcaster:0xabc", rate=0.5)
    second = stable_holdout_draw(decision_key="farcaster:0xabc", rate=0.5)
    assert first == second


def test_a_draw_records_the_rate_that_produced_it() -> None:
    assert stable_holdout_draw(decision_key="farcaster:0xabc", rate=0.3).rate == 0.3


def test_selection_is_monotonic_in_the_rate() -> None:
    """A key selected at a lower rate stays selected at a higher one.

    This is what lets an operator raise the rate without the already-held
    population changing underneath the labels written against it.
    """
    key = holdout_decision_key(platform="farcaster", platform_id="0xabc")
    drawn = stable_holdout_draw(decision_key=key, rate=1.0).value
    lower = stable_holdout_draw(decision_key=key, rate=min(1.0, drawn + 0.01))
    higher = stable_holdout_draw(decision_key=key, rate=1.0)
    assert lower.selected and higher.selected


def test_the_decision_key_is_platform_identity_only() -> None:
    assert holdout_decision_key(platform="farcaster", platform_id="0xabc") == "farcaster:0xabc"


@pytest.mark.parametrize("rate", [-0.1, 1.1, 2.0])
def test_the_sampler_rejects_a_rate_outside_the_unit_interval(rate: float) -> None:
    with pytest.raises(ValueError, match="between 0.0 and 1.0"):
        build_holdout_sampler(rate)


@pytest.mark.parametrize("rate", [0.0, 0.1, 1.0])
def test_the_sampler_accepts_every_rate_inside_the_unit_interval(rate: float) -> None:
    assert build_holdout_sampler(rate)(platform="farcaster", platform_id="0xabc").rate == rate


def test_the_configured_default_rate_is_one_tenth() -> None:
    import scout.config as config

    assert config.RELEVANCE_HOLDOUT_RATE == 0.1


# --- the boundary -----------------------------------------------------------


class _PhaseSpy:
    """Counts every phase the pipeline step actually runs."""

    def __init__(self, relevance: RelevancePhaseOutput) -> None:
        self.relevance = relevance
        self.calls: list[str] = []

    async def run(self, **kwargs: Any) -> Any:
        phase = kwargs["phase"]
        self.calls.append(phase)
        if phase == "relevance":
            return Ok(
                PhaseExecution(
                    parsed=self.relevance,
                    trace_id="trace-1",
                    phase_run_id=1,
                    phase=phase,
                    model="relevance-model",
                )
            )
        if phase == "reply_draft":
            return Ok(
                PhaseExecution(
                    parsed=StructuredDraftOutput(posture="engage", segments=[], claims=[]),
                    trace_id="trace-2",
                    phase_run_id=2,
                    phase=phase,
                    model="draft-model",
                )
            )
        return Ok(
            PhaseExecution(
                parsed=CritiquePhaseOutput(verdict="approve", feedback="ok"),
                trace_id="trace-3",
                phase_run_id=3,
                phase=phase,
                model="critic-model",
            )
        )


def _context(msg: Message) -> dict[str, Any]:
    from unittest.mock import Mock

    execution = Mock()
    execution.state = Mock()
    execution.scan_id = 1
    execution.post_id = 1
    execution.relevance = Mock(snapshot_phase_id=1, model="relevance-model")
    execution.reply_draft = Mock(snapshot_phase_id=2, model="draft-model")
    execution.critic = Mock(snapshot_phase_id=3, model="critic-model")
    return {
        "input": RoutedMessage(message=msg, keyword_route=None),
        "phase_configs": Mock(),
        "dossier_summaries": {},
        "execution_context": execution,
    }


def _always_held(*, platform: str, platform_id: str) -> HoldoutDraw:
    return HoldoutDraw(
        decision_key=holdout_decision_key(platform=platform, platform_id=platform_id),
        rate=1.0,
        value=0.0,
        selected=True,
    )


def _never_held(*, platform: str, platform_id: str) -> HoldoutDraw:
    return HoldoutDraw(
        decision_key=holdout_decision_key(platform=platform, platform_id=platform_id),
        rate=0.0,
        value=0.5,
        selected=False,
    )


@pytest.mark.parametrize(
    ("relevant", "score"),
    [
        pytest.param(True, 0.9, id="positive"),
        pytest.param(False, 0.1, id="negative"),
        pytest.param(True, 0.2, id="llm-low-score"),
    ],
)
async def test_a_held_decision_runs_no_phase_after_relevance(
    monkeypatch: pytest.MonkeyPatch, relevant: bool, score: float
) -> None:
    spy = _PhaseSpy(
        RelevancePhaseOutput(relevant=relevant, score=score, reason="r", relevant_to=["agent-ops"])
    )
    monkeypatch.setattr("scout.scanning.pipeline._run_phase", spy.run)
    ctx = _context(_message()) | {"holdout_sampler": _always_held}

    result = await score_and_draft_step(ctx)

    assert isinstance(result, Ok)
    assert spy.calls == ["relevance"]
    assert result.value.holdout is not None and result.value.holdout.selected
    assert result.value.structured_draft is None
    assert result.value.critique_verdict is None


async def test_a_held_decision_keeps_the_classifier_relevance_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _PhaseSpy(
        RelevancePhaseOutput(relevant=True, score=0.9, reason="r", relevant_to=["agent-ops"])
    )
    monkeypatch.setattr("scout.scanning.pipeline._run_phase", spy.run)
    ctx = _context(_message()) | {"holdout_sampler": _always_held}

    result = await score_and_draft_step(ctx)

    assert isinstance(result, Ok)
    assert result.value.relevant is True


async def test_an_unselected_draw_still_drafts_and_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _PhaseSpy(
        RelevancePhaseOutput(relevant=True, score=0.9, reason="r", relevant_to=["agent-ops"])
    )
    monkeypatch.setattr("scout.scanning.pipeline._run_phase", spy.run)
    monkeypatch.setattr("scout.scanning.pipeline.format_reply_draft_input", lambda **_: "draft")
    monkeypatch.setattr("scout.scanning.pipeline.format_critic_input", lambda **_: "critic")
    ctx = _context(_message()) | {"holdout_sampler": _never_held}

    result = await score_and_draft_step(ctx)

    assert isinstance(result, Ok)
    assert spy.calls == ["relevance", "reply_draft", "critic"]
    assert result.value.holdout is not None and not result.value.holdout.selected


async def test_a_failed_relevance_call_is_never_sampled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No decision, no draw. A retry must be free to decide the post later."""
    drawn: list[str] = []

    def _recording_sampler(*, platform: str, platform_id: str) -> HoldoutDraw:
        drawn.append(platform_id)
        return _always_held(platform=platform, platform_id=platform_id)

    from scout.errors import LLMError

    async def _failing(**kwargs: Any) -> Any:
        return Err(LLMError(operation="relevance", message_id="0xabc", detail="boom"))

    monkeypatch.setattr("scout.scanning.pipeline._run_phase", _failing)
    ctx = _context(_message()) | {"holdout_sampler": _recording_sampler}

    result = await score_and_draft_step(ctx)

    assert isinstance(result, Err)
    assert drawn == []


async def test_no_sampler_means_no_holdout_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """The human-override path supplies none; it must not start holding posts."""
    spy = _PhaseSpy(
        RelevancePhaseOutput(relevant=False, score=0.1, reason="r", relevant_to=[])
    )
    monkeypatch.setattr("scout.scanning.pipeline._run_phase", spy.run)

    result = await score_and_draft_step(_context(_message()))

    assert isinstance(result, Ok)
    assert result.value.holdout is None


# --- what a held decision persists ------------------------------------------


def _candidate(
    *,
    relevant: bool = True,
    score: float = 0.9,
    selected: bool = True,
    action: str = "respond",
    classifier: str = "jev",
    contributor_phase_run_ids: tuple[int, ...] = (),
) -> ReplyCandidate:
    return ReplyCandidate(
        relevant=relevant,
        score=score,
        reason="the classifier's reason",
        relevant_to=["agent-ops"],
        project_key="agent-ops",
        contributor_phase_run_ids=contributor_phase_run_ids,
        relevance_decision=RecordedRelevanceDecision(
            classifier=classifier,  # type: ignore[arg-type]
            model="jev-latest",
            action=action,  # type: ignore[arg-type]
            reason="the classifier's reason",
        ),
        holdout=HoldoutDraw(
            decision_key="farcaster:0xabc", rate=1.0, value=0.0, selected=selected
        ),
    )


def _persist_held(
    state: StateManager,
    candidate: ReplyCandidate,
    msg: Message,
    *,
    scan_id: int | None = None,
) -> tuple[int, int, int]:
    """Classify and persist one candidate. Returns (evaluation, post, scan).

    Seeds one durable relevance phase run, because that is exactly what a
    held decision contributes: the relevance call and nothing after it.
    """
    scan_id = state.start_scan(environment="test") if scan_id is None else scan_id
    post_id = state.save_post(msg, scan_id)
    contributors = seed_phase_run_contributors(state, scan_id, post_id, count=1)
    candidate = candidate.model_copy(update={"contributor_phase_run_ids": contributors})
    decision = scan_runner.classify_outcome(candidate, msg, {})
    context = scan_runner.PersistenceContext(
        post_id=post_id,
        scan_id=scan_id,
        keyword_route_id=None,
        dossier_revision="r1",
        dossier_summary_id="d1",
        surfaced_at=msg.created_at.isoformat(),
        project=scan_runner.FrozenProjectIdentity(
            key="agent-ops", name="Agent Ops", description="A description."
        ),
    )
    evaluation_id = scan_runner.persist_outcome(state, decision, context)
    return evaluation_id, post_id, scan_id


@pytest.mark.parametrize(
    ("relevant", "score", "action"),
    [
        pytest.param(True, 1.0, "respond", id="respond"),
        pytest.param(True, 1.0, "review", id="review"),
        pytest.param(False, 0.0, "drop", id="drop"),
        pytest.param(True, 0.2, "drop", id="llm-low-score-drop"),
    ],
)
def test_every_action_can_be_held(
    sm: StateManager, relevant: bool, score: float, action: str
) -> None:
    msg = _message()
    evaluation_id, _post_id, _scan_id = _persist_held(
        sm, _candidate(relevant=relevant, score=score, action=action), msg
    )

    row = sm.get_evaluation(evaluation_id)
    assert row is not None and row["surface_status"] == "held"
    held = sm.holdouts.get_by_evaluation(evaluation_id)
    assert held is not None and held.status == "pending"


def test_a_held_decision_is_never_drafting_failed(sm: StateManager) -> None:
    """A hold has no draft by construction; that must not read as a defect."""
    candidate = _candidate().model_copy(update={"project_key": None, "relevant_to": []})
    decision = scan_runner.classify_outcome(candidate, _message(), {})
    assert decision.status == "held"


def test_a_held_decision_records_its_selection_and_evidence(sm: StateManager) -> None:
    msg = _message()
    evaluation_id, post_id, scan_id = _persist_held(sm, _candidate(), msg)

    recorded = sm.holdouts.get_decision(evaluation_id)
    assert recorded is not None
    assert recorded.selected_for_holdout is True
    assert recorded.action == "respond"
    held = sm.holdouts.get_by_evaluation(evaluation_id)
    assert held is not None
    assert (held.post_id, held.scan_id, held.project_key) == (post_id, scan_id, "agent-ops")
    assert held.frozen_input.text == msg.content
    assert held.frozen_input.project_name == "Agent Ops"


def test_an_unselected_draw_records_a_decision_and_no_hold(sm: StateManager) -> None:
    msg = _message()
    scan_id = sm.start_scan(environment="test")
    post_id = sm.save_post(msg, scan_id)
    contributors = seed_phase_run_contributors(sm, scan_id, post_id, count=1)
    candidate = _candidate(
        relevant=False,
        score=0.0,
        action="drop",
        selected=False,
        contributor_phase_run_ids=contributors,
    )
    decision = scan_runner.classify_outcome(candidate, msg, {})
    assert decision.status == "not_relevant"
    evaluation_id = scan_runner.persist_outcome(
        sm,
        decision,
        scan_runner.PersistenceContext(
            post_id=post_id,
            scan_id=scan_id,
            keyword_route_id=None,
            dossier_revision=None,
            dossier_summary_id=None,
            surfaced_at=msg.created_at.isoformat(),
        ),
    )

    recorded = sm.holdouts.get_decision(evaluation_id)
    assert recorded is not None and recorded.selected_for_holdout is False
    assert sm.holdouts.get_by_evaluation(evaluation_id) is None


def test_a_refused_hold_rolls_the_whole_outcome_back(
    sm: StateManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No partial result: no evaluation, no decision row, and the post stays."""
    msg = _message()

    def _refuse(_write: object) -> object:
        return Err(
            HoldoutStorageError(operation="hold", detail="injected failure", evaluation_id=None)
        )

    monkeypatch.setattr(sm.holdouts, "hold", _refuse)
    with pytest.raises(RuntimeError, match="injected failure"):
        _persist_held(sm, _candidate(), msg)

    post_row = sm.conn.execute(
        "SELECT id FROM posts WHERE platform_msg_id = ?", (msg.platform_id,)
    ).fetchone()
    assert post_row is not None
    assert sm.conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 0
    assert sm.conn.execute("SELECT COUNT(*) FROM relevance_decisions").fetchone()[0] == 0
    assert sm.conn.execute("SELECT COUNT(*) FROM relevance_holdouts").fetchone()[0] == 0


def test_a_second_hold_on_the_same_source_evaluation_is_refused(sm: StateManager) -> None:
    """The source UNIQUE is the last line: one evaluation, at most one hold."""
    msg = _message()
    evaluation_id, post_id, scan_id = _persist_held(sm, _candidate(), msg)

    second = sm.holdouts.hold(
        HoldoutWrite(
            evaluation_id=evaluation_id,
            post_id=post_id,
            scan_id=scan_id,
            project_key="agent-ops",
            frozen_input=sm.holdouts.get(1).frozen_input,  # type: ignore[union-attr]
        )
    )

    assert isinstance(second, Err)
    assert sm.conn.execute("SELECT COUNT(*) FROM relevance_holdouts").fetchone()[0] == 1


def test_a_retry_over_a_held_post_writes_no_second_decision(sm: StateManager) -> None:
    """A concurrent scan or a crash retry finds the hold and leaves it alone.

    The scan loop's guard is the one that matters here: an already-held post
    never reaches the classifier a second time, so the single held decision
    stays the one that was graded.
    """
    msg = _message()
    _evaluation_id, post_id, _scan_id = _persist_held(sm, _candidate(), msg)

    assert sm.holdouts.has_hold_for_post(post_id) is True
    assert sm.conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 1
    assert sm.conn.execute("SELECT COUNT(*) FROM relevance_decisions").fetchone()[0] == 1


def test_a_held_post_is_recognised_as_held(sm: StateManager) -> None:
    msg = _message()
    _evaluation_id, post_id, _scan_id = _persist_held(sm, _candidate(), msg)

    assert sm.holdouts.has_hold_for_post(post_id) is True
    assert sm.holdouts.has_hold_for_post(post_id + 1000) is False


def test_held_relevance_evidence_stays_queryable(sm: StateManager) -> None:
    msg = _message()
    evaluation_id, post_id, _scan_id = _persist_held(sm, _candidate(), msg)

    row = sm.conn.execute(
        "SELECT e.score, e.reason, e.relevant, d.classifier, d.action "
        "FROM evaluations e JOIN relevance_decisions d ON d.evaluation_id = e.id "
        "JOIN relevance_holdouts h ON h.evaluation_id = e.id WHERE e.id = ?",
        (evaluation_id,),
    ).fetchone()
    assert row is not None
    assert (row["classifier"], row["action"]) == ("jev", "respond")
    assert sm.load_post(post_id) is not None
