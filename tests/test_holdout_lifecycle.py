"""The holdout lifecycle end to end, over one synthetic population.

One in-memory database carries a synthetic scan through the whole thing:
both classifiers decide, every routing action is recorded, the sampled posts
are held, the pending population exports blind, labelled and unlabelled
holds release, and each released hold lands on whatever the downstream gates
actually did with it. Every assertion reads back what committed.

Three facts are kept apart throughout, because the cohort this file belongs
to exists to stop them being folded together:

- the **source action** the classifier recorded,
- the **release authority** that acted on the hold — a label, or the
  recorded action,
- the **actual surface_status** the released target reached.

Synthetic throughout: invented posts, recorded decisions rather than model
calls, and the reply phases replaced. No private catalogue, no credential,
and no real label packet appears here.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Generator, Mapping
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

import scout.holdouts.release as release_module
from scout.cli.analysis import audit_status_consumers
from scout.config import Account, Message
from scout.dossiers.resolver import DossierSummary
from scout.holdouts.export import (
    BLIND_CASE_FIELDS,
    blind_case,
    export_holdouts,
    render_blind_jsonl,
    surfaced_or_dropped,
)
from scout.holdouts.release import (
    AssayKeyCase,
    AssayKeyFile,
    AssayLabelCase,
    AssayLabelsFile,
    ReleaseLabels,
    release_pending_holdouts,
    resolve_labels,
)
from scout.registry import KeywordRoute, ProjectTarget, RuntimeRegistry
from scout.result import Ok
from scout.scanning.runner import (
    FrozenProjectIdentity,
    PersistenceContext,
    classify_outcome,
    persist_outcome,
)
from scout.scanning.schemas import (
    DeclarativeSegment,
    HoldoutDraw,
    RecordedRelevanceDecision,
    ReplyCandidate,
    StructuredDraftOutput,
)
from scout.storage.evaluations import (
    SurfaceStatusCounts,
    is_actionable_for_posting,
    is_held,
)
from scout.storage.state import StateManager
from tests.conftest import seed_phase_run_contributors

PROJECT = "agent-ops"
ROUTE_ID = 1
_DIGEST = "c" * 64
_PLAN = "d" * 64

#: Distinguishes one seeded attempt's traces from the next, so two phase runs
#: in one scan never collide on the trace_id UNIQUE.
_attempts = itertools.count()


# ---------------------------------------------------------------------------
# One synthetic scan
# ---------------------------------------------------------------------------


@pytest.fixture
def state() -> Generator[StateManager, None, None]:
    manager = StateManager(db_path=":memory:")
    _seed_project(manager)
    yield manager
    manager.commit()
    manager.close()


def _seed_project(state: StateManager) -> None:
    now = "2026-09-01T00:00:00+00:00"
    state.conn.execute(
        "INSERT OR IGNORE INTO projects (key, name, description, link, created_at, "
        "updated_at, dossier_summary_id) VALUES (?, 'Agent Ops', 'A description.', "
        "'https://example.invalid', ?, ?, 'd-current')",
        (PROJECT, now, now),
    )
    state.conn.execute(
        "INSERT OR IGNORE INTO project_keywords (id, project_key, keyword, priority, "
        "created_at, updated_at) VALUES (?, ?, 'planner', 10, ?, ?)",
        (ROUTE_ID, PROJECT, now, now),
    )


def _message(platform_id: str) -> Message:
    # One author per case. The author-rate cap is a real gate on this path,
    # and a population that shared an author would start tripping it partway
    # through rather than reaching the outcome each case is about.
    return Message(
        platform="farcaster",
        platform_id=platform_id,
        channel_name="agents",
        channel_id="agents",
        author=Account(
            platform="farcaster", id=f"author-{platform_id}", name="Ada", handle="ada"
        ),
        content=f"our planner retries every failed step twice ({platform_id})",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        url=f"https://warpcast.com/ada/{platform_id}",
    )


def _dossiers() -> dict[str, DossierSummary]:
    return {
        PROJECT: DossierSummary(
            project_key=PROJECT,
            last_reviewed=datetime(2026, 9, 1, tzinfo=UTC).date(),
            reviewer="tester",
            facts=[],
            resources=[],
            prohibitions=[],
        )
    }


def _draft() -> StructuredDraftOutput:
    return StructuredDraftOutput(
        posture="answer",
        segments=[DeclarativeSegment(type="declarative", fact_id="f1", text="text")],
        claims=["text"],
        resources_used=[],
    )


def _registry() -> RuntimeRegistry:
    return RuntimeRegistry(
        projects={
            PROJECT: ProjectTarget(
                key=PROJECT,
                name="Agent Ops",
                description="A description.",
                link="https://example.invalid",
                dossier_summary_id="d-current",
            )
        },
        keywords=(
            KeywordRoute(
                id=ROUTE_ID,
                project_key=PROJECT,
                keyword="planner",
                evaluate_prompt=None,
                respond_prompt=None,
                critique_prompt=None,
                priority=10,
            ),
        ),
        prompt_templates={},
    )


def _candidate(
    *,
    classifier: str = "jev",
    action: str = "respond",
    selected: bool = False,
    verdict: str | None = "approve",
    drafts: bool = True,
    contributors: tuple[int, ...] = (),
) -> ReplyCandidate:
    """One scored candidate, as the pipeline would hand it to classification."""
    relevant = action != "drop"
    return ReplyCandidate(
        relevant=relevant,
        score=1.0 if relevant else 0.0,
        reason=f"{classifier} said {action}",
        relevant_to=[PROJECT],
        project_key=PROJECT,
        critique_verdict=None if not relevant or not drafts else verdict,  # type: ignore[arg-type]
        critique_feedback="ok" if verdict == "approve" else "off-topic",
        structured_draft=_draft() if relevant and drafts else None,
        contributor_phase_run_ids=contributors,
        relevance_decision=RecordedRelevanceDecision(
            classifier=classifier,  # type: ignore[arg-type]
            model="jev-latest" if classifier == "jev" else "test-model",
            action=action,  # type: ignore[arg-type]
            reason=f"{classifier} said {action}",
            catalogue_id="routed-features-fixture" if classifier == "jev" else None,
            catalogue_version="0" * 64 if classifier == "jev" else None,
            router_version="agent_ops_route/v1" if classifier == "jev" else None,
        ),
        holdout=HoldoutDraw(
            decision_key=f"farcaster:{action}",
            rate=1.0 if selected else 0.0,
            value=0.0 if selected else 0.5,
            selected=selected,
        ),
    )


def _scan(state: StateManager, platform_id: str, candidate_phases: int) -> tuple[int, int, Message]:
    """A fresh scan, its post, and the durable phase evidence the run produced."""
    scan_id = state.start_scan(environment="test")
    message = _message(platform_id)
    post_id = state.save_post(message, scan_id)
    seed_phase_run_contributors(state, scan_id, post_id, count=candidate_phases)
    return scan_id, post_id, message


def _decide(
    state: StateManager,
    platform_id: str,
    candidate_kwargs: Mapping[str, Any],
    *,
    dossiers: Mapping[str, DossierSummary] | None = None,
) -> int:
    """Run one synthetic post through classification and persistence.

    The real `classify_outcome`/`persist_outcome` pair, so the status a row
    ends up with is the production classification rather than a hand-picked
    string.
    """
    stops_before_drafting = (
        bool(candidate_kwargs.get("selected")) or candidate_kwargs.get("action") == "drop"
    )
    phases = 1 if stops_before_drafting else 3
    scan_id, post_id, message = _scan(state, platform_id, phases)
    contributors = tuple(
        row["id"]
        for row in state.conn.execute(
            "SELECT id FROM evaluation_phase_runs WHERE post_id = ? AND evaluation_id IS NULL "
            "ORDER BY id",
            (post_id,),
        ).fetchall()
    )
    candidate = _candidate(contributors=contributors, **candidate_kwargs)
    decision = classify_outcome(
        candidate, message, dict(_dossiers() if dossiers is None else dossiers)
    )
    evaluation_id = persist_outcome(
        state,
        decision,
        PersistenceContext(
            post_id=post_id,
            scan_id=scan_id,
            keyword_route_id=ROUTE_ID,
            dossier_revision="r-old",
            dossier_summary_id="d-old",
            surfaced_at=message.created_at.isoformat(),
            project=FrozenProjectIdentity(
                key=PROJECT, name="Agent Ops", description="A description."
            ),
        ),
    )
    state.commit()
    return evaluation_id


@pytest.fixture
def passing_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Content gates that pass, so the gate under test is the one named.

    The synthetic draft cites an invented fact, which the real verifier
    rightly refuses. The gate-blocked case in the population reaches its
    status through the missing-dossier gate instead, which is checked before
    the verifier runs and so is unaffected by this.
    """
    from scout.verifier import VerifyResult

    monkeypatch.setattr(
        "scout.scanning.runner.verify_draft_content",
        lambda **kwargs: VerifyResult(ok=True, violations=[], assembled_text="a reply"),
    )


