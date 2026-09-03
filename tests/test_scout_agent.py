"""Tests for Scout's active phase pipeline and prompt formatting."""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock

import pytest
from jig import (
    CompletionParams,
    LLMClient,
    LLMResponse,
    SQLiteFeedbackLoop,
    SQLiteTracer,
    ToolCall,
    Usage,
)

import scout.scanning.pipeline as pipeline
from scout.config import MODES, Message, SourceAuthor, SourceParent
from scout.dossiers.resolver import DossierFact, DossierProhibition, DossierResource, DossierSummary
from scout.errors import ParseError
from scout.grading.feedback import LegacySection, PhaseFeedbackBundle, SnapshotBody
from scout.registry import KeywordRoute, ProjectTarget
from scout.result import Err, Ok
from scout.scanning.agent import (
    PhaseRunIdentity,
    ScoutExecutionContext,
    ScoutPhaseConfigs,
    build_critic_system_prompt,
    build_relevance_system_prompt,
    build_reply_draft_system_prompt,
    build_scout_phase_configs,
    format_dossier_for_drafting,
    format_message_input,
    format_projects,
    format_routed_message_input,
    resolve_mode_for_message,
)
from scout.scanning.pipeline import draft_and_critic_step, score_and_draft_step
from scout.scanning.prefilter import RoutedMessage
from scout.scanning.schemas import CritiquePhaseOutput, RelevancePhaseOutput, StructuredDraftOutput
from scout.storage.state import StateManager


@pytest.fixture
def tracer(tmp_path) -> SQLiteTracer:
    return SQLiteTracer(db_path=str(tmp_path / "traces.db"))


@pytest.fixture
def feedback(tmp_path) -> SQLiteFeedbackLoop:
    return SQLiteFeedbackLoop(db_path=str(tmp_path / "feedback.db"))


def _make_execution_context(state: StateManager) -> ScoutExecutionContext:
    """Seed a scan, post, and committed feedback snapshot in `state`, then
    build the ScoutExecutionContext score_and_draft_step requires — the
    real per-post identity threaded into every _run_phase call so each
    phase's durable evaluation_phase_runs row can be inserted."""
    scan_id = state.start_scan()
    post_id = state.save_post(_make_message(), scan_id)
    snapshot = state.record_feedback_snapshot(scan_id, mode="shadow")
    phase_by_name = {p.phase: p.snapshot_phase_id for p in snapshot.phases}
    identity = {
        phase: PhaseRunIdentity(snapshot_phase_id=phase_by_name[phase], model="test-model")
        for phase in ("relevance", "reply_draft", "critic")
    }
    return ScoutExecutionContext(
        state=state,
        scan_id=scan_id,
        post_id=post_id,
        relevance=identity["relevance"],
        reply_draft=identity["reply_draft"],
        critic=identity["critic"],
    )


@pytest.fixture
def execution_context() -> ScoutExecutionContext:
    return _make_execution_context(StateManager(db_path=":memory:"))


def test_format_projects_no_projects() -> None:
    assert format_projects({}) == "(no projects loaded)"


def test_format_projects_renders_fields() -> None:
    p = ProjectTarget(key="gw", name="Gateway", description="Desc", link="https://gw.io")
    result = format_projects({"gw": p})
    assert "Gateway" in result
    assert "Desc" in result
    assert "https://gw.io" in result


def test_format_projects_without_link_forbids_invention() -> None:
    p = ProjectTarget(key="agent-ops", name="Agent Ops", description="Desc", link="")

    result = format_projects({"agent-ops": p})

    assert "Link: (none provided; do not invent a URL)" in result


def _make_dossier() -> DossierSummary:
    return DossierSummary(
        project_key="gateway",
        last_reviewed=date(2026, 7, 17),
        reviewer="tester",
        facts=[
            DossierFact(
                id="fact-1",
                text="Gateway can replace CAPTCHA with payment verification.",
                safe_phrasings=[
                    "Gateway replaces CAPTCHA with payment verification.",
                    "Gateway can replace CAPTCHA with payment verification.",
                ],
                immutable_evidence=["ev-1"],
            )
        ],
        resources=[
            DossierResource(
                id="resource-1",
                label="Gateway docs",
                canonical_url="https://example.com/gateway",
                immutable_evidence=["ev-2"],
            )
        ],
        prohibitions=[
            DossierProhibition(
                id="prohibition-1",
                mode="exact_phrase",
                pattern="guaranteed results",
                immutable_evidence=["ev-3"],
            )
        ],
    )


