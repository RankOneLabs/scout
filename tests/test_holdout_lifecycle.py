"""The holdout lifecycle end to end, over one synthetic integrated scan.

Two scans carry a synthetic population through the whole thing: an `llm`
scan and a `jev` scan, both driven by the real `score_messages` — the same
orchestration production runs, including the real pipeline, the real
tracer, and the durable `evaluation_phase_runs` rows each phase writes only
after its trace is read back and verified. Then the pending population
exports blind, and labelled and unlabelled holds release through the real
`release_pending_holdouts`, whose reply phases run the real
`draft_and_critic_step`.

Exactly two boundaries are replaced, and both are external IO:

- **the model.** `from_model` returns a scripted `LLMClient` that answers
  each phase's `submit_output` call from a table. Everything above it —
  prompt assembly, `run_agent`, the trace spans, the read-back, the phase-run
  insert — is the production path.
- **the JEV endpoint.** An `httpx.MockTransport` answers the catalogue's
  questions with a per-post probability vector. The request, the router and
  the recorded decision are production code.

Nothing else is faked: no `classify_outcome` shim, no hand-inserted phase
runs, no fabricated trace ids. Every status, decision, hold and phase-run
row these tests read was written by the code that writes it in production,
and `test_every_linked_phase_run_resolves_to_a_stored_agent_run_root`
checks the linked evidence against the trace store that actually holds it.

Three facts are kept apart throughout, because the cohort this file belongs
to exists to stop them being folded together:

- the **source action** the classifier recorded,
- the **release authority** that acted on the hold — a label, or the
  recorded action,
- the **actual surface_status** the released target reached.

Synthetic throughout: invented posts, a redistributable fixture catalogue,
scripted model answers and a mock transport. No private catalogue, no
credential, and no real label packet appears here.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncGenerator, Generator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from jig import (
    CompletionParams,
    LLMClient,
    LLMResponse,
    NullFeedbackLoop,
    SQLiteTracer,
    ToolCall,
    Usage,
)

import scout.config as _config
import scout.holdouts.release as release_module
import scout.scanning.agent as agent_module
import scout.scanning.runner as scan_runner
from scout.cli.analysis import audit_status_consumers
from scout.config import Account, Message
from scout.dossiers.resolver import DossierFact, DossierResource, DossierSummary
from scout.holdouts.export import (
    BLIND_CASE_FIELDS,
    blind_case,
    export_holdouts,
    render_blind_jsonl,
    render_holdout_jsonl,
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
from scout.scanning.jev import JevEndpoint
from scout.scanning.jev_catalogue import load_jev_catalogue
from scout.scanning.pipeline import holdout_decision_key
from scout.scanning.prefilter import RoutedMessage
from scout.scanning.relevance import JevRuntime, verify_agent_run_root
from scout.scanning.schemas import HoldoutDraw
from scout.storage.evaluations import (
    SurfaceStatusCounts,
    is_actionable_for_posting,
    is_held,
)
from scout.storage.state import StateManager

CATALOGUE = Path("tests/fixtures/relevance/routed-features.fixture.yaml")

#: The grounded project. Every case routes here unless it is testing what
#: happens when a post does not.
PROJECT = "agent-ops"
#: Routed, but deliberately absent from the resolved dossiers: the
#: gate_blocked case reaches its status through the missing-dossier gate.
UNGROUNDED = "agent-ops-ungrounded"
ROUTE_ID = 1
UNGROUNDED_ROUTE_ID = 2

_DIGEST = "c" * 64
_PLAN = "d" * 64

#: The grounding the scripted draft cites, so the real content verifier has
#: something to resolve it against.
FACT_ID = "lifecycle-fact"
RESOURCE_ID = "lifecycle-resource"
SAFE_PHRASING = "Our planner retries every failed step twice."


# ---------------------------------------------------------------------------
# The script: one row per post, read by both mocked boundaries
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Case:
    """One synthetic post and what each mocked boundary answers for it.

    `answers` drives the JEV transport; `relevant`/`score`/`relevant_to`
    drive the scripted relevance model. `posture` and `verdict` drive the
    drafter and critic for whichever classifier decided the post.
    """

    name: str
    classifier: str
    held: bool = False
    project: str | None = PROJECT
    answers: Mapping[str, float] = field(default_factory=dict)
    relevant: bool = True
    score: float = 0.9
    relevant_to: tuple[str, ...] = (PROJECT,)
    posture: str = "answer"
    verdict: str = "approve"

    @property
    def platform_id(self) -> str:
        return self.name.replace("_", "-")


#: Answer vectors over the fixture catalogue, chosen for the router line
#: each one lands on. Everything the vector omits answers 0.0, which is
#: below every threshold and further than the margin from all of them, so
#: only the named features decide.
_RESPOND = {"answerable_from_post": 0.9, "about_agent_work": 0.9}
_REVIEW = {"needs_thread": 0.9}
_DROP: Mapping[str, float] = {}

#: The whole population, in scan order. Both classifiers, every action, a
#: hold for each action, and every downstream outcome a decided post can
#: reach.
CASES: tuple[Case, ...] = (
    # --- the llm scan ---
    Case("llm_respond_held", "llm", held=True),
    Case("llm_drop_held", "llm", held=True, relevant=False, score=0.0, relevant_to=()),
    Case("llm_respond_surfaced", "llm"),
    Case("llm_drop", "llm", relevant=False, score=0.0, relevant_to=()),
    Case("llm_critic_rejected", "llm", verdict="reject"),
    Case("llm_low_relevance", "llm", score=0.4),
    Case("llm_abstained", "llm", posture="abstain"),
    Case("llm_gate_blocked", "llm", project=UNGROUNDED, relevant_to=(UNGROUNDED,)),
    Case("llm_drafting_failed", "llm", project=None, relevant_to=()),
    # --- the jev scan ---
    Case("jev_respond_held", "jev", held=True, answers=_RESPOND),
    Case("jev_review_held", "jev", held=True, answers=_REVIEW),
    Case("jev_drop_held", "jev", held=True, answers=_DROP),
    # Released, drafted, and then rejected by the critic: the case where the
    # release authority and the downstream outcome disagree.
    Case("jev_respond_held_rejected", "jev", held=True, answers=_RESPOND, verdict="reject"),
    Case("jev_respond_surfaced", "jev", answers=_RESPOND),
    Case("jev_review_surfaced", "jev", answers=_REVIEW),
    Case("jev_drop", "jev", answers=_DROP),
)


def _case_for_text(text: str) -> Case:
    """The case a post's text belongs to.

    Every synthetic post carries its own platform id in its content, which
    is what lets one scripted model and one mock transport serve the whole
    population without either of them seeing a scan id or a database row.
    """
    for case in CASES:
        if f"({case.platform_id})" in text:
            return case
    raise AssertionError(f"no scripted case for {text!r}")


# ---------------------------------------------------------------------------
# The two mocked boundaries
# ---------------------------------------------------------------------------


class ScriptedLLM(LLMClient):
    """The model boundary, and nothing above it.

    Answers every phase's injected `submit_output` call from `CASES`. The
    phase is identified by the schema jig put on the tool, and the post by
    the platform id carried in the prompt, so no scan state leaks into the
    script.
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self.calls: list[tuple[str, str]] = []

    async def complete(self, params: CompletionParams) -> LLMResponse:
        phase = _phase_of(params)
        case = _case_for_text(_prompt_text(params))
        self.calls.append((case.name, phase))
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id=f"{case.name}-{phase}", name="submit_output",
                         arguments=_phase_output(phase, case))
            ],
            usage=Usage(input_tokens=0, output_tokens=0),
            latency_ms=0.0,
            model=self.model,
        )


