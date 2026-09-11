"""author_classifications: upsert semantics and the v42 migration."""

from __future__ import annotations

from pathlib import Path

from scout.storage.schema import LATEST_SCHEMA_VERSION
from scout.storage.state import StateManager


def test_record_then_read_round_trips(in_memory_state: StateManager) -> None:
    changed = in_memory_state.record_author_classification(
        platform=" Bluesky ",
        author_id="did:plc:feed",
        author_class="aggregator",
        rule_version=1,
        matched_text="Daily",
    )
    stored = in_memory_state.get_author_classification(
        platform="bluesky", author_id="did:plc:feed"
    )
    assert changed
    assert stored is not None
    assert (stored.platform, stored.author_class, stored.rule_version, stored.matched_text) == (
        "bluesky",
        "aggregator",
        1,
        "Daily",
    )


def test_same_outcome_is_a_no_op_and_keeps_classified_at(in_memory_state: StateManager) -> None:
    key = {"platform": "bluesky", "author_id": "did:plc:feed"}
    in_memory_state.record_author_classification(
        **key, author_class="aggregator", rule_version=1, matched_text="Daily"
    )
    first = in_memory_state.get_author_classification(**key)
    assert first is not None
    changed = in_memory_state.record_author_classification(
        **key, author_class="aggregator", rule_version=1, matched_text="Daily"
    )
    assert not changed
    again = in_memory_state.get_author_classification(**key)
    assert again is not None
    assert again.classified_at == first.classified_at


def test_rule_version_bump_rewrites_the_row(in_memory_state: StateManager) -> None:
    key = {"platform": "bluesky", "author_id": "did:plc:brand"}
    in_memory_state.record_author_classification(
        **key, author_class="unknown", rule_version=1, matched_text=None
    )
    changed = in_memory_state.record_author_classification(
        **key, author_class="aggregator", rule_version=2, matched_text="bio: newsletter"
    )
    stored = in_memory_state.get_author_classification(**key)
    assert changed
    assert stored is not None
    assert (stored.author_class, stored.rule_version) == ("aggregator", 2)


def test_unseen_author_reads_as_none(in_memory_state: StateManager) -> None:
    assert (
        in_memory_state.get_author_classification(platform="bluesky", author_id="did:plc:none")
        is None
    )


def test_v41_upgrade_adds_the_table_and_matches_bootstrap(tmp_path: Path) -> None:
    path = tmp_path / "v41.db"
    with StateManager(str(path)) as before, before.db.transaction():
        before.conn.execute("DROP INDEX author_classifications_class_idx")
        before.conn.execute("DROP TABLE author_classifications")
        before.conn.execute("PRAGMA user_version = 41")
        before.conn.execute("INSERT INTO posts(platform, platform_msg_id) VALUES ('test', 'kept')")
    query = (
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE name LIKE 'author_classifications%' ORDER BY type, name"
    )
    with StateManager(str(path)) as upgraded, StateManager(":memory:") as fresh:
        assert upgraded.conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
        assert upgraded.conn.execute("SELECT platform_msg_id FROM posts").fetchone()[0] == "kept"
        assert [tuple(row) for row in upgraded.conn.execute(query)] == [
            tuple(row) for row in fresh.conn.execute(query)
        ]
        assert len(upgraded.conn.execute(query).fetchall()) == 2