def test_format_dossier_for_drafting_exposes_verifier_contract() -> None:
    formatted = format_dossier_for_drafting(_make_dossier())

    assert '"fact_id": "fact-1"' in formatted
    assert '"resource_id": "resource-1"' in formatted
    assert "Gateway can replace CAPTCHA with payment verification." in formatted
    assert '"pattern": "guaranteed results"' in formatted
    assert "Do not invent IDs or paraphrase declarative claims" in formatted


_FEEDBACK_BUILDERS = {
    "relevance": build_relevance_system_prompt,
    "reply_draft": build_reply_draft_system_prompt,
    "critic": build_critic_system_prompt,
}


def _build(phase: str, feedback) -> str:
    """Call the given phase's system-prompt builder with a feedback entry,
    using minimal shared args (empty projects/templates get_prompt_db
    lookups resolve to the mode_cfg's bare prompt names, which is fine —
    only the feedback section's formatting is under test)."""
    builder = _FEEDBACK_BUILDERS[phase]
    return builder(MODES["lead_gen"], projects={}, templates={}, feedback=feedback)


@pytest.mark.parametrize("phase", ["relevance", "reply_draft", "critic"])
def test_prompt_omits_feedback_heading_when_legacy_section_empty(phase: str) -> None:
    """Disabled mode (LegacySection), no legacy signal text: byte-for-byte
    prior behavior — no heading, no body."""
    prompt = _build(phase, LegacySection(text=""))
    assert "Recent Human Grading Feedback" not in prompt


@pytest.mark.parametrize("phase", ["relevance", "reply_draft", "critic"])
def test_prompt_inserts_legacy_section_verbatim_when_disabled(phase: str) -> None:
    """Disabled mode (LegacySection) with signal text: heading, blank line,
    then the legacy text verbatim — same shape the legacy grading_signals
    path always used."""
    prompt = _build(phase, LegacySection(text="3 of 5 passed. 1 false positives."))
    assert "\n\n## Recent Human Grading Feedback\n\n3 of 5 passed. 1 false positives." in prompt


@pytest.mark.parametrize("phase", ["relevance", "reply_draft", "critic"])
def test_prompt_omits_feedback_heading_when_active_phase_body_empty(phase: str) -> None:
    """Active mode (SnapshotBody), empty stored rendered_text: the
    no-eligible-feedback case omits both heading and body, matching the
    established no-feedback behavior."""
    prompt = _build(phase, SnapshotBody(text=""))
    assert "Recent Human Grading Feedback" not in prompt


@pytest.mark.parametrize("phase", ["relevance", "reply_draft", "critic"])
def test_prompt_inserts_snapshot_body_verbatim_when_active(phase: str) -> None:
    """Active mode (SnapshotBody): the stored rendered_text is inserted
    verbatim under the literal heading — no strip, normalization, or
    trailing-newline changes, including embedded whitespace/Unicode."""
    body = (
        "## Relevance Feedback (evaluation-feedback/v1)\n"
        "5 graded: café  \n"
        "- trailing space above, emoji: 🎯"
    )
    prompt = _build(phase, SnapshotBody(text=body))
    assert f"\n\n## Recent Human Grading Feedback\n\n{body}" in prompt