@pytest.fixture
def population(state: StateManager, passing_verifier: None) -> dict[str, int]:
    """One synthetic population: both classifiers, every action, every outcome.

    Returns the evaluation id of each case by name, so a test names the case
    it is asserting about rather than indexing a list.
    """
    return {
        "jev_respond_held": _decide(
            state, "jev-respond-held", {"action": "respond", "selected": True}
        ),
        "jev_review_held": _decide(
            state, "jev-review-held", {"action": "review", "selected": True}
        ),
        "jev_drop_held": _decide(state, "jev-drop-held", {"action": "drop", "selected": True}),
        "jev_respond_surfaced": _decide(state, "jev-respond-live", {"action": "respond"}),
        "jev_review_surfaced": _decide(state, "jev-review-live", {"action": "review"}),
        "llm_respond_rejected": _decide(
            state, "llm-reject", {"classifier": "llm", "action": "respond", "verdict": "reject"}
        ),
        "llm_drop": _decide(state, "llm-drop", {"classifier": "llm", "action": "drop"}),
        "jev_respond_gate_blocked": _decide(
            state, "jev-gated", {"action": "respond"}, dossiers={}
        ),
        "llm_drafting_failed": _decide(
            state, "llm-nodraft", {"classifier": "llm", "action": "respond", "drafts": False}
        ),
    }


