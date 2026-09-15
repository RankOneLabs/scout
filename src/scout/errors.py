"""Domain error types — carry enough context to debug without reading the implementation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from scout.config import Message


@dataclass(frozen=True, slots=True)
class LLMError:
    """An LLM call failed."""

    operation: str
    message_id: str
    detail: str


@dataclass(frozen=True, slots=True)
class ParseError:
    """Failed to parse structured data from LLM response."""

    raw_text: str
    detail: str


@dataclass(frozen=True, slots=True)
class NetworkError:
    """An HTTP request failed."""

    url: str
    status: int | None = None
    detail: str = ""


# Closed vocabulary for PlatformFetchFailure.operation_phase — which stage
# of scan coverage the failure occurred in. 'unknown' is the deliberately
# unclassified value: storage finalization (scans.py) always treats it as
# blocking watermark advance regardless of blocks_watermark_advance, since an
# unclassified failure carries no evidence that it is safe to ignore. It
# remains a valid vocabulary member for explicit use, but is never a default
# value — every construction site must state a classification.
OperationPhase = Literal["fetch", "parent_lookup", "scan", "digest", "unknown"]


@dataclass(frozen=True, slots=True, kw_only=True)
class PlatformFetchFailure:
    """A platform fetch failed — must not be treated as an empty result.

    `operation_phase` and `blocks_watermark_advance` are the durable
    classification storage finalization derives its authoritative blocking
    set from (see scout.storage.scans). Both are required with no default:
    every construction site across the codebase (scout.platforms.*,
    scout.scanning.runner, scout.grading.promotion) must state an explicit
    classification rather than silently falling back to "unknown" — a
    fallback that would hide a real miscategorization rather than surface it
    as a construction-time error.
    """

    platform: str
    kind: str  # Stable category for fetch, truncation, scan, and digest failures.
    message: str
    operation_phase: OperationPhase
    blocks_watermark_advance: bool
    context: str | None = None
    http_status: int | None = None
    retry_after: str | None = None
    retryable: bool = True


# How one source's pagination ended. Only 'exhausted' and 'since_boundary'
# mean the source was fully covered for the requested window; the other
# values always coincide with a blocking PlatformFetchFailure for that
# source.
SourceTermination = Literal[
    "exhausted", "since_boundary", "page_ceiling", "failure", "skipped"
]


@dataclass(frozen=True, slots=True)
class SourceFetchOutcome:
    """Per-source evidence from one platform fetch: how many pages one
    independently-checkpointed source consumed, how its pagination ended,
    and the failure (if any) that ended it. `source_key` is
    scout.platforms.base.derive_source_key of the source's descriptor.
    `covered` is the only field coverage finalization consumes."""

    source_key: str
    platform: str
    source_kind: str
    provider_key: str
    page_count: int
    termination: SourceTermination
    message_count: int
    failure: PlatformFetchFailure | None = None

    @property
    def covered(self) -> bool:
        return self.failure is None and self.termination in ("exhausted", "since_boundary")


@dataclass(frozen=True)
class PlatformFetchSuccess:
    """A platform fetch completed. messages may be empty for a valid empty window."""

    platform: str
    messages: list[Message]
    context: str | None = None
    page_ceiling_reached: bool = False
    failures: tuple[PlatformFetchFailure, ...] = field(default_factory=tuple)
    source_outcomes: tuple[SourceFetchOutcome, ...] = field(default_factory=tuple)
