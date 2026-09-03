"""Explicit shared-connection transaction boundary for aggregate stores.

`StateManager` owns exactly one `db.Db` connection and wraps it in exactly
one `UnitOfWork`, then constructs every aggregate store (`ScanStore`,
`PostStore`, `EvaluationStore`, `GradeStore`, `RegistryStore`) with that
same `UnitOfWork` instance — never a fresh `Db` or a raw `sqlite3.Connection`
of their own. This is what keeps a write spanning more than one store
atomic: the one production case is `grading/promotion.py` composing
an `EvaluationStore` write (`persist_terminal_outcome`/
`persist_surfaced_outcome`, via `StateManager`) with a `GradeStore` write
(`complete_human_positive_promotion`) inside one
`state.db.begin_immediate()` block. Because both stores read/write through
the same `Db`, `Db`'s reentrant transaction nesting (root BEGIN IMMEDIATE,
everything opened underneath joins as a SAVEPOINT) makes that block one
atomic unit with no special-casing in either store — see
docs/transactions-and-scan-durability.md for the full contract this
extends.

`UnitOfWork` itself does not add new transaction semantics; it exists so
"a store never opens its own connection" is enforced by what stores are
constructible with, not just by convention.
"""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager

from scout.storage.db import Db


class UnitOfWork:
    """Shared transaction-mechanics handle every aggregate store is
    constructed with, wrapping the one `Db` connection `StateManager` owns."""

    def __init__(self, db: Db) -> None:
        self._db = db

    @property
    def db(self) -> Db:
        return self._db

    @property
    def conn(self) -> sqlite3.Connection:
        return self._db.conn

    @property
    def in_transaction(self) -> bool:
        return self._db.in_transaction

    def begin(self) -> AbstractContextManager[None]:
        """Deferred (BEGIN) transaction — see `Db.transaction`."""
        return self._db.transaction()

    def begin_immediate(self) -> AbstractContextManager[None]:
        """Immediate (BEGIN IMMEDIATE) transaction — see `Db.begin_immediate`."""
        return self._db.begin_immediate()

    def read(self) -> AbstractContextManager[None]:
        """Root-only read-only snapshot — see `Db.read_transaction`."""
        return self._db.read_transaction()
