"""Scout's phase-level Jig agent configs.

Scout owns the engagement workflow as explicit phases: relevance,
reply drafting, and critique/revision. Each phase is a normal Jig
``AgentConfig`` with its own prompt, schema, and model, which keeps
model selection and evals tunable per phase while preserving the
downstream ``ReplyCandidate`` shape.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from jig import AgentConfig, FeedbackLoop, ToolRegistry, TracingLogger, from_model

from scout.config import (
    CritiqueLesson,
    Message,
    ModeConfig,
)
from scout.dossiers.resolver import DossierSummary
from scout.grading.feedback import (
    LegacySection,
    PhaseFeedbackBundle,
    PhaseFeedbackEntry,
    legacy_feedback_bundle,
)
from scout.prompts import get_prompt_db
from scout.registry import KeywordRoute, ProjectTarget
from scout.scanning.prefilter import RoutedMessage
from scout.scanning.schemas import CritiquePhaseOutput, RelevancePhaseOutput, StructuredDraftOutput
from scout.storage.state import StateManager

_EMPTY_FEEDBACK_BUNDLE = legacy_feedback_bundle("")
_EMPTY_FEEDBACK_ENTRY: PhaseFeedbackEntry = LegacySection(text="")


def _feedback_section_lines(entry: PhaseFeedbackEntry) -> list[str]:
    """Render the '## Recent Human Grading Feedback' block for one phase.

    Identical formatting for both a disabled-mode `LegacySection` and an
    active-mode `SnapshotBody` — only the source of `entry.text` differs
    (legacy selector/formatter vs. a snapshot's committed rendered_text).
    An empty `entry.text` omits both the heading and the body, matching
    the prior grading_signals behavior byte-for-byte in disabled mode and
    the no-feedback behavior for an empty active-mode phase body.
    """
    if not entry.text:
        return []
    return ["", "## Recent Human Grading Feedback", "", entry.text]

_PLATFORM_LIMIT_RULE = (
    "The final assembled reply must fit the source platform's hard limit: Bluesky"
    " <=300 Unicode code points; Farcaster <=320 UTF-8 bytes; Discord <=2,000"
    " Unicode code points. Resource segments expand to `Resource: {label} —"
    " {canonical_url}` and that expansion counts toward the limit. Prefer the"
    " fewest short segments needed; the verifier rejects rather than truncates."
)


@dataclass(frozen=True, slots=True)
class ScoutPhaseConfigs:
    """Jig configs for Scout's per-message phase pipeline."""

    relevance: AgentConfig[RelevancePhaseOutput]
    reply_draft: AgentConfig[StructuredDraftOutput]
    critic: AgentConfig[CritiquePhaseOutput]


@dataclass(frozen=True, slots=True)
class PhaseRunIdentity:
    """Immutable identity threaded into one phase's _run_phase call: the
    exact feedback_snapshot_phases row that governed its prompt, and the
    resolved model string that produced its output — both durably recorded
    on evaluation_phase_runs alongside the phase's AGENT_RUN trace id."""

    snapshot_phase_id: int
    model: str


@dataclass(frozen=True, slots=True)
class ScoutExecutionContext:
    """Per-post execution context for one pipeline run.

    Threads StateManager identity and this scan's already-committed
    feedback-snapshot phase ids into every _run_phase call for one post, so
    each durable evaluation_phase_runs row can be inserted (unlinked) as
    soon as its phase's trace is verified, without any per-phase call
    needing to rediscover scan/post/snapshot identity on its own.
    """

    state: StateManager
    scan_id: int
    post_id: int
    relevance: PhaseRunIdentity
    reply_draft: PhaseRunIdentity
    critic: PhaseRunIdentity


def format_projects(projects: Mapping[str, ProjectTarget]) -> str:
    """Format the projects mapping for inclusion in the system prompt."""
    if not projects:
        return "(no projects loaded)"
    blocks = []
    for key, p in projects.items():
        link = p.link.strip() or "(none provided; do not invent a URL)"
        blocks.append(
            f"- **{key}** — {p.name}\n"
            f"  Description: {p.description}\n"
            f"  Link: {link}"
        )
    return "\n".join(blocks)


def resolve_mode_for_message(
    base: ModeConfig,
    route: KeywordRoute | None,
) -> ModeConfig:
    """Return a ModeConfig for a specific message, overriding from the route when set.

    Falls back to base when route is None or when a route prompt column is null.
    """
    if route is None:
        return base
    return {
        "evaluate": route.evaluate_prompt or base["evaluate"],
        "respond": route.respond_prompt or base["respond"],
        "critique": route.critique_prompt or base["critique"],
    }


_VERDICT_LABELS = {"approve": "approved", "revise": "revised", "reject": "rejected"}


def _format_lessons(lessons: Sequence[CritiqueLesson]) -> str:
    if not lessons:
        return ""
    bullets = [
        f"- A past draft was **{_VERDICT_LABELS.get(lesson.verdict, lesson.verdict)}**"
        f" because: '{lesson.feedback}'"
        for lesson in lessons[:5]
    ]
    return "## Lessons from Past Critiques\n\n" + "\n".join(bullets)


def build_relevance_system_prompt(
    mode_cfg: ModeConfig,
    projects: Mapping[str, ProjectTarget],
    templates: Mapping[str, str],
    feedback: PhaseFeedbackEntry = _EMPTY_FEEDBACK_ENTRY,
) -> str:
    eval_rules = get_prompt_db(mode_cfg["evaluate"], templates)
    parts = [
        "You are Scout's relevance evaluator. Decide whether the message is"
        " a genuine engagement opportunity for one of the projects below.",
        "",
        "## Relevance evaluation",
        "",
        eval_rules,
        "",
        "## Projects",
        "",
        format_projects(projects),
    ]
    parts.extend(_feedback_section_lines(feedback))
    parts.extend(
        [
            "",
            "Return only your final relevance decision via `submit_output`.",
            "If not relevant, leave `relevant_to` empty.",
        ]
    )
    return "\n".join(parts)


def build_reply_draft_system_prompt(
    mode_cfg: ModeConfig,
    projects: Mapping[str, ProjectTarget],
    templates: Mapping[str, str],
    lessons: Sequence[CritiqueLesson] | None = None,
    feedback: PhaseFeedbackEntry = _EMPTY_FEEDBACK_ENTRY,
) -> str:
    draft_rules = get_prompt_db(mode_cfg["respond"], templates)
    parts = [
        "You are Scout's reply drafter. Write a dossier-grounded engagement"
        " reply for a message already judged relevant.",
        "",
        "## Comment drafting style",
        "",
        draft_rules,
        "",
        "## Projects",
        "",
        format_projects(projects),
    ]
    lessons_block = _format_lessons(lessons or [])
    if lessons_block:
        parts.extend(["", lessons_block])
    parts.extend(_feedback_section_lines(feedback))
    parts.extend(
        [
            "",
            "The per-message input includes the authoritative dossier for the"
            " routed project. Copy fact and resource IDs exactly. Every declarative"
            " segment and its matching `claims` entry must use one allowed safe"
            " phrasing verbatim.",
            "",
            _PLATFORM_LIMIT_RULE,
            "",
            "Return only the structured draft via `submit_output`.",
        ]
    )
    return "\n".join(parts)


# PAA response_quality (llm_judge) producer version (paa/declarations.py
# resolves the inbound response_quality/llm_judge evaluator against this).
# Bump together with the PAA declaration reference whenever the critic
# rubric, verdict vocabulary, or prompt assembly below materially changes.
LLM_CRITIC_PROMPT_VERSION = "1"


def build_critic_system_prompt(
    mode_cfg: ModeConfig,
    projects: Mapping[str, ProjectTarget],
    templates: Mapping[str, str],
    lessons: Sequence[CritiqueLesson] | None = None,
    feedback: PhaseFeedbackEntry = _EMPTY_FEEDBACK_ENTRY,
) -> str:
    critique_rules = get_prompt_db(mode_cfg["critique"], templates)
    parts = [
        "You are Scout's critic. Review the drafted engagement reply against"
        " the original message, relevance rationale, and project facts.",
        "",
        "## Self-critique rubric",
        "",
        critique_rules,
        "",
        "## Projects",
        "",
        format_projects(projects),
    ]
    lessons_block = _format_lessons(lessons or [])
    if lessons_block:
        parts.extend(["", lessons_block])
    parts.extend(_feedback_section_lines(feedback))
    parts.extend(
        [
            "",
            "If the draft is acceptable, use verdict `approve`.",
            "If it can be fixed, use verdict `revise` and provide the complete"
            " revised draft as the nested `revised_draft` object. Preserve all"
            " dossier citation IDs. Revised declarative text must remain one of"
            " that fact's allowed safe phrasings verbatim.",
            _PLATFORM_LIMIT_RULE,
            "If the message should not get a reply after all, use verdict `reject`.",
            "Return only the critique result via `submit_output`.",
        ]
    )
    return "\n".join(parts)


def format_message_input(message: Message) -> str:
    """Format the initial user-turn input describing the message to evaluate."""
    return _format_message_fields(message)


def _format_parent_section(message: Message) -> str:
    """Return the parent context block for inclusion in model input.

    Returns an empty string for non-replies, a populated section for resolved
    parents, and a compact unavailable marker for failed lookups.
    """
    if message.parent_lookup_status == "not_applicable":
        return ""
    if message.parent_lookup_status == "resolved" and message.parent is not None:
        p = message.parent
        lines = [
            "\n## Immediate parent",
            "",
            f"- **Author:** {p.author.name}",
            f"- **Content:** {p.text}",
        ]
        if p.url:
            lines.append(f"- **Link:** {p.url}")
        return "\n".join(lines)
    return "\n\n**Parent context unavailable** (reply whose parent could not be fetched)"


def _format_message_fields(message: Message) -> str:
    source_block = (
        "Evaluate this message.\n\n"
        "## Source post\n\n"
        f"- **Platform:** {message.platform}\n"
        f"- **Author:** {message.author_name}\n"
        f"- **Channel:** #{message.channel_name}\n"
        f"- **Content:** {message.content}"
    )
    parent_block = _format_parent_section(message)
    return source_block + parent_block


def format_routed_message_input(routed: RoutedMessage) -> str:
    """Format a routed message, preserving the base message fields and route context."""
    message = routed.message
    route = routed.keyword_route
    if route is None:
        return format_message_input(message)

    def _format_context(label: str, values: tuple[str, ...]) -> str:
        if not values:
            return f"- **{label}:** (none)"
        return f"- **{label}:**\n" + "\n".join(f"  - {value}" for value in values)

    return (
        f"{_format_message_fields(message)}\n\n"
        "## Matched route\n\n"
        f"- **Project key:** {route.project_key}\n"
        f"- **Keyword:** {route.keyword}\n"
        f"- **Intent:** {route.intent or '(none)'}\n"
        f"{_format_context('Positive context', route.positive_context)}\n"
        f"{_format_context('Negative context', route.negative_context)}"
    )


def format_reply_draft_input(
    *,
    message_input: str,
    relevance: RelevancePhaseOutput,
    dossier: DossierSummary | None = None,
) -> str:
    formatted = (
        f"{message_input}\n\n"
        "## Relevance decision\n\n"
        f"- **Score:** {relevance.score:.2f}\n"
        f"- **Reason:** {relevance.reason}\n"
        f"- **Relevant to:** {', '.join(relevance.relevant_to) or '(none)'}"
    )
    if dossier is not None:
        formatted += f"\n\n{format_dossier_for_drafting(dossier)}"
    return formatted


def format_dossier_for_drafting(dossier: DossierSummary) -> str:
    """Render the verifier's exact dossier contract for the drafting phases."""
    payload = {
        "project_key": dossier.project_key,
        "facts": [
            {
                "fact_id": fact.id,
                "context": fact.text,
                "allowed_safe_phrasings": fact.safe_phrasings,
            }
            for fact in dossier.facts
        ],
        "resources": [
            {
                "resource_id": resource.id,
                "label": resource.label,
                "canonical_url": resource.canonical_url,
            }
            for resource in dossier.resources
        ],
        "prohibitions": [
            {
                "prohibition_id": prohibition.id,
                "mode": prohibition.mode,
                "pattern": prohibition.pattern,
                "flags": prohibition.flags,
            }
            for prohibition in dossier.prohibitions
        ],
    }
    return (
        "## Authoritative project dossier\n\n"
        "Use only the IDs and allowed safe phrasings below. Do not invent IDs or"
        " paraphrase declarative claims.\n\n"
        f"```json\n{json.dumps(payload, indent=2, ensure_ascii=False)}\n```"
    )


def format_critic_input(
    *,
    message_input: str,
    relevance: RelevancePhaseOutput,
    draft: StructuredDraftOutput,
    dossier: DossierSummary | None = None,
) -> str:
    draft_json = json.dumps(draft.model_dump(), indent=2)
    formatted = (
        f"{message_input}\n\n"
        "## Relevance decision\n\n"
        f"- **Score:** {relevance.score:.2f}\n"
        f"- **Reason:** {relevance.reason}\n"
        f"- **Relevant to:** {', '.join(relevance.relevant_to) or '(none)'}\n\n"
        "## Draft reply (StructuredDraftOutput)\n\n"
        f"```json\n{draft_json}\n```"
    )
    if dossier is not None:
        formatted += f"\n\n{format_dossier_for_drafting(dossier)}"
    return formatted


def _empty_tools() -> ToolRegistry:
    return ToolRegistry([])


def build_scout_phase_configs(
    *,
    relevance_model: str,
    reply_draft_model: str,
    critic_model: str,
    mode_cfg: ModeConfig,
    projects: Mapping[str, ProjectTarget],
    templates: Mapping[str, str],
    tracer: TracingLogger,
    feedback: FeedbackLoop,
    lessons: Sequence[CritiqueLesson] | None = None,
    feedback_bundle: PhaseFeedbackBundle = _EMPTY_FEEDBACK_BUNDLE,
) -> ScoutPhaseConfigs:
    """Build per-phase Jig configs for Scout's message pipeline.

    Selects each phase's own entry from `feedback_bundle` — relevance,
    reply_draft, and critic never see each other's feedback entry, by
    construction of the bundle's three named fields.
    """
    common = {
        "feedback": feedback,
        "tracer": tracer,
        "tools": _empty_tools(),
        "max_tool_calls": 1,
        "max_llm_calls": 4,
        "max_parse_retries": 2,
        "include_memory_in_prompt": False,
        "include_feedback_in_prompt": False,
    }
    return ScoutPhaseConfigs(
        relevance=AgentConfig[RelevancePhaseOutput](
            name="scout_relevance",
            description="Scout relevance evaluator.",
            system_prompt=build_relevance_system_prompt(
                mode_cfg,
                projects=projects,
                templates=templates,
                feedback=feedback_bundle.relevance,
            ),
            llm=from_model(relevance_model),
            output_schema=RelevancePhaseOutput,
            **common,
        ),
        reply_draft=AgentConfig[StructuredDraftOutput](
            name="scout_reply_draft",
            description="Scout reply drafter.",
            system_prompt=build_reply_draft_system_prompt(
                mode_cfg,
                projects=projects,
                templates=templates,
                lessons=lessons,
                feedback=feedback_bundle.reply_draft,
            ),
            llm=from_model(reply_draft_model),
            output_schema=StructuredDraftOutput,
            **common,
        ),
        critic=AgentConfig[CritiquePhaseOutput](
            name="scout_critic",
            description="Scout reply critic and reviser.",
            system_prompt=build_critic_system_prompt(
                mode_cfg,
                projects=projects,
                templates=templates,
                lessons=lessons,
                feedback=feedback_bundle.critic,
            ),
            llm=from_model(critic_model),
            output_schema=CritiquePhaseOutput,
            **common,
        ),
    )


__all__ = [
    "LLM_CRITIC_PROMPT_VERSION",
    "PhaseRunIdentity",
    "ScoutExecutionContext",
    "ScoutPhaseConfigs",
    "build_critic_system_prompt",
    "build_relevance_system_prompt",
    "build_reply_draft_system_prompt",
    "build_scout_phase_configs",
    "format_critic_input",
    "format_dossier_for_drafting",
    "format_reply_draft_input",
    "format_routed_message_input",
    "format_message_input",
    "format_projects",
    "resolve_mode_for_message",
]
