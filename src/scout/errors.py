"""Domain error types — carry enough context to debug without reading the implementation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

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


@dataclass(frozen=True, slots=True)
class PlatformFetchFailure:
    """A platform fetch failed — must not be treated as an empty result."""

    platform: str
    kind: str  # Stable category for fetch, truncation, scan, and digest failures.
    message: str
    context: str | None = None
    http_status: int | None = None
    retry_after: str | None = None
    retryable: bool = True


@dataclass(frozen=True)
class PlatformFetchSuccess:
    """A platform fetch completed. messages may be empty for a valid empty window."""

    platform: str
    messages: list[Message]
    context: str | None = None
    page_ceiling_reached: bool = False
    failures: tuple[PlatformFetchFailure, ...] = field(default_factory=tuple)
