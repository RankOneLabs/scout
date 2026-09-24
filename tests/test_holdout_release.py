"""Releasing held posts: label resolution, the gates, and crash safety.

Label resolution is a pure transform and is tested as one. The release
itself is tested against a real in-memory database with the model phases
replaced, because every claim that matters here — one target evaluation, one
surface event, a fenced stale completion — is a claim about what committed.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import threading
from collections.abc import Generator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from jsonschema import Draft7Validator

import scout.config as config
import scout.holdouts.release as release_module
import scout.storage.holdouts as holdout_storage
from scout.config import Account, Message, RelevanceResult
from scout.dossiers.resolver import DossierSummary
from scout.errors import LLMError
from scout.holdouts.release import (
    NO_EXCLUSION,
    AssayKeyCase,
    AssayKeyFile,
    AssayLabelCase,
    AssayLabelsFile,
    AssayPacketFile,
    ReleaseInputs,
    ReleaseLabels,
    label_from_case,
    load_release_labels,
    release_pending_holdouts,
    released_label_record,
    resolve_labels,
    resolve_release,
)
from scout.registry import KeywordRoute, ProjectTarget, RuntimeRegistry
from scout.result import Err, Ok
from scout.scanning.schemas import (
    DeclarativeSegment,
    ReplyCandidate,
    StructuredDraftOutput,
)
from scout.storage.holdouts import (
    FrozenHoldoutInput,
    HoldoutWrite,
    RelevanceDecisionWrite,
)
from scout.storage.state import StateManager

_LABELS_SCHEMA_PATH = (
    Path(__file__).parent.parent / "contracts" / "relevance" / "holdout-labels.v1.schema.json"
)

_DIGEST = "a" * 64
_PLAN = "b" * 64


def _sm() -> Generator[StateManager, None, None]:
    state = StateManager(db_path=":memory:")
    yield state
    state.commit()
    state.close()


sm = pytest.fixture(_sm)


# ---------------------------------------------------------------------------
# Label resolution
# ---------------------------------------------------------------------------


def _case(
    *,
    case_id: int = 1,
    exclusion: str = "none",
    needs_thread: bool = False,
    substance: str | None = "none",
    note: str | None = None,
) -> AssayLabelCase:
    return AssayLabelCase(
        case_id=case_id,
        exclusion=exclusion,
        needs_thread=needs_thread,
        substance=substance,
        note=note,
    )


@pytest.mark.parametrize(
    ("case", "label", "action"),
    [
        pytest.param(
            _case(exclusion="promo_spam", substance=None), "exclusion", "drop", id="exclusion"
        ),
        pytest.param(_case(substance="in_post"), "in_post", "respond", id="in_post"),
        pytest.param(_case(substance="pointer"), "pointer", "review", id="pointer"),
        pytest.param(_case(substance="none"), "none", "drop", id="none"),
    ],
)
def test_every_label_maps_to_its_action(
    case: AssayLabelCase, label: str, action: str
) -> None:
    resolved = label_from_case(case)
    assert isinstance(resolved, Ok)
    assert resolved.value == label
    assert release_module.action_for_label(resolved.value) == action


def test_an_exclusion_beside_a_substance_answer_is_refused() -> None:
    """A state assay's own router refuses, so a saved file cannot hold it.

    The exclusion question ends the case, so an excluded post is never asked
    what it carries. Reading past the contradiction — in either direction —
    would grade the post on an answer the reviewer was never shown.
    """
    resolved = label_from_case(_case(exclusion="promo_spam", substance="in_post"))
    assert isinstance(resolved, Err)
    assert "is not asked on an excluded post" in resolved.error.detail


@pytest.mark.parametrize("value", ["none", "NONE", "  none  "])
def test_only_the_literal_none_exclusion_did_not_fire(value: str) -> None:
    resolved = label_from_case(_case(exclusion=value, substance="in_post"))
    assert isinstance(resolved, Ok)
    assert resolved.value == "in_post"


def test_the_no_exclusion_sentinel_is_the_one_assay_writes() -> None:
    assert NO_EXCLUSION == "none"


def test_a_blank_exclusion_is_refused_rather_than_read_as_not_fired() -> None:
    """The dangerous direction, pinned.

    Assay answers `exclusion` from a closed set of catalogue names holding no
    blank, so a blank means the answer was not recorded. Reading it as
    not-fired would release the post on its substance answer as though a
    reviewer had cleared it.
    """
    resolved = label_from_case(_case(exclusion="", substance="in_post"))
    assert isinstance(resolved, Err)
    assert "no exclusion answer was recorded" in resolved.error.detail


def test_needs_thread_does_not_change_the_action() -> None:
    with_thread = label_from_case(_case(substance="in_post", needs_thread=True))
    without = label_from_case(_case(substance="in_post", needs_thread=False))
    assert isinstance(with_thread, Ok) and isinstance(without, Ok)
    assert with_thread.value == without.value == "in_post"


def test_a_missing_substance_is_incomplete_not_a_none() -> None:
    resolved = label_from_case(_case(substance=None))
    assert isinstance(resolved, Err)
    assert "substance is absent" in resolved.error.detail


def test_an_unknown_substance_never_falls_back() -> None:
    resolved = label_from_case(_case(substance="maybe"))
    assert isinstance(resolved, Err)
    assert "not a known label" in resolved.error.detail


def _labels(*cases: AssayLabelCase, packet: str = "round-6") -> AssayLabelsFile:
    return AssayLabelsFile(
        format="assay.label-packet-labels/v3",
        packet=packet,
        packet_digest=_DIGEST,
        plan_digest=_PLAN,
        reviewer="reviewer",
        saved_at="2026-09-24T00:00:00Z",
        cases=list(cases),
    )


def _key(*cases: AssayKeyCase, name: str = "round-6", digest: str = _DIGEST) -> AssayKeyFile:
    return AssayKeyFile(
        format="assay.label-packet-key/v2",
        name=name,
        sitting="first",
        digest=digest,
        plan_digest=_PLAN,
        seed="seed-1",
        strata={"held": len(cases)},
        cases=list(cases),
    )


def _key_case(
    case_id: int = 1, evaluation_id: int = 1, project_key: str = "agent-ops"
) -> AssayKeyCase:
    return AssayKeyCase(
        case_id=case_id,
        evaluation_id=evaluation_id,
        project_key=project_key,
        production_decision=True,
        production_score=1.0,
    )


def test_labels_join_to_evaluations_through_the_key() -> None:
    resolved = resolve_labels(
        _labels(_case(case_id=5, substance="pointer")),
        _key(_key_case(case_id=5, evaluation_id=41)),
    )
    assert isinstance(resolved, Ok)
    assert set(resolved.value.by_evaluation) == {41}
    assert resolved.value.by_evaluation[41].action == "review"


def test_a_mismatched_packet_digest_is_refused() -> None:
    resolved = resolve_labels(_labels(_case()), _key(_key_case(), digest="c" * 64))
    assert isinstance(resolved, Err)
    assert "packet_digest" in resolved.error.detail


def test_a_mismatched_packet_name_is_refused() -> None:
    resolved = resolve_labels(_labels(_case()), _key(_key_case(), name="round-5"))
    assert isinstance(resolved, Err)
    assert "key name" in resolved.error.detail


def test_a_supplied_packet_file_is_verified_too() -> None:
    packet = AssayPacketFile(
        format="assay.label-packet/v1",
        name="round-6",
        sitting="first",
        digest="c" * 64,
        plan_digest=_PLAN,
    )
    resolved = resolve_labels(_labels(_case()), _key(_key_case()), packet)
    assert isinstance(resolved, Err)
    assert "packet digest" in resolved.error.detail


def test_an_agreeing_packet_file_passes() -> None:
    packet = AssayPacketFile(
        format="assay.label-packet/v1",
        name="round-6",
        sitting="first",
        digest=_DIGEST,
        plan_digest=_PLAN,
    )
    assert isinstance(resolve_labels(_labels(_case()), _key(_key_case()), packet), Ok)


def test_a_duplicate_label_case_is_refused() -> None:
    resolved = resolve_labels(
        _labels(_case(case_id=1), _case(case_id=1)), _key(_key_case(case_id=1))
    )
    assert isinstance(resolved, Err)
    assert "duplicate case id" in resolved.error.detail


def test_a_duplicate_key_case_is_refused() -> None:
    resolved = resolve_labels(
        _labels(_case(case_id=1)),
        _key(_key_case(case_id=1, evaluation_id=1), _key_case(case_id=1, evaluation_id=2)),
    )
    assert isinstance(resolved, Err)
    assert "duplicate case id" in resolved.error.detail


def test_a_key_mapping_two_cases_to_one_evaluation_is_refused() -> None:
    resolved = resolve_labels(
        _labels(_case(case_id=1)),
        _key(_key_case(case_id=1, evaluation_id=7), _key_case(case_id=2, evaluation_id=7)),
    )
    assert isinstance(resolved, Err)
    assert "two cases" in resolved.error.detail


def test_a_label_the_key_does_not_know_is_refused() -> None:
    resolved = resolve_labels(_labels(_case(case_id=9)), _key(_key_case(case_id=1)))
    assert isinstance(resolved, Err)
    assert "the key does not contain" in resolved.error.detail


def test_one_malformed_label_refuses_the_whole_file() -> None:
    resolved = resolve_labels(
        _labels(_case(case_id=1, substance="in_post"), _case(case_id=2, substance="maybe")),
        _key(_key_case(case_id=1, evaluation_id=1), _key_case(case_id=2, evaluation_id=2)),
    )
    assert isinstance(resolved, Err)


def test_a_mixed_file_resolves_every_label_it_carries() -> None:
    resolved = resolve_labels(
        _labels(
            _case(case_id=1, exclusion="promo", substance=None),
            _case(case_id=2, substance="in_post"),
            _case(case_id=3, substance="pointer"),
            _case(case_id=4, substance="none"),
        ),
        _key(
            _key_case(case_id=1, evaluation_id=1),
            _key_case(case_id=2, evaluation_id=2),
            _key_case(case_id=3, evaluation_id=3),
            _key_case(case_id=4, evaluation_id=4),
        ),
    )
    assert isinstance(resolved, Ok)
    assert [resolved.value.by_evaluation[i].action for i in (1, 2, 3, 4)] == [
        "drop",
        "respond",
        "review",
        "drop",
    ]


def test_no_files_means_an_ungraded_release() -> None:
    loaded = load_release_labels(ReleaseInputs())
    assert isinstance(loaded, Ok)
    assert loaded.value.is_empty


def test_labels_without_a_key_are_refused(tmp_path: Path) -> None:
    loaded = load_release_labels(ReleaseInputs(labels_path=tmp_path / "labels.json"))
    assert isinstance(loaded, Err)
    assert "together" in loaded.error.detail


def test_a_malformed_labels_file_is_refused(tmp_path: Path) -> None:
    labels_path = tmp_path / "labels.json"
    labels_path.write_text('{"format": "wrong"}')
    key_path = tmp_path / "key.json"
    key_path.write_text(_key(_key_case()).model_dump_json())

    loaded = load_release_labels(ReleaseInputs(labels_path=labels_path, key_path=key_path))

    assert isinstance(loaded, Err)
    assert loaded.error.operation == "load_labels"


def test_files_round_trip_from_disk(tmp_path: Path) -> None:
    labels_path = tmp_path / "labels.json"
    labels_path.write_text(_labels(_case(case_id=1, substance="in_post")).model_dump_json())
    key_path = tmp_path / "key.json"
    key_path.write_text(_key(_key_case(case_id=1, evaluation_id=41)).model_dump_json())

    loaded = load_release_labels(ReleaseInputs(labels_path=labels_path, key_path=key_path))

    assert isinstance(loaded, Ok)
    assert loaded.value.by_evaluation[41].label == "in_post"


# ---------------------------------------------------------------------------
# Releasing against a real database
# ---------------------------------------------------------------------------


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


def _frozen(platform_id: str, *, project_key: str = "agent-ops") -> FrozenHoldoutInput:
    return FrozenHoldoutInput(
        platform="farcaster",
        platform_id=platform_id,
        url=f"https://warpcast.com/ada/{platform_id}",
        channel="agents",
        text="our planner retries every failed step twice",
        author_id="42",
        author_name="Ada",
        author_handle="ada",
        project_key=project_key,
        project_name="Agent Ops",
        project_description="A description.",
        keyword_route_id=1,
        dossier_summary_id="d-old",
        dossier_revision="r-old",
    )


def _seed_route(state: StateManager, project_key: str = "agent-ops") -> None:
    """The stored project and route the frozen keyword_route_id points at."""
    now = "2026-09-01T00:00:00+00:00"
    state.conn.execute(
        "INSERT OR IGNORE INTO projects (key, name, description, link, created_at, updated_at, "
        "dossier_summary_id) VALUES (?, 'Agent Ops', 'A description.', "
        "'https://example.invalid', ?, ?, 'd-current')",
        (project_key, now, now),
    )
    state.conn.execute(
        "INSERT OR IGNORE INTO project_keywords (id, project_key, keyword, priority, "
        "created_at, updated_at) VALUES (1, ?, 'planner', 10, ?, ?)",
        (project_key, now, now),
    )


def _hold(
    state: StateManager,
    platform_id: str = "0xabc",
    *,
    action: str = "respond",
    relevant: bool = True,
    score: float = 1.0,
    project_key: str = "agent-ops",
) -> tuple[int, int]:
    _seed_route(state, project_key)
    scan_id = state.start_scan(environment="test")
    msg = _message(platform_id)
    post_id = state.save_post(msg, scan_id)
    evaluation_id = state.evaluations.save_evaluation(
        RelevanceResult(
            message=msg,
            relevant=relevant,
            score=score,
            reason="classifier reason",
            relevant_to=(project_key,),
        ),
        post_id,
        scan_id,
        keyword_route_id=1,
        surface_status="held",
        project_key=project_key,
        dossier_summary_id="d-old",
        dossier_revision="r-old",
    )
    written = state.holdouts.record_decision(
        RelevanceDecisionWrite(
            evaluation_id=evaluation_id,
            classifier="jev",
            model="jev-latest",
            action=action,  # type: ignore[arg-type]
            reason="classifier reason",
        )
    )
    assert isinstance(written, Ok), written
    held = state.holdouts.hold(
        HoldoutWrite(
            evaluation_id=evaluation_id,
            post_id=post_id,
            scan_id=scan_id,
            project_key=project_key,
            frozen_input=_frozen(platform_id, project_key=project_key),
        )
    )
    assert isinstance(held, Ok), held
    return held.value.id, evaluation_id


def _dossier() -> DossierSummary:
    return DossierSummary(
        project_key="agent-ops",
        last_reviewed=datetime(2026, 9, 1, tzinfo=UTC).date(),
        reviewer="tester",
        facts=[],
        resources=[],
        prohibitions=[],
    )


def _registry(*, project_key: str = "agent-ops") -> RuntimeRegistry:
    return RuntimeRegistry(
        projects={
            project_key: ProjectTarget(
                key=project_key,
                name="Agent Ops",
                description="A description.",
                link="https://example.invalid",
                dossier_summary_id="d-current",
            )
        },
        keywords=(
            KeywordRoute(
                id=1,
                project_key=project_key,
                keyword="planner",
                evaluate_prompt=None,
                respond_prompt=None,
                critique_prompt=None,
                priority=10,
            ),
        ),
        prompt_templates={},
    )


def _draft() -> StructuredDraftOutput:
    return StructuredDraftOutput(
        posture="answer",
        segments=[DeclarativeSegment(type="declarative", fact_id="f1", text="text")],
        claims=["text"],
        resources_used=[],
    )


#: Distinguishes one seeded attempt's traces from the next. A real phase run
#: gets a fresh trace id per model call; a fixture that reused one would trip
#: the trace_id UNIQUE as soon as two attempts shared a scan.
_seeded_attempts = itertools.count()


def _seed_response_phase_runs(
    state: StateManager, scan_id: int, post_id: int
) -> tuple[int, ...]:
    """The reply_draft and critic runs a release actually produces."""
    attempt = next(_seeded_attempts)
    snapshot = state.conn.execute(
        "SELECT id FROM feedback_snapshots WHERE scan_id = ?", (scan_id,)
    ).fetchone()
    if snapshot is None:
        phases = {
            phase.phase: phase.snapshot_phase_id
            for phase in state.record_feedback_snapshot(scan_id, mode="shadow").phases
        }
    else:
        phases = {
            row["phase"]: row["id"]
            for row in state.conn.execute(
                "SELECT phase, id FROM feedback_snapshot_phases WHERE snapshot_id = ?",
                (snapshot["id"],),
            ).fetchall()
        }
    return tuple(
        state.insert_phase_run(
            scan_id=scan_id,
            post_id=post_id,
            snapshot_phase_id=phases[phase],
            phase=phase,
            trace_id=f"trace-{phase}-{post_id}-{scan_id}-{attempt}",
            model="model",
            status="complete",
        )
        for phase in ("reply_draft", "critic")
    )


@pytest.fixture
def release_env(sm: StateManager, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Release wired to a real database with every model call replaced."""
    registry = _registry()
    monkeypatch.setattr(sm, "load_runtime_registry", lambda: registry)
    monkeypatch.setattr(
        release_module, "load_project_dossiers", lambda projects: ({"agent-ops": _dossier()}, [])
    )
    monkeypatch.setattr(release_module._config, "SCOUT_DOSSIER_ROOT", "")

    def fake_build_runtime(**kwargs: Any) -> Any:
        return SimpleNamespace(
            phase_configs=Mock(),
            execution=SimpleNamespace(
                state=kwargs["state"], scan_id=kwargs["scan_id"], post_id=kwargs["post_id"]
            ),
        )

    monkeypatch.setattr(release_module, "build_response_phase_runtime", fake_build_runtime)

    calls: list[str] = []

    async def fake_draft_and_critic_step(ctx: dict[str, Any]) -> Any:
        calls.append("draft_and_critic")
        execution = ctx["execution_context"]
        contributors = _seed_response_phase_runs(sm, execution.scan_id, execution.post_id)
        return Ok(
            ReplyCandidate(
                relevant=True,
                score=ctx["relevance_output"].score,
                reason=ctx["relevance_output"].reason,
                relevant_to=ctx["relevance_output"].relevant_to,
                project_key="agent-ops",
                critique_verdict="approve",
                critique_feedback="ok",
                structured_draft=_draft(),
                contributor_phase_run_ids=contributors,
            )
        )

    monkeypatch.setattr(release_module, "draft_and_critic_step", fake_draft_and_critic_step)

    from scout.verifier import VerifyResult

    monkeypatch.setattr(
        "scout.scanning.runner.verify_draft_content",
        lambda **kwargs: VerifyResult(ok=True, violations=[], assembled_text="a reply"),
    )
    return {"state": sm, "calls": calls, "registry": registry, "seed": _seed_response_phase_runs}


