"""The declared relevance-state name to Scout source binding table."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import TypedDict


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

HOLDOUT_SOURCE_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "channel_name": "channel",
        "content": "text",
    }
)


class RelevancePostStateSource(TypedDict):
    platform: str
    channel_name: str | None
    url: str | None
    content: str | None
    parent_author_name: str | None
    parent_text: str | None
    author_name: str | None
    author_handle: str | None


def source_for(group: str, declared_name: str) -> str | None:
    bindings = STATE_FIELD_BINDINGS.get(group)
    return None if bindings is None else bindings.get(declared_name)


def _record_value(record: Mapping[str, object], source: str) -> object:
    if source in record:
        return record[source]
    alias = HOLDOUT_SOURCE_ALIASES.get(source)
    if alias is not None and alias in record:
        return record[alias]
    raise ValueError(f"relevance state source is missing {source!r}")


def bind_state(
    declared_fields: Mapping[str, Sequence[str]],
    post: Mapping[str, object],
    project: Mapping[str, object],
) -> dict[str, object]:
    """Project declared output names from stored or holdout source fields."""
    state: dict[str, object] = {}
    for group, names in declared_fields.items():
        source_values = project if group == "project" else post
        projected = {
            name: _record_value(source_values, STATE_FIELD_BINDINGS[group][name])
            for name in names
        }
        state[group] = (
            None
            if group == "parent_context_only" and all(value is None for value in projected.values())
            else projected
        )
    return state


__all__ = [
    "BINDABLE_STATE_SOURCES",
    "RelevancePostStateSource",
    "STATE_FIELD_BINDINGS",
    "bind_state",
    "source_for",
]
