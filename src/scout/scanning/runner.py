"""Scan orchestration — config validation, fetching, scoring, and main loop."""
# ruff: noqa: E501

from __future__ import annotations

import argparse
import asyncio
import contextlib
import heapq
import json
import logging
import os
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from jig import SQLiteFeedbackLoop, SQLiteTracer, TracingLogger, run_pipeline

import scout.config as _config
from scout.config import (
    BLUESKY_API_URL,
    BLUESKY_APP_PASSWORD,
    BLUESKY_FEED_URIS,
    BLUESKY_IDENTIFIER,
    BLUESKY_MAX_RESULTS_PER_QUERY,
    CRITIC_MODEL,
    DB_PATH,
    DISCORD_BOT_TOKEN,
    DISCORD_CHANNEL_IDS,
    DISCORD_SERVER_ID,
    FARCASTER_CHANNEL_IDS,
    FARCASTER_MAX_RESULTS_PER_QUERY,
    FEEDBACK_DB_PATH,
    FEEDBACK_PROMPT_ENABLED,
    MAX_MESSAGES_PER_CHANNEL,
    MODES,
    NEYNAR_API_KEY,
    NEYNAR_API_URL,
    PROMPT_DIAGNOSTIC_ROUTE_THRESHOLD,
    RELEVANCE_MODEL,
    RELEVANCE_THRESHOLD,
    REPLY_DRAFT_MODEL,
    SCAN_INTERVAL_HOURS,
    SCAN_MAX_NEW_MESSAGES,
    SCOUT_ENVIRONMENT,
    TRACE_DB_PATH,
    CritiqueLesson,
    CritiqueResult,
    Message,
    ModeConfig,
    RelevanceResult,
    build_search_queries,
)
from scout.dossiers.resolver import (
    DossierResolutionError,
    DossierSummary,
    get_pinned_dossier_revision,
    resolve_dossier,
)
from scout.errors import PlatformFetchFailure, PlatformFetchSuccess
from scout.grading.feedback import (
    FeedbackMode,
    PersistedFeedbackSnapshot,
    PhaseFeedbackBundle,
    legacy_feedback_bundle,
)
from scout.platforms.bluesky import BlueskyScanner
from scout.platforms.discord import DiscordScanner
from scout.platforms.farcaster import FarcasterScanner
from scout.prompts import prompt_source_report
from scout.registry import ProjectTarget, RuntimeRegistry
from scout.result import Err, Ok
from scout.scanning.agent import (
    PhaseRunIdentity,
    ScoutExecutionContext,
    ScoutPhaseConfigs,
    build_scout_phase_configs,
    resolve_mode_for_message,
)
from scout.scanning.digest import (
    append_to_digest,
    finalize_digest,
    format_result_block,
    write_digest_header,
)
from scout.scanning.pipeline import build_scout_pipeline
from scout.scanning.prefilter import RoutedMessage, keyword_prefilter
from scout.scanning.schemas import ReplyCandidate, StructuredDraftOutput, unpack_candidate
from scout.storage.state import (
    AUTHOR_RATE_EVALUATOR_VERSION,
    ScanStatus,
    StateManager,
    SurfaceRateLimitedError,
)
from scout.verifier import GateViolation, verify_draft_content

logger = logging.getLogger("scout.scanning.runner")


def _failure_to_dict(failure: PlatformFetchFailure) -> dict[str, object]:
    return {
        "platform": failure.platform,
        "context": failure.context,
        "kind": failure.kind,
        "message": failure.message,
        "http_status": failure.http_status,
        "retry_after": failure.retry_after,
        "retryable": failure.retryable,
    }


def _required_prompt_names(
    registry: RuntimeRegistry, mode_names: Sequence[str]
) -> set[str]:
    """Collect every prompt name the upcoming scan may resolve.

    Covers each active mode's evaluate/respond/critique defaults plus any
    non-null prompt overrides on the registry's keyword routes.
    """
    names: set[str] = set()
    for mode_name in mode_names:
        cfg = MODES[mode_name]
        names.update((cfg["evaluate"], cfg["respond"], cfg["critique"]))
    for route in registry.keywords:
        for prompt in (route.evaluate_prompt, route.respond_prompt, route.critique_prompt):
            if prompt:
                names.add(prompt)
    return names


def _active_route_prompt_overrides(registry: RuntimeRegistry) -> int:
    return sum(
        1
        for route in registry.keywords
        if any(
            prompt
            for prompt in (
                route.evaluate_prompt,
                route.respond_prompt,
                route.critique_prompt,
            )
        )
    )


def log_prompt_diagnostics(
    registry: RuntimeRegistry, mode_names: Sequence[str]
) -> None:
    """Surface prompts that are not DB-backed before a scan runs.

    Warns about prompts served from file fallback and missing prompts,
    and highlights when too many active routes depend on non-DB-backed
    prompt resolution. The scan continues regardless.
    """
    names = _required_prompt_names(registry, mode_names)
    report = prompt_source_report(names, registry.prompt_templates)
    override_count = _active_route_prompt_overrides(registry)
    file_fallback = sorted(n for n, src in report.items() if src == "file")
    missing = sorted(n for n, src in report.items() if src == "missing")
    problematic_routes = [
        route
        for route in registry.keywords
        if any(
            prompt and report.get(prompt) in {"file", "missing"}
            for prompt in (
                route.evaluate_prompt,
                route.respond_prompt,
                route.critique_prompt,
            )
        )
    ]

    if file_fallback:
        logger.warning(
            "Prompt diagnostics: %d prompt(s) not DB-backed, served from file "
            "fallback: %s",
            len(file_fallback),
            ", ".join(file_fallback),
        )
    if missing:
        logger.warning(
            "Prompt diagnostics: %d referenced prompt(s) missing from both DB "
            "and files: %s",
            len(missing),
            ", ".join(missing),
        )
    logger.info(
        "Prompt diagnostics: %d active keyword route(s) define at least one prompt override",
        override_count,
    )
    if problematic_routes and len(problematic_routes) >= PROMPT_DIAGNOSTIC_ROUTE_THRESHOLD:
        route_summaries = ", ".join(
            f"{route.id}:{route.project_key}/{route.keyword}" for route in problematic_routes
        )
        logger.warning(
            "Prompt diagnostics: %d active keyword route(s) reference file-backed "
            "or missing prompts (threshold=%d): %s",
            len(problematic_routes),
            PROMPT_DIAGNOSTIC_ROUTE_THRESHOLD,
            route_summaries,
        )
    if not file_fallback and not missing:
        logger.debug(
            "Prompt diagnostics: all %d referenced prompt(s) are DB-backed",
            len(names),
        )


def cap_new_messages(
    messages: Sequence[Message],
    max_messages: int | None = None,
) -> list[Message]:
    """Return newest-first messages capped to the configured live-scan budget."""
    resolved_max = SCAN_MAX_NEW_MESSAGES if max_messages is None else max_messages
    if resolved_max <= 0 or len(messages) <= resolved_max:
        return sorted(messages, key=lambda m: m.created_at, reverse=True)

    newest_first = heapq.nlargest(resolved_max, messages, key=lambda m: m.created_at)
    logger.info(
        "New messages capped: %d -> %d",
        len(messages),
        resolved_max,
    )
    return newest_first


