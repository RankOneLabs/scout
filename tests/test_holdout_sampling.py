"""Holdout sampling at the relevance boundary: the draw, and what it stops.

The draw itself is a pure transform and is tested as one. The boundary it
sits on is tested through the real pipeline step with spied phase runners,
because "no draft call was made" is a claim about the step's control flow,
not about a value it returns.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable, Generator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
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
    unpack_candidate,
)
from scout.storage.holdouts import HoldoutStorageError, HoldoutWrite, SamplingWrite
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
    """Counts every phase the pipeline step actually runs.

    `on_call` is the seam for asserting what is already durable at the moment
    a phase starts — "the draw was recorded before drafting" is a claim about
    ordering, and the only place to observe it is from inside the next phase.
    """

    def __init__(
        self,
        relevance: RelevancePhaseOutput,
        *,
        phase_run_ids: tuple[int, ...] = (1, 2, 3),
        on_call: Callable[[str], None] | None = None,
    ) -> None:
        self.relevance = relevance
        self.phase_run_ids = phase_run_ids
        self.on_call = on_call
        self.calls: list[str] = []

    async def run(self, **kwargs: Any) -> Any:
        phase = kwargs["phase"]
        self.calls.append(phase)
        if self.on_call is not None:
            self.on_call(phase)
        if phase == "relevance":
            return Ok(
                PhaseExecution(
                    parsed=self.relevance,
                    trace_id="trace-1",
                    phase_run_id=self.phase_run_ids[0],
                    phase=phase,
                    model="relevance-model",
                )
            )
        if phase == "reply_draft":
            return Ok(
                PhaseExecution(
                    parsed=StructuredDraftOutput(posture="engage", segments=[], claims=[]),
                    trace_id="trace-2",
                    phase_run_id=self.phase_run_ids[1],
                    phase=phase,
                    model="draft-model",
                )
            )
        return Ok(
            PhaseExecution(
                parsed=CritiquePhaseOutput(verdict="approve", feedback="ok"),
                trace_id="trace-3",
                phase_run_id=self.phase_run_ids[2],
                phase=phase,
                model="critic-model",
            )
        )


def _context(
    msg: Message,
    *,
    state: StateManager,
    post_id: int,
    scan_id: int,
) -> dict[str, Any]:
    """The pipeline step's context over a real post in a real store.

    The store is real because the boundary writes to it: the draw is recorded
    durably there before any drafting call, so a mock would assert nothing
    about the behaviour that matters. Only the phase identities are stubbed,
    and only because `_run_phase` itself is patched out.
    """
    from unittest.mock import Mock

    execution = Mock()
    execution.state = state
    execution.scan_id = scan_id
    execution.post_id = post_id
    execution.relevance = Mock(snapshot_phase_id=1, model="relevance-model")
    execution.reply_draft = Mock(snapshot_phase_id=2, model="draft-model")
    execution.critic = Mock(snapshot_phase_id=3, model="critic-model")
    return {
        "input": RoutedMessage(message=msg, keyword_route=None),
        "phase_configs": Mock(),
        "dossier_summaries": {},
        "execution_context": execution,
    }


def _boundary(
    state: StateManager, msg: Message, *, sampler: Any = None
) -> tuple[dict[str, Any], int, tuple[int, ...]]:
    """Seed one post and one scan's three phase runs.

    Returns (ctx, post, phase run ids). Real ids because the recorded draw
    cites the relevance run it was taken beside, and because a persisted
    outcome links its contributors — a made-up id fails either foreign key.
    """
    scan_id = state.start_scan(environment="test")
    post_id = state.save_post(msg, scan_id)
    phase_run_ids = seed_phase_run_contributors(state, scan_id, post_id, count=3)
    ctx = _context(msg, state=state, post_id=post_id, scan_id=scan_id)
    if sampler is not None:
        ctx = ctx | {"holdout_sampler": sampler}
    return ctx, post_id, phase_run_ids


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
    sm: StateManager, monkeypatch: pytest.MonkeyPatch, relevant: bool, score: float
) -> None:
    ctx, _post_id, phase_run_ids = _boundary(sm, _message(), sampler=_always_held)
    spy = _PhaseSpy(
        RelevancePhaseOutput(relevant=relevant, score=score, reason="r", relevant_to=["agent-ops"]),
        phase_run_ids=phase_run_ids,
    )
    monkeypatch.setattr("scout.scanning.pipeline._run_phase", spy.run)

    result = await score_and_draft_step(ctx)

    assert isinstance(result, Ok)
    assert spy.calls == ["relevance"]
    assert result.value.holdout is not None and result.value.holdout.selected
    assert result.value.structured_draft is None
    assert result.value.critique_verdict is None


async def test_a_held_decision_keeps_the_classifier_relevance_verdict(
    sm: StateManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _post_id, phase_run_ids = _boundary(sm, _message(), sampler=_always_held)
    spy = _PhaseSpy(
        RelevancePhaseOutput(relevant=True, score=0.9, reason="r", relevant_to=["agent-ops"]),
        phase_run_ids=phase_run_ids,
    )
    monkeypatch.setattr("scout.scanning.pipeline._run_phase", spy.run)

    result = await score_and_draft_step(ctx)

    assert isinstance(result, Ok)
    assert result.value.relevant is True


async def test_an_unselected_draw_still_drafts_and_is_recorded(
    sm: StateManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _post_id, phase_run_ids = _boundary(sm, _message(), sampler=_never_held)
    spy = _PhaseSpy(
        RelevancePhaseOutput(relevant=True, score=0.9, reason="r", relevant_to=["agent-ops"]),
        phase_run_ids=phase_run_ids,
    )
    monkeypatch.setattr("scout.scanning.pipeline._run_phase", spy.run)
    monkeypatch.setattr("scout.scanning.pipeline.format_reply_draft_input", lambda **_: "draft")
    monkeypatch.setattr("scout.scanning.pipeline.format_critic_input", lambda **_: "critic")

    result = await score_and_draft_step(ctx)

    assert isinstance(result, Ok)
    assert spy.calls == ["relevance", "reply_draft", "critic"]
    assert result.value.holdout is not None and not result.value.holdout.selected


async def test_a_failed_relevance_call_is_never_sampled(
    sm: StateManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No decision, no draw, and nothing recorded against the post.

    A retry must be free to decide the post later, which means the reservation
    must not exist either — a sampling row written for a post with no decision
    would settle its draw on the strength of a call that failed.
    """
    drawn: list[str] = []

    def _recording_sampler(*, platform: str, platform_id: str) -> HoldoutDraw:
        drawn.append(platform_id)
        return _always_held(platform=platform, platform_id=platform_id)

    from scout.errors import LLMError

    async def _failing(**kwargs: Any) -> Any:
        return Err(LLMError(operation="relevance", message_id="0xabc", detail="boom"))

    monkeypatch.setattr("scout.scanning.pipeline._run_phase", _failing)
    ctx, post_id, _phase_run_ids = _boundary(sm, _message(), sampler=_recording_sampler)

    result = await score_and_draft_step(ctx)

    assert isinstance(result, Err)
    assert drawn == []
    assert sm.holdouts.get_sampling_capture(post_id) is None