def _prompt_text(params: CompletionParams) -> str:
    return "\n".join(message.content for message in params.messages)


def _phase_of(params: CompletionParams) -> str:
    """Which phase asked, read from the output schema jig injected."""
    tool = next(tool for tool in params.tools or () if tool.name == "submit_output")
    properties = set(tool.parameters.get("properties", {}))
    if "relevant" in properties:
        return "relevance"
    if "posture" in properties:
        return "reply_draft"
    if "verdict" in properties:
        return "critic"
    raise AssertionError(f"unrecognized output schema: {sorted(properties)}")


def _draft_payload(case: Case) -> dict[str, Any]:
    if case.posture == "abstain":
        return {"posture": "abstain", "abstain_reason": "nothing to add",
                "segments": [], "claims": [], "resources_used": []}
    return {
        "posture": case.posture,
        "segments": [
            {"type": "declarative", "fact_id": FACT_ID, "text": SAFE_PHRASING},
            {"type": "resource", "resource_id": RESOURCE_ID},
        ],
        "claims": [SAFE_PHRASING],
        "resources_used": [RESOURCE_ID],
    }


def _phase_output(phase: str, case: Case) -> dict[str, Any]:
    if phase == "relevance":
        return {
            "relevant": case.relevant,
            "score": case.score,
            "reason": f"scripted {case.name}",
            "relevant_to": list(case.relevant_to),
        }
    if phase == "reply_draft":
        return _draft_payload(case)
    return {
        "verdict": case.verdict,
        "feedback": "ok" if case.verdict == "approve" else "off-topic",
    }


