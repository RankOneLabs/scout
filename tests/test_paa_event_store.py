"""ScoutEventStore against the five load-bearing EventStore semantics.

paa_runtime.store.EventStore documents five semantics every implementation
must satisfy, and states them as requirements on implementations rather
than descriptions of the default one. Scout has to override the store —
see the module docstring in paa_event_store — so "the default store gets
this right" is not evidence about Scout. These tests assert each semantic
directly against ScoutEventStore.

Nothing here goes through paa_service: the store is the unit under test,
and the service layer is still Scout's own until the lifecycle swap.
"""

from __future__ import annotations

import sqlite3

import pytest
from paa_runtime.events import CURRENT_EVENT_SCHEMA
from paa_runtime.store import AutonomyEvent

from scout.paa.event_store import ScoutEventStore
from scout.storage.state import StateManager


@pytest.fixture
def store(in_memory_state: StateManager) -> ScoutEventStore:
    return ScoutEventStore(in_memory_state)


def _insert(
    store: ScoutEventStore,
    *,
    event_id: str = "evt-1",
    motion_id: str = "mot-1",
    task: str = "outbound_content_publish",
    declaration_version: int = 1,
    scope: str | None = "publish:bluesky",
    event: str = "position_changed",
    from_position: str = "hitl",
    to_position: str = "hotl",
    created_at: str = "2026-01-01T00:00:00.000000Z",
    **overrides: object,
) -> None:
    """Insert one event inside its own transaction boundary."""
    with store.transaction():
        store.insert_autonomy_event(
            event_id=event_id,
            motion_id=motion_id,
            task=task,
            declaration_version=declaration_version,
            scope=scope,
            event=event,  # type: ignore[arg-type]
            from_position=from_position,  # type: ignore[arg-type]
            to_position=to_position,  # type: ignore[arg-type]
            evidence_ref="evidence/paa/" + "a" * 64 + "/evidence.json",
            evidence_sha256="a" * 64,
            actor="operator",
            reason="requested transition hitl to hotl",
            created_at=created_at,
            **overrides,  # type: ignore[arg-type]
        )


class TestReturnsContractTypeNotStorageRow:
    """The protocol forbids leaking the underlying storage row past this
    boundary — that is what keeps callers independent of how Scout stores
    events."""

    def test_reader_returns_autonomy_event_not_sqlite_row(self, store: ScoutEventStore) -> None:
        _insert(store)
        events = store.get_autonomy_events_for_motion("mot-1")
        assert isinstance(events[0], AutonomyEvent)

    def test_reader_never_returns_sqlite_row(self, store: ScoutEventStore) -> None:
        _insert(store)
        events = store.get_autonomy_events_for_motion("mot-1")
        assert not isinstance(events[0], sqlite3.Row)

    def test_event_carries_all_fourteen_contract_fields(self, store: ScoutEventStore) -> None:
        _insert(store)
        event = store.get_autonomy_events_for_motion("mot-1")[0]
        assert len(event.to_json_dict()) == 14

    def test_event_schema_defaults_to_current_contract_version(
        self, store: ScoutEventStore
    ) -> None:
        _insert(store)
        event = store.get_autonomy_events_for_motion("mot-1")[0]
        assert event.event_schema == CURRENT_EVENT_SCHEMA

    def test_explicit_event_schema_is_recorded_verbatim(self, store: ScoutEventStore) -> None:
        _insert(store, event_schema="paa-autonomy-event/9.9.9-test")
        event = store.get_autonomy_events_for_motion("mot-1")[0]
        assert event.event_schema == "paa-autonomy-event/9.9.9-test"


class TestNullScope:
    """Semantic: scope=None matches rows whose scope is null.

    This is the reason migration 33 relaxed the NOT NULL and the readers
    moved to `scope IS ?`. A declaration that omits `scopes:` — which both
    canonical_promotion and inbound_reply_surfacing do — resolves at scope
    None, and under `scope = ?` it would read as "authority never moved"
    forever, silently ignoring every promotion recorded for it.
    """

    def test_null_scope_event_round_trips(self, store: ScoutEventStore) -> None:
        _insert(store, scope=None, task="canonical_promotion")
        event = store.get_autonomy_events_for_motion("mot-1")[0]
        assert event.scope is None

    def test_latest_position_changed_finds_null_scope_row(self, store: ScoutEventStore) -> None:
        _insert(store, scope=None, task="canonical_promotion")
        found = store.get_latest_position_changed_event(
            task="canonical_promotion", declaration_version=1, scope=None,
        )
        assert found is not None

    def test_null_scope_query_does_not_match_a_scoped_row(self, store: ScoutEventStore) -> None:
        _insert(store, scope="publish:bluesky")
        found = store.get_latest_position_changed_event(
            task="outbound_content_publish", declaration_version=1, scope=None,
        )
        assert found is None

    def test_scoped_query_does_not_match_a_null_scope_row(self, store: ScoutEventStore) -> None:
        _insert(store, scope=None)
        found = store.get_latest_position_changed_event(
            task="outbound_content_publish", declaration_version=1, scope="publish:bluesky",
        )
        assert found is None

    def test_position_changed_before_finds_null_scope_baseline(
        self, store: ScoutEventStore
    ) -> None:
        _insert(store, event_id="evt-1", motion_id="mot-1", scope=None,
                created_at="2026-01-01T00:00:00.000000Z")
        baseline = store.get_position_changed_event_before(
            task="outbound_content_publish",
            declaration_version=1,
            scope=None,
            created_at="2026-01-02T00:00:00.000000Z",
            event_id="evt-2",
        )
        assert baseline is not None


