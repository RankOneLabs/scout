"""Answer-key mapping and recoverable held-row release lifecycle."""

from __future__ import annotations

import json
import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import scout.relevance.holdout_release as release
from scout.registry import KeywordRoute, ProjectTarget, RuntimeRegistry
from scout.relevance.holdout_release import (
    AnswerKeyEntry,
    HoldoutReleaseError,
    action_from_labels,
    load_answer_key,
    release_holdout_batch,
)
from scout.result import Ok
from scout.scanning.schemas import ReplyCandidate
from scout.storage.state import StateManager
from tests.test_relevance_holdout_export import _seed_holdout


def _batch(state: StateManager, *evaluation_ids: int) -> str:
    batch_id = "batch-1"
    placeholders = ",".join("?" for _ in evaluation_ids)
    state.conn.execute(
        f"UPDATE relevance_holdouts SET batch_id = ?, exported_at = ? "
        f"WHERE evaluation_id IN ({placeholders})",
        (batch_id, "2026-09-02T00:00:00+00:00", *evaluation_ids),
    )
    return batch_id


def _answer(evaluation_id: int, action: str) -> AnswerKeyEntry:
    return {
        "drop": AnswerKeyEntry(evaluation_id, True, False, False),
        "review": AnswerKeyEntry(evaluation_id, False, True, True),
        "respond": AnswerKeyEntry(evaluation_id, False, False, True),
    }[action]


def test_argparse_wires_holdout_release(monkeypatch: pytest.MonkeyPatch) -> None:
    from scout.cli.main import parse_args

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scout",
            "replay",
            "holdout",
            "release",
            "--batch",
            "batch-1",
            "--answer-key",
            "labels.json",
        ],
    )
    args = parse_args()
    assert (args.holdout_command, args.batch, args.answer_key) == (
        "release",
        "batch-1",
        "labels.json",
    )


def test_loads_categorical_assay_labels(tmp_path) -> None:
    path = tmp_path / "labels.json"
    path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "evaluation_id": 42,
                        "exclusion": "none",
                        "needs_thread": False,
                        "substance": "pointer",
                    }
                ]
            }
        )
    )
    assert load_answer_key(path) == (AnswerKeyEntry(42, "none", False, "pointer"),)


def test_label_mapping_precedence_and_logged_fallback(caplog: pytest.LogCaptureFixture) -> None:
    assert action_from_labels(
        AnswerKeyEntry(1, "hype", True, "in_post"), "respond"
    ) == "drop"
    assert action_from_labels(
        AnswerKeyEntry(1, "none", True, "in_post"), "drop"
    ) == "review"
    assert action_from_labels(
        AnswerKeyEntry(1, "none", False, "in_post"), "drop"
    ) == "respond"
    assert action_from_labels(
        AnswerKeyEntry(1, "none", False, "pointer"), "drop"
    ) == "review"
    assert action_from_labels(
        AnswerKeyEntry(1, "none", False, "none"), "respond"
    ) == "drop"
    with caplog.at_level(logging.WARNING):
        assert action_from_labels(AnswerKeyEntry(1, None, None, None), "review") == "review"
    assert "falling back to recorded action review" in caplog.text


@pytest.mark.asyncio
async def test_out_of_batch_answer_refuses_every_release() -> None:
    with StateManager(db_path=":memory:") as state:
        _seed_holdout(state, evaluation_id=11, project_key="agent-ops", action="respond")
        batch_id = _batch(state, 11)
        with pytest.raises(HoldoutReleaseError, match="outside batch"):
            await release_holdout_batch(
                state=state,
                tracer=Mock(),
                feedback=Mock(),
                batch_id=batch_id,
                answers=(_answer(11, "respond"), _answer(99, "drop")),
            )
        row = state.relevance_holdouts.get_for_evaluation(11)
        assert row is not None and row.status == "pending"
        assert row.target_evaluation_id is None


