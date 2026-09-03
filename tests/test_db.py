"""Tests for db.Db: connection ownership and transaction mechanics."""

from __future__ import annotations

import sqlite3

import pytest

from scout.storage.db import Db, TransactionError, TransactionModeError


@pytest.fixture
def db() -> Db:
    d = Db(":memory:")
    d.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    d.commit()
    return d


def _values(db: Db) -> list[str]:
    return [row["v"] for row in db.execute("SELECT v FROM t ORDER BY id")]


class TestConnectionSetup:
    def test_row_factory_is_sqlite_row(self, db: Db) -> None:
        assert db.conn.row_factory is sqlite3.Row

    def test_foreign_keys_on_by_default(self, db: Db) -> None:
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_foreign_keys_can_start_off(self) -> None:
        d = Db(":memory:", foreign_keys=False)
        assert d.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        d.close()

    def test_set_foreign_keys_toggles_live(self, db: Db) -> None:
        db.set_foreign_keys(False)
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        db.set_foreign_keys(True)
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_wal_mode(self, tmp_path) -> None:
        d = Db(str(tmp_path / "wal.db"))
        assert d.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        d.close()

    def test_synchronous_normal(self, db: Db) -> None:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 1


class TestRootTransaction:
    def test_success_commits(self, db: Db) -> None:
        with db.transaction():
            db.execute("INSERT INTO t (v) VALUES ('a')")
        assert _values(db) == ["a"]
        assert not db.in_transaction

    def test_exception_rolls_back_and_reraises(self, db: Db) -> None:
        with pytest.raises(ValueError, match="boom"), db.transaction():
            db.execute("INSERT INTO t (v) VALUES ('a')")
            raise ValueError("boom")
        assert _values(db) == []
        assert not db.in_transaction

    def test_root_flushes_caller_left_implicit_transaction(self, db: Db) -> None:
        # A raw execute outside any Db context leaves SQLite's implicit
        # transaction open; a root transaction() must flush it rather than
        # nesting an illegal BEGIN inside it.
        db.execute("INSERT INTO t (v) VALUES ('pending')")
        assert db.conn.in_transaction
        with db.transaction():
            db.execute("INSERT INTO t (v) VALUES ('in-root')")
        assert _values(db) == ["pending", "in-root"]

    def test_begin_immediate_acquires_a_write_lock(self, tmp_path) -> None:
        db_path = str(tmp_path / "immediate.db")
        writer = Db(db_path)
        writer.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        writer.commit()

        reader = Db(db_path)
        with writer.begin_immediate():
            writer.execute("INSERT INTO t (v) VALUES ('locked')")
            # A second connection cannot also acquire an immediate/write
            # lock while writer's transaction is open.
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                reader.execute("BEGIN IMMEDIATE")
        reader.close()
        writer.close()


class TestNestedTransaction:
    def test_nested_success_joins_via_savepoint(self, db: Db) -> None:
        with db.transaction():
            db.execute("INSERT INTO t (v) VALUES ('outer')")
            with db.transaction():
                db.execute("INSERT INTO t (v) VALUES ('inner')")
            assert db.in_transaction  # still inside the outer root
        assert _values(db) == ["outer", "inner"]

    def test_nested_failure_rolls_back_only_the_inner_savepoint(self, db: Db) -> None:
        with db.transaction():
            db.execute("INSERT INTO t (v) VALUES ('outer')")
            with pytest.raises(ValueError, match="inner boom"), db.transaction():
                db.execute("INSERT INTO t (v) VALUES ('inner')")
                raise ValueError("inner boom")
            # The outer root is still open and uncorrupted after the inner
            # savepoint rolled back.
            assert db.in_transaction
        assert _values(db) == ["outer"]

    def test_outer_failure_after_nested_success_rolls_back_both(self, db: Db) -> None:
        with pytest.raises(ValueError, match="outer boom"), db.transaction():
            db.execute("INSERT INTO t (v) VALUES ('outer')")
            with db.transaction():
                db.execute("INSERT INTO t (v) VALUES ('inner')")
            raise ValueError("outer boom")
        assert _values(db) == []

    def test_nested_begin_immediate_under_immediate_root_joins_via_savepoint(
        self, db: Db
    ) -> None:
        with db.begin_immediate(), db.begin_immediate():
            db.execute("INSERT INTO t (v) VALUES ('a')")
        assert _values(db) == ["a"]

    def test_nested_begin_immediate_under_deferred_root_raises(self, db: Db) -> None:
        with db.transaction():
            with pytest.raises(TransactionModeError, match="deferred"):  # noqa: SIM117
                with db.begin_immediate():
                    db.execute("INSERT INTO t (v) VALUES ('a')")
            # The outer deferred root survives the rejected nested call.
            assert db.in_transaction
            db.execute("INSERT INTO t (v) VALUES ('outer')")
        assert _values(db) == ["outer"]