async def _release_result(env: dict[str, Any], labels: ReleaseLabels | None = None) -> Any:
    """The raw Result, for the cases that assert a whole-run refusal."""
    return await release_pending_holdouts(
        state=env["state"],
        tracer=Mock(),
        feedback=Mock(),
        labels=labels or ReleaseLabels(by_evaluation={}),
        owner="worker-1",
    )


async def _release(env: dict[str, Any], labels: ReleaseLabels | None = None) -> Any:
    released = await _release_result(env, labels)
    assert isinstance(released, Ok), released
    return released.value


def _labels_for(evaluation_id: int, label_case: AssayLabelCase) -> ReleaseLabels:
    return _labels_for_project(evaluation_id, label_case, "agent-ops")


def _labels_for_project(
    evaluation_id: int, label_case: AssayLabelCase, project_key: str
) -> ReleaseLabels:
    resolved = resolve_labels(
        _labels(label_case),
        _key(
            _key_case(
                case_id=label_case.case_id,
                evaluation_id=evaluation_id,
                project_key=project_key,
            )
        ),
    )
    assert isinstance(resolved, Ok), resolved
    return resolved.value


async def test_an_ungraded_hold_releases_on_its_recorded_action(
    release_env: dict[str, Any],
) -> None:
    holdout_id, _evaluation_id = _hold(release_env["state"], action="respond")

    report = await _release(release_env)

    assert report.released == 1
    outcome = report.outcomes[0]
    assert outcome.holdout_id == holdout_id
    assert outcome.authority == "recorded_action"
    assert outcome.action == "respond"
    assert outcome.surface_status == "surfaced"
    assert release_env["calls"] == ["draft_and_critic"]