def validate_config() -> list[str]:
    """Check that required configuration is present. Returns list of errors."""
    from scout.config import ANTHROPIC_API_KEY, get_env_errors

    errors = list(get_env_errors())

    def _validate_model(env_name: str, model: str) -> None:
        if model.startswith("claude-") and not ANTHROPIC_API_KEY:
            errors.append(
                f"ANTHROPIC_API_KEY not set (required when {env_name}={model!r})"
            )
        if model.startswith("openrouter/") and not os.getenv("OPENROUTER_API_KEY"):
            errors.append(
                f"OPENROUTER_API_KEY not set (required when {env_name}={model!r})"
            )
        if not model.startswith("dispatch/"):
            return
        dispatch_url = os.getenv("DISPATCH_URL", "").strip()
        if not dispatch_url:
            errors.append(
                f"DISPATCH_URL not set (required when {env_name}={model!r})"
            )
        elif not dispatch_url.startswith(("http://", "https://")):
            errors.append(
                f"DISPATCH_URL={dispatch_url!r} must be an http(s) URL"
            )

    phase_models = {
        "RELEVANCE_MODEL" if os.getenv("RELEVANCE_MODEL") else "LLM_MODEL": RELEVANCE_MODEL,
        "REPLY_DRAFT_MODEL" if os.getenv("REPLY_DRAFT_MODEL") else "LLM_MODEL": REPLY_DRAFT_MODEL,
        "CRITIC_MODEL" if os.getenv("CRITIC_MODEL") else "LLM_MODEL": CRITIC_MODEL,
    }
    seen: set[str] = set()
    for env_name, model in phase_models.items():
        if model in seen:
            continue
        seen.add(model)
        _validate_model(env_name, model)

    has_discord = bool(DISCORD_BOT_TOKEN and DISCORD_SERVER_ID and DISCORD_CHANNEL_IDS)
    has_farcaster = bool(NEYNAR_API_KEY and NEYNAR_API_URL)
    has_bluesky = bool(BLUESKY_API_URL and BLUESKY_IDENTIFIER and BLUESKY_APP_PASSWORD)

    if not has_discord and not has_farcaster and not has_bluesky:
        errors.append(
            "No platform configured. Set DISCORD_BOT_TOKEN + DISCORD_SERVER_ID + "
            "DISCORD_CHANNEL_IDS, or NEYNAR_API_KEY + NEYNAR_API_URL, "
            "or BLUESKY_API_URL + BLUESKY_IDENTIFIER + BLUESKY_APP_PASSWORD."
            " For Bluesky use https://bsky.social/xrpc as the API URL."
        )
    return errors


def _log_route_bundle(
    routed: RoutedMessage,
    resolved_mode: ModeConfig,
) -> None:
    route = routed.keyword_route
    logger.debug(
        "Resolved scoring bundle: route_id=%s project_key=%s keyword=%s "
        "evaluate=%s respond=%s critique=%s",
        route.id if route else None,
        route.project_key if route else None,
        route.keyword if route else None,
        resolved_mode["evaluate"],
        resolved_mode["respond"],
        resolved_mode["critique"],
    )


async def fetch_messages(
    discord_scanner: DiscordScanner | None,
    farcaster_scanner: FarcasterScanner | None,
    bluesky_scanner: BlueskyScanner | None,
    since: datetime | None,
    queries: list[str] | None = None,
) -> tuple[list[Message], list[PlatformFetchFailure]]:
    """Fetch messages from all configured platforms.

    Returns (messages, failures). Failures capture partial platform errors so
    callers can record them as scan metadata and set an appropriate scan status.
    """
    messages: list[Message] = []
    failures: list[PlatformFetchFailure] = []

    if discord_scanner:
        result = await discord_scanner.fetch_messages(since=since)
        match result:
            case PlatformFetchSuccess(
                platform=plat,
                messages=discord_msgs,
                page_ceiling_reached=ceiling,
                failures=partial_failures,
            ):
                logger.info("Fetched %d messages from Discord", len(discord_msgs))
                failures.extend(partial_failures)
                if ceiling and not any(f.kind == "page_ceiling" for f in partial_failures):
                    logger.warning(
                        "Discord page ceiling reached — some messages may be beyond fetched pages"
                    )
                    failures.append(PlatformFetchFailure(
                        platform=plat,
                        kind="page_ceiling",
                        message=f"Page ceiling reached; fetched {len(discord_msgs)} messages",
                        context="channel_history",
                        retryable=True,
                    ))
                messages.extend(discord_msgs)
            case PlatformFetchFailure() as failure:
                logger.error(
                    "Discord fetch failed (%s): %s", failure.kind, failure.message
                )
                failures.append(failure)

    if farcaster_scanner:
        result = await farcaster_scanner.fetch_messages(since=since, queries=queries)
        match result:
            case PlatformFetchSuccess(
                platform=plat,
                messages=farcaster_msgs,
                page_ceiling_reached=ceiling,
                failures=partial_failures,
            ):
                logger.info("Fetched %d casts from Farcaster", len(farcaster_msgs))
                failures.extend(partial_failures)
                if ceiling and not any(f.kind == "page_ceiling" for f in partial_failures):
                    logger.warning(
                        "Farcaster page ceiling reached — some casts may be beyond fetched pages"
                    )
                    failures.append(PlatformFetchFailure(
                        platform=plat,
                        kind="page_ceiling",
                        message=f"Page ceiling reached; fetched {len(farcaster_msgs)} casts",
                        context="keyword_search",
                        retryable=True,
                    ))
                messages.extend(farcaster_msgs)
            case PlatformFetchFailure() as failure:
                logger.error(
                    "Farcaster fetch failed (%s): %s", failure.kind, failure.message
                )
                failures.append(failure)

    if bluesky_scanner:
        result = await bluesky_scanner.fetch_messages(since=since, queries=queries)
        match result:
            case PlatformFetchSuccess(
                platform=plat,
                messages=bluesky_msgs,
                page_ceiling_reached=ceiling,
                failures=partial_failures,
            ):
                logger.info("Fetched %d posts from Bluesky", len(bluesky_msgs))
                failures.extend(partial_failures)
                if ceiling and not any(f.kind == "page_ceiling" for f in partial_failures):
                    logger.warning(
                        "Bluesky page ceiling reached — some posts may be beyond fetched pages"
                    )
                    failures.append(PlatformFetchFailure(
                        platform=plat,
                        kind="page_ceiling",
                        message=f"Page ceiling reached; fetched {len(bluesky_msgs)} posts",
                        context="feed_or_search",
                        retryable=True,
                    ))
                messages.extend(bluesky_msgs)
            case PlatformFetchFailure() as failure:
                logger.error(
                    "Bluesky fetch failed (%s): %s", failure.kind, failure.message
                )
                failures.append(failure)

    logger.info("Total messages across all platforms: %d", len(messages))
    return messages, failures


