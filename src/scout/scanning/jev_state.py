"""The state one JEV request carries, and the projections that build it.

Ported from ``RankOneLabs/assay``, ``experiments/typesafe_relevance/state.py``
at commit ``282479cdc877d1d470840684e8cbea891f9c54d3``. JEV was graded on this
exact projection, so its key names and its missing-value handling are a
contract rather than an implementation detail::

    post:                {platform, channel, url, text}
    parent_context_only: {author_name, text} | null
    author:              {name, handle}
    project:             {key, name, description}

Two entry points project into the same shape. ``state_input_from_message`` is
the live scanning path. ``state_input_from_record`` reads the population export
record form, which is what the graded runs were built from — it exists so the
pre-enable check described in docs/relevance-holdouts.md can project a round 5
export record and compare against the authoritative ``build_state``.

This is deliberately separate from ``scout.typesafe.state``, which serves the
disabled shadow node and emits different keys entirely.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from scout.config import Message
from scout.registry import ProjectTarget


@dataclass(frozen=True, slots=True)
class JevPost:
    platform: str
    channel: str | None
    url: str | None
    text: str | None


@dataclass(frozen=True, slots=True)
class JevParentContext:
    author_name: str | None
    text: str | None


@dataclass(frozen=True, slots=True)
class JevAuthor:
    name: str | None
    handle: str | None


@dataclass(frozen=True, slots=True)
class JevProject:
    key: str
    name: str | None
    description: str | None


@dataclass(frozen=True, slots=True)
class JevStateInput:
    """The post-side values the projection reads, before a project is attached.

    Named separately from ``JevState`` because the two entry points differ only
    in how they produce this, and the projection that combines it with a
    project is then shared.
    """

    platform: str
    channel: str | None
    url: str | None
    text: str | None
    parent_author_name: str | None
    parent_text: str | None
    author_name: str | None
    author_handle: str | None


@dataclass(frozen=True, slots=True)
class JevState:
    post: JevPost
    parent_context_only: JevParentContext | None
    author: JevAuthor
    project: JevProject

    def as_payload(self) -> dict[str, Any]:
        """The exact wire shape sent as the request's ``state``."""
        return {
            "post": {
                "platform": self.post.platform,
                "channel": self.post.channel,
                "url": self.post.url,
                "text": self.post.text,
            },
            "parent_context_only": (
                None
                if self.parent_context_only is None
                else {
                    "author_name": self.parent_context_only.author_name,
                    "text": self.parent_context_only.text,
                }
            ),
            "author": {"name": self.author.name, "handle": self.author.handle},
            "project": {
                "key": self.project.key,
                "name": self.project.name,
                "description": self.project.description,
            },
        }


def _absent_if_blank(value: str | None) -> str | None:
    """An empty string is Scout's representation of an absent url or channel.

    ``Message.url`` defaults to ``""`` and a message with no channel carries an
    empty ``channel_name``, where the record form the graded runs used carried
    null. Normalising here keeps the two entry points projecting the same
    state. Post text is deliberately not normalised: an empty post is a real
    empty post, not an absent one.
    """
    return value if value else None


def state_input_from_message(message: Message) -> JevStateInput:
    """Project one live scanned message into the state input."""
    parent = message.parent
    return JevStateInput(
        platform=message.platform,
        channel=_absent_if_blank(message.channel_name),
        url=_absent_if_blank(message.url),
        text=message.content,
        parent_author_name=None if parent is None else parent.author.name,
        parent_text=None if parent is None else parent.text,
        author_name=message.author.name,
        author_handle=message.author.handle,
    )


def state_input_from_record(record: Mapping[str, Any]) -> JevStateInput:
    """Project one population export record into the state input.

    ``platform`` is required, as it is in the authoritative loader; every other
    field reads as absent when the record does not carry it.
    """
    return JevStateInput(
        platform=record["platform"],
        channel=record.get("channel"),
        url=record.get("url"),
        text=record.get("text"),
        parent_author_name=record.get("parent_author_name"),
        parent_text=record.get("parent_text"),
        author_name=record.get("author_name"),
        author_handle=record.get("author_handle"),
    )


def project_from_target(target: ProjectTarget) -> JevProject:
    """The routed project, carrying only the three fields the state holds."""
    return JevProject(
        key=target.key, name=target.name, description=target.description
    )


def build_jev_state(state_input: JevStateInput, project: JevProject) -> JevState:
    """Combine the post-side input and the routed project into one state.

    ``parent_context_only`` is present when either parent field is, and null
    only when both are absent — the authoritative rule, kept so a post whose
    parent resolved with one field missing still carries its context.
    """
    parent = None
    if state_input.parent_author_name is not None or state_input.parent_text is not None:
        parent = JevParentContext(
            author_name=state_input.parent_author_name, text=state_input.parent_text
        )
    return JevState(
        post=JevPost(
            platform=state_input.platform,
            channel=state_input.channel,
            url=state_input.url,
            text=state_input.text,
        ),
        parent_context_only=parent,
        author=JevAuthor(
            name=state_input.author_name, handle=state_input.author_handle
        ),
        project=project,
    )
