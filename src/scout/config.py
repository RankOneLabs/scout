"""Configuration and shared types for scout."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypedDict

from dotenv import load_dotenv

from scout.registry import KeywordRoute
from scout.resources import runtime_resource

load_dotenv()

# --- Env-parsing helpers ---
_env_errors: list[str] = []


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        _env_errors.append(f"{name}={raw!r} is not a valid integer")
        return default


def _env_min_int(name: str, default: int, minimum: int) -> int:
    value = _env_int(name, default)
    if value < minimum:
        _env_errors.append(f"{name}={value!r} must be >= {minimum}")
        return default
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        _env_errors.append(f"{name}={raw!r} is not a valid float")
        return default


def _env_int_list(name: str, sep: str = ",") -> list[int]:
    raw = os.getenv(name) or ""
    result: list[int] = []
    for part in raw.split(sep):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            _env_errors.append(f"{name} contains non-integer value {part!r}")
    return result


def _env_str_list(name: str, sep: str = ",") -> tuple[str, ...]:
    """Parse a comma-separated env var into a tuple of non-empty strings."""
    raw = os.getenv(name) or ""
    return tuple(part.strip() for part in raw.split(sep) if part.strip())


_TRUE_VALUES = {"true", "1", "yes", "on"}
_FALSE_VALUES = {"false", "0", "no", "off"}


def _env_bool(name: str, default: bool) -> bool:
    """Strict boolean parser: accepts only the case-insensitive tokens in
    `_TRUE_VALUES`/`_FALSE_VALUES`. Any other non-empty value records an
    env-parsing error and falls back to `default`, so validate_config()
    fails a misconfigured deployment loudly instead of silently coercing
    an unrecognized value."""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    _env_errors.append(f"{name}={raw!r} is not a valid boolean")
    return default


def get_env_errors() -> list[str]:
    """Return and clear accumulated env-parsing errors."""
    errs = list(_env_errors)
    _env_errors.clear()
    return errs


# --- Environment ---
DISCORD_BOT_TOKEN: str = os.getenv("DISCORD_BOT_TOKEN", "")
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
DISCORD_SERVER_ID: int = _env_int("DISCORD_SERVER_ID", 0)
DISCORD_CHANNEL_IDS: list[int] = _env_int_list("DISCORD_CHANNEL_IDS")

# --- Farcaster (Neynar) ---
NEYNAR_API_KEY: str = os.getenv("NEYNAR_API_KEY", "")
NEYNAR_API_URL: str = os.getenv("NEYNAR_API_URL", "").strip().rstrip("/")
FARCASTER_CHANNEL_IDS: list[str] = [
    x.strip() for x in os.getenv("FARCASTER_CHANNEL_IDS", "").split(",") if x.strip()
]
# UUID of an approved Neynar managed signer. Required for publish() to work.
# Loaded at import time; the CLI restarts per invocation so this is fine.
# See https://docs.neynar.com/docs/getting-started-with-neynar for signer setup.
FARCASTER_SIGNER_UUID: str = os.getenv("FARCASTER_SIGNER_UUID", "").strip()
# Used only during one-time signer registration (EIP-712 SignedKeyRequest);
# never read at publish time. Loaded at import time, same caveat as above.
FARCASTER_DEVELOPER_MNEMONIC: str = os.getenv("FARCASTER_DEVELOPER_MNEMONIC", "").strip()

# --- Bluesky (AT Protocol) ---
BLUESKY_API_URL: str = os.getenv("BLUESKY_API_URL", "").strip().rstrip("/")
BLUESKY_IDENTIFIER: str = os.getenv("BLUESKY_IDENTIFIER", "").strip()
BLUESKY_APP_PASSWORD: str = os.getenv("BLUESKY_APP_PASSWORD", "").strip()
BLUESKY_FEED_URIS: list[str] = [
    x.strip() for x in os.getenv("BLUESKY_FEED_URIS", "").split(",") if x.strip()
]
BLUESKY_LANGS: tuple[str, ...] = _env_str_list("BLUESKY_LANGS")

# --- Tuning ---
SCAN_INTERVAL_HOURS: int = _env_int("SCAN_INTERVAL_HOURS", 6)
RELEVANCE_THRESHOLD: float = _env_float("RELEVANCE_THRESHOLD", 0.7)
# LLM model routed via jig's `from_model` prefix rules:
#   claude-*           → AnthropicClient (reads ANTHROPIC_API_KEY)
#   openrouter/<vendor>/<slug> → OpenRouterClient (reads OPENROUTER_API_KEY)
#   dispatch/<name>    → DispatchClient (service at DISPATCH_URL; no
#                        default — each deployment must set it explicitly)
#   ollama/<name>      → OllamaClient
# Use "dispatch/sonnet" (etc.) to route a model through the dispatch service.
LLM_MODEL: str = os.getenv("LLM_MODEL", "claude-sonnet-4-6")
RELEVANCE_MODEL: str = os.getenv("RELEVANCE_MODEL") or LLM_MODEL
REPLY_DRAFT_MODEL: str = os.getenv("REPLY_DRAFT_MODEL") or LLM_MODEL
CRITIC_MODEL: str = os.getenv("CRITIC_MODEL") or LLM_MODEL
DB_PATH: str = os.getenv("DB_PATH", "scout.storage.db")
TRACE_DB_PATH: str = os.getenv("TRACE_DB_PATH", "scout_traces.db")
FEEDBACK_DB_PATH: str = os.getenv("FEEDBACK_DB_PATH", "scout_feedback.db")
MAX_MESSAGES_PER_CHANNEL: int = _env_int("MAX_MESSAGES_PER_CHANNEL", 200)
FARCASTER_MAX_RESULTS_PER_QUERY: int = _env_min_int(
    "FARCASTER_MAX_RESULTS_PER_QUERY", 25, 1
)
BLUESKY_MAX_RESULTS_PER_QUERY: int = _env_min_int(
    "BLUESKY_MAX_RESULTS_PER_QUERY", 25, 1
)
BLUESKY_MAX_PAGES: int = _env_min_int("BLUESKY_MAX_PAGES", 10, 1)
FARCASTER_MAX_PAGES: int = _env_min_int("FARCASTER_MAX_PAGES", 10, 1)
DISCORD_MAX_PAGES: int = _env_min_int("DISCORD_MAX_PAGES", 5, 1)
SCAN_MAX_NEW_MESSAGES: int = _env_min_int("SCAN_MAX_NEW_MESSAGES", 300, 0)
KEYWORD_PREFILTER: bool = os.getenv("KEYWORD_PREFILTER", "true").lower() == "true"
PROMPT_DIAGNOSTIC_ROUTE_THRESHOLD: int = _env_int(
    "PROMPT_DIAGNOSTIC_ROUTE_THRESHOLD", 1
)

# --- Dossier grounding ---
# Root path of the read-only dossier-source checkout.
# Empty string is accepted at import time; scan startup rejects it.
SCOUT_DOSSIER_ROOT: str = os.getenv("SCOUT_DOSSIER_ROOT", "")
# Minimum total facts+resources a dossier must have to be considered ready.
# Must be >= 20; values below 20 are rejected and the default is used.
SCOUT_DOSSIER_MIN_ENTRIES: int = _env_min_int("SCOUT_DOSSIER_MIN_ENTRIES", 20, 20)
# Maximum age in days since last_reviewed before a dossier is considered stale.
# Must be >= 1; values below 1 are rejected and the default is used.
SCOUT_DOSSIER_MAX_AGE_DAYS: int = _env_min_int("SCOUT_DOSSIER_MAX_AGE_DAYS", 90, 1)
# Maximum surfaced replies to a single author in a rolling 7×24 hour window.
# Must be >= 1; values below 1 are rejected and the default is used.
SCOUT_AUTHOR_WEEKLY_CAP: int = _env_min_int("SCOUT_AUTHOR_WEEKLY_CAP", 1, 1)
SCOUT_ENVIRONMENT: str = os.getenv("SCOUT_ENVIRONMENT", "development").strip().lower()
if SCOUT_ENVIRONMENT not in {"development", "test", "production"}:
    _env_errors.append(
        "SCOUT_ENVIRONMENT must be one of development, test, production"
    )
    SCOUT_ENVIRONMENT = "development"

# The exact worker build/revision that produced a report or artifact, when the
# deployment environment sets it (e.g. from CI/image metadata). Empty string
# when unset — callers treat "" the same as absent rather than a placeholder
# value.
SCOUT_BUILD_SHA: str = os.getenv("SCOUT_BUILD_SHA", "").strip()

# --- evaluation-feedback/v1 ---
# Selection window, cap, and rendering budgets for the phase-specific
# feedback snapshot built once per scan (see grading/feedback.py).
# Every value here is a resolved operational default; _env_min_int records
# an env-parsing error and falls back rather than accepting an out-of-range
# value, so scan_runner.validate_config() fails the process early on a
# misconfigured deployment instead of silently changing selection behavior.
#
# FEEDBACK_PROMPT_ENABLED gates whether a scan's snapshot is supplied to
# live prompts (mode "active") or recorded shadow-only (mode "shadow"),
# exactly as before. Default false preserves legacy prompt behavior until
# an operator opts in; _env_bool is the startup-validated strict boolean
# parser, so an unrecognized value fails validate_config() rather than
# silently resolving to a default.
FEEDBACK_POLICY_VERSION = "evaluation-feedback/v1"
FEEDBACK_PROMPT_ENABLED: bool = _env_bool("FEEDBACK_PROMPT_ENABLED", False)
FEEDBACK_LOOKBACK_DAYS: int = _env_min_int("FEEDBACK_LOOKBACK_DAYS", 90, 1)
FEEDBACK_MAX_GRADES: int = _env_min_int("FEEDBACK_MAX_GRADES", 200, 1)
FEEDBACK_SEGMENT_MIN_GRADES: int = _env_min_int("FEEDBACK_SEGMENT_MIN_GRADES", 5, 1)
FEEDBACK_RELEVANCE_EXAMPLE_LIMIT: int = _env_min_int(
    "FEEDBACK_RELEVANCE_EXAMPLE_LIMIT", 2, 0
)
FEEDBACK_REPLY_DRAFT_EXAMPLE_LIMIT: int = _env_min_int(
    "FEEDBACK_REPLY_DRAFT_EXAMPLE_LIMIT", 2, 0
)
FEEDBACK_CRITIC_EXAMPLE_LIMIT: int = _env_min_int(
    "FEEDBACK_CRITIC_EXAMPLE_LIMIT", 3, 0
)
FEEDBACK_NOTE_MAX_CHARS: int = _env_min_int("FEEDBACK_NOTE_MAX_CHARS", 240, 1)
FEEDBACK_RELEVANCE_TOKEN_BUDGET: int = _env_min_int(
    "FEEDBACK_RELEVANCE_TOKEN_BUDGET", 800, 1
)
FEEDBACK_REPLY_DRAFT_TOKEN_BUDGET: int = _env_min_int(
    "FEEDBACK_REPLY_DRAFT_TOKEN_BUDGET", 800, 1
)
FEEDBACK_CRITIC_TOKEN_BUDGET: int = _env_min_int(
    "FEEDBACK_CRITIC_TOKEN_BUDGET", 1000, 1
)


# --- Typed Config Structures ---


class ModeConfig(TypedDict):
    """Configuration for a single evaluation mode."""

    evaluate: str
    respond: str
    critique: str


# --- Modes ---
# Each mode pairs an evaluate prompt with a respond prompt (filenames in prompts/)
MODES: dict[str, ModeConfig] = {
    "lead_gen": {
        "evaluate": "lead_gen",
        "respond": "comment_draft",
        "critique": "critique",
    },
    "temp_check": {
        "evaluate": "temp_check",
        "respond": "comment_draft",
        "critique": "critique",
    },
}


def build_search_queries(keywords: tuple[KeywordRoute, ...]) -> list[str]:
    """Build search queries from registry keywords.

    Deduplicates active keyword strings case-insensitively (lowercase seen-set),
    preserving first-seen original casing and order.
    Used by platform scanners (Farcaster, Bluesky) as their search terms.
    """
    seen: set[str] = set()
    queries: list[str] = []
    for kw_route in keywords:
        kw = kw_route.keyword.strip()
        if not kw:
            continue
        kw_lower = kw.lower()
        if kw_lower not in seen:
            seen.add(kw_lower)
            queries.append(kw)
    return queries


# --- Shared Data Types ---


@dataclass(frozen=True, slots=True)
class SourceAuthor:
    """Platform-neutral author identity."""

    id: str
    name: str


@dataclass(frozen=True, slots=True)
class SourceParent:
    """Immediate parent post context resolved from a reply reference."""

    id: str
    author: SourceAuthor
    text: str
    url: str


@dataclass(frozen=True, slots=True)
class Message:
    """Platform-agnostic message representation."""

    platform: str  # "discord", "farcaster", "bluesky"
    platform_id: str  # Platform-specific message ID
    channel_name: str
    channel_id: str
    author_name: str
    author_id: str
    content: str
    created_at: datetime
    url: str = ""
    parent: SourceParent | None = None
    parent_lookup_status: str = "not_applicable"  # not_applicable | resolved | failed


@dataclass(frozen=True, slots=True)
class RelevanceResult:
    """Result of relevance evaluation for a single message."""

    message: Message
    relevant: bool
    score: float
    reason: str
    relevant_to: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CritiqueResult:
    """Result of the critic reviewing a draft comment."""

    verdict: str  # "approve", "revise", or "reject"
    feedback: str  # Why, and what to fix if revise


@dataclass(frozen=True, slots=True)
class CritiqueLesson:
    """A lesson extracted from a past critique for prompt injection."""

    verdict: str
    feedback: str
    comment_text: str


@dataclass(frozen=True, slots=True)
class PublishedPost:
    """Result of a successful publish to a platform."""

    platform: str  # "bluesky" | "farcaster"
    platform_post_id: str  # canonical id (AT URI for Bluesky, cast hash for Farcaster)
    published_at: datetime
    url: str = ""  # public-facing URL when reconstructable


@dataclass(frozen=True, slots=True)
class ScanStats:
    """Aggregate statistics across all scans."""

    total_scans: int
    total_posts: int
    total_relevant: int
    total_drafts: int


# --- Grading Types ---

_SCHEMA_PATH = runtime_resource("web", "grading_schema.json")

try:
    _schema = json.loads(_SCHEMA_PATH.read_text())
except FileNotFoundError as exc:
    raise RuntimeError(
        f"Grading schema missing at {_SCHEMA_PATH}. "
        "Ensure web/grading_schema.json ships with the source tree."
    ) from exc
except json.JSONDecodeError as exc:
    raise RuntimeError(
        f"Grading schema at {_SCHEMA_PATH} is not valid JSON: {exc.msg}"
    ) from exc

_required_defs = {
    "relevanceJudgment",
    "actionJudgment",
    "failureDimension",
    "posture",
    "factualDisposition",
    "nonBlankString",
    "gradePayload",
    "validationEnvelope",
}
_defs = _schema.get("$defs") or {}
_missing = _required_defs - _defs.keys()
if _missing:
    raise RuntimeError(
        f"Grading schema at {_SCHEMA_PATH} missing required $defs: {sorted(_missing)}"
    )

GRADING_SCHEMA: dict[str, Any] = _schema

RELEVANCE_JUDGMENTS: tuple[str, ...] = tuple(_defs["relevanceJudgment"]["enum"])
ACTION_JUDGMENTS: tuple[str, ...] = tuple(_defs["actionJudgment"]["enum"])
FAILURE_DIMENSIONS: tuple[str, ...] = tuple(_defs["failureDimension"]["enum"])
POSTURES: tuple[str, ...] = tuple(_defs["posture"]["enum"])

# PAA response_quality (human) producer version (paa/declarations.py
# resolves the human-grading evaluator against this). The single code
# constant for the current human grade contract version — shared by
# grading.py, storage/state.py's grade-write checks, and the web schema
# parity tests. Bump together with the PAA declaration reference whenever
# the grade schema's required shape changes.
HUMAN_GRADE_SCHEMA_VERSION = 3


@dataclass(frozen=True, slots=True)
class GradeRecord:
    """Human judgment on a scout evaluation (v3 causal grading)."""

    post_id: int
    source: str  # "cli" | "web"
    graded_at: datetime
    relevance_judgment: str  # "correct" | "false_positive" | "false_negative"
    evaluation_id: int | None = None
    scan_id: int | None = None
    schema_version: int = HUMAN_GRADE_SCHEMA_VERSION
    needs_regrade: bool = False
    # v2 action fields (None on legacy v1 records)
    action_judgment: str | None = None  # "accept" | "fail"
    # Fail-only fields (all None on passes and legacy records)
    dimensions: list[str] | None = None
    failure_note: str | None = None
    # factual_support detail
    factual_offending_claim: str | None = None
    factual_disposition: str | None = None  # "unsupported" | "contradicted"
    factual_contradicting_evidence: str | None = None
    # contextual_understanding detail
    context_missing_input: str | None = None
    # posture detail
    posture_should_have_been: str | None = None
    # unsupported_implication detail
    implication_implied_claim: str | None = None
    implication_missing_support: str | None = None
    # v3: the grader's corrected reply text, captured by the CLI's editor
    # or the web UI. Must be null on accept; on fail, required only when
    # neither failure_note nor a selected dimension's own causal detail
    # already explains the failure (see web/grading_schema.json).
    edited_text: str | None = None


@dataclass(frozen=True, slots=True)
class GradingSignal:
    """Aggregated v2 grading signal: pass rate, dimension counts, causal examples."""

    total_graded: int
    pass_count: int
    false_positive_count: int
    false_negative_count: int
    fail_count: int
    dimension_counts: tuple[tuple[str, int], ...]
    posture_correction_count: int
    factual_unsupported_count: int
    factual_contradicted_count: int
    recent_causal_examples: tuple[str, ...]
