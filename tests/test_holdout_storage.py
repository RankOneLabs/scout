"""Relevance decision provenance, holds, and the fenced claim lifecycle."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from scout.config import Account, Message, RelevanceResult
from scout.result import Err, Ok
from scout.storage.holdouts import (
    LABEL_ACTIONS,
    FrozenHoldoutInput,
    HoldoutWrite,
    KeyProvenance,
    LabelProvenance,
    RelevanceDecisionWrite,
)
from scout.storage.state import StateManager


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


def _frozen(platform_id: str = "0xabc") -> FrozenHoldoutInput:
    return FrozenHoldoutInput(
        platform="farcaster",
        platform_id=platform_id,
        url=f"https://warpcast.com/ada/{platform_id}",
        channel="agents",
        text="our planner retries every failed step twice",
        author_id="42",
        author_name="Ada",
        author_handle="ada",
        project_key="agent-ops",
        project_name="Agent Ops",
        project_description="A description.",
        keyword_route_id=None,
        dossier_summary_id="d1",
        dossier_revision="r1",
    )


def _evaluation(
    state: StateManager, *, platform_id: str = "0xabc", surface_status: str = "held"
) -> tuple[int, int, int]:
    """Persist a post and a terminal evaluation. Returns (evaluation, post, scan)."""
    scan_id = state.start_scan(environment="test")
    msg = _message(platform_id)
    post_id = state.save_post(msg, scan_id)
    evaluation_id = state.evaluations.save_evaluation(
        RelevanceResult(
            message=msg, relevant=True, score=1.0, reason="respond", relevant_to=("agent-ops",)
        ),
        post_id,
        scan_id,
        surface_status=surface_status,
        project_key="agent-ops",
        dossier_summary_id="d1",
        dossier_revision="r1",
    )
    return evaluation_id, post_id, scan_id


def _hold(state: StateManager, platform_id: str = "0xabc"):
    evaluation_id, post_id, scan_id = _evaluation(state, platform_id=platform_id)
    held = state.holdouts.hold(
        HoldoutWrite(
            evaluation_id=evaluation_id,
            post_id=post_id,
            scan_id=scan_id,
            project_key="agent-ops",
            frozen_input=_frozen(platform_id),
        )
    )
    assert isinstance(held, Ok), held
    return held.value


def _provenance(evaluation_id: int) -> tuple[LabelProvenance, KeyProvenance]:
    return (
        LabelProvenance(
            format="assay.label-packet-labels/v3",
            packet="fixture",
            packet_digest="a" * 64,
            plan_digest="b" * 64,
            case_id=7,
            reviewer="reviewer",
            saved_at="2026-09-24T00:00:00Z",
        ),
        KeyProvenance(
            format="assay.label-packet-key/v2",
            name="fixture",
            digest="a" * 64,
            plan_digest="b" * 64,
            sitting="first",
            case_id=7,
            evaluation_id=evaluation_id,
            project_key="agent-ops",
        ),
    )


# --------------------------------------------------------------------------
# The held status
# --------------------------------------------------------------------------


def test_an_evaluation_can_be_persisted_as_held(sm: StateManager) -> None:
    evaluation_id, _post_id, _scan_id = _evaluation(sm)
    row = sm.conn.execute(
        "SELECT surface_status FROM evaluations WHERE id = ?", (evaluation_id,)
    ).fetchone()
    assert row["surface_status"] == "held"


def test_an_unknown_surface_status_is_still_rejected(sm: StateManager) -> None:
    with pytest.raises(ValueError, match="unknown surface status"):
        _evaluation(sm, surface_status="quarantined")


# --------------------------------------------------------------------------
# Decision provenance
# --------------------------------------------------------------------------


def test_a_jev_decision_records_answers_and_the_router_decision(
    sm: StateManager,
) -> None:
    evaluation_id, _post_id, _scan_id = _evaluation(sm)
    written = sm.holdouts.record_decision(
        RelevanceDecisionWrite(
            evaluation_id=evaluation_id,
            classifier="jev",
            model="jev-latest",
            action="review",
            reason="points_somewhere",
            catalogue_id="routed-features-fixture",
            catalogue_version="a" * 64,
            router_version="jev-route/round4",
            answers={"needs_thread": {"type": "noul", "noul": 0.2}},
            decision={"action": "review", "line": "points_somewhere", "exclusion": None},
        )
    )
    assert isinstance(written, Ok)
    read = sm.holdouts.get_decision(evaluation_id)
    assert read is not None
    assert read.answers == {"needs_thread": {"type": "noul", "noul": 0.2}}
    assert read.decision is not None
    assert read.decision["line"] == "points_somewhere"


def test_an_llm_decision_records_its_action_without_router_fields(
    sm: StateManager,
) -> None:
    evaluation_id, _post_id, _scan_id = _evaluation(sm)
    sm.holdouts.record_decision(
        RelevanceDecisionWrite(
            evaluation_id=evaluation_id,
            classifier="llm",
            model="claude-sonnet-4-6",
            action="respond",
            reason="relevant and above threshold",
        )
    )
    read = sm.holdouts.get_decision(evaluation_id)
    assert read is not None
    assert (read.classifier, read.action, read.router_version) == (
        "llm",
        "respond",
        None,
    )


def test_an_evaluation_without_a_decision_reads_as_unknown(sm: StateManager) -> None:
    evaluation_id, _post_id, _scan_id = _evaluation(sm)
    assert sm.holdouts.get_decision(evaluation_id) is None


def test_one_evaluation_cannot_record_two_decisions(sm: StateManager) -> None:
    evaluation_id, _post_id, _scan_id = _evaluation(sm)
    write = RelevanceDecisionWrite(
        evaluation_id=evaluation_id, classifier="llm", model="m", action="drop"
    )
    assert isinstance(sm.holdouts.record_decision(write), Ok)
    assert isinstance(sm.holdouts.record_decision(write), Err)


def test_successful_decisions_have_distinct_stable_ids_and_selection(sm: StateManager) -> None:
    first, _, _ = _evaluation(sm, platform_id="0xuid1")
    second, _, _ = _evaluation(sm, platform_id="0xuid2")
    for evaluation_id, selected in ((first, True), (second, False)):
        result = sm.holdouts.record_decision(
            RelevanceDecisionWrite(
                evaluation_id=evaluation_id,
                classifier="llm",
                model="m",
                action="respond",
                selected_for_holdout=selected,
            )
        )
        assert isinstance(result, Ok)
    a = sm.holdouts.get_decision(first)
    b = sm.holdouts.get_decision(second)
    assert a is not None and b is not None
    assert a.decision_uid and b.decision_uid and a.decision_uid != b.decision_uid
    assert (a.selected_for_holdout, b.selected_for_holdout) == (True, False)
    assert sm.holdouts.get_decision(first).decision_uid == a.decision_uid


def test_an_unknown_classifier_is_rejected_by_the_schema(sm: StateManager) -> None:
    evaluation_id, _post_id, _scan_id = _evaluation(sm)
    result = sm.holdouts.record_decision(
        RelevanceDecisionWrite(
            evaluation_id=evaluation_id,
            classifier="gemini",  # type: ignore[arg-type]
            model="m",
            action="drop",
        )
    )
    assert isinstance(result, Err)


# --------------------------------------------------------------------------
# Holds
# --------------------------------------------------------------------------


def test_a_hold_starts_pending_and_freezes_its_input(sm: StateManager) -> None:
    held = _hold(sm)
    assert held.status == "pending"
    assert held.released_at is None
    assert held.frozen_input == _frozen()


@pytest.mark.parametrize(
    "change",
    [
        "post_id",
        "scan_id",
        "project_key",
        "platform_id",
        "text",
        "dossier_revision",
    ],
)
def test_a_hold_rejects_mismatched_source_identity(sm: StateManager, change: str) -> None:
    evaluation_id, post_id, scan_id = _evaluation(sm)
    frozen = _frozen()
    values = {
        "evaluation_id": evaluation_id,
        "post_id": post_id,
        "scan_id": scan_id,
        "project_key": "agent-ops",
        "frozen_input": frozen,
    }
    if change == "post_id":
        _, values["post_id"], _ = _evaluation(sm, platform_id="0xother")
    elif change == "scan_id":
        values["scan_id"] = sm.start_scan(environment="test")
    elif change == "project_key":
        values["project_key"] = "other"
    else:
        values["frozen_input"] = replace(frozen, **{change: "other"})
    result = sm.holdouts.hold(HoldoutWrite(**values))
    assert isinstance(result, Err)
    assert sm.holdouts.get_by_evaluation(evaluation_id) is None


def test_a_hold_requires_a_held_source_evaluation(sm: StateManager) -> None:
    evaluation_id, post_id, scan_id = _evaluation(sm, surface_status="surfaced")
    result = sm.holdouts.hold(
        HoldoutWrite(
            evaluation_id=evaluation_id,
            post_id=post_id,
            scan_id=scan_id,
            project_key="agent-ops",
            frozen_input=_frozen(),
        )
    )
    assert isinstance(result, Err)
    assert "must be held" in result.error.detail


def test_one_source_evaluation_cannot_acquire_two_holds(sm: StateManager) -> None:
    held = _hold(sm)
    duplicate = sm.holdouts.hold(
        HoldoutWrite(
            evaluation_id=held.evaluation_id,
            post_id=held.post_id,
            scan_id=held.scan_id,
            frozen_input=_frozen(),
        )
    )
    assert isinstance(duplicate, Err)
    assert duplicate.error.evaluation_id == held.evaluation_id


def test_the_frozen_input_cannot_be_rewritten(sm: StateManager) -> None:
    held = _hold(sm)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"), sm.db.begin_immediate():
        sm.conn.execute(
            "UPDATE relevance_holdouts SET frozen_input_json = '{}' WHERE id = ?",
            (held.id,),
        )


def test_a_hold_and_its_evaluation_roll_back_together(sm: StateManager) -> None:
    """An injected failure after the evaluation write leaves neither behind."""
    evaluation_id, post_id, scan_id = _evaluation(sm, platform_id="0xrollback")
    before = sm.conn.execute("SELECT COUNT(*) AS n FROM evaluations").fetchone()["n"]
    with pytest.raises(RuntimeError, match="injected"), sm.db.begin_immediate():
        second = sm.evaluations.save_evaluation(
            RelevanceResult(
                message=_message("0xrollback"),
                relevant=False,
                score=0.0,
                reason="drop",
            ),
            post_id,
            scan_id,
            surface_status="held",
        )
        sm.holdouts.hold(
            HoldoutWrite(
                evaluation_id=second,
                post_id=post_id,
                scan_id=scan_id,
                frozen_input=_frozen("0xrollback"),
            )
        )
        raise RuntimeError("injected")
    after = sm.conn.execute("SELECT COUNT(*) AS n FROM evaluations").fetchone()["n"]
    assert after == before
    assert sm.holdouts.get_by_evaluation(evaluation_id) is None


def test_evaluation_hold_decision_and_contributor_link_roll_back_together(
    sm: StateManager,
) -> None:
    scan_id = sm.start_scan(environment="test")
    message = _message("0xatomic")
    post_id = sm.save_post(message, scan_id)
    now = datetime.now(UTC).isoformat()
    with sm.db.begin_immediate():
        sm.conn.execute(
            "INSERT INTO feedback_snapshots (scan_id, policy_version, mode, as_of, "
            "lookback_days, max_grades, segment_min_grades, note_max_chars, "
            "relevance_token_budget, reply_draft_token_budget, critic_token_budget, "
            "population_count, eligible_count, excluded_count, created_at) "
            "VALUES (?, 'test/v1', 'shadow', ?, 90, 200, 5, 240, 800, 800, 1000, 0, 0, 0, ?)",
            (scan_id, now, now),
        )
        snapshot_id = sm.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        sm.conn.execute(
            "INSERT INTO feedback_snapshot_phases "
            "(snapshot_id, phase, token_budget, token_estimate, truncated, "
            "structured_summary, rendered_text, rendered_sha256, created_at) "
            "VALUES (?, 'relevance', 800, 0, 0, '{}', '', 'x', ?)",
            (snapshot_id, now),
        )
        phase_id = sm.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        sm.conn.execute(
            "INSERT INTO evaluation_phase_runs "
            "(scan_id, post_id, snapshot_phase_id, phase, trace_id, model, status, created_at) "
            "VALUES (?, ?, ?, 'relevance', 'trace-atomic', 'm', 'complete', ?)",
            (scan_id, post_id, phase_id, now),
        )
        run_id = sm.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    before = sm.conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0]
    with pytest.raises(RuntimeError, match="injected"), sm.db.begin_immediate():
        evaluation_id = sm.evaluations.persist_terminal_outcome(
            RelevanceResult(message=message, relevant=False, score=0.0, reason="drop"),
            post_id,
            scan_id,
            surface_status="held",
            contributor_phase_run_ids=[run_id],
            project_key="agent-ops",
        )
        assert isinstance(
            sm.holdouts.record_decision(
                RelevanceDecisionWrite(
                    evaluation_id=evaluation_id,
                    classifier="llm",
                    model="m",
                    action="drop",
                    selected_for_holdout=True,
                )
            ),
            Ok,
        )
        assert isinstance(
            sm.holdouts.hold(
                HoldoutWrite(
                    evaluation_id=evaluation_id,
                    post_id=post_id,
                    scan_id=scan_id,
                    frozen_input=replace(
                        _frozen("0xatomic"), dossier_summary_id=None, dossier_revision=None
                    ),
                    project_key="agent-ops",
                )
            ),
            Ok,
        )
        raise RuntimeError("injected")
    assert sm.conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == before
    assert sm.conn.execute("SELECT COUNT(*) FROM relevance_holdouts").fetchone()[0] == 0
    assert (
        sm.conn.execute(
            "SELECT evaluation_id FROM evaluation_phase_runs WHERE id = ?", (run_id,)
        ).fetchone()[0]
        is None
    )


def test_two_holds_cannot_share_one_release_target(sm: StateManager) -> None:
    first = _hold(sm, "0xone")
    second = _hold(sm, "0xone")
    target, _post_id, _scan_id = _evaluation(sm, platform_id="0xone", surface_status="surfaced")
    for held in (first, second):
        claim = sm.holdouts.claim(held.id, owner="releaser")
        assert isinstance(claim, Ok)
        result = sm.holdouts.complete_release(
            claim.value,
            release_authority="recorded_action",
            release_action="respond",
            target_evaluation_id=target,
        )
        if held is first:
            assert isinstance(result, Ok)
        else:
            assert isinstance(result, Err)


def test_a_hold_cannot_target_its_own_source_evaluation(sm: StateManager) -> None:
    held = _hold(sm)
    claim = sm.holdouts.claim(held.id, owner="releaser")
    assert isinstance(claim, Ok)
    result = sm.holdouts.complete_release(
        claim.value,
        release_authority="recorded_action",
        release_action="respond",
        target_evaluation_id=held.evaluation_id,
    )
    assert isinstance(result, Err)


def test_release_rejects_target_evaluation_for_another_post(sm: StateManager) -> None:
    held = _hold(sm)
    target, _, _ = _evaluation(sm, platform_id="0xother", surface_status="surfaced")
    claim = sm.holdouts.claim(held.id, owner="releaser")
    assert isinstance(claim, Ok)
    result = sm.holdouts.complete_release(
        claim.value,
        release_authority="recorded_action",
        release_action="respond",
        target_evaluation_id=target,
    )
    assert isinstance(result, Err)
    assert "another post" in result.error.detail
    assert sm.holdouts.get(held.id).status == "claimed"


# --------------------------------------------------------------------------
# Claims: compare-and-swap and fencing
# --------------------------------------------------------------------------


def test_a_claim_takes_the_hold_and_bumps_the_fence(sm: StateManager) -> None:
    held = _hold(sm)
    claim = sm.holdouts.claim(held.id, owner="releaser")
    assert isinstance(claim, Ok)
    assert claim.value.fence == held.claim_fence + 1
    current = sm.holdouts.get(held.id)
    assert current is not None and current.status == "claimed"


def test_a_second_claim_on_a_live_claim_is_refused(sm: StateManager) -> None:
    held = _hold(sm)
    assert isinstance(sm.holdouts.claim(held.id, owner="first"), Ok)
    competing = sm.holdouts.claim(held.id, owner="second")
    assert isinstance(competing, Err)
    assert "not claimable" in competing.error.detail


def test_an_expired_claim_can_be_taken_over(sm: StateManager) -> None:
    held = _hold(sm)
    abandoned = sm.holdouts.claim(held.id, owner="abandoned")
    assert isinstance(abandoned, Ok)
    stale = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with sm.db.begin_immediate():
        sm.conn.execute(
            "UPDATE relevance_holdouts SET claim_expires_at = ? WHERE id = ?",
            (stale, held.id),
        )
    taken = sm.holdouts.claim(held.id, owner="recovered")
    assert isinstance(taken, Ok)
    assert taken.value.fence > abandoned.value.fence


def test_expired_holder_cannot_complete_before_takeover(sm: StateManager) -> None:
    held = _hold(sm)
    claim = sm.holdouts.claim(held.id, owner="first")
    assert isinstance(claim, Ok)
    stale = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with sm.db.begin_immediate():
        sm.conn.execute(
            "UPDATE relevance_holdouts SET claim_expires_at = ? WHERE id = ?",
            (stale, held.id),
        )
    result = sm.holdouts.complete_release(
        claim.value, release_authority="recorded_action", release_action="drop"
    )
    assert isinstance(result, Err)
    assert "expired" in result.error.detail
    current = sm.holdouts.get(held.id)
    assert current is not None and current.status == "claimed"


def test_expired_holder_cannot_fail_attempt_before_takeover(sm: StateManager) -> None:
    held = _hold(sm)
    claim = sm.holdouts.claim(held.id, owner="first")
    assert isinstance(claim, Ok)
    stale = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with sm.db.begin_immediate():
        sm.conn.execute(
            "UPDATE relevance_holdouts SET claim_expires_at = ? WHERE id = ?",
            (stale, held.id),
        )
    assert isinstance(sm.holdouts.fail_attempt(claim.value, detail="late"), Err)


def test_completion_rejects_wrong_token_at_current_fence(sm: StateManager) -> None:
    held = _hold(sm)
    claim = sm.holdouts.claim(held.id, owner="first")
    assert isinstance(claim, Ok)
    wrong = replace(claim.value, token="wrong-token")
    result = sm.holdouts.complete_release(
        wrong, release_authority="recorded_action", release_action="drop"
    )
    assert isinstance(result, Err)
    current = sm.holdouts.get(held.id)
    assert current is not None and current.status == "claimed"


def test_a_superseded_claim_cannot_complete(sm: StateManager) -> None:
    held = _hold(sm)
    abandoned = sm.holdouts.claim(held.id, owner="abandoned")
    assert isinstance(abandoned, Ok)
    with sm.db.begin_immediate():
        sm.conn.execute(
            "UPDATE relevance_holdouts SET claim_expires_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), held.id),
        )
    assert isinstance(sm.holdouts.claim(held.id, owner="recovered"), Ok)

    refused = sm.holdouts.complete_release(
        abandoned.value, release_authority="recorded_action", release_action="drop"
    )
    assert isinstance(refused, Err)
    assert "stale" in refused.error.detail


def test_a_committed_completion_is_idempotently_readable(sm: StateManager) -> None:
    held = _hold(sm)
    claim = sm.holdouts.claim(held.id, owner="releaser")
    assert isinstance(claim, Ok)
    first = sm.holdouts.complete_release(
        claim.value, release_authority="recorded_action", release_action="review"
    )
    assert isinstance(first, Ok)
    again = sm.holdouts.complete_release(
        claim.value, release_authority="recorded_action", release_action="review"
    )
    assert isinstance(again, Ok)
    assert again.value.released_at == first.value.released_at


def test_a_released_hold_cannot_be_claimed_again(sm: StateManager) -> None:
    held = _hold(sm)
    claim = sm.holdouts.claim(held.id, owner="releaser")
    assert isinstance(claim, Ok)
    assert isinstance(
        sm.holdouts.complete_release(
            claim.value, release_authority="recorded_action", release_action="drop"
        ),
        Ok,
    )
    assert isinstance(sm.holdouts.claim(held.id, owner="another"), Err)


def test_claiming_a_missing_holdout_is_refused(sm: StateManager) -> None:
    result = sm.holdouts.claim(9999, owner="releaser")
    assert isinstance(result, Err)
    assert result.error.detail == "no such holdout"


# --------------------------------------------------------------------------
# Release authority and label precedence
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("label", "action"), sorted(LABEL_ACTIONS.items()))
def test_each_label_releases_to_its_confirmed_action(
    sm: StateManager, label: str, action: str
) -> None:
    held = _hold(sm, f"0x{label}")
    claim = sm.holdouts.claim(held.id, owner="releaser")
    assert isinstance(claim, Ok)
    label_provenance, key_provenance = _provenance(held.evaluation_id)
    released = sm.holdouts.complete_release(
        claim.value,
        release_authority="label",
        release_action=action,  # type: ignore[arg-type]
        label=label,  # type: ignore[arg-type]
        label_source="packet-1",
        labelled_at="2026-09-24T00:00:00+00:00",
        label_provenance=label_provenance,
        key_provenance=key_provenance,
    )
    assert isinstance(released, Ok)
    assert released.value.release_action == action


def test_release_retains_structured_label_and_key_provenance(sm: StateManager) -> None:
    held = _hold(sm)
    claim = sm.holdouts.claim(held.id, owner="releaser")
    assert isinstance(claim, Ok)
    label_provenance, key_provenance = _provenance(held.evaluation_id)
    result = sm.holdouts.complete_release(
        claim.value,
        release_authority="label",
        release_action="respond",
        label="in_post",
        label_provenance=label_provenance,
        key_provenance=key_provenance,
    )
    assert isinstance(result, Ok)
    assert result.value.label_provenance == label_provenance
    assert result.value.key_provenance == key_provenance


@pytest.mark.parametrize("missing", ["label", "key"])
def test_label_release_requires_both_provenance_records(sm: StateManager, missing: str) -> None:
    held = _hold(sm)
    claim = sm.holdouts.claim(held.id, owner="releaser")
    assert isinstance(claim, Ok)
    label_provenance, key_provenance = _provenance(held.evaluation_id)
    result = sm.holdouts.complete_release(
        claim.value,
        release_authority="label",
        release_action="respond",
        label="in_post",
        label_provenance=None if missing == "label" else label_provenance,
        key_provenance=None if missing == "key" else key_provenance,
    )
    assert isinstance(result, Err)
    assert "requires label and key provenance" in result.error.detail
    assert sm.holdouts.get(held.id).status == "claimed"


def test_a_label_release_without_a_label_is_refused(sm: StateManager) -> None:
    held = _hold(sm)
    claim = sm.holdouts.claim(held.id, owner="releaser")
    assert isinstance(claim, Ok)
    result = sm.holdouts.complete_release(
        claim.value, release_authority="label", release_action="respond"
    )
    assert isinstance(result, Err)
    assert "requires a label" in result.error.detail


def test_a_label_disagreeing_with_its_action_is_refused(sm: StateManager) -> None:
    held = _hold(sm)
    claim = sm.holdouts.claim(held.id, owner="releaser")
    assert isinstance(claim, Ok)
    result = sm.holdouts.complete_release(
        claim.value,
        release_authority="label",
        release_action="respond",
        label="pointer",
    )
    assert isinstance(result, Err)
    assert "resolves to 'review'" in result.error.detail


# --------------------------------------------------------------------------
# Failed attempts
# --------------------------------------------------------------------------


def test_a_failed_attempt_returns_the_hold_to_the_queue(sm: StateManager) -> None:
    held = _hold(sm)
    claim = sm.holdouts.claim(held.id, owner="releaser")
    assert isinstance(claim, Ok)
    failed = sm.holdouts.fail_attempt(claim.value, detail="drafting blew up")
    assert isinstance(failed, Ok)
    assert failed.value.status == "failed"
    assert failed.value.last_error == "drafting blew up"
    assert [holdout.id for holdout in sm.holdouts.list_pending()] == [held.id]


def test_a_retry_after_a_failure_claims_a_higher_fence(sm: StateManager) -> None:
    held = _hold(sm)
    first = sm.holdouts.claim(held.id, owner="releaser")
    assert isinstance(first, Ok)
    assert isinstance(sm.holdouts.fail_attempt(first.value, detail="nope"), Ok)
    second = sm.holdouts.claim(held.id, owner="releaser")
    assert isinstance(second, Ok)
    assert second.value.fence > first.value.fence


def test_pending_holds_are_listed_with_their_decisions(sm: StateManager) -> None:
    held = _hold(sm)
    sm.holdouts.record_decision(
        RelevanceDecisionWrite(
            evaluation_id=held.evaluation_id,
            classifier="jev",
            model="jev-latest",
            action="respond",
        )
    )
    pairs = sm.holdouts.pending_with_decisions()
    assert len(pairs) == 1
    holdout, decision = pairs[0]
    assert holdout.id == held.id
    assert decision is not None and decision.classifier == "jev"