def _counts(state: StateManager) -> SurfaceStatusCounts:
    return state.evaluations.count_by_surface_status()


def _status_of(state: StateManager, evaluation_id: int) -> str:
    row = state.conn.execute(
        "SELECT surface_status FROM evaluations WHERE id = ?", (evaluation_id,)
    ).fetchone()
    return str(row["surface_status"])


# ---------------------------------------------------------------------------
# The scan half: what each classifier and action actually persisted
# ---------------------------------------------------------------------------


def test_every_sampled_action_is_held_whatever_it_decided(
    state: StateManager, population: dict[str, int]
) -> None:
    """Respond, review and drop are all held — the population is the decisions."""
    held = [population[name] for name in ("jev_respond_held", "jev_review_held", "jev_drop_held")]
    assert [_status_of(state, evaluation_id) for evaluation_id in held] == ["held"] * 3


def test_each_unsampled_decision_reaches_its_own_downstream_outcome(
    state: StateManager, population: dict[str, int]
) -> None:
    assert {
        name: _status_of(state, population[name])
        for name in (
            "jev_respond_surfaced",
            "jev_review_surfaced",
            "llm_respond_rejected",
            "llm_drop",
            "jev_respond_gate_blocked",
            "llm_drafting_failed",
        )
    } == {
        "jev_respond_surfaced": "surfaced",
        "jev_review_surfaced": "surfaced",
        "llm_respond_rejected": "critic_rejected",
        "llm_drop": "not_relevant",
        "jev_respond_gate_blocked": "gate_blocked",
        "llm_drafting_failed": "drafting_failed",
    }


def test_a_review_action_surfaces_exactly_as_a_respond_does(
    state: StateManager, population: dict[str, int]
) -> None:
    """The badge distinguishes them; the pipeline does not."""
    assert _status_of(state, population["jev_review_surfaced"]) == _status_of(
        state, population["jev_respond_surfaced"]
    )


def test_the_recorded_action_survives_beside_the_status(
    state: StateManager, population: dict[str, int]
) -> None:
    """A surfaced review is still readable as a review after the fact."""
    decision = state.holdouts.get_decision(population["jev_review_surfaced"])
    assert decision is not None
    assert (decision.action, decision.classifier) == ("review", "jev")