def test_build_scout_phase_configs_selects_bundle_field_per_phase(tracer, feedback) -> None:
    """Each phase's system prompt must carry only its own bundle entry —
    a relevance correction must never leak into the draft or critic
    prompt, or vice versa."""
    bundle = PhaseFeedbackBundle(
        relevance=SnapshotBody(text="RELEVANCE_ONLY_MARKER"),
        reply_draft=SnapshotBody(text="REPLY_DRAFT_ONLY_MARKER"),
        critic=SnapshotBody(text="CRITIC_ONLY_MARKER"),
    )
    configs = build_scout_phase_configs(
        relevance_model="claude-haiku-4-5-20251001",
        reply_draft_model="claude-haiku-4-5-20251001",
        critic_model="claude-haiku-4-5-20251001",
        mode_cfg=MODES["lead_gen"],
        projects={},
        templates={},
        tracer=tracer,
        feedback=feedback,
        feedback_bundle=bundle,
    )

    assert "RELEVANCE_ONLY_MARKER" in configs.relevance.system_prompt
    assert "REPLY_DRAFT_ONLY_MARKER" not in configs.relevance.system_prompt
    assert "CRITIC_ONLY_MARKER" not in configs.relevance.system_prompt

    assert "REPLY_DRAFT_ONLY_MARKER" in configs.reply_draft.system_prompt
    assert "RELEVANCE_ONLY_MARKER" not in configs.reply_draft.system_prompt
    assert "CRITIC_ONLY_MARKER" not in configs.reply_draft.system_prompt

    assert "CRITIC_ONLY_MARKER" in configs.critic.system_prompt
    assert "RELEVANCE_ONLY_MARKER" not in configs.critic.system_prompt
    assert "REPLY_DRAFT_ONLY_MARKER" not in configs.critic.system_prompt


def test_resolve_mode_for_message_returns_base_when_no_route() -> None:
    base = MODES["lead_gen"]
    result = resolve_mode_for_message(base, None)
    assert result == base


def test_resolve_mode_for_message_overrides_from_route() -> None:
    base = MODES["lead_gen"]
    route = KeywordRoute(
        id=1, project_key="gw", keyword="foo",
        evaluate_prompt="custom_eval", respond_prompt=None, critique_prompt="custom_critique",
        priority=0,
    )
    result = resolve_mode_for_message(base, route)
    assert result["evaluate"] == "custom_eval"
    assert result["respond"] == base["respond"]
    assert result["critique"] == "custom_critique"


def test_format_message_input_includes_platform_and_content() -> None:
    msg = _make_message("looking for CAPTCHA alternatives")

    formatted = format_message_input(msg)

    assert "discord" in formatted
    assert "alice" in formatted
    assert "general" in formatted
    assert "looking for CAPTCHA alternatives" in formatted


def test_format_routed_message_input_includes_route_context() -> None:
    msg = _make_message("looking for CAPTCHA alternatives")
    route = KeywordRoute(
        id=1,
        project_key="gateway",
        keyword="captcha",
        evaluate_prompt=None,
        respond_prompt=None,
        critique_prompt=None,
        priority=0,
        match_type="substring",
        intent="reduce signup friction",
        positive_context=("signup conversion", "abuse prevention"),
        negative_context=("spam",),
    )
    routed = RoutedMessage(message=msg, keyword_route=route)

    formatted = format_routed_message_input(routed)

    assert "Matched route" in formatted
    assert "**Project key:** gateway" in formatted
    assert "**Keyword:** captcha" in formatted
    assert "**Intent:** reduce signup friction" in formatted
    assert "signup conversion" in formatted
    assert "abuse prevention" in formatted
    assert "spam" in formatted


def test_format_message_input_non_reply_has_source_post_header() -> None:
    msg = _make_message("standalone post")
    formatted = format_message_input(msg)
    assert "## Source post" in formatted
    assert "## Immediate parent" not in formatted
    assert "Parent context unavailable" not in formatted


def test_format_message_input_with_resolved_parent() -> None:
    parent = SourceParent(
        id="at://did:plc:parent/post/p001",
        author=SourceAuthor(id="did:plc:parent", name="ParentUser"),
        text="The original question about auth",
        url="https://bsky.app/profile/parent/post/p001",
    )
    msg = Message(
        platform="bluesky",
        platform_id="reply-001",
        channel_name="bluesky",
        channel_id="bsky",
        author_name="alice",
        author_id="a1",
        content="Great point about auth!",
        created_at=datetime(2026, 4, 18, tzinfo=UTC),
        parent=parent,
        parent_lookup_status="resolved",
    )
    formatted = format_message_input(msg)
    assert "## Source post" in formatted
    assert "## Immediate parent" in formatted
    assert "ParentUser" in formatted
    assert "The original question about auth" in formatted
    assert "https://bsky.app/profile/parent/post/p001" in formatted