def _jev_transport() -> httpx.MockTransport:
    """The JEV endpoint, answering each post's catalogue questions.

    Every question the catalogue asks is answered, so `validate_answers`
    sees a complete vector; the script only overrides the features whose
    value decides the post's line.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        case = _case_for_text(body["state"]["post"]["text"])
        return httpx.Response(
            200,
            json={
                "answers": {
                    name: {"type": "noul", "noul": case.answers.get(name, 0.0)}
                    for name in body["questions"]
                }
            },
        )

    return httpx.MockTransport(handler)


def _sampler(held: frozenset[str]) -> Any:
    """The holdout draw, scripted rather than random.

    `score_messages` takes the sampler as an argument precisely so a test
    can decide the population instead of tuning a rate until the stable
    draw happens to pick the posts it wanted.
    """

    def sample(*, platform: str, platform_id: str) -> HoldoutDraw:
        selected = platform_id in held
        return HoldoutDraw(
            decision_key=holdout_decision_key(platform=platform, platform_id=platform_id),
            rate=1.0 if selected else 0.0,
            value=0.0 if selected else 0.5,
            selected=selected,
        )

    return sample


# ---------------------------------------------------------------------------
# The scan: real orchestration over the scripted boundaries
# ---------------------------------------------------------------------------


def _message(case: Case) -> Message:
    # One author per case. The author-rate cap is a real gate on this path,
    # and a population that shared an author would start tripping it partway
    # through rather than reaching the outcome each case is about.
    platform_id = case.platform_id
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


def _target(key: str) -> ProjectTarget:
    return ProjectTarget(
        key=key,
        name="Agent Ops",
        description="A description.",
        link="https://example.invalid",
        dossier_summary_id="d-current",
    )


def _registry() -> RuntimeRegistry:
    return RuntimeRegistry(
        projects={PROJECT: _target(PROJECT), UNGROUNDED: _target(UNGROUNDED)},
        keywords=(
            KeywordRoute(
                id=ROUTE_ID, project_key=PROJECT, keyword="planner",
                evaluate_prompt=None, respond_prompt=None, critique_prompt=None, priority=10,
            ),
            KeywordRoute(
                id=UNGROUNDED_ROUTE_ID, project_key=UNGROUNDED, keyword="planner",
                evaluate_prompt=None, respond_prompt=None, critique_prompt=None, priority=5,
            ),
        ),
        prompt_templates={},
    )


def _dossiers() -> dict[str, DossierSummary]:
    """Only the grounded project resolves. UNGROUNDED deliberately does not.

    Carries a real fact and resource so the scripted draft below cites
    grounding that exists. The content verifier therefore runs for real on
    every surfaced case: nothing here patches it out, and a draft citing an
    invented fact would gate-block exactly as it should.
    """
    return {
        PROJECT: DossierSummary(
            project_key=PROJECT,
            last_reviewed=datetime(2026, 9, 1, tzinfo=UTC).date(),
            reviewer="tester",
            facts=[
                DossierFact(
                    id=FACT_ID,
                    text=f"Background: {SAFE_PHRASING}",
                    safe_phrasings=[SAFE_PHRASING],
                    immutable_evidence=["https://source.example.invalid/evidence"],
                )
            ],
            resources=[
                DossierResource(
                    id=RESOURCE_ID,
                    label="Agent Ops Docs",
                    canonical_url="https://docs.example.invalid/agent-ops",
                    immutable_evidence=["https://source.example.invalid/evidence"],
                )
            ],
            prohibitions=[],
        )
    }


def _seed_projects(state: StateManager) -> None:
    now = "2026-09-01T00:00:00+00:00"
    for key, route_id, priority in (
        (PROJECT, ROUTE_ID, 10),
        (UNGROUNDED, UNGROUNDED_ROUTE_ID, 5),
    ):
        state.conn.execute(
            "INSERT OR IGNORE INTO projects (key, name, description, link, created_at, "
            "updated_at, dossier_summary_id) VALUES (?, 'Agent Ops', 'A description.', "
            "'https://example.invalid', ?, ?, 'd-current')",
            (key, now, now),
        )
        state.conn.execute(
            "INSERT OR IGNORE INTO project_keywords (id, project_key, keyword, priority, "
            "created_at, updated_at) VALUES (?, ?, 'planner', ?, ?, ?)",
            (route_id, key, priority, now, now),
        )
    state.commit()


def _routes() -> Mapping[str | None, KeywordRoute | None]:
    by_project = {route.project_key: route for route in _registry().keywords}
    return {PROJECT: by_project[PROJECT], UNGROUNDED: by_project[UNGROUNDED], None: None}


async def _run_scan(
    state: StateManager,
    tracer: SQLiteTracer,
    classifier: str,
    digest_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One classifier's scan, through the production entry point."""
    cases = [case for case in CASES if case.classifier == classifier]
    routes = _routes()
    routed = [
        RoutedMessage(message=_message(case), keyword_route=routes[case.project])
        for case in cases
    ]
    scan_id = state.start_scan(environment="test")
    snapshot = state.record_feedback_snapshot(scan_id, mode="shadow")
    state.commit()

    runtime: JevRuntime | None = None
    if classifier == "jev":
        loaded = load_jev_catalogue(CATALOGUE)
        assert isinstance(loaded, Ok), loaded
        runtime = JevRuntime(
            catalogue=loaded.value,
            endpoint=JevEndpoint(api_key="synthetic-key"),
            transport=_jev_transport(),
        )
    monkeypatch.setattr(scan_runner, "build_jev_runtime", lambda: runtime)

    registry = _registry()
    await scan_runner.score_messages(
        routed_candidates=routed,
        all_messages=[item.message for item in routed],
        base_mode_cfg=_config.MODES["lead_gen"],
        projects=registry.projects,
        templates=registry.prompt_templates,
        relevance_model="scripted-relevance",
        reply_draft_model="scripted-reply-draft",
        critic_model="scripted-critic",
        tracer=tracer,
        feedback=NullFeedbackLoop(),
        state=state,
        scan_id=scan_id,
        digest_path=str(digest_path),
        feedback_snapshot=snapshot,
        dossier_summaries=_dossiers(),
        dossier_revision="r-current",
        holdout_sampler=_sampler(frozenset(case.platform_id for case in cases if case.held)),
    )
    state.commit()


