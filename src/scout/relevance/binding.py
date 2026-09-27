"""The declared relevance-state name to Scout source binding table."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType


def _frozen(values: dict[str, str]) -> Mapping[str, str]:
    return MappingProxyType(values)


STATE_FIELD_BINDINGS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "post": _frozen(
            {
                "platform": "platform",
                "channel": "channel_name",
                "url": "url",
                "text": "content",
            }
        ),
        "parent_context_only": _frozen(
            {
                "author_name": "parent_author_name",
                "text": "parent_text",
            }
        ),
        "author": _frozen(
            {
                "name": "author_name",
                "handle": "author_handle",
            }
        ),
        "project": _frozen(
            {
                "key": "key",
                "name": "name",
                "description": "description",
            }
        ),
    }
)

BINDABLE_STATE_SOURCES: Mapping[str, frozenset[str]] = MappingProxyType(
    {group: frozenset(bindings.values()) for group, bindings in STATE_FIELD_BINDINGS.items()}
)


def source_for(group: str, declared_name: str) -> str | None:
    bindings = STATE_FIELD_BINDINGS.get(group)
    return None if bindings is None else bindings.get(declared_name)


__all__ = ["BINDABLE_STATE_SOURCES", "STATE_FIELD_BINDINGS", "source_for"]