async def test_an_ungraded_drop_never_drafts(release_env: dict[str, Any]) -> None:
    _hold(release_env["state"], action="drop", relevant=False, score=0.0)

    report = await _release(release_env)

    assert report.released == 1
    assert report.outcomes[0].action == "drop"
    assert report.outcomes[0].target_evaluation_id is None
    assert release_env["calls"] == []


async def test_a_graded_false_negative_drafts(release_env: dict[str, Any]) -> None:
    """The original decision was drop; the human says in_post, so it responds."""
    holdout_id, evaluation_id = _hold(
        release_env["state"], action="drop", relevant=False, score=0.0
    )
    labels = _labels_for(evaluation_id, _case(case_id=1, substance="in_post"))

    report = await _release(release_env, labels)

    assert report.released == 1
    outcome = report.outcomes[0]
    assert (outcome.authority, outcome.action, outcome.label) == ("label", "respond", "in_post")
    assert outcome.surface_status == "surfaced"
    assert release_env["calls"] == ["draft_and_critic"]

    held = release_env["state"].holdouts.get(holdout_id)
    assert held is not None
    assert held.release_authority == "label"
    assert held.label == "in_post"
    assert held.label_provenance is not None
    assert held.label_provenance.packet_digest == _DIGEST
    assert held.key_provenance is not None
    assert held.key_provenance.evaluation_id == evaluation_id


async def test_a_graded_drop_never_drafts(release_env: dict[str, Any]) -> None:
    """The original decision was respond; the human excludes it, so it stops."""
    _holdout_id, evaluation_id = _hold(release_env["state"], action="respond")
    labels = _labels_for(
        evaluation_id, _case(case_id=1, exclusion="promo_spam", substance=None)
    )

    report = await _release(release_env, labels)

    assert report.outcomes[0].action == "drop"
    assert release_env["calls"] == []


async def test_the_original_decision_survives_release(release_env: dict[str, Any]) -> None:
    state: StateManager = release_env["state"]
    holdout_id, evaluation_id = _hold(state, action="drop", relevant=False, score=0.0)
    labels = _labels_for(evaluation_id, _case(case_id=1, substance="in_post"))

    await _release(release_env, labels)

    original = state.holdouts.get_decision(evaluation_id)
    assert original is not None and original.action == "drop"
    source = state.get_evaluation(evaluation_id)
    assert source is not None and source["surface_status"] == "held"
    held = state.holdouts.get(holdout_id)
    assert held is not None and held.target_evaluation_id != evaluation_id