@pytest.fixture(scope="module")
def scanned(tmp_path_factory: pytest.TempPathFactory) -> Generator[dict[str, Any], None, None]:
    """Both scans, run once. Every test reads a private copy of the result.

    Scanning fifteen posts through the real pipeline is the expensive part
    of this file and it produces the same rows every time, so it runs once
    and each test copies the database it left behind. The traces it wrote
    are read-only evidence and are shared as they are.
    """
    root = tmp_path_factory.mktemp("holdout-lifecycle")
    db_path = root / "scout.db"
    traces_path = root / "traces.db"
    state = StateManager(db_path=str(db_path))

    async def _scan_both(monkeypatch: pytest.MonkeyPatch) -> None:
        tracer = SQLiteTracer(db_path=str(traces_path))
        # The one model boundary, for both scans and every phase.
        monkeypatch.setattr(agent_module, "from_model", lambda model: ScriptedLLM(model))
        _seed_projects(state)
        for classifier in ("llm", "jev"):
            await _run_scan(state, tracer, classifier, root / f"{classifier}.md", monkeypatch)
        await tracer.flush()
        await tracer.close()

    with pytest.MonkeyPatch.context() as patcher:
        asyncio.run(_scan_both(patcher))
    cases = {
        row["name"]: row["id"]
        for row in state.conn.execute(
            "SELECT p.platform_msg_id AS name, e.id AS id FROM evaluations e "
            "JOIN posts p ON p.id = e.post_id"
        ).fetchall()
    }
    state.commit()
    state.close()
    yield {
        "db_path": db_path,
        "traces_path": traces_path,
        "population": {case.name: cases[case.platform_id] for case in CASES},
    }


