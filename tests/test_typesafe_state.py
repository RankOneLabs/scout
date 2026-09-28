from datetime import UTC, datetime

import pytest

from scout.config import Account, Message
from scout.registry import ProjectTarget
from scout.relevance.loader import load_catalogue_bytes
from scout.typesafe.catalogue import load_catalogue
from scout.typesafe.state import build_state

V2_CATALOGUE = b"""\
id: state-projection-test
decide: agent_ops_route/v1
description: Synthetic state projection catalogue.
state:
  post: [platform, channel, url, text]
  parent_context_only: [author_name, text]
  author: [name, handle]
  project: [key, name, description]
questions:
  excl_test: &question
    type: noul
    instructions: Synthetic question
    criteria: {true: {what: Yes.}, false: {what: No.}}
  needs_thread: *question
  answerable_from_post: *question
  about_agent_work: *question
  points_somewhere: *question
"""


def test_build_state_projects_only_declared_fields() -> None:
    msg = Message(
        "bluesky",
        "p1",
        "feed",
        "c",
        Account("bluesky", "a", "A", "a.test", bio="private"),
        "hello",
        datetime(2026, 1, 1, tzinfo=UTC),
    )
    project = ProjectTarget("agent-ops", "Agent Ops", "desc", "https://example.test")
    catalogue = load_catalogue("tests/fixtures/typesafe/fixture-two-question.v1.yaml")
    state = build_state(msg, project, catalogue)
    assert state["author"] == {"name": "A", "handle": "a.test"}
    assert "bio" not in str(state)


def _stored_record(**changes: object) -> dict[str, object]:
    record: dict[str, object] = {
        "platform": "bluesky",
        "channel_name": "research",
        "url": "https://bsky.app/profile/alice.test/post/1",
        "content": "Post text",
        "parent_author_name": None,
        "parent_text": None,
        "author_name": "Alice",
        "author_handle": "alice.test",
    }
    return record | changes


def _v2_state(record: dict[str, object]) -> dict[str, object]:
    catalogue = load_catalogue_bytes(V2_CATALOGUE)
    project = ProjectTarget("agent-ops", "Agent Ops", "Operations", "https://example.test")
    return build_state(record, project, catalogue)


def test_declared_state_preserves_null_channel() -> None:
    state = _v2_state(_stored_record(channel_name=None))
    assert state == {
        "post": {
            "platform": "bluesky",
            "channel": None,
            "url": "https://bsky.app/profile/alice.test/post/1",
            "text": "Post text",
        },
        "parent_context_only": None,
        "author": {"name": "Alice", "handle": "alice.test"},
        "project": {"key": "agent-ops", "name": "Agent Ops", "description": "Operations"},
    }


def test_declared_state_preserves_null_content() -> None:
    state = _v2_state(_stored_record(content=None))
    assert state == {
        "post": {
            "platform": "bluesky",
            "channel": "research",
            "url": "https://bsky.app/profile/alice.test/post/1",
            "text": None,
        },
        "parent_context_only": None,
        "author": {"name": "Alice", "handle": "alice.test"},
        "project": {"key": "agent-ops", "name": "Agent Ops", "description": "Operations"},
    }


def test_declared_state_keeps_partial_unresolved_parent() -> None:
    state = _v2_state(
        _stored_record(
            parent_lookup_status="failed", parent_author_name="Parent", parent_text=None
        )
    )
    assert state == {
        "post": {
            "platform": "bluesky",
            "channel": "research",
            "url": "https://bsky.app/profile/alice.test/post/1",
            "text": "Post text",
        },
        "parent_context_only": {"author_name": "Parent", "text": None},
        "author": {"name": "Alice", "handle": "alice.test"},
        "project": {"key": "agent-ops", "name": "Agent Ops", "description": "Operations"},
    }


def test_declared_state_rejects_unbound_holdout_export_source_names() -> None:
    record = _stored_record()
    record["channel"] = record.pop("channel_name")
    record["text"] = record.pop("content")

    with pytest.raises(KeyError, match="channel_name"):
        _v2_state(record)