def load_project_dossiers(
    projects: Mapping[str, ProjectTarget],
) -> tuple[dict[str, DossierSummary], list[str]]:
    """Load dossiers for all active projects.

    Returns (summaries_by_project_key, errors). errors is non-empty whenever
    any project cannot be fully grounded — the scan must abort in that case.

    Fail-closed invariants enforced here:
    - The active project registry is the sole authority: whatever projects
      are active is what gets checked, with no hardcoded project set. An
      empty registry is valid readiness with nothing to resolve.
    - SCOUT_DOSSIER_ROOT must be configured (not just absent) once any
      project is active.
    - Every active project must have a dossier_summary_id.
    - Every project with a dossier_summary_id must resolve a ready dossier.
    - Every active project is checked independently, so one project's
      failure does not hide another's.
    """
    if not projects:
        return {}, []

    root_str = _config.SCOUT_DOSSIER_ROOT
    if not root_str:
        return {}, ["SCOUT_DOSSIER_ROOT is not configured; cannot ground any active project"]

    root = Path(root_str)
    try:
        revision = get_pinned_dossier_revision(root)
    except RuntimeError as exc:
        return {}, [f"dossier-source revision is unavailable: {exc}"]
    summaries: dict[str, DossierSummary] = {}
    errors: list[str] = []

    for key, project in projects.items():
        if not project.dossier_summary_id:
            errors.append(
                f"project {key!r} is active but has no dossier_summary_id; "
                "set dossier_summary_id or deactivate the project"
            )
            continue
        try:
            resolved = resolve_dossier(
                root, revision, key, project.dossier_summary_id,
                max_age_days=_config.SCOUT_DOSSIER_MAX_AGE_DAYS,
                min_entries=_config.SCOUT_DOSSIER_MIN_ENTRIES,
            )
            summaries[key] = resolved.summary
        except DossierResolutionError as exc:
            errors.append(str(exc))

    return summaries, errors


def run_preflight(db_path: str, dossier_root: str) -> dict[str, object]:
    """Return deployment diagnostics without creating clients, rows, or scans."""
    errors: list[str] = []
    details: dict[str, object] = {}
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.execute("PRAGMA query_only=ON")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            details["database_user_version"] = version
            if version < 18:
                errors.append(f"database migration is behind: user_version={version}, need at least 18")
            # Inline read mirrors the registry's active-project query while keeping the DB read-only.
            rows = conn.execute(
                "SELECT key, name, description, link, dossier_summary_id FROM projects WHERE active=1"
            ).fetchall()
        projects = {
            row[0]: ProjectTarget(row[0], row[1], row[2], row[3], row[4]) for row in rows
        }
    except (sqlite3.Error, OSError) as exc:
        return {"ok": False, "errors": [f"database preflight failed: {exc}"], "details": details}

    details["active_projects"] = sorted(projects)

    if projects:
        try:
            root = Path(dossier_root)
            revision = get_pinned_dossier_revision(root)
            details["dossier_revision"] = revision
            for key, project in projects.items():
                if not project.dossier_summary_id:
                    errors.append(f"project {key!r} has no dossier_summary_id")
                    continue
                try:
                    resolve_dossier(root, revision, key, project.dossier_summary_id,
                                    max_age_days=_config.SCOUT_DOSSIER_MAX_AGE_DAYS,
                                    min_entries=_config.SCOUT_DOSSIER_MIN_ENTRIES)
                except DossierResolutionError as exc:
                    errors.append(str(exc))
        except RuntimeError as exc:
            errors.append(f"dossier-source revision is unavailable: {exc}")

    families = {model.split("/", 1)[0].split("-", 1)[0] for model in (RELEVANCE_MODEL, REPLY_DRAFT_MODEL, CRITIC_MODEL)}
    details["model_families"] = sorted(families)
    if len(families) < 2:
        errors.append("configured model families are not sufficiently independent")
    details["corpus_diagnostics"] = "run scripts/lint_eval_corpus.py with this checkout"
    return {"ok": not errors, "errors": errors, "details": details}


SurfaceStatus = Literal[
    "surfaced",
    "low_relevance",
    "abstained",
    "critic_rejected",
    "gate_blocked",
    "not_relevant",
    "drafting_failed",
]


@dataclass(frozen=True, slots=True)
class OutcomeDecision:
    """The complete, immutable classification of one message's terminal outcome.

    Produced by classify_outcome — a pure function with no DB, logging, or
    clock access. persist_outcome needs only this plus PersistenceContext to
    write the correct row shape; every other collaborator (StateManager,
    the digest, author-rate) stays outside this value.
    """

    status: SurfaceStatus
    evaluation: RelevanceResult
    project_key: str | None
    posture: str | None
    gate_violations: tuple[GateViolation, ...]
    validated_text: str | None
    terminal_reason: str | None
    structured_draft: StructuredDraftOutput | None
    critique: CritiqueResult | None
    contributor_phase_run_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PersistenceContext:
    """Scan/row identifiers needed to persist an OutcomeDecision.

    Platform and author identity live on decision.evaluation.message, so
    they are not duplicated here.
    """

    post_id: int
    scan_id: int
    keyword_route_id: int | None
    dossier_revision: str | None
    dossier_summary_id: str | None
    surfaced_at: str | None
    allow_response_only_phase_runs: bool = False


def _resolve_project_key(candidate: ReplyCandidate) -> str | None:
    """A routed project_key wins; otherwise the first nonblank relevant_to entry.

    relevant_to[0] is the deterministic fallback for KEYWORD_PREFILTER=false,
    where no keyword route resolved a project_key. No alphabetical sorting or
    multi-project fan-out occurs.
    """
    if candidate.project_key and candidate.project_key.strip():
        return candidate.project_key
    for candidate_key in candidate.relevant_to:
        if candidate_key and candidate_key.strip():
            return candidate_key
    return None


