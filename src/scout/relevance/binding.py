"""The declared relevance-state name to Scout source binding table."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, TypedDict


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

POPULATION_EXPORT_STATE_BINDINGS: Mapping[str, str] = MappingProxyType(
    {
        "platform": "platform",
        "channel": "channel_name",
        "url": "url",
        "text": "content",
        "parent_author_name": "parent_author_name",
        "parent_text": "parent_text",
        "author_name": "author_name",
        "author_handle": "author_handle",
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


def population_export_record_fields(source: Mapping[str, object]) -> dict[str, Any]:
    """Project state-source fields into the population export record names."""
    return {
        record_field: source[state_source]
        for record_field, state_source in POPULATION_EXPORT_STATE_BINDINGS.items()
    }


def population_export_state_source(record: Mapping[str, object]) -> dict[str, object]:
    """Restore state-source names from one population export record."""
    return {
        state_source: record[record_field]
        for record_field, state_source in POPULATION_EXPORT_STATE_BINDINGS.items()
    }


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
            name: source_values[STATE_FIELD_BINDINGS[group][name]]
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
    "POPULATION_EXPORT_STATE_BINDINGS",
    "RelevancePostStateSource",
    "STATE_FIELD_BINDINGS",
    "bind_state",
    "population_export_record_fields",
    "population_export_state_source",
    "source_for",
]
