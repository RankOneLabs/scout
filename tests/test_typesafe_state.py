from datetime import UTC, datetime

from scout.config import Account, Message
from scout.registry import ProjectTarget
from scout.typesafe.catalogue import load_catalogue
from scout.typesafe.state import build_state


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
