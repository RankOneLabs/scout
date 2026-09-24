"""Releasing held posts: label resolution, the gates, and crash safety.

Label resolution is a pure transform and is tested as one. The release
itself is tested against a real in-memory database with the model phases
replaced, because every claim that matters here — one target evaluation, one
surface event, a fenced stale completion — is a claim about what committed.
"""

from __future__ import annotations

import json
from collections.abc import Generator
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
    NO_EXCLUSION_VALUES,
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
        pytest.param(_case(exclusion="promo_spam"), "exclusion", "drop", id="exclusion"),
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


def test_an_exclusion_beats_a_substance_answer() -> None:
    """Precedence, not a merge: an excluded post drops whatever it carried."""
    resolved = label_from_case(_case(exclusion="promo_spam", substance="in_post"))
    assert isinstance(resolved, Ok)
    assert resolved.value == "exclusion"


@pytest.mark.parametrize("value", ["", "none", "NONE", "  none  "])
def test_a_blank_or_none_exclusion_did_not_fire(value: str) -> None:
    resolved = label_from_case(_case(exclusion=value, substance="in_post"))
    assert isinstance(resolved, Ok)
    assert resolved.value == "in_post"


def test_the_no_exclusion_sentinels_are_the_documented_two() -> None:
    assert frozenset({"", "none"}) == NO_EXCLUSION_VALUES


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
            _case(case_id=1, exclusion="promo"),
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


def _seed_response_phase_runs(
    state: StateManager, scan_id: int, post_id: int
) -> tuple[int, ...]:
    """The reply_draft and critic runs a release actually produces."""
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
            trace_id=f"trace-{phase}-{post_id}-{scan_id}",
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


async def _release(env: dict[str, Any], labels: ReleaseLabels | None = None) -> Any:
    return await release_pending_holdouts(
        state=env["state"],
        tracer=Mock(),
        feedback=Mock(),
        labels=labels or ReleaseLabels(by_evaluation={}),
        owner="worker-1",
    )


def _labels_for(evaluation_id: int, label_case: AssayLabelCase) -> ReleaseLabels:
    resolved = resolve_labels(
        _labels(label_case),
        _key(_key_case(case_id=label_case.case_id, evaluation_id=evaluation_id)),
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
    labels = _labels_for(evaluation_id, _case(case_id=1, exclusion="promo_spam"))

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


async def test_a_wrong_project_label_is_refused_without_fallback(
    release_env: dict[str, Any],
) -> None:
    _holdout_id, evaluation_id = _hold(release_env["state"], action="respond")
    resolved = resolve_labels(
        _labels(_case(case_id=1, substance="in_post")),
        _key(_key_case(case_id=1, evaluation_id=evaluation_id, project_key="other-project")),
    )
    assert isinstance(resolved, Ok)

    report = await _release(release_env, resolved.value)

    assert report.failed == 1
    assert "does not match the frozen project" in report.outcomes[0].error
    assert release_env["calls"] == []


async def test_a_label_for_no_pending_hold_is_reported(release_env: dict[str, Any]) -> None:
    _hold(release_env["state"], action="respond")
    labels = _labels_for(9999, _case(case_id=3, substance="in_post"))

    report = await _release(release_env, labels)

    assert report.unmatched_label_cases == (3,)


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
    monkeypatch_target, evaluation_id = _hold(state, "0x1", action="respond")
    _hold(state, "0x2", action="respond", project_key="agent-ops")
    resolved = resolve_labels(
        _labels(_case(case_id=1, substance="in_post")),
        _key(_key_case(case_id=1, evaluation_id=evaluation_id, project_key="other")),
    )
    assert isinstance(resolved, Ok)

    report = await _release(release_env, resolved.value)

    assert report.attempted == 2
    assert report.failed == 1
    assert report.released == 1
    assert monkeypatch_target in {o.holdout_id for o in report.outcomes}


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
    assert second.attempted == 0

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
