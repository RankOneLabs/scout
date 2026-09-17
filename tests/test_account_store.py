"""Account snapshot persistence and schema-v46 convergence."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scout.config import Account
from scout.storage.schema import LATEST_SCHEMA_VERSION
from scout.storage.state import StateManager


def _account(observed_at: datetime) -> Account:
    return Account(
        platform="bluesky",
        id="did:plc:alice",
        name="Alice",
        handle="alice.bsky.social",
        bio="Builder",
        followers=12,
        following=4,
        posts=30,
        created_at=datetime(2020, 1, 1, tzinfo=UTC),
        verified=True,
        observed_at=observed_at,
    )


def test_snapshot_is_idempotent_and_latest_is_typed(in_memory_state: StateManager) -> None:
    first_at = datetime(2026, 1, 1, tzinfo=UTC)
    first = _account(first_at)
    latest = _account(first_at + timedelta(days=1))

    assert in_memory_state.record_account_snapshot(first)
    assert not in_memory_state.record_account_snapshot(first)
    assert in_memory_state.record_account_snapshot(latest)
    assert in_memory_state.latest_account("bluesky", first.id) == latest
    assert in_memory_state.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 2


def test_snapshot_rejects_blank_identity_and_naive_observation(
    in_memory_state: StateManager,
) -> None:
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    for account in (
        replace(_account(observed_at), platform=""),
        replace(_account(observed_at), id=" "),
        replace(_account(observed_at), observed_at=datetime(2026, 1, 1)),
    ):
        with pytest.raises(ValueError):
            in_memory_state.record_account_snapshot(account)


def test_snapshot_normalizes_observation_to_utc(in_memory_state: StateManager) -> None:
    observed_at = datetime.fromisoformat("2025-12-31T20:30:00-05:00")
    account = _account(observed_at)
    assert in_memory_state.record_account_snapshot(account)
    stored = in_memory_state.latest_account(account.platform, account.id)
    assert stored is not None
    assert stored.observed_at == datetime(2026, 1, 1, 1, 30, tzinfo=UTC)


def test_v45_upgrade_matches_fresh_v46_accounts_schema(tmp_path: Path) -> None:
    path = tmp_path / "v45.db"
    with StateManager(str(path)) as state, state.db.transaction():
        state.conn.execute("DROP INDEX accounts_latest_idx")
        state.conn.execute("DROP TABLE accounts")
        state.conn.execute("PRAGMA user_version = 45")

    query = (
        "SELECT type, name, sql FROM sqlite_master WHERE name LIKE 'accounts%' ORDER BY type, name"
    )
    with StateManager(str(path)) as upgraded, StateManager(":memory:") as fresh:
        assert upgraded.conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
        assert [tuple(row) for row in upgraded.conn.execute(query)] == [
            tuple(row) for row in fresh.conn.execute(query)
        ]
