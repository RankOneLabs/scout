"""Tests for PostStore: post persistence, dedup, and retrieval."""

from __future__ import annotations

from datetime import UTC, datetime

from scout.config import Message, RelevanceResult, SourceAuthor, SourceParent
from scout.storage.state import StateManager


def _make_msg_with_parent(platform_id: str, parent: SourceParent | None, status: str) -> Message:
    return Message(
        platform="bluesky",
        platform_id=platform_id,
        channel_name="bluesky",
        channel_id="bsky",
        author_name="tester",
        author_id="did:plc:tester",
        content="test reply",
        created_at=datetime.now(UTC),
        url="https://bsky.app/profile/tester/post/abc",
        parent=parent,
        parent_lookup_status=status,
    )


def _make_source_parent(parent_id: str = "at://did:plc:parent/post/p001") -> SourceParent:
    return SourceParent(
        id=parent_id,
        author=SourceAuthor(id="did:plc:parent", name="Parent Author"),
        text="The original post content",
        url="https://bsky.app/profile/parent/post/p001",
    )



class TestPostDedup:
    def _make_msg(self, platform_id: str = "msg-1") -> Message:
        return Message(
            platform="discord",
            platform_id=platform_id,
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="user-1",
            content="test content",
            created_at=datetime.now(UTC),
        )

    def test_save_post_returns_id(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()
        msg = self._make_msg()
        post_id = in_memory_state.save_post(msg, scan_id)
        assert post_id >= 1

    def test_duplicate_post_returns_existing_id(
        self,
        in_memory_state: StateManager,
    ) -> None:
        scan_id = in_memory_state.start_scan()
        msg = self._make_msg()
        id1 = in_memory_state.save_post(msg, scan_id)
        id2 = in_memory_state.save_post(msg, scan_id)
        assert id1 == id2

    def test_has_seen_message(self, in_memory_state: StateManager) -> None:
        assert not in_memory_state.has_seen_message("discord", "msg-999")

        scan_id = in_memory_state.start_scan()
        in_memory_state.save_post(self._make_msg("msg-999"), scan_id)
        assert in_memory_state.has_seen_message("discord", "msg-999")

class TestLoadPosts:
    def test_load_posts_round_trip(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()
        original = Message(
            platform="discord",
            platform_id="load-1",
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="Test content for loading",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            url="https://example.com",
        )
        in_memory_state.save_post(original, scan_id)

        loaded = in_memory_state.load_posts(scan_id=scan_id)
        assert len(loaded) == 1
        assert loaded[0].platform == "discord"
        assert loaded[0].platform_id == "load-1"
        assert loaded[0].content == "Test content for loading"
        assert loaded[0].author_name == "alice"

class TestLoadUnevaluatedPosts:
    def test_returns_posts_without_evaluation(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()
        msg = Message(
            platform="discord",
            platform_id="uneval-1",
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="No evaluation yet",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        in_memory_state.save_post(msg, scan_id)

        loaded = in_memory_state.load_unevaluated_posts(scan_id=scan_id)
        assert len(loaded) == 1
        assert loaded[0].platform_id == "uneval-1"

    def test_excludes_evaluated_posts(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()
        msg = Message(
            platform="discord",
            platform_id="eval-1",
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="Already evaluated",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        post_id = in_memory_state.save_post(msg, scan_id)
        result = RelevanceResult(
            message=msg,
            relevant=False,
            score=0.1,
            reason="not relevant",
            relevant_to=(),
        )
        in_memory_state.save_evaluation(result, post_id, scan_id)

        loaded = in_memory_state.load_unevaluated_posts(scan_id=scan_id)
        assert len(loaded) == 0

    def test_targeted_recovery_excludes_post_evaluated_in_later_scan(
        self, in_memory_state: StateManager
    ) -> None:
        source_scan_id = in_memory_state.start_scan()
        msg = Message(
            platform="discord",
            platform_id="recovered-later",
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="Recovered by a later scan",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        post_id = in_memory_state.save_post(msg, source_scan_id)
        recovery_scan_id = in_memory_state.start_scan()
        result = RelevanceResult(
            message=msg,
            relevant=False,
            score=0.1,
            reason="not relevant",
            relevant_to=(),
        )
        in_memory_state.save_evaluation(result, post_id, recovery_scan_id)

        loaded = in_memory_state.load_unevaluated_posts(scan_id=source_scan_id)

        assert loaded == []

    def test_mixed_evaluated_and_unevaluated(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()

        evaluated_msg = Message(
            platform="discord",
            platform_id="eval-2",
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="Evaluated",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        post_id = in_memory_state.save_post(evaluated_msg, scan_id)
        result = RelevanceResult(
            message=evaluated_msg,
            relevant=True,
            score=0.9,
            reason="relevant",
            relevant_to=("gateway",),
        )
        in_memory_state.save_evaluation(result, post_id, scan_id)

        unevaluated_msg = Message(
            platform="discord",
            platform_id="uneval-2",
            channel_name="general",
            channel_id="ch-1",
            author_name="bob",
            author_id="u2",
            content="Not evaluated",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        in_memory_state.save_post(unevaluated_msg, scan_id)

        loaded = in_memory_state.load_unevaluated_posts(scan_id=scan_id)
        assert len(loaded) == 1
        assert loaded[0].platform_id == "uneval-2"

    def test_no_scan_id_returns_all_unevaluated(self, in_memory_state: StateManager) -> None:
        scan1 = in_memory_state.start_scan()
        msg1 = Message(
            platform="discord",
            platform_id="s1-eval",
            channel_name="general",
            channel_id="ch-1",
            author_name="alice",
            author_id="u1",
            content="Evaluated in scan 1",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        post_id = in_memory_state.save_post(msg1, scan1)
        result = RelevanceResult(
            message=msg1,
            relevant=True,
            score=0.9,
            reason="relevant",
            relevant_to=("gateway",),
        )
        in_memory_state.save_evaluation(result, post_id, scan1)

        scan2 = in_memory_state.start_scan()
        msg2 = Message(
            platform="discord",
            platform_id="s2-uneval",
            channel_name="general",
            channel_id="ch-1",
            author_name="bob",
            author_id="u2",
            content="Not evaluated in scan 2",
            created_at=datetime(2025, 1, 2, tzinfo=UTC),
        )
        in_memory_state.save_post(msg2, scan2)

        loaded = in_memory_state.load_unevaluated_posts()
        assert len(loaded) == 1
        assert loaded[0].platform_id == "s2-uneval"

class TestParentContextPersistence:
    def test_save_and_load_resolved_parent(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()
        parent = _make_source_parent()
        msg = _make_msg_with_parent("reply-001", parent, "resolved")

        in_memory_state.save_post(msg, scan_id)
        loaded = in_memory_state.load_posts(scan_id)

        assert len(loaded) == 1
        loaded_msg = loaded[0]
        assert loaded_msg.parent_lookup_status == "resolved"
        assert loaded_msg.parent is not None
        assert loaded_msg.parent.id == parent.id
        assert loaded_msg.parent.text == parent.text
        assert loaded_msg.parent.author.id == parent.author.id
        assert loaded_msg.parent.author.name == parent.author.name
        assert loaded_msg.parent.url == parent.url

    def test_save_and_load_not_applicable(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()
        msg = _make_msg_with_parent("standalone-001", None, "not_applicable")

        in_memory_state.save_post(msg, scan_id)
        loaded = in_memory_state.load_posts(scan_id)

        assert len(loaded) == 1
        assert loaded[0].parent_lookup_status == "not_applicable"
        assert loaded[0].parent is None

    def test_save_and_load_failed_parent(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()
        msg = _make_msg_with_parent("failed-reply-001", None, "failed")

        in_memory_state.save_post(msg, scan_id)
        loaded = in_memory_state.load_posts(scan_id)

        assert len(loaded) == 1
        assert loaded[0].parent_lookup_status == "failed"
        assert loaded[0].parent is None

    def test_heal_failed_to_resolved_on_duplicate(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()

        # First save: parent lookup failed
        msg_failed = _make_msg_with_parent("heal-001", None, "failed")
        in_memory_state.save_post(msg_failed, scan_id)

        # Verify failed status persisted
        before = in_memory_state.load_posts(scan_id)
        assert before[0].parent_lookup_status == "failed"

        # Second save: same platform_id, now resolved
        parent = _make_source_parent()
        msg_resolved = _make_msg_with_parent("heal-001", parent, "resolved")
        in_memory_state.save_post(msg_resolved, scan_id)

        # Should now be healed to resolved
        after = in_memory_state.load_posts(scan_id)
        assert len(after) == 1
        assert after[0].parent_lookup_status == "resolved"
        assert after[0].parent is not None
        assert after[0].parent.text == parent.text

    def test_no_downgrade_resolved_to_failed_on_duplicate(
        self, in_memory_state: StateManager
    ) -> None:
        scan_id = in_memory_state.start_scan()

        # First save: parent resolved
        parent = _make_source_parent()
        msg_resolved = _make_msg_with_parent("no-downgrade-001", parent, "resolved")
        in_memory_state.save_post(msg_resolved, scan_id)

        # Second save: same platform_id, but now failed
        msg_failed = _make_msg_with_parent("no-downgrade-001", None, "failed")
        in_memory_state.save_post(msg_failed, scan_id)

        # Should still be resolved — no downgrade
        after = in_memory_state.load_posts(scan_id)
        assert len(after) == 1
        assert after[0].parent_lookup_status == "resolved"
        assert after[0].parent is not None

    def test_heal_not_applicable_to_resolved(self, in_memory_state: StateManager) -> None:
        scan_id = in_memory_state.start_scan()

        # Saved first as not_applicable (non-reply in first scan)
        msg_na = _make_msg_with_parent("na-to-res-001", None, "not_applicable")
        in_memory_state.save_post(msg_na, scan_id)

        # Now saved again with parent resolved
        parent = _make_source_parent()
        msg_resolved = _make_msg_with_parent("na-to-res-001", parent, "resolved")
        in_memory_state.save_post(msg_resolved, scan_id)

        after = in_memory_state.load_posts(scan_id)
        assert after[0].parent_lookup_status == "resolved"
        assert after[0].parent is not None