def test_held_is_counted_as_neither_surfaced_nor_drafting_failed(
    state: StateManager, population: dict[str, int]
) -> None:
    counts = _counts(state)
    assert (counts.held, counts.surfaced, counts.drafting_failed) == (3, 2, 1)
    assert counts.total == len(population)


def test_no_held_evaluation_is_actionable_for_posting(
    state: StateManager, population: dict[str, int]
) -> None:
    assert not is_actionable_for_posting("held")
    assert _counts(state).actionable == 2


def test_a_held_evaluation_never_drafts_or_surfaces(
    state: StateManager, population: dict[str, int]
) -> None:
    held = population["jev_respond_held"]
    assert is_held(_status_of(state, held))
    assert (
        state.conn.execute(
            "SELECT COUNT(*) FROM draft_comments WHERE evaluation_id = ?", (held,)
        ).fetchone()[0]
        == 0
    )
    assert (
        state.conn.execute(
            "SELECT COUNT(*) FROM surfaced_events WHERE evaluation_id = ?", (held,)
        ).fetchone()[0]
        == 0
    )


def test_every_decision_links_the_phase_evidence_that_produced_it(
    state: StateManager, population: dict[str, int]
) -> None:
    """No evaluation in the population is missing its durable phase runs."""
    unlinked = [
        name
        for name, evaluation_id in population.items()
        if state.conn.execute(
            "SELECT COUNT(*) FROM evaluation_phase_runs WHERE evaluation_id = ?",
            (evaluation_id,),
        ).fetchone()[0]
        == 0
    ]
    assert unlinked == []


def test_both_classifiers_are_recorded_across_the_population(
    state: StateManager, population: dict[str, int]
) -> None:
    rows = state.conn.execute(
        "SELECT classifier, action, COUNT(*) AS count FROM relevance_decisions "
        "GROUP BY classifier, action"
    ).fetchall()
    recorded = {(row["classifier"], row["action"]): row["count"] for row in rows}
    assert set(recorded) == {
        ("jev", "respond"),
        ("jev", "review"),
        ("jev", "drop"),
        ("llm", "respond"),
        ("llm", "drop"),
    }


def test_the_status_audit_finds_no_violated_invariant(
    state: StateManager, population: dict[str, int]
) -> None:
    audit = audit_status_consumers(state.conn)
    assert audit.findings == ()
    assert audit.held_evaluations == 3
    assert audit.actionable_evaluations == 2
    assert audit.evaluations_by_surface_status["held"] == 3


def test_the_status_audit_reports_a_held_row_that_acquired_a_draft(
    state: StateManager, population: dict[str, int]
) -> None:
    """The audit is not vacuously clean: a violated invariant is reported."""
    held = population["jev_respond_held"]
    post_id = state.conn.execute(
        "SELECT post_id FROM evaluations WHERE id = ?", (held,)
    ).fetchone()["post_id"]
    state.conn.execute(
        "INSERT INTO draft_comments (post_id, evaluation_id, project_key, comment_text, "
        "created_at) VALUES (?, ?, ?, 'a reply', '2026-09-24T00:00:00+00:00')",
        (post_id, held, PROJECT),
    )

    findings = {finding.invariant: finding.count for finding in audit_status_consumers(state.conn)
                .findings}

    assert findings == {"held_evaluation_has_no_draft": 1}