def classify_outcome(
    candidate: ReplyCandidate,
    msg: Message,
    dossiers: Mapping[str, DossierSummary],
) -> OutcomeDecision:
    """Classify one scored candidate into its terminal OutcomeDecision.

    Pure and DB-free: the only collaborators are RELEVANCE_THRESHOLD,
    immutable input objects, and verify_draft_content. Fixed precedence:
    critic reject; structured abstain; not relevant; below-threshold
    relevance; missing project key or structured draft; missing dossier;
    content verifier rejection; verifier success with no publishable text;
    surfaced.
    """
    evaluation, critique = unpack_candidate(candidate, msg)
    structured = candidate.structured_draft
    posture = structured.posture if structured is not None else None

    def _decision(
        status: SurfaceStatus,
        *,
        project_key: str | None = None,
        gate_violations: tuple[GateViolation, ...] = (),
        validated_text: str | None = None,
        terminal_reason: str | None = None,
    ) -> OutcomeDecision:
        return OutcomeDecision(
            status=status,
            evaluation=evaluation,
            project_key=project_key,
            posture=posture,
            gate_violations=gate_violations,
            validated_text=validated_text,
            terminal_reason=terminal_reason,
            structured_draft=structured,
            critique=critique,
            contributor_phase_run_ids=candidate.contributor_phase_run_ids,
        )

    # 1. Critic reject is an intentional terminal decision — it must not be
    # hidden behind relevance or an empty draft's segment count.
    if candidate.critique_verdict == "reject":
        return _decision(
            "critic_rejected",
            project_key=_resolve_project_key(candidate),
            terminal_reason=candidate.critique_feedback,
        )

    # 2. Structured abstain is a deliberate no-reply decision, checked before
    # relevance so legacy/malformed candidates (relevant=False with an
    # abstain draft) still classify correctly.
    if structured is not None and structured.posture == "abstain":
        return _decision(
            "abstained",
            project_key=_resolve_project_key(candidate),
            terminal_reason=structured.abstain_reason,
        )

    # 3. Not relevant.
    if not evaluation.relevant:
        return _decision("not_relevant")

    # 4. Relevant but below the surfacing threshold.
    if evaluation.score < RELEVANCE_THRESHOLD:
        return _decision("low_relevance", project_key=_resolve_project_key(candidate))

    project_key = _resolve_project_key(candidate)

    # 5. Missing project key or structured draft are terminal content-
    # generation defects, not irrelevance and not retryable.
    if not project_key:
        return _decision(
            "drafting_failed",
            terminal_reason="relevant evaluation did not identify a project",
        )
    if structured is None:
        return _decision(
            "drafting_failed",
            project_key=project_key,
            terminal_reason="relevant evaluation did not produce a structured draft",
        )

    # 6. Missing dossier: the candidate identified a project, but runtime
    # grounding is unavailable — a security-relevant gate failure, not
    # drafting_failed or not_relevant.
    dossier = dossiers.get(project_key)
    if dossier is None:
        return _decision(
            "gate_blocked",
            project_key=project_key,
            gate_violations=(
                GateViolation(
                    reason_code="missing_dossier",
                    offending_text=None,
                    segment_index=None,
                ),
            ),
        )

    # 7 & 8. Content verifier gates. assembled_text is retained even on
    # failure so a blocked artifact stays auditable.
    result = verify_draft_content(
        dossier=dossier,
        structured_draft=structured,
        platform=msg.platform,
        author_id=msg.author_id,
    )
    if not result.ok:
        return _decision(
            "gate_blocked",
            project_key=project_key,
            gate_violations=tuple(result.violations),
            validated_text=result.assembled_text,
        )
    if not result.assembled_text:
        # Unreachable in theory via the real verifier — a non-abstain draft
        # that passes every gate always assembles non-empty text, since
        # platform_limits fails closed on empty text otherwise. Fail closed
        # rather than assert or treat as retryable.
        return _decision(
            "drafting_failed",
            project_key=project_key,
            terminal_reason="verified draft produced no publishable text",
        )

    # 9. Every gate passed.
    return _decision(
        "surfaced",
        project_key=project_key,
        validated_text=result.assembled_text,
    )


def persist_outcome(
    state: StateManager,
    decision: OutcomeDecision,
    context: PersistenceContext,
) -> int:
    """Write one OutcomeDecision through the correct StateManager API.

    surfaced delegates to persist_surfaced_outcome; every other status
    delegates to persist_terminal_outcome with terminal_reason, critique
    (only for critic_rejected), and gate violations (only for gate_blocked).
    Invariant violations on a surfaced decision raise as programming errors
    — they are not converted to a retryable scoring failure.
    """
    critique_pair = (
        (decision.critique.verdict, decision.critique.feedback)
        if decision.critique is not None
        else None
    )

    if decision.status == "surfaced":
        assert decision.validated_text, "surfaced decision must carry validated_text"
        assert decision.project_key, "surfaced decision must carry a project_key"
        assert decision.structured_draft is not None, "surfaced decision must carry a structured draft"
        evaluation_id, _draft_id, _event_id = state.persist_surfaced_outcome(
            decision.evaluation,
            context.post_id,
            context.scan_id,
            project_key=decision.project_key,
            author_id=decision.evaluation.message.author_id,
            platform=decision.evaluation.message.platform,
            comment_text=decision.validated_text,
            structured_output=json.dumps(decision.structured_draft.model_dump()),
            contributor_phase_run_ids=decision.contributor_phase_run_ids,
            keyword_route_id=context.keyword_route_id,
            posture=decision.posture,
            critique=critique_pair,
            dossier_revision=context.dossier_revision,
            dossier_summary_id=context.dossier_summary_id,
            surfaced_at=context.surfaced_at,
            allow_response_only_phase_runs=context.allow_response_only_phase_runs,
        )
        return evaluation_id

    return state.persist_terminal_outcome(
        decision.evaluation,
        context.post_id,
        context.scan_id,
        surface_status=decision.status,
        contributor_phase_run_ids=decision.contributor_phase_run_ids,
        keyword_route_id=context.keyword_route_id,
        project_key=decision.project_key,
        posture=decision.posture,
        failure_reason=decision.terminal_reason,
        dossier_revision=context.dossier_revision,
        dossier_summary_id=context.dossier_summary_id,
        critique=critique_pair if decision.status == "critic_rejected" else None,
        gate_violations=decision.gate_violations or None,
        allow_response_only_phase_runs=context.allow_response_only_phase_runs,
    )