class TestOrdering:
    """Semantic 2: reads order by the (created_at, id) tuple, and
    get_position_changed_event_before compares it as a strict tuple.

    A timestamp-only comparison passes the easy cases and fails exactly
    when two events share a created_at — which is what makes it worth
    asserting rather than assuming.
    """

    def test_events_for_motion_are_oldest_first(self, store: ScoutEventStore) -> None:
        _insert(store, event_id="evt-2", event="motion_approved",
                created_at="2026-01-02T00:00:00.000000Z")
        _insert(store, event_id="evt-1", event="motion_proposed",
                created_at="2026-01-01T00:00:00.000000Z")
        ids = [event.id for event in store.get_autonomy_events_for_motion("mot-1")]
        assert ids == ["evt-1", "evt-2"]

    def test_latest_position_changed_breaks_created_at_tie_by_id(
        self, store: ScoutEventStore
    ) -> None:
        same_instant = "2026-01-01T00:00:00.000000Z"
        _insert(store, event_id="evt-a", motion_id="mot-a", created_at=same_instant)
        _insert(store, event_id="evt-b", motion_id="mot-b", created_at=same_instant)
        latest = store.get_latest_position_changed_event(
            task="outbound_content_publish", declaration_version=1, scope="publish:bluesky",
        )
        assert latest is not None and latest.id == "evt-b"

    def test_before_excludes_the_boundary_point_itself(self, store: ScoutEventStore) -> None:
        _insert(store, event_id="evt-1", motion_id="mot-1",
                created_at="2026-01-01T00:00:00.000000Z")
        baseline = store.get_position_changed_event_before(
            task="outbound_content_publish",
            declaration_version=1,
            scope="publish:bluesky",
            created_at="2026-01-01T00:00:00.000000Z",
            event_id="evt-1",
        )
        assert baseline is None

    def test_before_uses_tuple_comparison_not_timestamp_alone(
        self, store: ScoutEventStore
    ) -> None:
        """Two events at the same instant: the earlier id is strictly
        before the later one. A `created_at < ?` comparison would return
        None here and lose the intervening-change detection entirely."""
        same_instant = "2026-01-01T00:00:00.000000Z"
        _insert(store, event_id="evt-a", motion_id="mot-a", created_at=same_instant)
        baseline = store.get_position_changed_event_before(
            task="outbound_content_publish",
            declaration_version=1,
            scope="publish:bluesky",
            created_at=same_instant,
            event_id="evt-b",
        )
        assert baseline is not None and baseline.id == "evt-a"

    def test_before_detects_a_position_that_cycled_back(self, store: ScoutEventStore) -> None:
        """The semantic this ordering exists for: hitl -> hotl -> hitl ->
        hotl leaves the resolved position unchanged, so only the baseline
        event's identity reveals that authority moved in between."""
        _insert(store, event_id="evt-1", motion_id="mot-1",
                from_position="hitl", to_position="hotl",
                created_at="2026-01-01T00:00:00.000000Z")
        _insert(store, event_id="evt-2", motion_id="mot-2",
                from_position="hotl", to_position="hitl",
                created_at="2026-01-02T00:00:00.000000Z")
        _insert(store, event_id="evt-3", motion_id="mot-3",
                from_position="hitl", to_position="hotl",
                created_at="2026-01-03T00:00:00.000000Z")
        baseline = store.get_position_changed_event_before(
            task="outbound_content_publish",
            declaration_version=1,
            scope="publish:bluesky",
            created_at="2026-01-02T00:00:00.000000Z",
            event_id="evt-2",
        )
        latest = store.get_latest_position_changed_event(
            task="outbound_content_publish", declaration_version=1, scope="publish:bluesky",
        )
        assert latest is not None
        assert baseline is not None
        # Same resolved position, different event identity — the whole point.
        assert baseline.to_position == latest.to_position
        assert baseline.id != latest.id

    def test_all_events_are_oldest_first(self, store: ScoutEventStore) -> None:
        _insert(store, event_id="evt-2", motion_id="mot-2",
                created_at="2026-01-02T00:00:00.000000Z")
        _insert(store, event_id="evt-1", motion_id="mot-1",
                created_at="2026-01-01T00:00:00.000000Z")
        assert [event.id for event in store.get_autonomy_events()] == ["evt-1", "evt-2"]

    def test_all_events_filter_by_task(self, store: ScoutEventStore) -> None:
        _insert(store, event_id="evt-1", motion_id="mot-1")
        _insert(store, event_id="evt-2", motion_id="mot-2",
                task="canonical_promotion", scope=None)
        filtered = store.get_autonomy_events(task="canonical_promotion")
        assert [event.id for event in filtered] == ["evt-2"]