def test_the_status_audit_runs_as_an_analysis_command(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator-facing half: `scout analysis status-audit --db-path ...`."""
    import argparse

    from scout.cli.analysis import StatusConsumerAudit, run_analysis

    db_path = tmp_path / "scout.db"
    manager = StateManager(db_path=str(db_path))
    manager.commit()
    manager.close()

    result = run_analysis(
        argparse.Namespace(analysis_command="status-audit", db_path=str(db_path))
    )

    assert isinstance(result, Ok), result
    assert isinstance(result.value, StatusConsumerAudit)
    assert result.value.findings == ()


def test_the_status_audit_reads_a_database_with_no_holdout_schema() -> None:
    """A pre-JEV database audits clean, with absence reported as absence."""
    legacy = StateManager(db_path=":memory:")
    legacy.conn.execute("DROP TABLE relevance_holdouts")
    legacy.conn.execute("DROP TABLE relevance_decisions")
    audit = audit_status_consumers(legacy.conn)
    legacy.close()
    assert (audit.findings, audit.holdouts_by_status, audit.decisions_by_classifier) == ((), {}, {})


# ---------------------------------------------------------------------------
# The export half: the pending population, and the blind projection of it
# ---------------------------------------------------------------------------


def test_the_export_is_exactly_the_pending_holds(
    state: StateManager, population: dict[str, int]
) -> None:
    exported = export_holdouts(state.conn)
    assert isinstance(exported, Ok), exported
    assert {record.evaluation_id for record in exported.value} == {
        population["jev_respond_held"],
        population["jev_review_held"],
        population["jev_drop_held"],
    }


def test_the_export_carries_the_source_action_in_its_private_envelope(
    state: StateManager, population: dict[str, int]
) -> None:
    exported = export_holdouts(state.conn)
    assert isinstance(exported, Ok), exported
    by_evaluation = {record.evaluation_id: record for record in exported.value}
    assert by_evaluation[population["jev_review_held"]].private.action == "review"
    assert by_evaluation[population["jev_review_held"]].private.classifier.name == "jev"


def test_the_blind_projection_carries_no_classifier_metadata(
    state: StateManager, population: dict[str, int]
) -> None:
    """What a blind reviewer sees: the post, and nothing production decided."""
    exported = export_holdouts(state.conn)
    assert isinstance(exported, Ok), exported
    for line in render_blind_jsonl(exported.value).splitlines():
        assert set(json.loads(line)) == set(BLIND_CASE_FIELDS)
    projected = blind_case(exported.value[0])
    assert not hasattr(projected, "private")


def test_the_assay_decision_boundary_puts_review_beside_respond() -> None:
    assert [surfaced_or_dropped(action) for action in ("respond", "review", "drop")] == [
        True,
        True,
        False,
    ]


# ---------------------------------------------------------------------------
# The release half
# ---------------------------------------------------------------------------


def _seed_response_phase_runs(state: StateManager, scan_id: int, post_id: int) -> tuple[int, ...]:
    """The reply_draft and critic runs a release actually produces."""
    attempt = next(_attempts)
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
            trace_id=f"release-{phase}-{post_id}-{scan_id}-{attempt}",
            model="test-model",
            status="complete",
        )
        for phase in ("reply_draft", "critic")
    )


@pytest.fixture
def release_env(
    state: StateManager, population: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Release wired to the synthetic population with every model call replaced."""
    from scout.scanning.schemas import ReplyCandidate as Candidate
    from scout.verifier import VerifyResult

    registry = _registry()
    monkeypatch.setattr(state, "load_runtime_registry", lambda: registry)
    monkeypatch.setattr(
        release_module, "load_project_dossiers", lambda projects: (_dossiers(), [])
    )
    monkeypatch.setattr(release_module._config, "SCOUT_DOSSIER_ROOT", "")
    monkeypatch.setattr(
        release_module,
        "build_response_phase_runtime",
        lambda **kwargs: SimpleNamespace(
            phase_configs=Mock(),
            execution=SimpleNamespace(
                state=kwargs["state"], scan_id=kwargs["scan_id"], post_id=kwargs["post_id"]
            ),
        ),
    )
    monkeypatch.setattr(
        "scout.scanning.runner.verify_draft_content",
        lambda **kwargs: VerifyResult(ok=True, violations=[], assembled_text="a reply"),
    )
    verdicts: dict[int, str] = {}

    async def fake_draft_and_critic_step(ctx: dict[str, Any]) -> Any:
        execution = ctx["execution_context"]
        contributors = _seed_response_phase_runs(state, execution.scan_id, execution.post_id)
        return Ok(
            Candidate(
                relevant=True,
                score=ctx["relevance_output"].score,
                reason=ctx["relevance_output"].reason,
                relevant_to=ctx["relevance_output"].relevant_to,
                project_key=PROJECT,
                critique_verdict=verdicts.get(execution.post_id, "approve"),  # type: ignore[arg-type]
                critique_feedback="ok",
                structured_draft=_draft(),
                contributor_phase_run_ids=contributors,
            )
        )

    monkeypatch.setattr(release_module, "draft_and_critic_step", fake_draft_and_critic_step)
    return {"state": state, "population": population, "verdicts": verdicts}


def _labels_for(cases: Mapping[int, AssayLabelCase]) -> ReleaseLabels:
    """Resolve synthetic reviewer answers through a synthetic private key."""
    resolved = resolve_labels(
        AssayLabelsFile(
            format="assay.label-packet-labels/v3",
            packet="synthetic-sitting",
            packet_digest=_DIGEST,
            plan_digest=_PLAN,
            reviewer="reviewer",
            saved_at="2026-09-24T00:00:00Z",
            cases=list(cases.values()),
        ),
        AssayKeyFile(
            format="assay.label-packet-key/v2",
            name="synthetic-sitting",
            sitting="first",
            digest=_DIGEST,
            plan_digest=_PLAN,
            seed="seed-1",
            strata={"held": len(cases)},
            cases=[
                AssayKeyCase(
                    case_id=case.case_id,
                    evaluation_id=evaluation_id,
                    project_key=PROJECT,
                    production_decision=True,
                    production_score=1.0,
                )
                for evaluation_id, case in cases.items()
            ],
        ),
    )
    assert isinstance(resolved, Ok), resolved
    return resolved.value


async def _release(env: dict[str, Any], labels: ReleaseLabels | None = None) -> Any:
    released = await release_pending_holdouts(
        state=env["state"],
        tracer=Mock(),
        feedback=Mock(),
        labels=labels or ReleaseLabels(by_evaluation={}),
        owner="lifecycle-worker",
    )
    assert isinstance(released, Ok), released
    return released.value


async def test_an_ungraded_population_releases_on_its_recorded_actions(
    release_env: dict[str, Any],
) -> None:
    report = await _release(release_env)

    by_evaluation = {outcome.evaluation_id: outcome for outcome in report.outcomes}
    population = release_env["population"]
    assert report.released == 3
    assert {
        name: (by_evaluation[population[name]].authority, by_evaluation[population[name]].action)
        for name in ("jev_respond_held", "jev_review_held", "jev_drop_held")
    } == {
        "jev_respond_held": ("recorded_action", "respond"),
        "jev_review_held": ("recorded_action", "review"),
        "jev_drop_held": ("recorded_action", "drop"),
    }


async def test_a_graded_population_releases_on_its_labels(
    release_env: dict[str, Any],
) -> None:
    """The label overrides the recorded action, in both directions."""
    population = release_env["population"]
    labels = _labels_for(
        {
            # A respond the reviewer read as carrying nothing: released as a drop.
            population["jev_respond_held"]: AssayLabelCase(
                case_id=1, exclusion="none", needs_thread=False, substance="none", note=None
            ),
            # A drop the reviewer read as answerable in post: released as respond.
            population["jev_drop_held"]: AssayLabelCase(
                case_id=2, exclusion="none", needs_thread=False, substance="in_post", note=None
            ),
            # A pointer is a review, whatever the classifier said.
            population["jev_review_held"]: AssayLabelCase(
                case_id=3, exclusion="none", needs_thread=True, substance="pointer", note=None
            ),
        }
    )

    report = await _release(release_env, labels)

    by_evaluation = {outcome.evaluation_id: outcome for outcome in report.outcomes}
    assert {
        name: (
            by_evaluation[population[name]].authority,
            by_evaluation[population[name]].label,
            by_evaluation[population[name]].action,
        )
        for name in ("jev_respond_held", "jev_drop_held", "jev_review_held")
    } == {
        "jev_respond_held": ("label", "none", "drop"),
        "jev_drop_held": ("label", "in_post", "respond"),
        "jev_review_held": ("label", "pointer", "review"),
    }


async def test_release_never_overwrites_the_decision_it_released(
    release_env: dict[str, Any],
) -> None:
    """The source stays held; the released outcome is a distinct evaluation."""
    state, population = release_env["state"], release_env["population"]
    source = population["jev_respond_held"]

    report = await _release(release_env)

    released = next(outcome for outcome in report.outcomes if outcome.evaluation_id == source)
    assert _status_of(state, source) == "held"
    assert released.target_evaluation_id is not None
    assert released.target_evaluation_id != source
    assert _status_of(state, released.target_evaluation_id) == "surfaced"


async def test_a_released_drop_produces_no_target_at_all(
    release_env: dict[str, Any],
) -> None:
    state, population = release_env["state"], release_env["population"]

    report = await _release(release_env)

    dropped = next(
        outcome
        for outcome in report.outcomes
        if outcome.evaluation_id == population["jev_drop_held"]
    )
    assert (dropped.action, dropped.target_evaluation_id, dropped.surface_status) == (
        "drop",
        None,
        None,
    )
    assert _status_of(state, population["jev_drop_held"]) == "held"


async def test_a_suppressed_release_records_the_release_and_the_suppression_apart(
    release_env: dict[str, Any],
) -> None:
    """The hold released as `respond`; the critic rejected what it drafted.

    Release authority and downstream outcome are different facts, and this
    is the case where they disagree.
    """
    state, population = release_env["state"], release_env["population"]
    post_id = state.conn.execute(
        "SELECT post_id FROM evaluations WHERE id = ?", (population["jev_respond_held"],)
    ).fetchone()["post_id"]
    release_env["verdicts"][post_id] = "reject"

    report = await _release(release_env)

    outcome = next(
        outcome
        for outcome in report.outcomes
        if outcome.evaluation_id == population["jev_respond_held"]
    )
    assert (outcome.status, outcome.action) == ("released", "respond")
    assert outcome.surface_status == "critic_rejected"
    assert _status_of(state, outcome.target_evaluation_id or 0) == "critic_rejected"


async def test_a_released_target_carries_its_own_phase_evidence(
    release_env: dict[str, Any],
) -> None:
    state, population = release_env["state"], release_env["population"]

    report = await _release(release_env)

    target = next(
        outcome.target_evaluation_id
        for outcome in report.outcomes
        if outcome.evaluation_id == population["jev_review_held"]
    )
    phases = [
        row["phase"]
        for row in state.conn.execute(
            "SELECT phase FROM evaluation_phase_runs WHERE evaluation_id = ? ORDER BY id",
            (target,),
        ).fetchall()
    ]
    assert phases == ["reply_draft", "critic"]


async def test_a_released_target_records_no_classifier_of_its_own(
    release_env: dict[str, Any],
) -> None:
    """Its authority is the hold's release record, not a classifier run."""
    state, population = release_env["state"], release_env["population"]

    report = await _release(release_env)

    target = next(
        outcome.target_evaluation_id
        for outcome in report.outcomes
        if outcome.evaluation_id == population["jev_respond_held"]
    )
    assert target is not None
    assert state.holdouts.get_decision(target) is None
    assert state.holdouts.get_decision(population["jev_respond_held"]) is not None


async def test_the_status_audit_stays_clean_after_the_whole_lifecycle(
    release_env: dict[str, Any],
) -> None:
    state = release_env["state"]

    await _release(release_env)

    audit = audit_status_consumers(state.conn)
    assert audit.findings == ()
    assert audit.holdouts_by_status == {"released": 3}
    assert audit.released_by_authority == {"recorded_action": 3}
    assert audit.held_evaluations == 3
    assert audit.release_targets_without_classifier_record == 2


async def test_the_export_empties_as_the_population_releases(
    release_env: dict[str, Any],
) -> None:
    state = release_env["state"]

    await _release(release_env)

    exported = export_holdouts(state.conn)
    assert isinstance(exported, Ok), exported
    assert exported.value == ()


async def test_a_pending_hold_survives_a_rollback_to_the_llm_classifier(
    release_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rollback changes what decides next; it decides nothing about pending holds.

    The holdout rate is a separate control and the pending population is
    already recorded, so a hold taken under `jev` still releases on the
    action `jev` recorded for it after the classifier goes back to `llm`.
    """
    state, population = release_env["state"], release_env["population"]
    monkeypatch.setattr(release_module._config, "RELEVANCE_CLASSIFIER", "llm")
    monkeypatch.setattr(release_module._config, "RELEVANCE_HOLDOUT_RATE", 0.0)

    report = await _release(release_env)

    assert report.released == 3
    outcome = next(
        outcome
        for outcome in report.outcomes
        if outcome.evaluation_id == population["jev_review_held"]
    )
    assert (outcome.authority, outcome.action) == ("recorded_action", "review")
    assert state.holdouts.get_decision(population["jev_review_held"]) is not None