@pytest.mark.asyncio
async def test_drop_persists_without_drafting_and_records_deltas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = AsyncMock()
    monkeypatch.setattr(release, "draft_and_critic_step", draft)
    with StateManager(db_path=":memory:") as state:
        _seed_holdout(state, evaluation_id=11, project_key="agent-ops", action="respond")
        state.conn.execute(
            "UPDATE relevance_holdouts SET dossier_revision = 'held-revision'"
        )
        batch_id = _batch(state, 11)

        result = await release_holdout_batch(
            state=state,
            tracer=Mock(),
            feedback=Mock(),
            batch_id=batch_id,
            answers=(_answer(11, "drop"),),
        )

        stored = state.relevance_holdouts.get_for_evaluation(11)
        assert stored is not None and stored.status == "released"
        assert stored.release_action == "drop"
        target = state.get_evaluation(stored.target_evaluation_id or -1)
        assert target is not None
        provenance = json.loads(str(target["reason"]).removeprefix("holdout_release:"))
        assert provenance["hold_to_release_seconds"] > 0
        assert provenance["dossier_revision_changed"] is True
        assert provenance["registry_changed"] is True
        assert result.action_counts == {"respond": 0, "review": 0, "drop": 1}
    draft.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("release_action", ["respond", "review"])
async def test_positive_release_runs_draft_classify_and_persist(
    monkeypatch: pytest.MonkeyPatch,
    release_action: str,
) -> None:
    project = ProjectTarget(
        "agent-ops", "Agent Ops", "Operations", "https://example.test", None
    )
    route = KeywordRoute(7, "agent-ops", "agent", None, None, None, 0)
    registry = RuntimeRegistry({"agent-ops": project}, (route,), {})

    def prepare(**kwargs):
        return release._ResponseFlow(
            registry=registry,
            route=route,
            dossiers={},
            dossier_revision="release-revision",
            phase_configs=SimpleNamespace(),
            execution=SimpleNamespace(),
        )

    async def draft(context):
        relevance = context["relevance_output"]
        return Ok(
            ReplyCandidate(
                relevant=False,
                score=0.0,
                reason=relevance.reason,
                relevant_to=["agent-ops"],
                project_key="agent-ops",
                relevance_classifier="human",
                relevance_action="respond",
            )
        )

    draft_mock = AsyncMock(side_effect=draft)
    classify_mock = Mock(wraps=release.classify_outcome)

    def persist(state, decision, context):
        return state.save_evaluation(
            decision.evaluation,
            context.post_id,
            context.scan_id,
            project_key=decision.project_key,
            surface_status=decision.status,
            dossier_revision=context.dossier_revision,
        )

    persist_mock = Mock(side_effect=persist)
    monkeypatch.setattr(release, "_prepare_response_flow", prepare)
    monkeypatch.setattr(release, "draft_and_critic_step", draft_mock)
    monkeypatch.setattr(release, "classify_outcome", classify_mock)
    monkeypatch.setattr(release, "persist_outcome", persist_mock)

    with StateManager(db_path=":memory:") as state:
        _seed_holdout(state, evaluation_id=11, project_key="agent-ops", action="drop")
        batch_id = _batch(state, 11)
        await release_holdout_batch(
            state=state,
            tracer=Mock(),
            feedback=Mock(),
            batch_id=batch_id,
            answers=(_answer(11, release_action),),
        )
        stored = state.relevance_holdouts.get_for_evaluation(11)
        assert stored is not None
        assert stored.release_action == release_action
        target = state.get_evaluation(stored.target_evaluation_id or -1)
        assert target is not None
        assert "hold_to_release_seconds" in str(target["reason"])

    draft_mock.assert_awaited_once()
    classify_mock.assert_called_once()
    persist_mock.assert_called_once()


@pytest.mark.asyncio
async def test_crashed_release_uses_fail_path_and_is_claimable_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with StateManager(db_path=":memory:") as state:
        monkeypatch.setattr(
            state, "save_evaluation", Mock(side_effect=RuntimeError("crash"))
        )
        _seed_holdout(state, evaluation_id=11, project_key="agent-ops", action="drop")
        batch_id = _batch(state, 11)

        with pytest.raises(HoldoutReleaseError, match="crash"):
            await release_holdout_batch(
                state=state,
                tracer=Mock(),
                feedback=Mock(),
                batch_id=batch_id,
                answers=(_answer(11, "drop"),),
            )

        failed = state.relevance_holdouts.get_for_evaluation(11)
        assert failed is not None and failed.status == "failed"
        reclaimed = state.relevance_holdouts.claim_release(failed.id, owner="retry")
        assert reclaimed.status == "claimed"
        assert reclaimed.attempts == 2