def test_format_message_input_with_failed_parent() -> None:
    msg = Message(
        platform="bluesky",
        platform_id="reply-002",
        channel_name="bluesky",
        channel_id="bsky",
        author_name="alice",
        author_id="a1",
        content="Agreed!",
        created_at=datetime(2026, 4, 18, tzinfo=UTC),
        parent=None,
        parent_lookup_status="failed",
    )
    formatted = format_message_input(msg)
    assert "## Source post" in formatted
    assert "Parent context unavailable" in formatted
    assert "## Immediate parent" not in formatted


def test_format_message_input_source_post_before_parent() -> None:
    parent = SourceParent(
        id="at://did:plc:parent/post/p002",
        author=SourceAuthor(id="did:plc:parent", name="OtherUser"),
        text="First post",
        url="",
    )
    msg = Message(
        platform="bluesky",
        platform_id="reply-003",
        channel_name="bluesky",
        channel_id="bsky",
        author_name="alice",
        author_id="a1",
        content="My reply",
        created_at=datetime(2026, 4, 18, tzinfo=UTC),
        parent=parent,
        parent_lookup_status="resolved",
    )
    formatted = format_message_input(msg)
    source_pos = formatted.index("## Source post")
    parent_pos = formatted.index("## Immediate parent")
    assert source_pos < parent_pos


# --- Fake LLMClient plumbing for end-to-end agent tests ---


def _make_message(content: str = "hello world") -> Message:
    return Message(
        platform="discord",
        platform_id="msg-1",
        channel_name="general",
        channel_id="123",
        author_name="alice",
        author_id="a1",
        content=content,
        created_at=datetime(2026, 4, 18, tzinfo=UTC),
    )


class ScriptedLLMClient(LLMClient):
    """Replay scripted LLMResponses in order. Extra `.complete` calls raise.

    Tests construct a list of responses matching expected turns: e.g.
    [submit_output-only] for a one-turn agent, or [revise_draft, submit_output]
    for a two-turn revision flow.
    """

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self._calls: list[CompletionParams] = []

    @property
    def calls(self) -> list[CompletionParams]:
        return self._calls

    async def complete(self, params: CompletionParams) -> LLMResponse:
        self._calls.append(params)
        if not self._responses:
            raise RuntimeError("ScriptedLLMClient exhausted — test expected fewer turns")
        return self._responses.pop(0)


def _submit_output_call(args: dict) -> ToolCall:
    return ToolCall(id="call-submit", name="submit_output", arguments=args)


def _response(tool_calls: list[ToolCall]) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=tool_calls,
        usage=Usage(input_tokens=100, output_tokens=50, cost=0.0),
        latency_ms=10.0,
        model="scripted",
    )


def _phase_configs(
    tracer,
    feedback,
    *,
    relevance_llm: LLMClient,
    draft_llm: LLMClient | None = None,
    critic_llm: LLMClient | None = None,
) -> ScoutPhaseConfigs:
    configs = build_scout_phase_configs(
        relevance_model="claude-haiku-4-5-20251001",
        reply_draft_model="claude-haiku-4-5-20251001",
        critic_model="claude-haiku-4-5-20251001",
        mode_cfg=MODES["lead_gen"],
        projects={},
        templates={},
        tracer=tracer,
        feedback=feedback,
    )
    return ScoutPhaseConfigs(
        relevance=configs.relevance.with_(llm=relevance_llm),
        reply_draft=configs.reply_draft.with_(llm=draft_llm or ScriptedLLMClient([])),
        critic=configs.critic.with_(llm=critic_llm or ScriptedLLMClient([])),
    )


def test_drafter_and_critic_prompts_include_hard_platform_limits(
    tracer, feedback
) -> None:
    configs = _phase_configs(
        tracer,
        feedback,
        relevance_llm=ScriptedLLMClient([]),
    )

    for prompt in (configs.reply_draft.system_prompt, configs.critic.system_prompt):
        assert "Bluesky <=300 Unicode code points" in prompt
        assert "Farcaster <=320 UTF-8 bytes" in prompt
        assert "verifier rejects rather than truncates" in prompt