@pytest.fixture
def state(scanned: dict[str, Any], tmp_path: Path) -> Generator[StateManager, None, None]:
    """A private copy of the scanned database, so tests can mutate freely."""
    db_path = tmp_path / "scout.db"
    shutil.copy(scanned["db_path"], db_path)
    manager = StateManager(db_path=str(db_path))
    yield manager
    manager.commit()
    manager.close()


@pytest.fixture
def population(scanned: dict[str, Any]) -> dict[str, int]:
    """Each case's evaluation id by name, so a test names what it asserts."""
    return dict(scanned["population"])


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


def test_every_scripted_case_reached_an_evaluation(population: dict[str, int]) -> None:
    """The scan really ran: fifteen posts in, fifteen evaluations out."""
    assert sorted(population) == sorted(case.name for case in CASES)


def test_every_sampled_action_is_held_whatever_it_decided(
    state: StateManager, population: dict[str, int]
) -> None:
    """Respond, review and drop are all held — the population is the decisions."""
    held = [case.name for case in CASES if case.held]
    assert [_status_of(state, population[name]) for name in held] == ["held"] * len(held)


def test_an_llm_positive_and_an_llm_drop_are_both_held(
    state: StateManager, population: dict[str, int]
) -> None:
    """The hold is drawn on the decision, not on the decision's sign.

    `llm` never records `review`, so its two actions are the whole of what
    it can hold, and both are here.
    """
    actions = {
        name: state.holdouts.get_decision(population[name]).action
        for name in ("llm_respond_held", "llm_drop_held")
    }
    assert actions == {"llm_respond_held": "respond", "llm_drop_held": "drop"}
    assert {_status_of(state, population[name]) for name in actions} == {"held"}


