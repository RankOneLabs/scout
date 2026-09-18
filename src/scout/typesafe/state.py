"""Pure projection of Scout inputs into catalogue state."""

from __future__ import annotations

from scout.config import Message
from scout.registry import ProjectTarget
from scout.typesafe.catalogue import Catalogue


def build_state(
    message: Message, project: ProjectTarget, catalogue: Catalogue
) -> dict[str, object]:
    projection = catalogue.document.state
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