class TestUniqueness:
    """Semantic 3: at most one event of each type per motion, enforced by
    the storage constraint rather than caller discipline."""

    def test_duplicate_event_type_for_one_motion_is_rejected(
        self, store: ScoutEventStore
    ) -> None:
        _insert(store, event_id="evt-1", event="motion_proposed")
        with pytest.raises(sqlite3.IntegrityError):
            _insert(store, event_id="evt-2", event="motion_proposed")

    def test_same_event_type_across_motions_is_allowed(self, store: ScoutEventStore) -> None:
        _insert(store, event_id="evt-1", motion_id="mot-1", event="motion_proposed")
        _insert(store, event_id="evt-2", motion_id="mot-2", event="motion_proposed")
        assert len(store.get_autonomy_events()) == 2


class TestAppendOnly:
    """Semantic 4: mutation is rejected by the store, not by convention.

    Asserted against raw SQL rather than through the adapter, because the
    point is that no code path can mutate the table — including one that
    bypasses this class entirely.
    """

    def test_update_is_aborted(self, in_memory_state: StateManager, store: ScoutEventStore) -> None:
        _insert(store)
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute(
                "UPDATE autonomy_events SET actor = 'someone-else' WHERE id = 'evt-1'"
            )

    def test_delete_is_aborted(self, in_memory_state: StateManager, store: ScoutEventStore) -> None:
        _insert(store)
        with pytest.raises(sqlite3.IntegrityError):
            in_memory_state.conn.execute("DELETE FROM autonomy_events WHERE id = 'evt-1'")


class TestTransactionBoundary:
    """Semantic 1: the caller owns the boundary, and a pair written under
    one boundary lands atomically or not at all."""

    def test_sibling_events_commit_atomically(self, store: ScoutEventStore) -> None:
        with store.begin_immediate():
            store.insert_autonomy_event(
                event_id="evt-1", motion_id="mot-1", task="outbound_content_publish",
                declaration_version=1, scope="publish:bluesky", event="motion_approved",
                from_position="hitl", to_position="hotl",
                evidence_ref="evidence/paa/" + "a" * 64 + "/evidence.json",
                evidence_sha256="a" * 64, actor="operator", reason="approve",
                created_at="2026-01-01T00:00:00.000000Z",
            )
            store.insert_autonomy_event(
                event_id="evt-2", motion_id="mot-1", task="outbound_content_publish",
                declaration_version=1, scope="publish:bluesky", event="position_changed",
                from_position="hitl", to_position="hotl",
                evidence_ref="evidence/paa/" + "a" * 64 + "/evidence.json",
                evidence_sha256="a" * 64, actor="operator", reason="approve",
                created_at="2026-01-01T00:00:00.000000Z",
            )
        assert len(store.get_autonomy_events_for_motion("mot-1")) == 2

    def test_a_failed_sibling_rolls_back_the_pair(self, store: ScoutEventStore) -> None:
        """An approval whose position_changed fails must not leave the
        motion_approved behind: an approval with no position change is
        indistinguishable from corruption after the fact."""
        with pytest.raises(sqlite3.IntegrityError), store.begin_immediate():
            store.insert_autonomy_event(
                event_id="evt-1", motion_id="mot-1", task="outbound_content_publish",
                declaration_version=1, scope="publish:bluesky", event="motion_approved",
                from_position="hitl", to_position="hotl",
                evidence_ref="evidence/paa/" + "a" * 64 + "/evidence.json",
                evidence_sha256="a" * 64, actor="operator", reason="approve",
                created_at="2026-01-01T00:00:00.000000Z",
            )
            # Duplicate (motion_id, event) — violates the unique index.
            store.insert_autonomy_event(
                event_id="evt-2", motion_id="mot-1", task="outbound_content_publish",
                declaration_version=1, scope="publish:bluesky", event="motion_approved",
                from_position="hitl", to_position="hotl",
                evidence_ref="evidence/paa/" + "a" * 64 + "/evidence.json",
                evidence_sha256="a" * 64, actor="operator", reason="approve",
                created_at="2026-01-01T00:00:00.000000Z",
            )
        assert store.get_autonomy_events_for_motion("mot-1") == []