def test_each_unsampled_decision_reaches_its_own_downstream_outcome(
    state: StateManager, population: dict[str, int]
) -> None:
    assert {
        name: _status_of(state, population[name])
        for name in (
            "llm_respond_surfaced",
            "llm_drop",
            "llm_critic_rejected",
            "llm_low_relevance",
            "llm_abstained",
            "llm_gate_blocked",
            "llm_drafting_failed",
            "jev_respond_surfaced",
            "jev_review_surfaced",
            "jev_drop",
        )
    } == {
        "llm_respond_surfaced": "surfaced",
        "llm_drop": "not_relevant",
        "llm_critic_rejected": "critic_rejected",
        "llm_low_relevance": "low_relevance",
        "llm_abstained": "abstained",
        "llm_gate_blocked": "gate_blocked",
        "llm_drafting_failed": "drafting_failed",
        "jev_respond_surfaced": "surfaced",
        "jev_review_surfaced": "surfaced",
        "jev_drop": "not_relevant",
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


def test_the_jev_decision_records_the_catalogue_that_produced_it(
    state: StateManager, population: dict[str, int]
) -> None:
    """Provenance is the fixture's identity, not an invented one."""
    decision = state.holdouts.get_decision(population["jev_respond_surfaced"])
    assert decision is not None
    assert decision.catalogue_id == "routed-features-fixture"
    assert decision.router_version == "jev-route/round4"


def test_held_is_counted_as_neither_surfaced_nor_drafting_failed(
    state: StateManager, population: dict[str, int]
) -> None:
    counts = _counts(state)
    assert (counts.held, counts.surfaced, counts.drafting_failed) == (6, 3, 1)
    assert counts.total == len(population)


def test_no_held_evaluation_is_actionable_for_posting(
    state: StateManager, population: dict[str, int]
) -> None:
    assert not is_actionable_for_posting("held")
    assert _counts(state).actionable == 3


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


def test_a_hold_costs_exactly_one_relevance_call(
    state: StateManager, population: dict[str, int]
) -> None:
    """Sampling is at the relevance boundary, so no draft is ever paid for."""
    phases = [
        row["phase"]
        for row in state.conn.execute(
            "SELECT phase FROM evaluation_phase_runs WHERE evaluation_id = ? ORDER BY id",
            (population["jev_respond_held"],),
        ).fetchall()
    ]
    assert phases == ["relevance"]


def test_a_surfaced_case_ran_all_three_phases(
    state: StateManager, population: dict[str, int]
) -> None:
    """The counterpart: an unheld positive paid for relevance, draft and critic.

    Together with the hold above this is the evidence that the whole
    pipeline ran rather than a relevance shim — three phases, three
    trace-verified rows.
    """
    phases = [
        row["phase"]
        for row in state.conn.execute(
            "SELECT phase FROM evaluation_phase_runs WHERE evaluation_id = ? ORDER BY id",
            (population["jev_respond_surfaced"],),
        ).fetchall()
    ]
    assert phases == ["relevance", "reply_draft", "critic"]


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


async def test_every_linked_phase_run_resolves_to_a_stored_agent_run_root(
    state: StateManager, scanned: dict[str, Any]
) -> None:
    """The linkage is checked against the trace store, not against itself.

    `finalize_and_persist_phase_run` promises that a row exists only if its
    trace read back as an AGENT_RUN root. This reads every linked row's
    trace_id out of the store the scan actually wrote and re-runs
    production's own verification over it, so a phase run citing a trace
    that is absent, headless or mistyped fails here.
    """
    tracer = SQLiteTracer(db_path=str(scanned["traces_path"]))
    linked = state.conn.execute(
        "SELECT id, phase, trace_id FROM evaluation_phase_runs "
        "WHERE evaluation_id IS NOT NULL ORDER BY id"
    ).fetchall()
    assert len(linked) > 0
    try:
        for row in linked:
            spans = await tracer.get_trace(row["trace_id"])
            verify_agent_run_root(spans, row["trace_id"])
    finally:
        await tracer.close()


async def test_a_phase_run_citing_an_absent_trace_would_be_caught(
    scanned: dict[str, Any],
) -> None:
    """The check above is not vacuous: an unknown trace id fails it."""
    tracer = SQLiteTracer(db_path=str(scanned["traces_path"]))
    try:
        spans = await tracer.get_trace("no-such-trace")
    finally:
        await tracer.close()
    with pytest.raises(Exception, match="did not resolve"):
        verify_agent_run_root(spans, "no-such-trace")


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
    assert audit.held_evaluations == 6
    assert audit.actionable_evaluations == 3
    assert audit.evaluations_by_surface_status["held"] == 6


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        population[case.name] for case in CASES if case.held
    }