async def test_no_sampler_means_no_holdout_at_all(
    sm: StateManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The human-override path supplies none; it must not start holding posts."""
    ctx, post_id, phase_run_ids = _boundary(sm, _message())
    spy = _PhaseSpy(
        RelevancePhaseOutput(relevant=False, score=0.1, reason="r", relevant_to=[]),
        phase_run_ids=phase_run_ids,
    )
    monkeypatch.setattr("scout.scanning.pipeline._run_phase", spy.run)

    result = await score_and_draft_step(ctx)

    assert isinstance(result, Ok)
    assert result.value.holdout is None
    assert result.value.holdout_capture is None
    assert sm.holdouts.get_sampling_capture(post_id) is None


# --- the draw is durable before drafting ------------------------------------


async def test_the_draw_is_durable_before_the_first_draft_call(
    sm: StateManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Observed from inside the draft phase, which is the only honest vantage.

    An unselected draw is the case that reaches drafting at all, and it is the
    one a crash could lose: the post is about to spend two more model calls,
    and if nothing recorded the draw first, the retry would be free to draw
    again at whatever rate is in force by then.
    """
    ctx, post_id, phase_run_ids = _boundary(sm, _message(), sampler=_never_held)
    seen: list[Any] = []

    def _observe(phase: str) -> None:
        if phase == "reply_draft":
            seen.append(sm.holdouts.get_sampling_capture(post_id))

    spy = _PhaseSpy(
        RelevancePhaseOutput(relevant=True, score=0.9, reason="r", relevant_to=["agent-ops"]),
        phase_run_ids=phase_run_ids,
        on_call=_observe,
    )
    monkeypatch.setattr("scout.scanning.pipeline._run_phase", spy.run)
    monkeypatch.setattr("scout.scanning.pipeline.format_reply_draft_input", lambda **_: "draft")
    monkeypatch.setattr("scout.scanning.pipeline.format_critic_input", lambda **_: "critic")

    result = await score_and_draft_step(ctx)

    assert isinstance(result, Ok)
    recorded = seen[0]
    assert recorded is not None
    assert (recorded.rate, recorded.selected) == (0.0, False)
    assert recorded.draw == _never_held(platform="farcaster", platform_id="0xabc").value
    assert recorded.evaluation_id is None
    assert recorded.relevance_phase_run_id == phase_run_ids[0]


async def test_a_selected_draw_is_durable_with_the_rate_that_produced_it(
    sm: StateManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, post_id, phase_run_ids = _boundary(sm, _message(), sampler=build_holdout_sampler(1.0))
    spy = _PhaseSpy(
        RelevancePhaseOutput(relevant=True, score=0.9, reason="r", relevant_to=["agent-ops"]),
        phase_run_ids=phase_run_ids,
    )
    monkeypatch.setattr("scout.scanning.pipeline._run_phase", spy.run)

    result = await score_and_draft_step(ctx)

    assert isinstance(result, Ok)
    recorded = sm.holdouts.get_sampling_capture(post_id)
    assert recorded is not None
    assert (recorded.rate, recorded.selected, recorded.fence) == (1.0, True, 1)
    assert recorded.decision_key == "farcaster:0xabc"
    assert result.value.holdout_capture is not None
    assert result.value.holdout_capture.sampling_id == recorded.id


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


# ---------------------------------------------------------------------------
# The recorded decision is the sampling record, and it wins over a redraw
# ---------------------------------------------------------------------------


def test_sampling_is_unsettled_until_a_decision_is_recorded(sm: StateManager) -> None:
    scan_id = sm.start_scan(environment="test")
    post_id = sm.save_post(_message(), scan_id)

    assert sm.holdouts.sampling_is_settled_for_post(post_id) is False


def test_a_held_decision_settles_this_post_s_sampling(sm: StateManager) -> None:
    msg = _message()
    _evaluation_id, post_id, _scan_id = _persist_held(sm, _candidate(), msg)

    assert sm.holdouts.sampling_is_settled_for_post(post_id) is True


def _persist_unselected(
    state: StateManager, msg: Message, *, scan_id: int | None = None
) -> tuple[int, int, int]:
    """Persist one decided-but-unselected post. Returns (evaluation, post, scan).

    A non-relevant decision is the shortest path to the unselected record:
    terminal at relevance, one contributor, no draft to build.
    """
    scan_id = state.start_scan(environment="test") if scan_id is None else scan_id
    post_id = state.save_post(msg, scan_id)
    contributors = seed_phase_run_contributors(state, scan_id, post_id, count=1)
    candidate = _candidate(
        relevant=False, score=0.0, action="drop", selected=False
    ).model_copy(update={"contributor_phase_run_ids": contributors})
    decision = scan_runner.classify_outcome(candidate, msg, {})
    evaluation_id = scan_runner.persist_outcome(
        state,
        decision,
        scan_runner.PersistenceContext(
            post_id=post_id,
            scan_id=scan_id,
            keyword_route_id=None,
            dossier_revision="r1",
            dossier_summary_id="d1",
            surfaced_at=msg.created_at.isoformat(),
        ),
    )
    return evaluation_id, post_id, scan_id


def test_an_unselected_decision_settles_it_too(sm: StateManager) -> None:
    """The unselected side is a recorded answer, not an absence of one.

    `selected_for_holdout = 0` beside the evaluation is what stops a later
    pass from drawing again for a post that was already decided against.
    """
    evaluation_id, post_id, _scan_id = _persist_unselected(sm, _message())

    recorded = sm.holdouts.get_decision(evaluation_id)
    assert recorded is not None and recorded.selected_for_holdout is False
    assert sm.holdouts.get_by_evaluation(evaluation_id) is None
    assert sm.holdouts.sampling_is_settled_for_post(post_id) is True


def test_a_settled_post_is_not_redrawn_when_the_rate_changes(sm: StateManager) -> None:
    """The configuration-change case, which a redraw would get wrong.

    Sampling is a function of the post's identity and the rate in force. A
    rescore that drew again under a lowered rate could un-select a post whose
    hold is already recorded, and under a raised rate could hold one that has
    already surfaced. Reading the recorded answer is what makes the decision
    survive the rate moving underneath it.
    """
    msg = _message()
    _evaluation_id, post_id, _scan_id = _persist_held(sm, _candidate(), msg)

    # The draw itself would flip: this post was selected at rate 1.0 and no
    # key is selected at rate 0.0.
    redrawn = build_holdout_sampler(0.0)(platform=msg.platform, platform_id=msg.platform_id)
    assert redrawn.selected is False

    # The recorded answer does not.
    assert sm.holdouts.sampling_is_settled_for_post(post_id) is True
    held = sm.holdouts.get_by_evaluation(_evaluation_id)
    assert held is not None and held.status == "pending"


def test_a_settled_post_stays_settled_for_a_raised_rate(sm: StateManager) -> None:
    """The other direction: an already-decided post is not newly held."""
    msg = _message()
    _evaluation_id, post_id, _scan_id = _persist_unselected(sm, msg)

    # Every key is selected at rate 1.0, so a redraw would hold this post.
    redrawn = build_holdout_sampler(1.0)(platform=msg.platform, platform_id=msg.platform_id)
    assert redrawn.selected is True

    assert sm.holdouts.sampling_is_settled_for_post(post_id) is True


def test_the_settled_check_is_per_post(sm: StateManager) -> None:
    msg = _message()
    _evaluation_id, post_id, scan_id = _persist_held(sm, _candidate(), msg)
    other_post_id = sm.save_post(_message("0xdef"), scan_id)

    assert sm.holdouts.sampling_is_settled_for_post(post_id) is True
    assert sm.holdouts.sampling_is_settled_for_post(other_post_id) is False


def test_the_scan_loop_withholds_the_sampler_from_a_settled_post(sm: StateManager) -> None:
    """The guard as the runner applies it, over a real settled post.

    Asserted against the same expression the loop evaluates, so a change to
    the storage read that broke the guard's polarity would fail here rather
    than silently re-enable the redraw.
    """
    msg = _message()
    _evaluation_id, post_id, scan_id = _persist_held(sm, _candidate(), msg)
    fresh_post_id = sm.save_post(_message("0xfeed"), scan_id)
    sampler = build_holdout_sampler(1.0)

    settled = None if sm.holdouts.sampling_is_settled_for_post(post_id) is True else sampler
    fresh = None if sm.holdouts.sampling_is_settled_for_post(fresh_post_id) is True else sampler

    assert settled is None
    assert fresh is sampler


# ---------------------------------------------------------------------------
# A crash between the decision and its evaluation resumes the recorded draw
# ---------------------------------------------------------------------------


async def _decide_at_the_boundary(
    state: StateManager,
    msg: Message,
    *,
    rate: float,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ReplyCandidate, int, int]:
    """One worker's pass over one post, up to and including the boundary.

    Returns (candidate, post, scan). Nothing is persisted beyond what the
    boundary itself commits, so returning without a persist_outcome call is
    exactly a crash between the decision and its evaluation.
    """
    ctx, post_id, phase_run_ids = _boundary(state, msg, sampler=build_holdout_sampler(rate))
    spy = _PhaseSpy(
        RelevancePhaseOutput(relevant=True, score=0.9, reason="r", relevant_to=["agent-ops"]),
        phase_run_ids=phase_run_ids,
    )
    monkeypatch.setattr("scout.scanning.pipeline._run_phase", spy.run)
    monkeypatch.setattr("scout.scanning.pipeline.format_reply_draft_input", lambda **_: "draft")
    monkeypatch.setattr("scout.scanning.pipeline.format_critic_input", lambda **_: "critic")
    result = await score_and_draft_step(ctx)
    assert isinstance(result, Ok)
    return result.value, post_id, ctx["execution_context"].scan_id


@pytest.mark.parametrize(
    ("first_rate", "second_rate", "selected"),
    [
        pytest.param(1.0, 0.0, True, id="lowered-rate-keeps-the-hold"),
        pytest.param(0.0, 1.0, False, id="raised-rate-does-not-add-one"),
    ],
)
async def test_a_crash_before_the_evaluation_resumes_the_recorded_draw(
    sm: StateManager,
    monkeypatch: pytest.MonkeyPatch,
    first_rate: float,
    second_rate: float,
    selected: bool,
) -> None:
    """The gap the durable draw exists to close, in both rate directions.

    The first attempt decides the post and records its draw, then dies before
    any evaluation commits — so the decision-based settled check sees nothing,
    and the retry is free to draw again. It must not: the recorded draw is the
    answer, and a rate that moved in either direction in the meantime cannot
    flip it. Without the recorded row a lowered rate would release a post
    already marked for grading, and a raised one would hold a post whose
    decision had already been made in the open.
    """
    msg = _message()
    first, post_id, _scan_id = await _decide_at_the_boundary(
        sm, msg, rate=first_rate, monkeypatch=monkeypatch
    )
    assert first.holdout is not None and first.holdout.selected is selected
    crashed = sm.holdouts.get_sampling_capture(post_id)
    assert crashed is not None and crashed.evaluation_id is None
    assert sm.holdouts.sampling_is_settled_for_post(post_id) is False

    # This pass's own draw disagrees with the recorded one. It loses.
    redrawn = build_holdout_sampler(second_rate)(
        platform=msg.platform, platform_id=msg.platform_id
    )
    assert redrawn.selected is not selected

    retried, retried_post_id, retry_scan_id = await _decide_at_the_boundary(
        sm, msg, rate=second_rate, monkeypatch=monkeypatch
    )

    assert retried_post_id == post_id
    assert retried.holdout is not None
    assert retried.holdout.selected is selected
    assert retried.holdout.rate == first_rate
    assert retried.holdout.value == crashed.draw
    assert retried.holdout_capture is not None
    assert retried.holdout_capture.resumed is True
    # The resumed capture belongs to this attempt now, at the next fence.
    assert retried.holdout_capture.fence == 2

    decision = scan_runner.classify_outcome(retried, msg, {})
    # What the unselected post classifies as beyond "not held" is the ordinary
    # drafting path's business, not this test's.
    assert (decision.status == "held") is selected
    evaluation_id = scan_runner.persist_outcome(
        sm,
        decision,
        scan_runner.PersistenceContext(
            post_id=post_id,
            scan_id=retry_scan_id,
            keyword_route_id=None,
            dossier_revision="r1",
            dossier_summary_id="d1",
            surfaced_at=msg.created_at.isoformat(),
            project=scan_runner.FrozenProjectIdentity(
                key="agent-ops", name="Agent Ops", description="A description."
            ),
        ),
    )

    capture = sm.holdouts.get_sampling_capture(post_id)
    assert capture is not None
    assert (capture.rate, capture.selected, capture.draw) == (
        first_rate,
        selected,
        crashed.draw,
    )
    assert capture.evaluation_id == evaluation_id and capture.settled_at is not None
    assert sm.conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 1
    assert sm.conn.execute("SELECT COUNT(*) FROM relevance_sampling_decisions").fetchone()[0] == 1
    held = sm.holdouts.get_by_evaluation(evaluation_id)
    assert (held is not None) is selected


async def test_the_abandoned_attempt_cannot_settle_behind_the_one_that_resumed_it(
    sm: StateManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fenced recovery: resuming a capture invalidates the attempt it replaced.

    A worker that was presumed dead and came back is the case a plain
    insert-or-read would get wrong — both attempts would hold an open capture
    and both would write a decision. The fence the retry took is what makes the
    revenant's own settlement a refusal.
    """
    msg = _message()
    abandoned, post_id, scan_id = await _decide_at_the_boundary(
        sm, msg, rate=1.0, monkeypatch=monkeypatch
    )
    assert abandoned.holdout_capture is not None and abandoned.holdout_capture.fence == 1

    resumed, _post_id, retry_scan_id = await _decide_at_the_boundary(
        sm, msg, rate=1.0, monkeypatch=monkeypatch
    )
    assert resumed.holdout_capture is not None and resumed.holdout_capture.fence == 2

    stale = scan_runner.classify_outcome(abandoned, msg, {})
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
    with pytest.raises(scan_runner.HoldoutCaptureLostError, match="stale"):
        scan_runner.persist_outcome(sm, stale, context)

    assert sm.conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 0
    assert sm.conn.execute("SELECT COUNT(*) FROM relevance_holdouts").fetchone()[0] == 0

    # The attempt that holds the capture still settles normally.
    live = scan_runner.classify_outcome(resumed, msg, {})
    evaluation_id = scan_runner.persist_outcome(
        sm, live, replace(context, scan_id=retry_scan_id)
    )
    capture = sm.holdouts.get_sampling_capture(post_id)
    assert capture is not None and capture.evaluation_id == evaluation_id
    assert sm.holdouts.get_by_evaluation(evaluation_id) is not None


def test_a_settled_capture_is_refused_rather_than_taken_over(sm: StateManager) -> None:
    """An attempt arriving after the post was decided has no claim on it.

    The runner withholds the sampler from a post whose decision is recorded, so
    reaching here means that check was taken before the decision committed —
    the lost race, not a rescore. Handing back the settled capture would let
    the late attempt write a second decision for the post, so it is refused,
    and refused as contention rather than as a defect.
    """
    msg = _message()
    evaluation_id, post_id, _scan_id = _persist_held(sm, _candidate(), msg)
    first = sm.holdouts.capture_sampling(
        SamplingWrite(
            post_id=post_id,
            decision_key="farcaster:0xabc",
            rate=1.0,
            draw=0.0,
            selected=True,
            owner="scan-1",
        )
    )
    assert isinstance(first, Ok)
    settled = sm.holdouts.settle_sampling_capture(
        sampling_id=first.value.id, fence=first.value.fence, evaluation_id=evaluation_id
    )
    assert isinstance(settled, Ok)

    again = sm.holdouts.capture_sampling(
        SamplingWrite(
            post_id=post_id,
            decision_key="farcaster:0xabc",
            rate=0.0,
            draw=0.9,
            selected=False,
            owner="scan-2",
        )
    )

    assert isinstance(again, Err)
    assert again.error.contended is True
    assert again.error.evaluation_id == evaluation_id
    assert "already captured by evaluation" in again.error.detail
    # Untouched: still the first attempt's capture, at its own fence.
    stored = sm.holdouts.get_sampling_capture(post_id)
    assert stored is not None
    assert (stored.fence, stored.owner, stored.selected) == (1, "scan-1", True)


def test_a_second_evaluation_cannot_settle_a_capture_that_already_did(
    sm: StateManager,
) -> None:
    msg = _message()
    evaluation_id, post_id, _scan_id = _persist_held(sm, _candidate(), msg)
    captured = sm.holdouts.capture_sampling(
        SamplingWrite(
            post_id=post_id,
            decision_key="farcaster:0xabc",
            rate=1.0,
            draw=0.0,
            selected=True,
            owner="scan-1",
        )
    )
    assert isinstance(captured, Ok)
    first = sm.holdouts.settle_sampling_capture(
        sampling_id=captured.value.id, fence=1, evaluation_id=evaluation_id
    )
    assert isinstance(first, Ok)

    second = sm.holdouts.settle_sampling_capture(
        sampling_id=captured.value.id, fence=1, evaluation_id=evaluation_id
    )

    assert isinstance(second, Err)
    assert "already captured by evaluation" in second.error.detail


def test_a_second_hold_for_the_same_post_is_refused(sm: StateManager) -> None:
    """The storage backstop behind the capture: one post, one hold, ever.

    Reached only if something upstream of the reservation went wrong — which
    is exactly why it is a UNIQUE index rather than an application check.
    """
    msg = _message()
    _evaluation_id, post_id, scan_id = _persist_held(sm, _candidate(), msg)
    first = sm.holdouts.get_by_evaluation(_evaluation_id)
    assert first is not None

    contributors = seed_phase_run_contributors(sm, scan_id, post_id, count=1)
    second_evaluation_id = sm.persist_terminal_outcome(
        unpack_candidate(_candidate(), msg)[0],
        post_id,
        scan_id,
        surface_status="held",
        contributor_phase_run_ids=contributors,
        project_key="agent-ops",
        dossier_revision="r1",
        dossier_summary_id="d1",
    )

    second = sm.holdouts.hold(
        HoldoutWrite(
            evaluation_id=second_evaluation_id,
            post_id=post_id,
            scan_id=scan_id,
            project_key="agent-ops",
            frozen_input=first.frozen_input,
        )
    )

    assert isinstance(second, Err)
    assert "UNIQUE" in second.error.detail
    assert sm.conn.execute("SELECT COUNT(*) FROM relevance_holdouts").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Two real workers, one post
# ---------------------------------------------------------------------------


def test_two_real_first_scan_workers_capture_one_post_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first-scan race, run rather than reasoned about.

    Two workers reaching the same undecided post is what a second scanner in
    another environment does, and the guards upstream cannot stop it: neither
    worker can see a hold or a decision that does not exist yet, so both
    classify. Real threads, real connections, real SQLite locking, synchronized
    immediately before classification and again immediately before
    persistence — the two moments where a lost race produces two decisions.

    Exactly one attempt may decide the post. The other must leave nothing
    behind, and the hold that survives must release exactly once.
    """
    db_path = str(tmp_path / "scout.db")
    msg = _message()
    phase_runs: dict[int, tuple[int, ...]] = {}
    scans: dict[str, int] = {}
    with StateManager(db_path=db_path) as seeder:
        post_id = seeder.save_post(msg, seeder.start_scan(environment="seed"))
        for name in ("worker-1", "worker-2"):
            scan_id = seeder.start_scan(environment=name)
            scans[name] = scan_id
            phase_runs[scan_id] = seed_phase_run_contributors(
                seeder, scan_id, post_id, count=1
            )
        seeder.commit()

    async def _relevance_only(**kwargs: Any) -> Any:
        return Ok(
            PhaseExecution(
                parsed=RelevancePhaseOutput(
                    relevant=True, score=0.9, reason="r", relevant_to=["agent-ops"]
                ),
                trace_id=f"trace-{kwargs['scan_id']}",
                phase_run_id=phase_runs[kwargs["scan_id"]][0],
                phase=kwargs["phase"],
                model="relevance-model",
            )
        )

    monkeypatch.setattr("scout.scanning.pipeline._run_phase", _relevance_only)

    before_classification = threading.Barrier(2)
    before_persistence = threading.Barrier(2)
    outcomes: list[Any] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        with StateManager(db_path=db_path) as state:
            state.conn.execute("PRAGMA busy_timeout=5000")
            scan_id = scans[name]
            ctx = _context(msg, state=state, post_id=post_id, scan_id=scan_id) | {
                "holdout_sampler": build_holdout_sampler(1.0)
            }

            before_classification.wait(timeout=30)
            scored = asyncio.run(score_and_draft_step(ctx))
            assert isinstance(scored, Ok)
            decision = scan_runner.classify_outcome(scored.value, msg, {})
            assert decision.status == "held"
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

            before_persistence.wait(timeout=30)
            try:
                persisted: Any = Ok(scan_runner.persist_outcome(state, decision, context))
            except scan_runner.HoldoutCaptureLostError as error:
                persisted = Err(error)
            with lock:
                outcomes.append(persisted)
            state.commit()

    threads = [threading.Thread(target=worker, args=(f"worker-{i}",)) for i in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    won = [outcome for outcome in outcomes if isinstance(outcome, Ok)]
    lost = [outcome for outcome in outcomes if isinstance(outcome, Err)]
    assert len(won) == 1, outcomes
    assert len(lost) == 1
    # Whichever worker resumed the reservation holds the live fence and wins,
    # in either persistence order. What the loser is told depends on that
    # order: its fence is stale if it arrives first, and the capture is
    # already settled if it arrives second.
    assert any(
        phrase in lost[0].error.detail
        for phrase in ("is stale", "already captured by evaluation")
    ), lost[0].error.detail

    with StateManager(db_path=db_path) as reader:
        counts = {
            table: reader.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            for table in (
                "evaluations",
                "relevance_decisions",
                "relevance_sampling_decisions",
                "relevance_holdouts",
            )
        }
        assert counts == {
            "evaluations": 1,
            "relevance_decisions": 1,
            "relevance_sampling_decisions": 1,
            "relevance_holdouts": 1,
        }
        capture = reader.holdouts.get_sampling_capture(post_id)
        assert capture is not None
        assert capture.evaluation_id == won[0].value
        # The second worker to reach the boundary resumed the first's
        # reservation, so it is the one holding the fence that settled.
        assert capture.fence == 2
        held = reader.holdouts.get_by_evaluation(won[0].value)
        assert held is not None and held.status == "pending"
        holdout_id = held.id

    # ... and one eventual release outcome, from two workers again.
    with StateManager(db_path=db_path) as first, StateManager(db_path=db_path) as second:
        for state in (first, second):
            state.conn.execute("PRAGMA busy_timeout=5000")
        claimed = first.holdouts.claim(holdout_id, owner="release-1")
        contended = second.holdouts.claim(holdout_id, owner="release-2")
        assert isinstance(claimed, Ok)
        assert isinstance(contended, Err)

        released = first.holdouts.complete_release(
            claimed.value, release_authority="recorded_action", release_action="respond"
        )
        assert isinstance(released, Ok)
        assert released.value.status == "released"
        assert isinstance(second.holdouts.claim(holdout_id, owner="release-3"), Err)
        first.commit()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        pytest.param("rate", 0.0, id="rate"),
        pytest.param("draw", 0.9, id="draw"),
        pytest.param("selected", 0, id="selected"),
        pytest.param("post_id", 999, id="post"),
    ],
)
def test_a_recorded_draw_cannot_be_rewritten(
    sm: StateManager, column: str, value: object
) -> None:
    """The draw is storage-level immutable, not merely unwritten-to.

    Everything downstream — the hold, the export, the label written against it
    — treats the recorded selection as the fact it was decided on. A rewrite
    would silently re-decide a post that has already been graded.
    """
    scan_id = sm.start_scan(environment="test")
    post_id = sm.save_post(_message(), scan_id)
    captured = sm.holdouts.capture_sampling(
        SamplingWrite(
            post_id=post_id,
            decision_key="farcaster:0xabc",
            rate=1.0,
            draw=0.0,
            selected=True,
            owner="scan-1",
        )
    )
    assert isinstance(captured, Ok)

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        sm.conn.execute(
            f"UPDATE relevance_sampling_decisions SET {column} = ? WHERE id = ?",  # noqa: S608
            (value, captured.value.id),
        )


def test_a_capture_fence_never_moves_backwards(sm: StateManager) -> None:
    scan_id = sm.start_scan(environment="test")
    post_id = sm.save_post(_message(), scan_id)
    captured = sm.holdouts.capture_sampling(
        SamplingWrite(
            post_id=post_id,
            decision_key="farcaster:0xabc",
            rate=1.0,
            draw=0.0,
            selected=True,
            owner="scan-1",
        )
    )
    assert isinstance(captured, Ok)
    resumed = sm.holdouts.capture_sampling(
        SamplingWrite(
            post_id=post_id,
            decision_key="farcaster:0xabc",
            rate=1.0,
            draw=0.0,
            selected=True,
            owner="scan-2",
        )
    )
    assert isinstance(resumed, Ok) and resumed.value.fence == 2

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        sm.conn.execute(
            "UPDATE relevance_sampling_decisions SET capture_fence = 1 WHERE id = ?",
            (captured.value.id,),
        )


@pytest.mark.parametrize(
    ("rate", "selected"),
    [
        pytest.param(1.0, True, id="selected"),
        pytest.param(0.0, False, id="unselected"),
    ],
)
async def test_an_attempt_arriving_after_the_decision_is_refused_before_drafting(
    sm: StateManager, monkeypatch: pytest.MonkeyPatch, rate: float, selected: bool
) -> None:
    """The staggered race, which the fence alone does not cover.

    Both workers passing the settled-sampling check before either commits is
    the overlapping case. This is the other order: the first worker finishes
    the post entirely while the second is still in its relevance call, so the
    second arrives at the boundary holding a check that was true when it was
    taken and is not any more. It must be refused there — not handed the
    settled capture and allowed to persist a second decision, which for an
    unselected draw nothing downstream would have stopped.
    """
    msg = _message()
    first, post_id, scan_id = await _decide_at_the_boundary(
        sm, msg, rate=rate, monkeypatch=monkeypatch
    )
    assert first.holdout is not None and first.holdout.selected is selected
    decision = scan_runner.classify_outcome(first, msg, {})
    evaluation_id = scan_runner.persist_outcome(
        sm,
        decision,
        scan_runner.PersistenceContext(
            post_id=post_id,
            scan_id=scan_id,
            keyword_route_id=None,
            dossier_revision="r1",
            dossier_summary_id="d1",
            surfaced_at=msg.created_at.isoformat(),
            project=scan_runner.FrozenProjectIdentity(
                key="agent-ops", name="Agent Ops", description="A description."
            ),
        ),
    )

    ctx, late_post_id, phase_run_ids = _boundary(
        sm, msg, sampler=build_holdout_sampler(rate)
    )
    spy = _PhaseSpy(
        RelevancePhaseOutput(relevant=True, score=0.9, reason="r", relevant_to=["agent-ops"]),
        phase_run_ids=phase_run_ids,
    )
    monkeypatch.setattr("scout.scanning.pipeline._run_phase", spy.run)

    late = await score_and_draft_step(ctx)

    assert late_post_id == post_id
    assert isinstance(late, Err)
    assert isinstance(late.error, HoldoutStorageError)
    assert late.error.contended is True
    assert late.error.evaluation_id == evaluation_id
    # Refused before drafting, and nothing of the late attempt persisted.
    assert spy.calls == ["relevance"]
    assert sm.conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 1
    assert sm.conn.execute("SELECT COUNT(*) FROM relevance_decisions").fetchone()[0] == 1
    assert sm.conn.execute("SELECT COUNT(*) FROM relevance_sampling_decisions").fetchone()[0] == 1
    capture = sm.holdouts.get_sampling_capture(post_id)
    assert capture is not None and capture.evaluation_id == evaluation_id


def test_a_recorded_draw_cannot_be_deleted(sm: StateManager) -> None:
    """The row is the post's reservation, so removing it would free a redraw.

    Deleting it would leave the post with no recorded draw and no reservation,
    and the next scan would draw again at whatever rate is in force by then —
    the exact outcome the durable record exists to prevent.
    """
    scan_id = sm.start_scan(environment="test")
    post_id = sm.save_post(_message(), scan_id)
    captured = sm.holdouts.capture_sampling(
        SamplingWrite(
            post_id=post_id,
            decision_key="farcaster:0xabc",
            rate=1.0,
            draw=0.0,
            selected=True,
            owner="scan-1",
        )
    )
    assert isinstance(captured, Ok)

    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        sm.conn.execute(
            "DELETE FROM relevance_sampling_decisions WHERE id = ?", (captured.value.id,)
        )

    assert sm.holdouts.get_sampling_capture(post_id) is not None
