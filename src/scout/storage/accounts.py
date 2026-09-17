"""Typed persistence for dated account profile snapshots."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from scout.config import Account
from scout.storage.unit_of_work import UnitOfWork


class AccountStore:
    """Owns idempotent writes and latest-snapshot reads for accounts."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    @property
    def _conn(self) -> sqlite3.Connection:
        return self._uow.conn

    def record_account_snapshot(self, account: Account) -> bool:
        """Record one observed account state; return whether a row was inserted."""
        if account.observed_at is None:
            raise ValueError("account.observed_at is required for a snapshot")
        with self._uow.begin():
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO accounts "
                "(platform, account_id, observed_at, name, handle, bio, followers, "
                "following, posts, created_at, verified) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    account.platform,
                    account.id,
                    account.observed_at.isoformat(),
                    account.name,
                    account.handle,
                    account.bio,
                    account.followers,
                    account.following,
                    account.posts,
                    account.created_at.isoformat() if account.created_at else None,
                    int(account.verified) if account.verified is not None else None,
                ),
            )
        return cursor.rowcount == 1

    def latest_account(self, platform: str, account_id: str) -> Account | None:
        """Return the most recently observed snapshot for an account."""
        row = self._conn.execute(
            "SELECT platform, account_id, name, handle, bio, followers, following, "
            "posts, created_at, verified, observed_at FROM accounts "
            "WHERE platform = ? AND account_id = ? ORDER BY observed_at DESC LIMIT 1",
            (platform, account_id),
        ).fetchone()
        if row is None:
            return None
        return Account(
            platform=row["platform"],
            id=row["account_id"],
            name=row["name"],
            handle=row["handle"],
            bio=row["bio"],
            followers=row["followers"],
            following=row["following"],
            posts=row["posts"],
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
            verified=bool(row["verified"]) if row["verified"] is not None else None,
            observed_at=datetime.fromisoformat(row["observed_at"]),
        )