async def test_pipeline_step_wraps_agent_result(
    tracer, feedback, execution_context
) -> None:
    """score_and_draft_step runs relevance and returns Ok(ReplyCandidate)."""
    candidate_args = {
        "relevant": False,
        "score": 0.2,
        "reason": "topic drift",
        "relevant_to": [],
    }
    llm = ScriptedLLMClient([_response([_submit_output_call(candidate_args)])])

    phase_configs = _phase_configs(tracer, feedback, relevance_llm=llm)

    msg = _make_message()
    ctx = {
        "input": msg,
        "phase_configs": phase_configs,
        "execution_context": execution_context,
    }

    step_result = await score_and_draft_step(ctx)

    match step_result:
        case Ok(candidate):
            assert candidate.relevant is False
            assert candidate.reason == "topic drift"
        case Err(err):
            pytest.fail(f"Expected Ok, got Err: {err}")


async def test_pipeline_step_formats_routed_input(
    tracer, feedback, execution_context
) -> None:
    """Routed input should include matched route metadata in the user turn."""
    candidate_args = {
        "relevant": False,
        "score": 0.2,
        "reason": "topic drift",
        "relevant_to": [],
    }
    llm = ScriptedLLMClient([_response([_submit_output_call(candidate_args)])])

    phase_configs = _phase_configs(tracer, feedback, relevance_llm=llm)

    routed = RoutedMessage(
        message=_make_message(),
        keyword_route=KeywordRoute(
            id=7,
            project_key="gateway",
            keyword="captcha",
            evaluate_prompt=None,
            respond_prompt=None,
            critique_prompt=None,
            priority=0,
            match_type="substring",
            intent="reduce signup friction",
            positive_context=("signup conversion",),
            negative_context=("spam", "abuse"),
        ),
    )
    ctx = {
        "input": routed,
        "phase_configs": phase_configs,
        "execution_context": execution_context,
    }

    step_result = await score_and_draft_step(ctx)

    match step_result:
        case Ok(candidate):
            assert candidate.relevant is False
        case Err(err):
            pytest.fail(f"Expected Ok, got Err: {err}")

    user_turn = next(m for m in llm.calls[0].messages if m.role.value == "user")
    assert "Matched route" in user_turn.content
    assert "**Project key:** gateway" in user_turn.content
    assert "**Keyword:** captcha" in user_turn.content
    assert "**Intent:** reduce signup friction" in user_turn.content
    assert "signup conversion" in user_turn.content
    assert "spam" in user_turn.content


async def test_pipeline_step_uses_nested_critic_revision(
    tracer, feedback, execution_context
) -> None:
    relevance_llm = ScriptedLLMClient(
        [
            _response(
                [
                    _submit_output_call(
                        {
                            "relevant": True,
                            "score": 0.9,
                            "reason": "direct fit",
                            "relevant_to": ["gateway"],
                        }
                    )
                ]
            )
        ]
    )
    draft_llm = ScriptedLLMClient(
        [
            _response(
                [
                    _submit_output_call(
                        {
                            "posture": "answer",
                            "segments": [
                                {
                                    "type": "declarative",
                                    "fact_id": "fact-1",
                                    "text": "Gateway replaces CAPTCHA with payment verification.",
                                }
                            ],
                            "claims": ["Gateway replaces CAPTCHA with payment verification."],
                            "resources_used": [],
                        }
                    )
                ]
            )
        ]
    )
    critic_llm = ScriptedLLMClient(
        [
            _response(
                [
                    _submit_output_call(
                        {
                            "verdict": "revise",
                            "feedback": "Softened the product framing.",
                            "revised_draft": {
                                "posture": "answer",
                                "segments": [
                                    {
                                        "type": "declarative",
                                        "fact_id": "fact-1",
                                        "text": (
                                            "Gateway can replace CAPTCHA with payment verification."
                                        ),
                                    }
                                ],
                                "claims": [
                                    "Gateway can replace CAPTCHA with payment verification."
                                ],
                                "resources_used": [],
                            },
                        }
                    )
                ]
            )
        ]
    )
    phase_configs = _phase_configs(
        tracer,
        feedback,
        relevance_llm=relevance_llm,
        draft_llm=draft_llm,
        critic_llm=critic_llm,
    )

    routed = RoutedMessage(
        message=_make_message(),
        keyword_route=KeywordRoute(
            id=1,
            project_key="gateway",
            keyword="captcha",
            evaluate_prompt=None,
            respond_prompt=None,
            critique_prompt=None,
            priority=0,
            match_type="substring",
            intent=None,
            positive_context=(),
            negative_context=(),
        ),
    )
    step_result = await score_and_draft_step(
        {
            "input": routed,
            "phase_configs": phase_configs,
            "dossier_summaries": {"gateway": _make_dossier()},
            "execution_context": execution_context,
        }
    )

    match step_result:
        case Ok(candidate):
            assert candidate.relevant is True
            assert candidate.project_key == "gateway"
            assert candidate.critique_verdict == "revise"
            assert candidate.structured_draft is not None
            assert candidate.structured_draft.posture == "answer"
            assert candidate.structured_draft.model_dump()["segments"][0]["text"] == (
                "Gateway can replace CAPTCHA with payment verification."
            )
        case Err(err):
            pytest.fail(f"Expected Ok, got Err: {err}")
    assert len(relevance_llm.calls) == 1
    assert len(draft_llm.calls) == 1
    assert len(critic_llm.calls) == 1
    draft_user_turn = next(
        message for message in draft_llm.calls[0].messages if message.role.value == "user"
    )
    critic_user_turn = next(
        message for message in critic_llm.calls[0].messages if message.role.value == "user"
    )
    assert '"fact_id": "fact-1"' in draft_user_turn.content
    assert '"fact_id": "fact-1"' in critic_user_turn.content


