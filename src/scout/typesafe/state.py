"""Pure projection of Scout inputs into catalogue state."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from scout.config import Message
from scout.registry import ProjectTarget
from scout.relevance.binding import bind_state
from scout.typesafe.catalogue import CatalogueDocument, StateProjection


class StateCatalogue(Protocol):
    @property
    def document(self) -> CatalogueDocument: ...


def _build_declared_state(
    record: Mapping[str, object],
    project: ProjectTarget,
    projection: StateProjection,
) -> dict[str, object]:
    declared_fields = {
        "post": projection.post,
        "parent_context_only": projection.parent_context_only,
        "author": projection.author,
        "project": projection.project,
    }
    project_source: dict[str, object] = {
        "key": project.key,
        "name": project.name,
        "description": project.description,
    }
    return bind_state(declared_fields, record, project_source)


def build_state(
    source: Message | Mapping[str, object], project: ProjectTarget, catalogue: StateCatalogue
) -> dict[str, object]:
    """Project either a legacy Message or stored post fields into catalogue state."""
    projection = catalogue.document.state
    if isinstance(projection, StateProjection):
        if isinstance(source, Message):
            raise TypeError("v2 relevance state requires stored post fields")
        return _build_declared_state(source, project, projection)
    if not isinstance(source, Message):
        raise TypeError("v1 catalogue state requires a Message")
    return _build_legacy_state(source, project, catalogue)


def _build_legacy_state(
    message: Message, project: ProjectTarget, catalogue: StateCatalogue
) -> dict[str, object]:
    projection = catalogue.document.state
    if isinstance(projection, StateProjection):
        raise TypeError("legacy state requires a v1 catalogue projection")
    post_values: dict[str, object] = {
        "id": message.platform_id,
        "platform": message.platform,
        "channel_name": message.channel_name,
        "content": message.content,
        "created_at": message.created_at.isoformat(),
        "url": message.url,
    }
    author_values: dict[str, object] = {
        "name": message.author.name,
        "handle": message.author.handle,
    }
    project_values: dict[str, object] = {
        "key": project.key,
        "name": project.name,
        "description": project.description,
        "link": project.link,
    }
    state: dict[str, object] = {
        "post": {field: post_values[field] for field in projection.post},
        "author": {field: author_values[field] for field in projection.author},
        "project": {field: project_values[field] for field in projection.project},
    }
    if projection.parent_context_only:
        state["parent_context"] = (
            None
            if message.parent is None
            else {
                "id": message.parent.id,
                "text": message.parent.text,
                "url": message.parent.url,
                "author": {
                    "name": message.parent.author.name,
                    "handle": message.parent.author.handle,
                },
            }
        )
    return state