def test_the_export_carries_the_source_action_in_its_private_envelope(
    state: StateManager, population: dict[str, int]
) -> None:
    exported = export_holdouts(state.conn)
    assert isinstance(exported, Ok), exported
    by_evaluation = {record.evaluation_id: record for record in exported.value}
    assert by_evaluation[population["jev_review_held"]].private.action == "review"
    assert by_evaluation[population["jev_review_held"]].private.classifier.name == "jev"
    assert by_evaluation[population["llm_drop_held"]].private.classifier.name == "llm"


def test_the_blind_projection_carries_no_classifier_metadata(
    state: StateManager, population: dict[str, int]
) -> None:
    """What a blind reviewer sees: the post, and nothing production decided.

    This is the blind projection — the one an external reviewer is actually
    handed by `scout holdout export --blind`. The allowlist is asserted as
    an exact key set per line, so a field added to the export record later
    is private here until someone adds it to BLIND_CASE_FIELDS on purpose.
    """
    exported = export_holdouts(state.conn)
    assert isinstance(exported, Ok), exported
    lines = render_blind_jsonl(exported.value).splitlines()
    assert len(lines) == len([case for case in CASES if case.held])
    for line in lines:
        assert set(json.loads(line)) == set(BLIND_CASE_FIELDS)
    projected = blind_case(exported.value[0])
    assert not hasattr(projected, "private")


def test_the_blind_projection_drops_what_the_full_export_keeps(
    state: StateManager, population: dict[str, int]
) -> None:
    """The two renderings, side by side, over the same records.

    The full export is what stays inside the deployment; the blind
    rendering is what leaves it. Every classifier-identifying key present
    in the first is absent from the second.
    """
    exported = export_holdouts(state.conn)
    assert isinstance(exported, Ok), exported
    private_keys = {
        "private", "production_decision", "production_score", "evaluation_id",
        "project", "url", "author_name", "author_handle", "human_label", "held_at",
    }
    full = [json.loads(line) for line in render_holdout_jsonl(exported.value).splitlines()]
    blind = [json.loads(line) for line in render_blind_jsonl(exported.value).splitlines()]
    assert private_keys <= set(full[0])
    assert all(private_keys.isdisjoint(record) for record in blind)


def test_the_assay_decision_boundary_puts_review_beside_respond() -> None:
    assert [surfaced_or_dropped(action) for action in ("respond", "review", "drop")] == [
        True,
        True,
        False,
    ]


# ---------------------------------------------------------------------------
# The release half
# ---------------------------------------------------------------------------