async def score_messages(
    routed_candidates: list[RoutedMessage],
    all_messages: Sequence[Message],
    base_mode_cfg: ModeConfig,
    projects: Mapping[str, ProjectTarget],
    templates: Mapping[str, str],
    relevance_model: str,
    reply_draft_model: str,
    critic_model: str,
    tracer: TracingLogger,
    feedback: SQLiteFeedbackLoop,
    state: StateManager,
    scan_id: int,
    digest_path: str,
    lessons: Sequence[CritiqueLesson] | None = None,
    feedback_bundle: PhaseFeedbackBundle | None = None,
    feedback_snapshot: PersistedFeedbackSnapshot | None = None,
    scan_status: str = "complete",
    overflow_count: int = 0,
    fetch_failures: list[dict[str, object]] | None = None,
    dossier_summaries: dict[str, DossierSummary] | None = None,
    dossier_revision: str | None = None,
) -> tuple[str, int, bool, list[PlatformFetchFailure]]:
    """Score messages via Scout's phase pipeline, write digest incrementally.

    Iterates over pre-filtered RoutedMessage list. Per-message routing
    selects the phase config bundle; configs are cached by the resolved
    (evaluate, respond, critique) tuple so identical bundles are built once.

    `feedback_snapshot` must be this scan's already-committed
    record_feedback_snapshot result — its three phase rows' ids are threaded
    into every post's ScoutExecutionContext so each phase's durable
    evaluation_phase_runs row cites the exact feedback_snapshot_phases row
    that governed its prompt.

    Returns (digest_text, relevant_count, digest_ok, processing_failures).
    digest_ok is False when any digest write/finalize step failed.
    """
    if feedback_snapshot is None:
        raise ValueError("score_messages requires this scan's feedback_snapshot")
    phase_run_identity = {
        p.phase: PhaseRunIdentity(snapshot_phase_id=p.snapshot_phase_id, model=model)
        for p, model in (
            (next(p for p in feedback_snapshot.phases if p.phase == "relevance"), relevance_model),
            (next(p for p in feedback_snapshot.phases if p.phase == "reply_draft"), reply_draft_model),
            (next(p for p in feedback_snapshot.phases if p.phase == "critic"), critic_model),
        )
    }
    processing_failures: list[PlatformFetchFailure] = []
    digest_writable = True
    digest_failure_recorded = False
    gate_blocked_count = 0

    def _record_processing_failure(
        kind: str,
        message: str,
        context: str | None = None,
        retryable: bool = False,
    ) -> None:
        processing_failures.append(PlatformFetchFailure(
            platform="scan_runner",
            kind=kind,
            message=message,
            context=context,
            retryable=retryable,
        ))

    def _record_digest_failure(message: str, context: str) -> None:
        nonlocal digest_failure_recorded
        if digest_failure_recorded:
            return
        digest_failure_recorded = True
        _record_processing_failure(
            "digest_error",
            message,
            context=context,
            retryable=False,
        )

    try:
        write_digest_header(digest_path, scan_id)
    except OSError as e:
        digest_writable = False
        _record_digest_failure(
            "write_digest_header failed; scan data is durable but digest output "
            f"is incomplete: {e}",
            "write_digest_header",
        )
        logger.error("write_digest_header failed; continuing without digest output", exc_info=True)

    for i, rm in enumerate(routed_candidates[:10]):
        m = rm.message
        logger.debug("  [%d] @%s in #%s: %s", i, m.author_name, m.channel_name, m.content[:120])
    if len(routed_candidates) > 10:
        logger.debug("  ... and %d more", len(routed_candidates) - 10)

    pipeline = build_scout_pipeline(tracer)
    # Cache of phase AgentConfigs keyed by (evaluate, respond, critique) tuple.
    # Lessons/feedback bundle are captured at build time (constant within one iteration).
    phase_config_cache: dict[tuple[str, str, str], ScoutPhaseConfigs] = {}
    resolved_feedback_bundle = feedback_bundle or legacy_feedback_bundle("")

    def _get_phase_configs(resolved: ModeConfig) -> ScoutPhaseConfigs:
        key = (resolved["evaluate"], resolved["respond"], resolved["critique"])
        if key not in phase_config_cache:
            phase_config_cache[key] = build_scout_phase_configs(
                relevance_model=relevance_model,
                reply_draft_model=reply_draft_model,
                critic_model=critic_model,
                mode_cfg=resolved,
                projects=projects,
                templates=templates,
                tracer=tracer,
                feedback=feedback,
                lessons=lessons or None,
                feedback_bundle=resolved_feedback_bundle,
            )
        return phase_config_cache[key]

    relevant_count = 0

    _dossiers: dict[str, DossierSummary] = dossier_summaries or {}

    for i, routed in enumerate(routed_candidates):
        msg = routed.message
        logger.info("Processing %d/%d: %s...", i + 1, len(routed_candidates), msg.content[:80])

        try:
            # save_post commits durably inside its own Db context before
            # any asynchronous evaluation work starts below — a crash
            # after this point leaves an auditable, recoverable post
            # rather than losing knowledge that it was seen.
            post_id = state.save_post(msg, scan_id)
        except sqlite3.Error as e:
            _record_processing_failure(
                "persistence_error",
                f"save_post failed: {e}",
                context=f"{msg.platform}:{msg.platform_id}",
                retryable=True,
            )
            logger.error("save_post failed for %s", msg.platform_id, exc_info=True)
            continue

        # Author blocks are checked from live SQLite state for every candidate,
        # so a block added from the web UI also stops later items in an active
        # scan. Posts remain persisted above for audit/deduplication, but no
        # prompt is built and no LLM call is made for the blocked account.
        if msg.author_id and state.is_author_blocked(
            platform=msg.platform,
            author_id=msg.author_id,
        ) is True:
            logger.info(
                "Skipping blocked author %s:%s (@%s)",
                msg.platform,
                msg.author_id,
                msg.author_name,
            )
            continue

        resolved_mode = resolve_mode_for_message(base_mode_cfg, routed.keyword_route)
        phase_configs = _get_phase_configs(resolved_mode)
        _log_route_bundle(routed, resolved_mode)

        execution_context = ScoutExecutionContext(
            state=state,
            scan_id=scan_id,
            post_id=post_id,
            relevance=phase_run_identity["relevance"],
            reply_draft=phase_run_identity["reply_draft"],
            critic=phase_run_identity["critic"],
        )

        # No Db transaction is open across this await: save_post above
        # already committed, and evaluation is pure LLM/network I/O with
        # no database access of its own. Each phase's own _run_phase call
        # opens its short, independent evaluation_phase_runs insert
        # transaction only after that phase's trace is verified durable.
        try:
            result = await run_pipeline(
                pipeline,
                input=routed,
                context={
                    "phase_configs": phase_configs,
                    "dossier_summaries": _dossiers,
                    "execution_context": execution_context,
                },
            )
        except asyncio.CancelledError:
            # The post committed above is retained as-is: no evaluation,
            # draft, grade, or outcome-event rows for it. Best-effort
            # record the non-clean scan end, then propagate cancellation
            # rather than swallowing it.
            with contextlib.suppress(Exception):
                state.fail_scan(
                    scan_id,
                    len(all_messages),
                    failure_post_id=post_id,
                    error_kind="cancelled",
                    error_message="scan cancelled during evaluation",
                )
            raise
        except Exception:
            # Recoverable incomplete post: preserved as saved-but-unevaluated,
            # recoverable via --rescore-failed. Scanning continues.
            _record_processing_failure(
                "scoring_error",
                "Unhandled scoring exception; post preserved as unevaluated",
                context=f"{msg.platform}:{msg.platform_id}",
                retryable=True,
            )
            logger.error(
                "Unhandled error scoring %s; post preserved as unevaluated",
                msg.platform_id,
                exc_info=True,
            )
            continue

        match result.step_outputs.get("score_and_draft"):
            case Ok(candidate):
                pass
            case Err(err):
                detail = getattr(err, "detail", str(err))
                operation = getattr(err, "operation", type(err).__name__)
                logger.error(
                    "Scoring failed for %s (%s): %s",
                    msg.platform_id,
                    operation,
                    detail,
                )
                _record_processing_failure(
                    "scoring_error",
                    detail,
                    context=f"{msg.platform}:{msg.platform_id}:{operation}",
                    retryable=True,
                )
                continue
            case _:
                logger.error("Scoring produced no output for %s", msg.platform_id)
                _record_processing_failure(
                    "scoring_error",
                    "Scoring produced no output",
                    context=f"{msg.platform}:{msg.platform_id}",
                    retryable=True,
                )
                continue

        # Classification and invariant errors propagate rather than being
        # caught here — a programmer defect must be loud, not folded into
        # an unevaluated scoring_error.
        decision = classify_outcome(candidate, msg, _dossiers)

        route_id = routed.keyword_route.id if routed.keyword_route else None
        dossier_summary_id = (
            projects[decision.project_key].dossier_summary_id
            if decision.project_key and decision.project_key in projects
            else None
        )
        context = PersistenceContext(
            post_id=post_id,
            scan_id=scan_id,
            keyword_route_id=route_id,
            dossier_revision=dossier_revision,
            dossier_summary_id=dossier_summary_id,
            surfaced_at=msg.created_at.isoformat(),
        )

        logger.info(
            "  → score=%.2f relevant=%s surface_status=%s reason=%s",
            decision.evaluation.score,
            decision.evaluation.relevant,
            decision.status,
            decision.evaluation.reason,
        )
        if decision.status == "gate_blocked" and any(
            v.reason_code == "missing_dossier" for v in decision.gate_violations
        ):
            logger.warning(
                "No dossier loaded for project %r; gate_blocked", decision.project_key
            )

        try:
            persist_outcome(state, decision, context)
        except SurfaceRateLimitedError as error:
            # StateManager has already committed the authoritative
            # gate_blocked evaluation and author_rate gate_blocks row under
            # the same write lock that observed the cap; only update the
            # in-memory decision for counters/digest suppression below and
            # log the durable IDs. Calling persist_outcome again here would
            # duplicate that terminal outcome.
            decision = OutcomeDecision(
                status="gate_blocked",
                evaluation=decision.evaluation,
                project_key=decision.project_key,
                posture=decision.posture,
                gate_violations=(GateViolation(
                    reason_code="author_rate",
                    offending_text=f"{error.count} events in last 7 days (cap {error.cap})",
                    segment_index=None,
                ),),
                validated_text=None,
                terminal_reason=None,
                structured_draft=decision.structured_draft,
                critique=decision.critique,
                contributor_phase_run_ids=decision.contributor_phase_run_ids,
            )
            logger.info(
                "  author-rate limited (evaluator v%s) for %s; "
                "persisted evaluation_id=%s gate_block_ids=%s",
                AUTHOR_RATE_EVALUATOR_VERSION, msg.platform_id,
                error.persisted_evaluation_id, error.gate_block_ids,
            )
        except sqlite3.Error as e:
            # persist_outcome's own begin_immediate() context has already
            # rolled back every row for this post's outcome — post_id
            # itself remains durable from save_post above, but this post
            # gets no evaluation/draft/grade/event rows. A local write
            # failure here is serious enough to stop the scan rather than
            # push through the remaining candidates: mark the scan as a
            # non-clean end in its own short transaction, then re-raise.
            logger.error(
                "persist_outcome failed for %s", msg.platform_id, exc_info=True
            )
            with contextlib.suppress(Exception):
                state.fail_scan(
                    scan_id,
                    len(all_messages),
                    failure_post_id=post_id,
                    error_kind="persistence_error",
                    error_message=f"persist_outcome failed: {e}",
                )
            raise

        if decision.status != "surfaced":
            if decision.status == "gate_blocked":
                gate_blocked_count += 1
                logger.info("  gate_blocked for %s", msg.platform_id)
            continue

        relevant_count += 1

        assert decision.validated_text
        block = format_result_block(
            decision.evaluation,
            decision.validated_text,
            decision.critique.verdict if decision.critique else None,
            decision.critique.feedback if decision.critique else None,
        )
        if digest_writable:
            try:
                append_to_digest(digest_path, block)
            except OSError as e:
                digest_writable = False
                _record_digest_failure(
                    "append_to_digest failed; scan data is durable but digest "
                    f"output is incomplete: {e}",
                    "append_to_digest",
                )
                logger.error(
                    "append_to_digest failed; committed DB work is preserved",
                    exc_info=True,
                )
        print(block)

    digest_ok = digest_writable
    if digest_writable:
        effective_scan_status = (
            "partial" if scan_status != "complete" or processing_failures else "complete"
        )
        try:
            digest = finalize_digest(
                digest_path,
                len(all_messages),
                relevant_count,
                status=effective_scan_status,
                overflow_count=overflow_count,
                failures=[*(fetch_failures or []), *map(_failure_to_dict, processing_failures)],
                gate_blocked_count=gate_blocked_count,
            )
        except OSError as e:
            digest_ok = False
            _record_digest_failure(
                "finalize_digest failed; scan data is durable but digest output "
                f"is incomplete: {e}",
                "finalize_digest",
            )
            logger.error(
                "finalize_digest failed; committed posts are preserved",
                exc_info=True,
            )
            digest = ""
    else:
        digest = ""
    return digest, relevant_count, digest_ok, processing_failures