async def test_pipeline_step_rejects_revision_without_nested_draft(
    tracer, feedback, execution_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    malformed_critique = CritiquePhaseOutput.model_construct(
        verdict="revise",
        feedback="The draft needs revision.",
        revised_draft=None,
    )
    run_phase = AsyncMock(
        side_effect=[
            Ok(
                pipeline.PhaseExecution(
                    parsed=RelevancePhaseOutput(
                        relevant=True,
                        score=0.9,
                        reason="direct fit",
                        relevant_to=["gateway"],
                    ),
                    trace_id="trace-relevance",
                    phase_run_id=1,
                    phase="relevance",
                    model="test-model",
                )
            ),
            Ok(
                pipeline.PhaseExecution(
                    parsed=StructuredDraftOutput(posture="answer"),
                    trace_id="trace-reply_draft",
                    phase_run_id=2,
                    phase="reply_draft",
                    model="test-model",
                )
            ),
            Ok(
                pipeline.PhaseExecution(
                    parsed=malformed_critique,
                    trace_id="trace-critic",
                    phase_run_id=3,
                    phase="critic",
                    model="test-model",
                )
            ),
        ]
    )
    monkeypatch.setattr(pipeline, "_run_phase", run_phase)

    result = await score_and_draft_step(
        {
            "input": _make_message(),
            "phase_configs": _phase_configs(
                tracer,
                feedback,
                relevance_llm=ScriptedLLMClient([]),
            ),
            "execution_context": execution_context,
        }
    )

    assert isinstance(result, Err)
    assert isinstance(result.error, ParseError)
    assert result.error.detail == (
        "critic phase returned verdict='revise' without revised_draft"
    )


async def test_pipeline_step_passes_through_empty_segment_draft_for_verifier_to_block(
    tracer, feedback, execution_context
) -> None:
    """The pipeline no longer assembles or blank-checks text itself.

    It lacks the dossier needed to expand resources or enforce platform
    length, so an empty-segment non-abstain draft still becomes Ok(candidate)
    carrying the raw StructuredDraftOutput; the dossier-aware verifier is
    responsible for rejecting it via platform_limits downstream.
    """
    relevance_llm = ScriptedLLMClient(
        [
            _response(
                [
                    _submit_output_call(
                        {
                            "relevant": True,
                            "score": 0.9,
                            "reason": "direct fit",
                            "relevant_to": ["gateway"],
                        }
                    )
                ]
            )
        ]
    )
    draft_llm = ScriptedLLMClient(
        [
            _response(
                [
                    _submit_output_call(
                        {
                            "posture": "answer",
                            "segments": [],
                            "claims": [],
                            "resources_used": [],
                        }
                    )
                ]
            )
        ]
    )
    critic_llm = ScriptedLLMClient(
        [
            _response(
                [
                    _submit_output_call(
                        {"verdict": "approve", "feedback": "Looks fine."}
                    )
                ]
            )
        ]
    )
    phase_configs = _phase_configs(
        tracer,
        feedback,
        relevance_llm=relevance_llm,
        draft_llm=draft_llm,
        critic_llm=critic_llm,
    )
    routed = RoutedMessage(
        message=_make_message(),
        keyword_route=KeywordRoute(
            id=1,
            project_key="gateway",
            keyword="captcha",
            evaluate_prompt=None,
            respond_prompt=None,
            critique_prompt=None,
            priority=0,
            match_type="substring",
            intent=None,
            positive_context=(),
            negative_context=(),
        ),
    )

    result = await score_and_draft_step(
        {
            "input": routed,
            "phase_configs": phase_configs,
            "execution_context": execution_context,
        }
    )

    match result:
        case Ok(candidate):
            assert candidate.relevant is True
            assert candidate.project_key == "gateway"
            assert candidate.structured_draft is not None
            assert candidate.structured_draft.posture == "answer"
            assert candidate.structured_draft.segments == []
        case Err(error):
            pytest.fail(f"Expected Ok, got Err: {error}")


async def test_human_positive_step_skips_relevance_and_runs_response_phases(
    tracer, feedback, execution_context
) -> None:
    relevance_llm = ScriptedLLMClient([])
    draft_llm = ScriptedLLMClient(
        [
            _response(
                [
                    _submit_output_call(
                        {
                            "posture": "answer",
                            "segments": [],
                            "claims": [],
                            "resources_used": [],
                        }
                    )
                ]
            )
        ]
    )
    critic_llm = ScriptedLLMClient(
        [
            _response(
                [
                    _submit_output_call(
                        {"verdict": "approve", "feedback": "Looks fine."}
                    )
                ]
            )
        ]
    )
    configs = _phase_configs(
        tracer,
        feedback,
        relevance_llm=relevance_llm,
        draft_llm=draft_llm,
        critic_llm=critic_llm,
    )
    route = KeywordRoute(
        id=1,
        project_key="gateway",
        keyword="captcha",
        evaluate_prompt=None,
        respond_prompt=None,
        critique_prompt=None,
        priority=0,
    )

    result = await draft_and_critic_step(
        {
            "input": RoutedMessage(message=_make_message(), keyword_route=route),
            "phase_configs": configs,
            "execution_context": execution_context,
            "relevance_output": RelevancePhaseOutput(
                relevant=True,
                score=1.0,
                reason="human override",
                relevant_to=["gateway"],
            ),
        }
    )

    assert isinstance(result, Ok)
    assert relevance_llm.calls == []
    assert len(draft_llm.calls) == 1
    assert len(critic_llm.calls) == 1
    assert len(result.value.contributor_phase_run_ids) == 2
    phases = [
        execution_context.state.get_phase_run(phase_run_id)["phase"]
        for phase_run_id in result.value.contributor_phase_run_ids
    ]
    assert phases == ["reply_draft", "critic"]


async def test_pipeline_step_returns_err_on_agent_exception(
    tracer, feedback, execution_context
) -> None:
    """Exceptions raised by run_agent are caught and returned as Err(LLMError)."""

    class ExplodingLLM(LLMClient):
        async def complete(self, params: CompletionParams) -> LLMResponse:
            raise RuntimeError("boom")

    configs = _phase_configs(tracer, feedback, relevance_llm=ExplodingLLM())
    phase_configs = ScoutPhaseConfigs(
        relevance=configs.relevance.with_(max_llm_retries=1),
        reply_draft=configs.reply_draft,
        critic=configs.critic,
    )

    msg = _make_message()
    ctx = {
        "input": msg,
        "phase_configs": phase_configs,
        "execution_context": execution_context,
    }

    step_result = await score_and_draft_step(ctx)

    match step_result:
        case Err(err):
            assert "boom" in err.detail or "boom" in str(err)
        case _:
            pytest.fail(f"Expected Err, got {step_result}")