@pytest.fixture
async def release_env(
    state: StateManager, population: dict[str, int], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[dict[str, Any], None]:
    """Release through the production path, over the same two boundaries.

    `release_pending_holdouts` builds its own phase runtime and runs the
    real `draft_and_critic_step`, so the reply and critic phases here write
    the same durable, trace-verified evidence the scan half did. Only the
    model and the dossier checkout — both external IO — are replaced. The
    content verifier runs for real, against the grounding `_dossiers`
    carries.
    """
    registry = _registry()
    monkeypatch.setattr(state, "load_runtime_registry", lambda: registry)
    monkeypatch.setattr(
        release_module, "load_project_dossiers", lambda projects: (_dossiers(), [])
    )
    monkeypatch.setattr(release_module._config, "SCOUT_DOSSIER_ROOT", "")
    monkeypatch.setattr(agent_module, "from_model", lambda model: ScriptedLLM(model))
    tracer = SQLiteTracer(db_path=str(tmp_path / "release-traces.db"))
    yield {"state": state, "population": population, "tracer": tracer}
    # aiosqlite holds a worker thread per connection; closing it here keeps
    # that thread from outliving the test's event loop.
    await tracer.close()


def _held_names() -> tuple[str, ...]:
    return tuple(case.name for case in CASES if case.held)


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
        tracer=env["tracer"],
        feedback=NullFeedbackLoop(),
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
    assert report.released == len(_held_names())
    assert {
        name: (by_evaluation[population[name]].authority, by_evaluation[population[name]].action)
        for name in _held_names()
    } == {
        "llm_respond_held": ("recorded_action", "respond"),
        "llm_drop_held": ("recorded_action", "drop"),
        "jev_respond_held": ("recorded_action", "respond"),
        "jev_review_held": ("recorded_action", "review"),
        "jev_drop_held": ("recorded_action", "drop"),
        "jev_respond_held_rejected": ("recorded_action", "respond"),
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
            # An llm drop the reviewer read as substantive: also a respond.
            population["llm_drop_held"]: AssayLabelCase(
                case_id=4, exclusion="none", needs_thread=False, substance="in_post", note=None
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
        for name in ("jev_respond_held", "jev_drop_held", "jev_review_held", "llm_drop_held")
    } == {
        "jev_respond_held": ("label", "none", "drop"),
        "jev_drop_held": ("label", "in_post", "respond"),
        "jev_review_held": ("label", "pointer", "review"),
        "llm_drop_held": ("label", "in_post", "respond"),
    }


async def test_an_unlabelled_hold_in_a_graded_run_keeps_its_recorded_action(
    release_env: dict[str, Any],
) -> None:
    """Authority is resolved per hold, not per run."""
    population = release_env["population"]
    labels = _labels_for(
        {
            population["jev_respond_held"]: AssayLabelCase(
                case_id=1, exclusion="none", needs_thread=False, substance="none", note=None
            )
        }
    )

    report = await _release(release_env, labels)

    by_evaluation = {outcome.evaluation_id: outcome for outcome in report.outcomes}
    assert by_evaluation[population["jev_respond_held"]].authority == "label"
    assert by_evaluation[population["llm_respond_held"]].authority == "recorded_action"


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
    is the case where they disagree. The verdict comes from the same script
    the scan read, so the rejection is the real critic phase reaching a real
    terminal decision, not an injected status.
    """
    state, population = release_env["state"], release_env["population"]

    report = await _release(release_env)

    outcome = next(
        outcome
        for outcome in report.outcomes
        if outcome.evaluation_id == population["jev_respond_held_rejected"]
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


async def test_a_released_targets_phase_runs_resolve_to_stored_trace_roots(
    release_env: dict[str, Any],
) -> None:
    """The release writes the same verified evidence a scan does."""
    state, population = release_env["state"], release_env["population"]

    report = await _release(release_env)

    target = next(
        outcome.target_evaluation_id
        for outcome in report.outcomes
        if outcome.evaluation_id == population["jev_review_held"]
    )
    rows = state.conn.execute(
        "SELECT trace_id FROM evaluation_phase_runs WHERE evaluation_id = ?", (target,)
    ).fetchall()
    assert len(rows) == 2
    for row in rows:
        spans = await release_env["tracer"].get_trace(row["trace_id"])
        verify_agent_run_root(spans, row["trace_id"])


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
    assert audit.holdouts_by_status == {"released": 6}
    assert audit.released_by_authority == {"recorded_action": 6}
    assert audit.held_evaluations == 6


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

    assert report.released == len(_held_names())
    outcome = next(
        outcome
        for outcome in report.outcomes
        if outcome.evaluation_id == population["jev_review_held"]
    )
    assert (outcome.authority, outcome.action) == ("recorded_action", "review")
    assert state.holdouts.get_decision(population["jev_review_held"]) is not None