async def test_release_records_the_current_dossier_identity(
    release_env: dict[str, Any],
) -> None:
    """The hold keeps its decision-time identity; the target gets today's."""
    state: StateManager = release_env["state"]
    holdout_id, _evaluation_id = _hold(state, action="respond")

    report = await _release(release_env)

    assert report.outcomes[0].dossier_summary_id == "d-current"
    held = state.holdouts.get(holdout_id)
    assert held is not None and held.frozen_input.dossier_summary_id == "d-old"
    target = state.get_evaluation(report.outcomes[0].target_evaluation_id)
    assert target is not None and target["dossier_summary_id"] == "d-current"


async def test_a_wrong_project_label_refuses_the_run_before_anything_is_claimed(
    release_env: dict[str, Any],
) -> None:
    """Checked up front: a wrong pair of files is a mistake about the whole run.

    Discovering it on the fortieth hold would mean thirty-nine already
    released under labels that may be just as wrong.
    """
    state: StateManager = release_env["state"]
    holdout_id, evaluation_id = _hold(state, action="respond")
    resolved = resolve_labels(
        _labels(_case(case_id=1, substance="in_post")),
        _key(_key_case(case_id=1, evaluation_id=evaluation_id, project_key="other-project")),
    )
    assert isinstance(resolved, Ok)

    released = await _release_result(release_env, resolved.value)

    assert isinstance(released, Err)
    assert "froze project" in released.error.detail
    assert release_env["calls"] == []
    held = state.holdouts.get(holdout_id)
    assert held is not None
    assert held.status == "pending" and held.attempts == 0


async def test_a_label_for_an_evaluation_that_was_never_held_refuses_the_run(
    release_env: dict[str, Any],
) -> None:
    """Labels written for a different population, caught before any release."""
    _hold(release_env["state"], action="respond")
    labels = _labels_for(9999, _case(case_id=3, substance="in_post"))

    released = await _release_result(release_env, labels)

    assert isinstance(released, Err)
    assert "never held" in released.error.detail


async def test_a_label_for_an_already_released_hold_is_reported_not_refused(
    release_env: dict[str, Any],
) -> None:
    """A re-run over last week's labels is ordinary, not an operator mistake."""
    state: StateManager = release_env["state"]
    _holdout_id, evaluation_id = _hold(state, action="respond")
    labels = _labels_for(evaluation_id, _case(case_id=3, substance="in_post"))

    first = await _release(release_env, labels)
    assert first.released == 1

    second = await _release(release_env, labels)

    assert second.attempted == 0
    assert second.unmatched_label_cases == (3,)


def test_the_per_record_project_guard_stands_behind_the_preflight(
    sm: StateManager,
) -> None:
    """Defence in depth: resolve_release refuses the same mismatch on its own.

    The pre-flight is what an operator meets; this is what protects a caller
    releasing a single hold without one.
    """
    _holdout_id, evaluation_id = _hold(sm, action="respond")
    held = sm.holdouts.get_by_evaluation(evaluation_id)
    assert held is not None
    resolved = resolve_labels(
        _labels(_case(case_id=1, substance="in_post")),
        _key(_key_case(case_id=1, evaluation_id=evaluation_id, project_key="other-project")),
    )
    assert isinstance(resolved, Ok)

    outcome = resolve_release(sm, held, resolved.value)

    assert isinstance(outcome, Err)
    assert "does not match the frozen project" in outcome.error.detail


# --- the gates ---------------------------------------------------------------


