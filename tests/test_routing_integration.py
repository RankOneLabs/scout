"""Integration test for per-keyword prompt routing in a single scan.

Spec (Testing → Integration test): given two keywords matching different
prompt bundles, two messages in one scan must resolve to different prompt
names. This drives the real scan_runner.score_messages loop end to end
(prefilter → route → resolve → agent-config cache → agent → persistence)
with a scripted LLM so no network is touched.
"""

from __future__ import annotations

from datetime import UTC, datetime

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

import scout.scanning.runner as scan_runner
from scout.config import MODES, Message
from scout.grading.feedback import legacy_feedback_bundle
from scout.registry import KeywordRoute
from scout.scanning.prefilter import keyword_prefilter
from scout.storage.state import StateManager

_EMPTY_FEEDBACK_BUNDLE = legacy_feedback_bundle("")


class _ScriptedLLMClient(LLMClient):
    """Replay one scripted LLMResponse per `.complete` call."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[CompletionParams] = []

    async def complete(self, params: CompletionParams) -> LLMResponse:
        self.calls.append(params)
        return self._responses.pop(0)


def _not_relevant_response() -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id="call-submit",
                name="submit_output",
                arguments={
                    "relevant": False,
                    "score": 0.1,
                    "reason": "off-topic",
                    "relevant_to": [],
                },
            )
        ],
        usage=Usage(input_tokens=10, output_tokens=5, cost=0.0),
        latency_ms=1.0,
        model="scripted",
    )


def _msg(platform_id: str, content: str) -> Message:
    return Message(
        platform="discord",
        platform_id=platform_id,
        channel_name="general",
        channel_id="123",
        author_name="alice",
        author_id="a1",
        content=content,
        created_at=datetime(2026, 4, 18, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_two_keywords_route_two_messages_to_different_prompts(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two keywords, each selecting a different evaluate prompt bundle.
    routes = (
        KeywordRoute(
            id=1, project_key="gw", keyword="alpha",
            evaluate_prompt="eval_alpha", respond_prompt=None,
            critique_prompt=None, priority=0,
            match_type="substring",
            intent="alpha intent",
            positive_context=("alpha upside",),
            negative_context=("alpha downside",),
        ),
        KeywordRoute(
            id=2, project_key="gw", keyword="beta",
            evaluate_prompt="eval_beta", respond_prompt=None,
            critique_prompt=None, priority=0,
            match_type="substring",
            intent="beta intent",
            positive_context=("beta upside",),
            negative_context=("beta downside",),
        ),
    )
    templates = {"eval_alpha": "ALPHA EVAL RULES", "eval_beta": "BETA EVAL RULES"}
    messages = [
        _msg("m-alpha", "looking into alpha tooling"),
        _msg("m-beta", "thoughts on beta tooling"),
    ]

    routed = keyword_prefilter(messages, routes)
    assert all(rm.keyword_route is not None for rm in routed)
    assert {rm.keyword_route.keyword for rm in routed if rm.keyword_route is not None} == {
        "alpha",
        "beta",
    }

    # Record the relevance prompt of each distinct bundle the scan builds, and
    # swap in a scripted relevance LLM so the phase pipeline runs without a real model.
    built: dict[str, str] = {}  # evaluate prompt name -> composed system prompt
    llms: dict[str, _ScriptedLLMClient] = {}
    real_build = scan_runner.build_scout_phase_configs  # type: ignore[attr-defined]

    def spy_build(
        *,
        relevance_model,
        reply_draft_model,
        critic_model,
        mode_cfg,
        projects,
        templates,
        tracer,
        feedback,
        lessons=None,
        feedback_bundle=_EMPTY_FEEDBACK_BUNDLE,
    ):
        configs = real_build(
            relevance_model=relevance_model,
            reply_draft_model=reply_draft_model,
            critic_model=critic_model,
            mode_cfg=mode_cfg,
            projects=projects,
            templates=templates,
            tracer=tracer,
            feedback=feedback,
            lessons=lessons,
            feedback_bundle=feedback_bundle,
        )
        built[mode_cfg["evaluate"]] = configs.relevance.system_prompt
        llm = _ScriptedLLMClient([_not_relevant_response()])
        llms[mode_cfg["evaluate"]] = llm
        return configs.__class__(
            relevance=configs.relevance.with_(llm=llm),
            reply_draft=configs.reply_draft,
            critic=configs.critic,
        )

    monkeypatch.setattr(scan_runner, "build_scout_phase_configs", spy_build)

    tracer = SQLiteTracer(db_path=str(tmp_path / "traces.db"))
    feedback = SQLiteFeedbackLoop(db_path=str(tmp_path / "feedback.db"))
    try:
        with StateManager(db_path=str(tmp_path / "scout.db")) as state:
            now = datetime(2026, 4, 18, tzinfo=UTC).isoformat()
            state.conn.execute(
                "INSERT INTO projects "
                "(key, name, description, link, active, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 1, ?, ?)",
                ("gw", "Gateway", "Desc", "https://example.com", now, now),
            )
            state.conn.execute(
                "INSERT INTO project_keywords "
                "(id, project_key, keyword, evaluate_prompt, respond_prompt, critique_prompt, "
                "match_type, active, priority, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)",
                (
                    1,
                    "gw",
                    "alpha",
                    "eval_alpha",
                    None,
                    None,
                    "substring",
                    now,
                    now,
                ),
            )
            state.conn.execute(
                "INSERT INTO project_keywords "
                "(id, project_key, keyword, evaluate_prompt, respond_prompt, critique_prompt, "
                "match_type, active, priority, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)",
                (
                    2,
                    "gw",
                    "beta",
                    "eval_beta",
                    None,
                    None,
                    "substring",
                    now,
                    now,
                ),
            )
            state.commit()
            scan_id = state.start_scan()
            feedback_snapshot = state.record_feedback_snapshot(scan_id, mode="shadow")
            await scan_runner.score_messages(
                routed,
                messages,
                MODES["lead_gen"],
                {},
                templates,
                "claude-haiku-4-5-20251001",
                "claude-haiku-4-5-20251001",
                "claude-haiku-4-5-20251001",
                tracer,
                feedback,
                state,
                scan_id,
                str(tmp_path / "digest.md"),
                feedback_snapshot=feedback_snapshot,
            )
    finally:
        await tracer.flush()
        await tracer.close()
        await feedback.close()

    # Two distinct prompt bundles were resolved — one per keyword route — and
    # each carried its own evaluate prompt body, not the other's.
    assert set(built) == {"eval_alpha", "eval_beta"}
    assert "ALPHA EVAL RULES" in built["eval_alpha"]
    assert "BETA EVAL RULES" in built["eval_beta"]
    assert "ALPHA EVAL RULES" not in built["eval_beta"]
    assert "BETA EVAL RULES" not in built["eval_alpha"]

    alpha_user = next(
        m for m in llms["eval_alpha"].calls[0].messages if m.role.value == "user"
    )
    beta_user = next(
        m for m in llms["eval_beta"].calls[0].messages if m.role.value == "user"
    )
    assert "Matched route" in alpha_user.content
    assert "**Project key:** gw" in alpha_user.content
    assert "**Keyword:** alpha" in alpha_user.content
    assert "**Intent:** alpha intent" in alpha_user.content
    assert "alpha upside" in alpha_user.content
    assert "alpha downside" in alpha_user.content
    assert "**Keyword:** beta" in beta_user.content
    assert "**Intent:** beta intent" in beta_user.content

    with StateManager(db_path=str(tmp_path / "scout.db")) as state:
        rows = state.conn.execute(
            "SELECT keyword_route_id FROM evaluations ORDER BY id"
        ).fetchall()
        assert [row["keyword_route_id"] for row in rows] == [1, 2]
