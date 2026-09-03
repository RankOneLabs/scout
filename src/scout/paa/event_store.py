"""Scout's ``EventStore`` implementation over its own database.

``paa_runtime`` ships ``SqliteEventStore``, and most consumers should use
it: the runtime owns its own database and nothing has to host a table to
adopt the package. Scout cannot, and the reason is specific rather than
incidental.

Scout's outbound publish path (since moved to a separate application)
re-resolved the autonomy position *inside* the claim transaction that
authorized a publish. That re-resolution was the authoritative one — the
earlier informational resolve was advisory, and a position that moved in
between had to invalidate the claim rather than be read from a snapshot
taken before the lock. A runtime-owned database cannot offer that: the
governed effect and the position read authorizing it would sit in two
lock domains, and no amount of ordering makes two SQLite files commit
atomically. The inbound surfacing task inherits the same requirement
when its declaration leaves shadow deployment.

``paa_runtime.store`` names exactly this consumer as the reason its
protocol stays small — "one whose governed effect and the position read
authorizing it must commit in a single lock domain". Scout is that
consumer, so it keeps ``autonomy_events`` in ``scout.storage.db`` and implements
the seven-method protocol against it.

The protocol's method names were extracted from ``StateManager`` in the
first place, so this adapter is thin by construction: it delegates every
call and converts ``sqlite3.Row`` to ``AutonomyEvent``. That conversion
is not incidental — the protocol requires implementations never to leak
their storage row past this boundary, which is what keeps the runtime
free to change how Scout stores events without changing what it reads.
"""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING

from paa_runtime.events import CURRENT_EVENT_SCHEMA, AutonomyEventType, AutonomyPosition
from paa_runtime.store import AutonomyEvent, EventStore

if TYPE_CHECKING:
    from scout.storage.state import StateManager

__all__ = ["ScoutEventStore"]


def _to_event(row: sqlite3.Row) -> AutonomyEvent:
    """One storage row as the contract's fourteen-field representation."""
    return AutonomyEvent(
        event_schema=row["event_schema"],
        id=row["id"],
        motion_id=row["motion_id"],
        task=row["task"],
        declaration_version=row["declaration_version"],
        scope=row["scope"],
        event=row["event"],
        from_position=row["from_position"],
        to_position=row["to_position"],
        evidence_ref=row["evidence_ref"],
        evidence_sha256=row["evidence_sha256"],
        actor=row["actor"],
        reason=row["reason"],
        created_at=row["created_at"],
    )


class ScoutEventStore:
    """``paa_runtime.store.EventStore`` backed by Scout's ``autonomy_events``.

    Holds no SQL of its own. ``StateManager`` owns the table, its schema,
    and its queries — including the ``scope IS ?`` comparison that makes
    null-scope declarations resolvable — because that table lives in the
    same database and under the same migration path as everything else
    Scout persists.

    All five load-bearing protocol semantics are satisfied by the
    underlying storage rather than by this class:

    1. **Transaction boundary** — ``transaction()`` and
       ``begin_immediate()`` delegate to ``Db``, and inserts rely on the
       caller holding one.
    2. **Ordering** — ``(created_at, id)`` tuple comparison lives in the
       ``StateManager`` queries.
    3. **Uniqueness** — ``autonomy_events_motion_event_unique``.
    4. **Append-only** — the BEFORE UPDATE/DELETE triggers, which abort
       regardless of which code path issues the statement.
    5. **No nesting required** — Scout's ``Db`` contexts happen to be
       reentrant, which the protocol permits but does not require.
    """

    def __init__(self, state: StateManager) -> None:
        self._state = state

    def transaction(self) -> AbstractContextManager[None]:
        return self._state.db.transaction()

    def begin_immediate(self) -> AbstractContextManager[None]:
        return self._state.db.begin_immediate()

    def insert_autonomy_event(
        self,
        *,
        event_id: str,
        motion_id: str,
        task: str,
        declaration_version: int,
        scope: str | None,
        event: AutonomyEventType,
        from_position: AutonomyPosition,
        to_position: AutonomyPosition,
        evidence_ref: str,
        evidence_sha256: str,
        actor: str,
        reason: str,
        created_at: str,
        event_schema: str = CURRENT_EVENT_SCHEMA,
    ) -> None:
        self._state.insert_autonomy_event(
            event_id=event_id,
            motion_id=motion_id,
            task=task,
            declaration_version=declaration_version,
            scope=scope,
            event=event,
            from_position=from_position,
            to_position=to_position,
            evidence_ref=evidence_ref,
            evidence_sha256=evidence_sha256,
            actor=actor,
            reason=reason,
            created_at=created_at,
            event_schema=event_schema,
        )

    def get_autonomy_events_for_motion(self, motion_id: str) -> list[AutonomyEvent]:
        return [_to_event(row) for row in self._state.get_autonomy_events_for_motion(motion_id)]

    def get_latest_position_changed_event(
        self,
        *,
        task: str,
        declaration_version: int,
        scope: str | None,
    ) -> AutonomyEvent | None:
        row = self._state.get_latest_position_changed_event(
            task=task, declaration_version=declaration_version, scope=scope,
        )
        return _to_event(row) if row is not None else None

    def get_position_changed_event_before(
        self,
        *,
        task: str,
        declaration_version: int,
        scope: str | None,
        created_at: str,
        event_id: str,
    ) -> AutonomyEvent | None:
        row = self._state.get_position_changed_event_before(
            task=task,
            declaration_version=declaration_version,
            scope=scope,
            created_at=created_at,
            event_id=event_id,
        )
        return _to_event(row) if row is not None else None

    def get_autonomy_events(self, *, task: str | None = None) -> list[AutonomyEvent]:
        return [_to_event(row) for row in self._state.get_autonomy_events(task=task)]


if TYPE_CHECKING:
    # Structural conformance, checked statically and costing nothing at
    # runtime. EventStore is a plain Protocol, so nothing else forces this
    # class to keep matching it — without this, a signature drifting out of
    # step with a future paa-runtime bump would surface as a failure deep
    # inside the service layer rather than here.
    def _assert_implements_protocol(store: ScoutEventStore) -> EventStore:
        return store