async def main_loop(args: argparse.Namespace) -> None:
    """Main agent loop — single scan or continuous."""

    errors = validate_config()
    if errors:
        for e in errors:
            logger.error("Config error: %s", e)
        logger.error("Copy .env.example to .env and fill in your credentials")
        sys.exit(1)

    discord_scanner: DiscordScanner | None = None
    farcaster_scanner: FarcasterScanner | None = None
    bluesky_scanner: BlueskyScanner | None = None

    if DISCORD_BOT_TOKEN and DISCORD_SERVER_ID and DISCORD_CHANNEL_IDS:
        discord_scanner = DiscordScanner(
            token=DISCORD_BOT_TOKEN,
            server_id=DISCORD_SERVER_ID,
            channel_ids=DISCORD_CHANNEL_IDS,
            max_messages=MAX_MESSAGES_PER_CHANNEL,
        )
        logger.info("Discord scanner enabled (%d channels)", len(DISCORD_CHANNEL_IDS))

    if NEYNAR_API_KEY and NEYNAR_API_URL:
        farcaster_scanner = FarcasterScanner(
            api_key=NEYNAR_API_KEY,
            channel_ids=FARCASTER_CHANNEL_IDS or None,
            max_results_per_query=FARCASTER_MAX_RESULTS_PER_QUERY,
        )
        channels_info = f", channels: {FARCASTER_CHANNEL_IDS}" if FARCASTER_CHANNEL_IDS else ""
        logger.info("Farcaster scanner enabled (keyword search%s)", channels_info)

    if BLUESKY_API_URL and BLUESKY_IDENTIFIER and BLUESKY_APP_PASSWORD:
        bluesky_scanner = BlueskyScanner(
            feed_uris=BLUESKY_FEED_URIS or None,
            max_results_per_query=BLUESKY_MAX_RESULTS_PER_QUERY,
        )
        logger.info(
            "Bluesky scanner enabled (feeds: %d)",
            len(BLUESKY_FEED_URIS),
        )

    mode_names = list(MODES.keys()) if args.mode == "both" else [args.mode]
    tracer: SQLiteTracer | None = None
    feedback: SQLiteFeedbackLoop | None = None
    with StateManager(db_path=DB_PATH) as state:
        try:
            while True:
                logger.info("=" * 60)
                logger.info("Starting scan...")
                logger.info("=" * 60)

                active_scan_id: int | None = None
                active_messages_scanned = 0
                active_overflow = 0
                try:
                    registry = state.load_runtime_registry()
                    search_queries = build_search_queries(registry.keywords)
                    log_prompt_diagnostics(registry, mode_names)

                    # Load dossiers for all active projects. Abort if any fail.
                    _dossier_summaries, _dossier_errors = load_project_dossiers(
                        registry.projects
                    )
                    if _dossier_errors:
                        for err in _dossier_errors:
                            logger.error("Dossier not ready: %s", err)
                        logger.error(
                            "dossier_readiness_failed: %d project(s) not ready",
                            len(_dossier_errors),
                        )
                        if not args.continuous:
                            sys.exit(1)
                        # Continuous mode retries after the normal scan
                        # interval instead of exiting the daemon — a loud,
                        # bounded backoff rather than a silent permanent
                        # exit (S-008.a) or a hot retry loop.
                        logger.info(
                            "Sleeping %d hours before retrying dossier readiness...",
                            SCAN_INTERVAL_HOURS,
                        )
                        await asyncio.sleep(SCAN_INTERVAL_HOURS * 3600)
                        continue

                    _dossier_revision: str | None = None
                    if _config.SCOUT_DOSSIER_ROOT:
                        from pathlib import Path

                        from scout.dossiers.resolver import get_pinned_dossier_revision

                        with contextlib.suppress(RuntimeError):
                            _dossier_revision = get_pinned_dossier_revision(
                                Path(_config.SCOUT_DOSSIER_ROOT)
                            )

                    os.makedirs("digests", exist_ok=True)
                    timestamp = datetime.now(UTC).strftime("%Y-%m-%d_%H%M")

                    fetch_failures: list[PlatformFetchFailure] = []
                    fetch_started_at: datetime | None = None
                    advances_watermark = not args.rescore and not args.rescore_failed
                    _overflow = 0

                    if args.rescore_failed:
                        failed_scan_id = (
                            int(args.rescore_failed) if args.rescore_failed != "all" else None
                        )
                        raw_messages = state.load_unevaluated_posts(scan_id=failed_scan_id)
                        logger.info("Rescoring %d unevaluated posts from DB", len(raw_messages))
                        new_messages = raw_messages
                        all_unseen = raw_messages
                    elif args.rescore:
                        rescore_scan_id = int(args.rescore) if args.rescore != "all" else None
                        raw_messages = state.load_posts(scan_id=rescore_scan_id)
                        logger.info("Rescoring %d posts from DB", len(raw_messages))
                        new_messages = raw_messages
                        all_unseen = raw_messages
                    else:
                        since = state.get_last_scan_timestamp()
                        if since:
                            logger.info("Scanning messages since %s", since.isoformat())
                        else:
                            logger.info("First scan — fetching recent messages")

                        fetch_started_at = datetime.now(UTC)
                        all_messages, fetch_failures = await fetch_messages(
                            discord_scanner,
                            farcaster_scanner,
                            bluesky_scanner,
                            since,
                            queries=search_queries,
                        )

                        all_unseen = (
                            [
                                m
                                for m in all_messages
                                if not state.has_seen_message(m.platform, m.platform_id)
                            ]
                            if all_messages
                            else []
                        )
                        new_messages = cap_new_messages(all_unseen)
                        _overflow = len(all_unseen) - len(new_messages)
                        if _overflow > 0:
                            logger.info(
                                "Messages beyond SCAN_MAX_NEW_MESSAGES cap "
                                "(will be persisted as unevaluated for later recovery): %d",
                                _overflow,
                            )
                        logger.info(
                            "New messages (not previously seen): %d", len(all_unseen)
                        )

                    skip_live_scan = (
                        not args.rescore
                        and not args.rescore_failed
                        and not search_queries
                        and not all_unseen
                        and not fetch_failures
                    )
                    if skip_live_scan:
                        logger.info(
                            "Skipping live scan: no valid search queries and no new messages"
                        )
                    else:
                        if tracer is None:
                            tracer = SQLiteTracer(db_path=TRACE_DB_PATH)
                        if feedback is None:
                            feedback = SQLiteFeedbackLoop(db_path=FEEDBACK_DB_PATH)

                    for mode_name in mode_names:
                        if skip_live_scan:
                            break
                        assert tracer is not None
                        assert feedback is not None
                        base_mode_cfg = MODES[mode_name]
                        logger.info(
                            "Running %s pass (evaluate=%s, respond=%s, critique=%s)...",
                            mode_name,
                            base_mode_cfg["evaluate"],
                            base_mode_cfg["respond"],
                            base_mode_cfg.get("critique", "none"),
                        )
                        run_kind = (
                            "rescore"
                            if args.rescore is not None or args.rescore_failed is not None
                            else "live"
                        )
                        scan_id = state.start_scan(
                            fetch_started_at=fetch_started_at,
                            environment=SCOUT_ENVIRONMENT,
                            run_kind=run_kind,
                        )
                        active_scan_id = scan_id
                        active_messages_scanned = len(all_unseen)
                        active_overflow = _overflow

                        # Record platform fetch failures now that we have a scan_id.
                        for failure in fetch_failures:
                            state.save_fetch_failure(
                                scan_id,
                                platform=failure.platform,
                                kind=failure.kind,
                                message=failure.message,
                                context=failure.context,
                                http_status=failure.http_status,
                                retry_after=failure.retry_after,
                                retryable=failure.retryable,
                            )

                        if fetch_failures:
                            non_ceiling = [f for f in fetch_failures if f.kind != "page_ceiling"]
                            if non_ceiling:
                                logger.warning(
                                    "Scan has %d platform fetch failure(s); status will be partial",
                                    len(non_ceiling),
                                )

                        # evaluation-feedback/v1: immutable precondition of
                        # the scan, resolved and built before any model
                        # call. FEEDBACK_PROMPT_ENABLED is resolved exactly
                        # once here and passed into persistence, so the
                        # snapshot's stored mode always matches what this
                        # scan's prompts actually use — never re-read
                        # mid-scan. Any failure here (including a
                        # committed-phase integrity failure in active
                        # mode — see load_committed_feedback_bundle)
                        # propagates to the outer `except Exception` below,
                        # which fails the scan through the existing
                        # terminal scan-failure path; there is no legacy
                        # fallback for active mode.
                        feedback_mode: FeedbackMode = (
                            "active" if FEEDBACK_PROMPT_ENABLED else "shadow"
                        )
                        feedback_snapshot = state.record_feedback_snapshot(
                            scan_id, mode=feedback_mode
                        )
                        # Metadata-only: snapshot/phase ids, hashes, and
                        # token counts, never rendered text, grade notes,
                        # posts, or drafts.
                        logger.info(
                            "Feedback snapshot metadata: %s",
                            json.dumps(
                                {
                                    "snapshot_id": feedback_snapshot.snapshot_id,
                                    "policy_version": feedback_snapshot.policy_version,
                                    "mode": feedback_snapshot.mode,
                                    "phases": [
                                        {
                                            "phase": p.phase,
                                            "snapshot_phase_id": p.snapshot_phase_id,
                                            "rendered_sha256": p.rendered_sha256,
                                            "token_estimate": p.token_estimate,
                                            "token_budget": p.token_budget,
                                        }
                                        for p in feedback_snapshot.phases
                                    ],
                                },
                                sort_keys=True,
                            ),
                        )

                        feedback_bundle: PhaseFeedbackBundle
                        if feedback_mode == "active":
                            # No legacy grade selector/formatter call here —
                            # only the committed snapshot's phase rows are
                            # eligible prompt input in active mode.
                            feedback_bundle = state.load_committed_feedback_bundle(
                                feedback_snapshot.snapshot_id, expected_mode="active"
                            )
                        else:
                            grading_signal = state.get_recent_grading_signals(
                                limit_scans=3
                            )

                            from scout.grading.service import format_grading_signals

                            legacy_feedback_text = format_grading_signals(grading_signal)
                            if legacy_feedback_text:
                                logger.info(
                                    "Injecting grading signals: %d chars",
                                    len(legacy_feedback_text),
                                )
                            feedback_bundle = legacy_feedback_bundle(legacy_feedback_text)

                        lessons = state.get_recent_critique_feedback(limit=10)
                        if lessons:
                            logger.info(
                                "Loaded %d lessons from past critiques", len(lessons)
                            )

                        # Persist ALL unseen messages before scoring so overflow messages
                        # (those beyond the cap) survive as unevaluated posts recoverable
                        # via load_unevaluated_posts / --rescore-failed.
                        for msg in all_unseen:
                            state.save_post(msg, scan_id)

                        # Pre-filter the *capped* set with registry keywords.
                        routed_candidates = keyword_prefilter(new_messages, registry.keywords)

                        suffix = f"_{mode_name}" if len(mode_names) > 1 else ""
                        digest_path = f"digests/digest_{timestamp}{suffix}.md"

                        # Any fetch failure — including a page-ceiling-only one —
                        # means unseen upstream rows may remain, so the scan
                        # stays partial and the watermark does not advance.
                        scan_status: ScanStatus = (
                            "partial" if fetch_failures else "complete"
                        )

                        if not routed_candidates:
                            digest_ok = True
                            digest_error = ""
                            try:
                                write_digest_header(digest_path, scan_id)
                                digest = finalize_digest(
                                    digest_path,
                                    len(all_unseen),
                                    0,
                                    status=scan_status,
                                    overflow_count=_overflow,
                                    failures=[_failure_to_dict(f) for f in fetch_failures],
                                )
                            except Exception as e:
                                digest_ok = False
                                digest_error = str(e)
                                digest = ""
                                logger.error(
                                    "Digest write/finalize failed (no-candidates path); "
                                    "scan data is durable",
                                    exc_info=True,
                                )
                            if not digest_ok:
                                scan_status = "partial"
                                state.save_fetch_failure(
                                    scan_id,
                                    platform="digest",
                                    kind="digest_error",
                                    message=(
                                        "digest write/finalize failed; scan data is durable "
                                        f"but digest output is incomplete: {digest_error}"
                                    ),
                                    context="digest",
                                    retryable=False,
                                )
                            state.complete_scan(
                                scan_id, len(all_unseen), 0,
                                status=scan_status,
                                overflow_count=_overflow,
                                advance_watermark=advances_watermark,
                            )
                            if _overflow > 0:
                                logger.info(
                                    "Scan outcome: %s | %d scanned, 0 relevant, %d overflow",
                                    scan_status, len(all_unseen), _overflow,
                                )
                        else:
                            logger.info(
                                "Messages to evaluate (%s): %d", mode_name, len(routed_candidates)
                            )

                            assert tracer is not None
                            (
                                digest,
                                relevant_count,
                                digest_ok,
                                processing_failures,
                            ) = await score_messages(
                                routed_candidates,
                                all_unseen,
                                base_mode_cfg,
                                registry.projects,
                                registry.prompt_templates,
                                RELEVANCE_MODEL,
                                REPLY_DRAFT_MODEL,
                                CRITIC_MODEL,
                                tracer,
                                feedback,
                                state,
                                scan_id,
                                digest_path,
                                lessons=lessons or None,
                                feedback_bundle=feedback_bundle,
                                feedback_snapshot=feedback_snapshot,
                                scan_status=scan_status,
                                overflow_count=_overflow,
                                fetch_failures=[_failure_to_dict(f) for f in fetch_failures],
                                dossier_summaries=_dossier_summaries,
                                dossier_revision=_dossier_revision,
                            )

                            for failure in processing_failures:
                                state.save_fetch_failure(
                                    scan_id,
                                    platform=failure.platform,
                                    kind=failure.kind,
                                    message=failure.message,
                                    context=failure.context,
                                    http_status=failure.http_status,
                                    retry_after=failure.retry_after,
                                    retryable=failure.retryable,
                                )

                            if processing_failures:
                                scan_status = "partial"

                            if not digest_ok:
                                scan_status = "partial"
                                if not any(
                                    f.kind == "digest_error" for f in processing_failures
                                ):
                                    state.save_fetch_failure(
                                        scan_id,
                                        platform="digest",
                                        kind="digest_error",
                                        message=(
                                            "digest finalize failed; scan data is durable "
                                            "but digest output is incomplete"
                                        ),
                                        context="finalize_digest",
                                        retryable=False,
                                    )

                            state.complete_scan(
                                scan_id, len(all_unseen), relevant_count,
                                status=scan_status,
                                overflow_count=_overflow,
                                advance_watermark=advances_watermark,
                            )

                            logger.info(
                                "Scan outcome: %s | %d scanned, %d relevant, %d overflow",
                                scan_status, len(all_unseen), relevant_count, _overflow,
                            )

                        active_scan_id = None
                        assert tracer is not None
                        await tracer.flush()
                        logger.info("Digest saved to %s", digest_path)

                except KeyboardInterrupt:
                    if active_scan_id is not None:
                        state.complete_scan(
                            active_scan_id,
                            active_messages_scanned,
                            0,
                            status="interrupted",
                            overflow_count=active_overflow,
                        )
                    logger.warning("Scan interrupted; completed work is preserved")
                    raise
                except Exception as e:
                    logger.error("Scan failed: %s", e, exc_info=True)
                    if active_scan_id is not None:
                        with contextlib.suppress(Exception):
                            state.fail_scan(
                                active_scan_id,
                                active_messages_scanned,
                                failure_post_id=None,
                                error_kind="scan_error",
                                error_message=f"{type(e).__name__}: {e}",
                            )
                    if not args.continuous:
                        raise

                if not args.continuous:
                    break

                logger.info("Sleeping %d hours until next scan...", SCAN_INTERVAL_HOURS)
                await asyncio.sleep(SCAN_INTERVAL_HOURS * 3600)

        finally:
            if feedback is not None:
                await feedback.close()
            if tracer is not None:
                await tracer.close()