class TestReadTransaction:
    def test_read_transaction_sees_committed_data_and_refuses_writes(self, db: Db) -> None:
        with db.transaction():
            db.execute("INSERT INTO t (v) VALUES ('a')")
        with db.read_transaction():
            assert _values(db) == ["a"]
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                db.execute("INSERT INTO t (v) VALUES ('b')")
        assert not db.in_transaction
        assert _values(db) == ["a"]

    def test_read_transaction_always_ends_with_rollback(self, db: Db, tmp_path) -> None:
        # Even a write that somehow lands (e.g. a second connection) must
        # not be observable as committed by this Db: read_transaction never
        # commits, only rolls back.
        with db.transaction():
            db.execute("INSERT INTO t (v) VALUES ('a')")
        with db.read_transaction():
            pass
        assert not db.conn.in_transaction

    def test_read_transaction_restores_prior_query_only_setting(self, db: Db) -> None:
        assert db.execute("PRAGMA query_only").fetchone()[0] == 0
        with db.read_transaction():
            assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        assert db.execute("PRAGMA query_only").fetchone()[0] == 0

    def test_read_transaction_restores_query_only_on_exception(self, db: Db) -> None:
        with pytest.raises(ValueError, match="boom"), db.read_transaction():
            raise ValueError("boom")
        assert db.execute("PRAGMA query_only").fetchone()[0] == 0
        assert not db.in_transaction

    def test_read_transaction_restores_query_only_on_cancellation(self, db: Db) -> None:
        class _Cancelled(BaseException):
            pass

        with pytest.raises(_Cancelled), db.read_transaction():
            raise _Cancelled()
        assert db.execute("PRAGMA query_only").fetchone()[0] == 0
        assert not db.in_transaction

    def test_read_transaction_refuses_to_nest(self, db: Db) -> None:
        with (
            db.transaction(),
            pytest.raises(TransactionModeError, match="nest"),
            db.read_transaction(),
        ):
            pass

    def test_read_transaction_refuses_nested_transaction_beneath_it(self, db: Db) -> None:
        with db.read_transaction():  # noqa: SIM117
            with pytest.raises(TransactionModeError, match="read_transaction"):
                with db.transaction():
                    pass

    def test_read_transaction_refuses_nested_begin_immediate_beneath_it(self, db: Db) -> None:
        with db.read_transaction():  # noqa: SIM117
            with pytest.raises(TransactionModeError, match="read_transaction"):
                with db.begin_immediate():
                    pass

    def test_nested_read_transaction_refused(self, db: Db) -> None:
        with db.read_transaction():  # noqa: SIM117
            with pytest.raises(TransactionModeError, match="nest"):
                with db.read_transaction():
                    pass


class TestClose:
    def test_close_refuses_while_managed_transaction_active(self, db: Db) -> None:
        cm = db.transaction()
        cm.__enter__()
        with pytest.raises(TransactionError, match="transaction"):
            db.close()
        cm.__exit__(None, None, None)
        db.close()

    def test_close_refuses_while_read_transaction_active(self, db: Db) -> None:
        cm = db.read_transaction()
        cm.__enter__()
        with pytest.raises(TransactionError, match="transaction"):
            db.close()
        cm.__exit__(None, None, None)
        db.close()

    def test_close_refuses_unmanaged_pending_transaction(self, db: Db) -> None:
        db.execute("INSERT INTO t (v) VALUES ('a')")
        with pytest.raises(TransactionError, match="unmanaged"):
            db.close()
        db.rollback()
        db.close()

    def test_close_succeeds_with_nothing_pending(self, db: Db) -> None:
        db.close()
