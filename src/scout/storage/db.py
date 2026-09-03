"""SQLite connection ownership and transaction mechanics.

Db owns exactly one sqlite3 connection: connect/PRAGMA setup and
exception-safe transactions. StateManager remains the domain facade;
Db knows nothing about schema, migrations, or scout's tables.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Literal

RootMode = Literal["deferred", "immediate", "read"]


class TransactionError(RuntimeError):
    """Raised when a transaction-related operation would leave Db in an
    unsafe or ambiguous state, e.g. closing while a transaction is active."""


class TransactionModeError(TransactionError):
    """Raised when a transaction context is requested in a way that would
    misrepresent the locking guarantee it implies: begin_immediate()
    nested beneath a deferred or read root (a savepoint cannot
    retroactively promote an already-open transaction to immediate
    locking), or any transaction()/begin_immediate()/read_transaction()
    call nested beneath an active read_transaction() root."""


class Db:
    """Owns a single sqlite3 connection: connect, Row factory, PRAGMAs, and
    exception-safe deferred/immediate/read transactions.

    ``transaction()`` and ``begin_immediate()`` are reentrant: the first
    (outermost) call on a given Db opens a root transaction — BEGIN or
    BEGIN IMMEDIATE respectively — that commits on clean exit and rolls
    back on any exception, including cancellation. Any call made while
    already inside one instead opens a uniquely named SAVEPOINT, so
    composed workflows can call composable helpers that each open their
    own context without either nesting an illegal BEGIN or accidentally
    committing the outer unit.

    ``begin_immediate()`` nested beneath a deferred or read root raises
    ``TransactionModeError`` instead of silently downgrading to a
    savepoint: a savepoint cannot retroactively promote an already-open
    transaction to immediate locking, so composing it that way would
    misrepresent the guarantee the caller asked for.

    ``read_transaction()`` is a separate, root-only mode: see its
    docstring.
    """

    def __init__(self, db_path: str, *, foreign_keys: bool = True) -> None:
        self.db_path = db_path
        self.conn: sqlite3.Connection = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.set_foreign_keys(foreign_keys)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.commit()
        self._depth = 0
        self._root_mode: RootMode | None = None

    @property
    def in_transaction(self) -> bool:
        """True while a transaction() / begin_immediate() / read_transaction()
        context (root or nested) opened through this Db is active."""
        return self._depth > 0

    def set_foreign_keys(self, enabled: bool) -> None:
        self.conn.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, parameters)

    def executemany(self, sql: str, seq_of_parameters: Sequence[Sequence[Any]]) -> sqlite3.Cursor:
        return self.conn.executemany(sql, seq_of_parameters)

    def executescript(self, script: str) -> sqlite3.Cursor:
        return self.conn.executescript(script)

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def close(self) -> None:
        """Close the connection.

        Refuses to close while a transaction() / begin_immediate() /
        read_transaction() context is still active (a bug in the caller —
        the context was never exited) or while SQLite has an unmanaged
        implicit transaction pending (a caller wrote outside any Db
        context and never called commit()). Either case would otherwise
        close silently and discard the pending write or leak a lock;
        raising makes the missing boundary an actionable error instead of
        silent data loss.
        """
        if self.in_transaction:
            raise TransactionError(
                "Db.close() called while a transaction()/begin_immediate()/"
                "read_transaction() context is still active"
            )
        if self.conn.in_transaction:
            raise TransactionError(
                "Db.close() called with an unmanaged pending transaction; "
                "commit() or rollback() it first"
            )
        self.conn.close()

    def transaction(self) -> AbstractContextManager[None]:
        """Deferred (BEGIN) transaction context. See class docstring for
        root-vs-nested semantics."""
        return self._transaction_context(mode="deferred")

    def begin_immediate(self) -> AbstractContextManager[None]:
        """Immediate (BEGIN IMMEDIATE) transaction context. See class
        docstring for root-vs-nested semantics and the deferred/read
        nesting restriction."""
        return self._transaction_context(mode="immediate")

    def read_transaction(self) -> AbstractContextManager[None]:
        """Root-only, mechanically read-only stable snapshot.

        Enables SQLite's ``query_only`` pragma for the duration (so any
        write attempted inside — directly or through a nested context —
        fails rather than silently succeeding), opens a deferred
        transaction to pin one consistent view across multiple queries,
        and always ends the transaction with ``ROLLBACK`` — there is
        nothing to commit, and rollback is what releases the read lock
        under ``BaseException`` (including cancellation) too. Restores
        the prior ``query_only`` setting on exit, even on error.

        Raises ``TransactionModeError`` if called while already inside
        any transaction() / begin_immediate() / read_transaction()
        context on this Db — a read snapshot's consistency guarantee
        cannot be honestly layered onto an already-open transaction of
        unknown mode, and it must never appear to compose with writes.
        """
        return self._read_transaction_context()

    @contextmanager
    def _read_transaction_context(self) -> Iterator[None]:
        if self._depth != 0:
            raise TransactionModeError(
                "Db.read_transaction() cannot nest inside an existing "
                f"transaction context (current root mode: {self._root_mode!r})"
            )
        prior_query_only = bool(self.conn.execute("PRAGMA query_only").fetchone()[0])
        if self.conn.in_transaction:
            self.conn.commit()
        self.conn.execute("PRAGMA query_only = ON")
        self._root_mode = "read"
        try:
            self.conn.execute("BEGIN")
            self._depth = 1
            try:
                yield
            finally:
                self.conn.rollback()
                self._depth = 0
        finally:
            self.conn.execute(f"PRAGMA query_only = {'ON' if prior_query_only else 'OFF'}")
            self._root_mode = None

    @contextmanager
    def _transaction_context(self, *, mode: Literal["deferred", "immediate"]) -> Iterator[None]:
        if self._depth == 0:
            # A caller may have left SQLite's implicit transaction open by
            # executing statements outside any Db context. Flush it first
            # so BEGIN below never nests inside it.
            if self.conn.in_transaction:
                self.conn.commit()
            self.conn.execute("BEGIN IMMEDIATE" if mode == "immediate" else "BEGIN")
            self._depth = 1
            self._root_mode = mode
            try:
                yield
            except BaseException:
                self.conn.rollback()
                self._depth = 0
                self._root_mode = None
                raise
            else:
                self.conn.commit()
                self._depth = 0
                self._root_mode = None
        else:
            if self._root_mode == "read":
                raise TransactionModeError(
                    "cannot open a transaction()/begin_immediate() context "
                    "nested beneath an active read_transaction() root"
                )
            if mode == "immediate" and self._root_mode != "immediate":
                raise TransactionModeError(
                    "Db.begin_immediate() cannot nest beneath a "
                    f"{self._root_mode!r} root — it cannot retroactively "
                    "upgrade the root transaction's lock"
                )
            savepoint = f"db_sp_{uuid.uuid4().hex}"
            self.conn.execute(f"SAVEPOINT {savepoint}")
            self._depth += 1
            try:
                yield
            except BaseException:
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                self._depth -= 1
                raise
            else:
                self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                self._depth -= 1