async def test_a_missing_route_keeps_the_hold_pending(
    release_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    state: StateManager = release_env["state"]
    holdout_id, _evaluation_id = _hold(state, action="respond")
    monkeypatch.setattr(
        state,
        "load_runtime_registry",
        lambda: RuntimeRegistry(
            projects=release_env["registry"].projects, keywords=(), prompt_templates={}
        ),
    )

    report = await _release(release_env)

    assert report.failed == 1
    assert "no active route" in report.outcomes[0].error
    held = state.holdouts.get(holdout_id)
    assert held is not None and held.status == "failed"
    assert release_env["calls"] == []


async def test_a_missing_dossier_keeps_the_hold_pending(
    release_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _hold(release_env["state"], action="respond")
    monkeypatch.setattr(release_module, "load_project_dossiers", lambda projects: ({}, []))

    report = await _release(release_env)

    assert report.failed == 1
    assert "no ready dossier" in report.outcomes[0].error
    assert release_env["calls"] == []


async def test_a_blocked_account_cannot_silently_surface(
    release_env: dict[str, Any],
) -> None:
    state: StateManager = release_env["state"]
    _hold(state, action="respond")
    state.block_author(
        platform="farcaster", author_id="42", author_name="Ada", reason="spam"
    )

    report = await _release(release_env)

    assert report.failed == 1
    assert "blocked" in report.outcomes[0].error
    assert release_env["calls"] == []


async def test_repaired_configuration_permits_a_retry_at_any_age(
    release_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No age cutoff: an old hold whose project came back releases normally."""
    state: StateManager = release_env["state"]
    with pytest.MonkeyPatch.context() as clock:
        clock.setattr(holdout_storage, "_now", lambda: "2020-01-01T00:00:00+00:00")
        _hold(state, action="respond")

    monkeypatch.setattr(
        state,
        "load_runtime_registry",
        lambda: RuntimeRegistry(
            projects=release_env["registry"].projects, keywords=(), prompt_templates={}
        ),
    )
    first = await _release(release_env)
    assert first.failed == 1

    monkeypatch.setattr(state, "load_runtime_registry", lambda: release_env["registry"])
    second = await _release(release_env)

    assert second.released == 1
    assert second.outcomes[0].age_seconds > 365 * 24 * 3600


async def test_release_reports_hold_to_release_age(release_env: dict[str, Any]) -> None:
    _hold(release_env["state"], action="respond")

    report = await _release(release_env)

    assert report.outcomes[0].age_seconds >= 0.0


async def test_a_failing_hold_does_not_stop_the_others(
    release_env: dict[str, Any],
) -> None:
    state: StateManager = release_env["state"]
    # A hold whose frozen project no longer has a route fails its own config
    # gate; the one behind it is untouched and must still be attempted.
    stranded, _stranded_evaluation = _hold(state, "0x1", action="respond", project_key="gone")
    healthy, _healthy_evaluation = _hold(state, "0x2", action="respond")

    report = await _release(release_env)

    assert report.attempted == 2
    assert report.failed == 1
    assert report.released == 1
    by_holdout = {outcome.holdout_id: outcome for outcome in report.outcomes}
    assert by_holdout[stranded].status == "failed"
    assert by_holdout[healthy].status == "released"


# --- concurrency and crash safety -------------------------------------------


async def test_a_live_claim_is_left_to_its_holder(release_env: dict[str, Any]) -> None:
    state: StateManager = release_env["state"]
    holdout_id, _evaluation_id = _hold(state, action="respond")
    assert isinstance(state.holdouts.claim(holdout_id, owner="other-worker"), Ok)

    report = await _release(release_env)

    assert report.attempted == 0
    assert release_env["calls"] == []


async def test_an_expired_claim_is_taken_over(release_env: dict[str, Any]) -> None:
    state: StateManager = release_env["state"]
    holdout_id, _evaluation_id = _hold(state, action="respond")
    claim = state.holdouts.claim(holdout_id, owner="crashed-worker", ttl_seconds=1)
    assert isinstance(claim, Ok)
    state.conn.execute(
        "UPDATE relevance_holdouts SET claim_expires_at = ? WHERE id = ?",
        ((datetime.now(UTC) - timedelta(hours=1)).isoformat(), holdout_id),
    )

    report = await _release(release_env)

    assert report.released == 1
    stale = state.holdouts.complete_release(
        claim.value, release_authority="recorded_action", release_action="drop"
    )
    assert isinstance(stale, Err)
    assert "stale" in stale.error.detail


async def test_one_completed_target_and_event_per_hold(
    release_env: dict[str, Any],
) -> None:
    state: StateManager = release_env["state"]
    holdout_id, _evaluation_id = _hold(state, action="respond")

    first = await _release(release_env)
    second = await _release(release_env)

    assert first.released == 1
    assert second.attempted == 0
    held = state.holdouts.get(holdout_id)
    assert held is not None and held.status == "released"
    assert state.conn.execute("SELECT COUNT(*) FROM surfaced_events").fetchone()[0] == 1
    assert (
        state.conn.execute(
            "SELECT COUNT(*) FROM evaluations WHERE surface_status = 'surfaced'"
        ).fetchone()[0]
        == 1
    )


async def test_a_crash_before_persistence_leaves_the_hold_retryable(
    release_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    state: StateManager = release_env["state"]
    holdout_id, _evaluation_id = _hold(state, action="respond")

    real_persist = release_module.persist_outcome
    crashes = [True]

    def _crash_once(*args: object, **kwargs: object) -> int:
        if crashes:
            crashes.pop()
            raise RuntimeError("crashed after generation")
        return real_persist(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(release_module, "persist_outcome", _crash_once)
    report = await _release(release_env)

    assert report.failed == 1
    held = state.holdouts.get(holdout_id)
    assert held is not None and held.status == "failed"
    assert held.target_evaluation_id is None
    assert state.conn.execute("SELECT COUNT(*) FROM surfaced_events").fetchone()[0] == 0

    retried = await _release(release_env)
    assert retried.released == 1
    assert state.conn.execute("SELECT COUNT(*) FROM surfaced_events").fetchone()[0] == 1


async def test_a_refused_completion_rolls_the_target_back(
    release_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No half-released hold: a refused completion takes the target with it."""
    state: StateManager = release_env["state"]
    holdout_id, _evaluation_id = _hold(state, action="respond")
    monkeypatch.setattr(
        state.holdouts,
        "complete_release",
        lambda *args, **kwargs: Err(
            holdout_storage.HoldoutStorageError(
                operation="complete_release", detail="injected refusal"
            )
        ),
    )

    report = await _release(release_env)

    assert report.failed == 1
    assert "injected refusal" in report.outcomes[0].error
    held = state.holdouts.get(holdout_id)
    assert held is not None and held.target_evaluation_id is None
    assert state.conn.execute("SELECT COUNT(*) FROM surfaced_events").fetchone()[0] == 0


async def test_a_generation_failure_keeps_the_hold_pending(
    release_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    state: StateManager = release_env["state"]
    holdout_id, _evaluation_id = _hold(state, action="respond")

    async def _failing(ctx: dict[str, Any]) -> Any:
        return Err(LLMError(operation="reply_draft", message_id="0xabc", detail="upstream 503"))

    monkeypatch.setattr(release_module, "draft_and_critic_step", _failing)

    report = await _release(release_env)

    assert report.failed == 1
    assert report.outcomes[0].error_category == "generation"
    held = state.holdouts.get(holdout_id)
    assert held is not None and held.status == "failed"


# --- downstream safeguards ---------------------------------------------------


async def test_a_critic_rejection_completes_with_the_right_status(
    release_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _rejecting(ctx: dict[str, Any]) -> Any:
        execution = ctx["execution_context"]
        contributors = _seed_response_phase_runs(
            release_env["state"], execution.scan_id, execution.post_id
        )
        return Ok(
            ReplyCandidate(
                relevant=False,
                score=1.0,
                reason="critic rejected",
                relevant_to=["agent-ops"],
                project_key="agent-ops",
                critique_verdict="reject",
                critique_feedback="off-topic",
                structured_draft=_draft(),
                contributor_phase_run_ids=contributors,
            )
        )

    monkeypatch.setattr(release_module, "draft_and_critic_step", _rejecting)
    _hold(release_env["state"], action="respond")

    report = await _release(release_env)

    assert report.released == 1
    assert report.outcomes[0].surface_status == "critic_rejected"


async def test_an_abstention_completes_with_the_right_status(
    release_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _abstaining(ctx: dict[str, Any]) -> Any:
        execution = ctx["execution_context"]
        contributors = _seed_response_phase_runs(
            release_env["state"], execution.scan_id, execution.post_id
        )[:1]
        return Ok(
            ReplyCandidate(
                relevant=True,
                score=1.0,
                reason="abstained",
                relevant_to=["agent-ops"],
                project_key="agent-ops",
                structured_draft=StructuredDraftOutput(
                    posture="abstain", abstain_reason="nothing useful to add"
                ),
                contributor_phase_run_ids=contributors,
            )
        )

    monkeypatch.setattr(release_module, "draft_and_critic_step", _abstaining)
    _hold(release_env["state"], action="respond")

    report = await _release(release_env)

    assert report.outcomes[0].surface_status == "abstained"


async def test_a_verifier_rejection_completes_with_the_right_status(
    release_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from scout.verifier import GateViolation, VerifyResult

    monkeypatch.setattr(
        "scout.scanning.runner.verify_draft_content",
        lambda **kwargs: VerifyResult(
            ok=False,
            violations=[
                GateViolation(
                    reason_code="unsupported_claim", offending_text="x", segment_index=0
                )
            ],
            assembled_text="a reply",
        ),
    )
    _hold(release_env["state"], action="respond")

    report = await _release(release_env)

    assert report.released == 1
    assert report.outcomes[0].surface_status == "gate_blocked"


async def test_release_bypasses_the_relevance_threshold_only(
    release_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A threshold above every score must not stop an explicit release."""
    monkeypatch.setattr(config, "RELEVANCE_THRESHOLD", 0.99)
    monkeypatch.setattr("scout.scanning.runner.RELEVANCE_THRESHOLD", 0.99)
    _hold(release_env["state"], action="respond")

    report = await _release(release_env)

    assert report.outcomes[0].surface_status == "surfaced"


async def test_release_runs_no_relevance_phase(release_env: dict[str, Any]) -> None:
    """The stored decision or the human label is the relevance authority."""
    _holdout_id, evaluation_id = _hold(release_env["state"], action="respond")

    await _release(release_env)

    assert release_env["calls"] == ["draft_and_critic"]
    decisions = release_env["state"].conn.execute(
        "SELECT COUNT(*) FROM relevance_decisions"
    ).fetchone()[0]
    assert decisions == 1
    assert release_env["state"].holdouts.get_decision(evaluation_id) is not None


# --- the contract ------------------------------------------------------------


def test_a_resolved_label_record_validates_against_the_v1_schema(sm: StateManager) -> None:
    holdout_id, evaluation_id = _hold(sm)
    held = sm.holdouts.get(holdout_id)
    assert held is not None
    labels = _labels_for(evaluation_id, _case(case_id=1, substance="pointer"))

    record = released_label_record(held, labels.by_evaluation[evaluation_id])

    Draft7Validator(json.loads(_LABELS_SCHEMA_PATH.read_text())).validate(record)
    assert record["released_action"] == "review"
    assert record["authority"] == "label"


def test_the_contract_documents_the_precedence() -> None:
    schema = json.loads(_LABELS_SCHEMA_PATH.read_text())
    resolution = schema["$defs"]["resolution"]["description"]
    for fragment in ("exclusion", "in_post", "pointer", "none"):
        assert fragment in resolution


async def test_an_ungraded_release_is_reproducible_under_a_moved_threshold(
    release_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recorded action is acted on; no threshold is recomputed."""
    state: StateManager = release_env["state"]
    _holdout_id, evaluation_id = _hold(state, action="drop", relevant=True, score=0.4)
    monkeypatch.setattr("scout.scanning.runner.RELEVANCE_THRESHOLD", 0.1)

    report = await _release(release_env)

    assert report.outcomes[0].action == "drop"
    assert release_env["calls"] == []

    held = state.holdouts.get(_holdout_id)
    assert held is not None
    again = resolve_release(state, held, ReleaseLabels(by_evaluation={}))
    assert isinstance(again, Ok)
    assert again.value.action == "drop"


async def test_author_rate_suppression_completes_with_the_right_status(
    release_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capped author blocks the reply but still completes the release."""
    monkeypatch.setattr(config, "SCOUT_AUTHOR_WEEKLY_CAP", 0)
    state: StateManager = release_env["state"]
    holdout_id, _evaluation_id = _hold(state, action="respond")

    report = await _release(release_env)

    assert report.released == 1
    assert report.outcomes[0].surface_status == "gate_blocked"
    held = state.holdouts.get(holdout_id)
    assert held is not None and held.status == "released"
    assert held.target_evaluation_id == report.outcomes[0].target_evaluation_id
    assert state.conn.execute("SELECT COUNT(*) FROM surfaced_events").fetchone()[0] == 0


async def test_a_committed_release_reads_back_idempotently(
    release_env: dict[str, Any],
) -> None:
    """A re-run over a released hold reports the committed outcome, not a new one."""
    state: StateManager = release_env["state"]
    holdout_id, _evaluation_id = _hold(state, action="respond")
    first = await _release(release_env)
    target = first.outcomes[0].target_evaluation_id

    # Put the released hold back in front of the worker to force the claim.
    second = await release_pending_holdouts(
        state=state,
        tracer=Mock(),
        feedback=Mock(),
        labels=ReleaseLabels(by_evaluation={}),
        owner="worker-2",
    )
    assert isinstance(second, Ok)
    assert second.value.attempted == 0

    from scout.holdouts.release import release_one_holdout

    held = state.holdouts.get(holdout_id)
    assert held is not None
    outcome = await release_one_holdout(
        state=state,
        tracer=Mock(),
        feedback=Mock(),
        holdout=held,
        labels=ReleaseLabels(by_evaluation={}),
        owner="worker-3",
    )

    assert outcome.status == "skipped"
    assert outcome.already_completed is True
    assert outcome.target_evaluation_id == target
    assert state.conn.execute("SELECT COUNT(*) FROM surfaced_events").fetchone()[0] == 1


async def test_model_calls_happen_outside_every_database_transaction(
    release_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transaction held across a model call would block every other writer."""
    state: StateManager = release_env["state"]
    _hold(state, action="respond")
    seen: list[bool] = []
    inner = release_module.draft_and_critic_step

    async def _observing(ctx: dict[str, Any]) -> Any:
        seen.append(state.db.in_transaction)
        return await inner(ctx)

    monkeypatch.setattr(release_module, "draft_and_critic_step", _observing)

    await _release(release_env)

    assert seen == [False]


# --- the committed interchange, enforced -------------------------------------


def _write(path: Path, document: dict[str, Any]) -> Path:
    path.write_text(json.dumps(document))
    return path


def _labels_document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "format": "assay.label-packet-labels/v3",
        "packet": "round-6",
        "packet_digest": _DIGEST,
        "plan_digest": _PLAN,
        "reviewer": "reviewer",
        "saved_at": "2026-09-24T00:00:00Z",
        "cases": [
            {
                "case_id": 1,
                "exclusion": "none",
                "needs_thread": False,
                "substance": "in_post",
                "note": None,
            }
        ],
    }
    document.update(overrides)
    return document


def _key_document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "format": "assay.label-packet-key/v2",
        "name": "round-6",
        "sitting": "first",
        "digest": _DIGEST,
        "plan_digest": _PLAN,
        "seed": "seed-1",
        "strata": {"held": 1},
        "cases": [
            {
                "case_id": 1,
                "evaluation_id": 1,
                "project_key": "agent-ops",
                "production_decision": True,
                "production_score": 1.0,
            }
        ],
    }
    document.update(overrides)
    return document


def _inputs(tmp_path: Path, labels: dict[str, Any], key: dict[str, Any]) -> ReleaseInputs:
    return ReleaseInputs(
        labels_path=_write(tmp_path / "labels.json", labels),
        key_path=_write(tmp_path / "key.json", key),
    )


def test_conforming_files_load(tmp_path: Path) -> None:
    resolved = load_release_labels(_inputs(tmp_path, _labels_document(), _key_document()))
    assert isinstance(resolved, Ok), resolved
    assert resolved.value.by_evaluation[1].label == "in_post"


def test_a_labels_file_with_an_unknown_field_is_refused(tmp_path: Path) -> None:
    """additionalProperties: false in the committed schema, enforced here."""
    resolved = load_release_labels(
        _inputs(tmp_path, _labels_document(rubric=["extra"]), _key_document())
    )
    assert isinstance(resolved, Err)
    assert "assay.label-packet-labels/v3" in resolved.error.detail


def test_a_labels_case_missing_note_is_refused(tmp_path: Path) -> None:
    """`note` is required, nullable-but-present. An absent one is not v3."""
    document = _labels_document()
    del document["cases"][0]["note"]
    resolved = load_release_labels(_inputs(tmp_path, document, _key_document()))
    assert isinstance(resolved, Err)
    assert "cases/0" in resolved.error.detail


def test_a_key_missing_its_seed_is_refused(tmp_path: Path) -> None:
    """A truncated or hand-made key, caught by the contract rather than used."""
    document = _key_document()
    del document["seed"]
    resolved = load_release_labels(_inputs(tmp_path, _labels_document(), document))
    assert isinstance(resolved, Err)
    assert "assay.label-packet-key/v2" in resolved.error.detail


def test_a_key_case_with_an_extra_field_is_refused(tmp_path: Path) -> None:
    document = _key_document()
    document["cases"][0]["human_label"] = "surfaced"
    resolved = load_release_labels(_inputs(tmp_path, _labels_document(), document))
    assert isinstance(resolved, Err)
    assert "cases/0" in resolved.error.detail


def test_a_wrong_format_string_is_refused(tmp_path: Path) -> None:
    resolved = load_release_labels(
        _inputs(
            tmp_path,
            _labels_document(format="assay.label-packet-labels/v2"),
            _key_document(),
        )
    )
    assert isinstance(resolved, Err)
    assert "format" in resolved.error.detail


def test_a_non_json_file_is_reported_as_one(tmp_path: Path) -> None:
    labels_path = tmp_path / "labels.json"
    labels_path.write_text("not json at all")
    resolved = load_release_labels(
        ReleaseInputs(
            labels_path=labels_path,
            key_path=_write(tmp_path / "key.json", _key_document()),
        )
    )
    assert isinstance(resolved, Err)
    assert "not valid JSON" in resolved.error.detail


def test_a_blind_packet_is_validated_against_its_own_contract(tmp_path: Path) -> None:
    packet = {
        "format": "assay.label-packet/v1",
        "name": "round-6",
        "sitting": "first",
        "digest": _DIGEST,
        "plan_digest": _PLAN,
        "rubric": [],
        "cases": [{"case_id": 1, "text": "a post", "parent_text": None, "human_label": "yes"}],
    }
    resolved = load_release_labels(
        ReleaseInputs(
            labels_path=_write(tmp_path / "labels.json", _labels_document()),
            key_path=_write(tmp_path / "key.json", _key_document()),
            packet_path=_write(tmp_path / "packet.json", packet),
        )
    )
    assert isinstance(resolved, Err)
    assert "assay.label-packet/v1" in resolved.error.detail


@pytest.mark.parametrize(
    "interchange_format",
    ["assay.label-packet-labels/v3", "assay.label-packet-key/v2", "assay.label-packet/v1"],
)
def test_every_interchange_format_has_a_committed_schema(interchange_format: str) -> None:
    validator = release_module.interchange_validator(interchange_format)
    assert validator.schema["properties"]["format"]["const"] == interchange_format
    assert validator.schema["additionalProperties"] is False


def test_the_models_and_the_committed_schemas_agree_on_required_fields() -> None:
    """A model that drifts from its contract fails here, not in production."""
    labels_schema = release_module.interchange_validator(
        "assay.label-packet-labels/v3"
    ).schema
    assert set(labels_schema["required"]) <= set(AssayLabelsFile.model_fields)
    case_schema = labels_schema["properties"]["cases"]["items"]
    assert set(case_schema["required"]) == set(AssayLabelCase.model_fields)

    key_schema = release_module.interchange_validator("assay.label-packet-key/v2").schema
    assert set(key_schema["required"]) <= set(AssayKeyFile.model_fields)
    key_case_schema = key_schema["properties"]["cases"]["items"]
    assert set(key_case_schema["required"]) == set(AssayKeyCase.model_fields)


# --- abandoned attempts stay attributable ------------------------------------


def _abandoned_attempt(
    state: StateManager, holdout_id: int, post_id: int
) -> tuple[int, tuple[int, ...]]:
    """The state a killed worker leaves: durable evidence, nothing closed.

    The phase runs are complete and unlinked, the release scan was never
    completed or failed, and the claim lease is left behind to expire. No
    handler ran, because the process was gone before one could.
    """
    scan_id = state.start_scan(environment="test", run_kind="holdout_release")
    runs = _seed_response_phase_runs(state, scan_id, post_id)
    claim = state.holdouts.claim(holdout_id, owner="killed-worker", ttl_seconds=1)
    assert isinstance(claim, Ok)
    state.conn.execute(
        "UPDATE relevance_holdouts SET claim_expires_at = ? WHERE id = ?",
        ((datetime.now(UTC) - timedelta(hours=1)).isoformat(), holdout_id),
    )
    return scan_id, runs


def _post_id_of(state: StateManager, holdout_id: int) -> int:
    held = state.holdouts.get(holdout_id)
    assert held is not None
    return held.post_id


def test_abandoned_evidence_is_attributable_to_the_hold(sm: StateManager) -> None:
    """The model calls were made and paid for; their traces are still readable."""
    holdout_id, _evaluation_id = _hold(sm, action="respond")
    post_id = _post_id_of(sm, holdout_id)
    _scan_id, runs = _abandoned_attempt(sm, holdout_id, post_id)

    evidence = sm.holdouts.abandoned_release_evidence(post_id)

    assert [run.id for run in evidence] == list(runs)
    assert [run.phase for run in evidence] == ["reply_draft", "critic"]
    assert all(run.trace_id for run in evidence)


def test_an_ordinary_scan_s_evidence_is_not_read_as_an_abandoned_release(
    sm: StateManager,
) -> None:
    """A live scan's in-flight phase runs are not a release attempt's leftovers."""
    holdout_id, _evaluation_id = _hold(sm, action="respond")
    post_id = _post_id_of(sm, holdout_id)
    live_scan = sm.start_scan(environment="test", run_kind="live")
    _seed_response_phase_runs(sm, live_scan, post_id)

    assert sm.holdouts.abandoned_release_evidence(post_id) == []
    assert sm.holdouts.abandoned_release_scan(post_id) is None


def test_a_closed_release_scan_is_not_resumed(sm: StateManager) -> None:
    """A handled failure closed its own scan; only a crash leaves one open."""
    holdout_id, _evaluation_id = _hold(sm, action="respond")
    post_id = _post_id_of(sm, holdout_id)
    scan_id, _runs = _abandoned_attempt(sm, holdout_id, post_id)
    assert sm.holdouts.abandoned_release_scan(post_id) == scan_id

    sm.complete_scan(scan_id, messages_scanned=1, relevant_found=0)

    assert sm.holdouts.abandoned_release_scan(post_id) is None
    # The evidence itself stays attributable either way.
    assert len(sm.holdouts.abandoned_release_evidence(post_id)) == 2


async def test_a_retry_resumes_the_abandoned_release_scan(
    release_env: dict[str, Any],
) -> None:
    state: StateManager = release_env["state"]
    holdout_id, _evaluation_id = _hold(state, action="respond")
    post_id = _post_id_of(state, holdout_id)
    abandoned_scan, _runs = _abandoned_attempt(state, holdout_id, post_id)
    abandoned_traces = tuple(
        run.trace_id for run in state.holdouts.abandoned_release_evidence(post_id)
    )

    report = await _release(release_env)

    assert report.released == 1
    outcome = report.outcomes[0]
    assert outcome.scan_id == abandoned_scan
    assert outcome.resumed_scan is True
    assert outcome.abandoned_traces == abandoned_traces


async def test_the_abandoned_runs_stay_unlinked_when_the_retry_commits(
    release_env: dict[str, Any],
) -> None:
    """One attempt's outcome never claims another attempt's evidence."""
    state: StateManager = release_env["state"]
    holdout_id, _evaluation_id = _hold(state, action="respond")
    post_id = _post_id_of(state, holdout_id)
    _abandoned_scan, abandoned_runs = _abandoned_attempt(state, holdout_id, post_id)

    report = await _release(release_env)
    target = report.outcomes[0].target_evaluation_id

    linked = {
        row["id"]
        for row in state.conn.execute(
            "SELECT id FROM evaluation_phase_runs WHERE evaluation_id = ?", (target,)
        ).fetchall()
    }
    assert linked.isdisjoint(abandoned_runs)
    assert len(linked) == 2
    still_unlinked = state.conn.execute(
        "SELECT COUNT(*) FROM evaluation_phase_runs "
        "WHERE evaluation_id IS NULL AND id IN (?, ?)",
        abandoned_runs,
    ).fetchone()[0]
    assert still_unlinked == 2


async def test_a_handled_failure_reports_the_abandoned_work_on_the_next_run(
    release_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between generating and persisting costs calls; the retry says so."""
    state: StateManager = release_env["state"]
    holdout_id, _evaluation_id = _hold(state, action="respond")
    post_id = _post_id_of(state, holdout_id)

    real_persist = release_module.persist_outcome
    crashes = [True]

    def _crash_once(*args: object, **kwargs: object) -> int:
        if crashes:
            crashes.pop()
            raise RuntimeError("crashed after generation")
        return real_persist(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(release_module, "persist_outcome", _crash_once)
    first = await _release(release_env)
    assert first.failed == 1
    assert first.outcomes[0].abandoned_traces == ()

    assert len(state.holdouts.abandoned_release_evidence(post_id)) == 2

    retried = await _release(release_env)

    assert retried.released == 1
    assert len(retried.outcomes[0].abandoned_traces) == 2


# --- overlapping workers -----------------------------------------------------


def test_two_real_workers_racing_one_hold_produce_one_claim(tmp_path: Path) -> None:
    """Concurrency asserted against real threads and real SQLite locking.

    Two processes reaching the same hold is the ordinary case for a scheduled
    release, so the compare-and-swap is tested by actually racing it rather
    than by calling it twice in a row.
    """
    db_path = str(tmp_path / "scout.db")
    with StateManager(db_path=db_path) as seeder:
        holdout_id, _evaluation_id = _hold(seeder, action="respond")
        seeder.commit()

    barrier = threading.Barrier(2)
    outcomes: list[Any] = []
    lock = threading.Lock()

    def worker(owner: str) -> None:
        with StateManager(db_path=db_path) as state:
            state.conn.execute("PRAGMA busy_timeout=5000")
            barrier.wait()
            claimed = state.holdouts.claim(holdout_id, owner=owner)
            with lock:
                outcomes.append(claimed)

    threads = [threading.Thread(target=worker, args=(f"worker-{i}",)) for i in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    won = [outcome for outcome in outcomes if isinstance(outcome, Ok)]
    lost = [outcome for outcome in outcomes if isinstance(outcome, Err)]
    assert len(won) == 1, outcomes
    assert len(lost) == 1
    assert "not claimable" in lost[0].error.detail
    with StateManager(db_path=db_path) as reader:
        held = reader.holdouts.get(holdout_id)
        assert held is not None
        assert held.status == "claimed"
        assert held.claim_fence == 1
        assert held.claim_token == won[0].value.token


def test_a_superseded_worker_cannot_complete_behind_the_one_that_took_over(
    tmp_path: Path,
) -> None:
    """The fence, exercised across two connections rather than one."""
    db_path = str(tmp_path / "scout.db")
    with StateManager(db_path=db_path) as seeder:
        holdout_id, _evaluation_id = _hold(seeder, action="respond")
        first = seeder.holdouts.claim(holdout_id, owner="slow-worker", ttl_seconds=1)
        assert isinstance(first, Ok)
        seeder.conn.execute(
            "UPDATE relevance_holdouts SET claim_expires_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(hours=1)).isoformat(), holdout_id),
        )
        seeder.commit()

    takeover: list[Any] = []

    def worker() -> None:
        with StateManager(db_path=db_path) as state:
            state.conn.execute("PRAGMA busy_timeout=5000")
            takeover.append(state.holdouts.claim(holdout_id, owner="fast-worker"))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert isinstance(takeover[0], Ok)
    assert takeover[0].value.fence == 2
    with StateManager(db_path=db_path) as state:
        state.conn.execute("PRAGMA busy_timeout=5000")
        stale = state.holdouts.complete_release(
            first.value, release_authority="recorded_action", release_action="drop"
        )
        assert isinstance(stale, Err)
        assert "stale" in stale.error.detail
        held = state.holdouts.get(holdout_id)
        assert held is not None and held.status == "claimed"


async def test_two_overlapping_release_runs_release_one_hold_once(
    release_env: dict[str, Any],
) -> None:
    """Interleaved at every await, on one hold, with one surfaced event."""
    state: StateManager = release_env["state"]
    holdout_id, _evaluation_id = _hold(state, action="respond")

    first, second = await asyncio.gather(
        _release_result(release_env),
        _release_result(release_env),
    )

    assert isinstance(first, Ok) and isinstance(second, Ok)
    assert first.value.released + second.value.released == 1
    held = state.holdouts.get(holdout_id)
    assert held is not None and held.status == "released"
    assert state.conn.execute("SELECT COUNT(*) FROM surfaced_events").fetchone()[0] == 1
    assert (
        state.conn.execute(
            "SELECT COUNT(*) FROM evaluations WHERE surface_status = 'surfaced'"
        ).fetchone()[0]
        == 1
    )


# --- per-record isolation ----------------------------------------------------


async def test_an_unexpected_error_on_one_hold_does_not_lose_the_rest(
    release_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every eligible hold is attempted, whatever one of them does."""
    state: StateManager = release_env["state"]
    exploding, _first = _hold(state, "0x1", action="respond")
    _hold(state, "0x2", action="respond")
    _hold(state, "0x3", action="respond")

    real_release = release_module.release_one_holdout

    async def _explode_on_one(**kwargs: Any) -> Any:
        if kwargs["holdout"].id == exploding:
            raise ZeroDivisionError("something nothing here anticipated")
        return await real_release(**kwargs)

    monkeypatch.setattr(release_module, "release_one_holdout", _explode_on_one)

    report = await _release(release_env)

    assert report.attempted == 3
    assert report.released == 2
    assert report.failed == 1
    failed = next(o for o in report.outcomes if o.holdout_id == exploding)
    assert failed.error_category == "unexpected"
    assert "ZeroDivisionError" in failed.error


async def test_an_isolated_failure_leaves_its_hold_recoverable(
    release_env: dict[str, Any],
) -> None:
    """The claim it took expires; the hold is not lost to the explosion."""
    state: StateManager = release_env["state"]
    holdout_id, _evaluation_id = _hold(state, action="respond")

    async def _explode(**kwargs: Any) -> Any:
        raise ZeroDivisionError("boom")

    # Scoped, so undoing it cannot also undo the fixture's own patches —
    # both would otherwise be held by the same monkeypatch instance.
    with pytest.MonkeyPatch.context() as exploding:
        exploding.setattr(release_module, "release_one_holdout", _explode)
        report = await _release(release_env)
    assert report.failed == 1

    state.conn.execute(
        "UPDATE relevance_holdouts SET claim_expires_at = ? WHERE id = ?",
        ((datetime.now(UTC) - timedelta(hours=1)).isoformat(), holdout_id),
    )
    retried = await _release(release_env)

    assert retried.released == 1


# --- the frozen project is validated on every path ---------------------------


async def test_a_graded_drop_still_validates_the_frozen_project(
    release_env: dict[str, Any],
) -> None:
    """A drop writes nothing, but it spends the hold, so the gate still runs."""
    state: StateManager = release_env["state"]
    holdout_id, evaluation_id = _hold(state, action="respond", project_key="gone")
    labels = _labels_for_project(
        evaluation_id, _case(case_id=1, exclusion="promo", substance=None), "gone"
    )

    report = await _release(release_env, labels)

    assert report.failed == 1
    assert "no longer active" in report.outcomes[0].error or (
        "no active route" in report.outcomes[0].error
    )
    held = state.holdouts.get(holdout_id)
    assert held is not None
    assert held.status == "failed"
    assert held.release_action is None


async def test_an_ungraded_drop_still_validates_the_frozen_project(
    release_env: dict[str, Any],
) -> None:
    state: StateManager = release_env["state"]
    holdout_id, _evaluation_id = _hold(state, action="drop", relevant=False, project_key="gone")

    report = await _release(release_env)

    assert report.failed == 1
    held = state.holdouts.get(holdout_id)
    assert held is not None and held.status == "failed"


async def test_a_drop_on_a_live_project_still_completes(
    release_env: dict[str, Any],
) -> None:
    """The gate must not block the ordinary case it exists to protect."""
    state: StateManager = release_env["state"]
    holdout_id, _evaluation_id = _hold(state, action="drop", relevant=False, score=0.0)

    report = await _release(release_env)

    assert report.released == 1
    held = state.holdouts.get(holdout_id)
    assert held is not None
    assert held.status == "released" and held.release_action == "drop"
    assert release_env["calls"] == []


def test_a_hold_that_froze_no_project_has_nothing_to_validate(sm: StateManager) -> None:
    """There is no project to check, and no label can name one that matches."""
    holdout_id, _evaluation_id = _hold(sm, action="drop", relevant=False)
    held = sm.holdouts.get(holdout_id)
    assert held is not None
    projectless = replace(
        held, frozen_input=replace(held.frozen_input, project_key=None)
    )

    validated = release_module.validate_frozen_project(sm, projectless)

    assert isinstance(validated, Ok)
    assert validated.value is None
