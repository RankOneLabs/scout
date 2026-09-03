"""Post aggregate: durable record of every fetched message. Owns the
`posts` table.

`load_unevaluated_posts` reads `evaluations` and `blocked_authors` (owned by
EvaluationStore and ScanStore respectively) as a read-only join — safe
without any cross-store coordination since it never writes. See
`unit_of_work.py` for why every store shares one connection regardless.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

from scout.config import Message, SourceAuthor, SourceParent
from scout.storage.unit_of_work import UnitOfWork

logger = logging.getLogger(__name__)


class PostStore:
    """Owns post persistence and retrieval."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    @property
    def _conn(self) -> sqlite3.Connection:
        return self._uow.conn

    def has_seen_message(self, platform: str, platform_msg_id: str) -> bool:
        """Check if we've already processed this message."""
        row = self._conn.execute(
            "SELECT 1 FROM posts WHERE platform = ? AND platform_msg_id = ?",
            (platform, platform_msg_id),
        ).fetchone()
        return row is not None

    def save_post(self, msg: Message, scan_id: int) -> int:
        """Save a post, returning its row ID.

        On duplicate, updates parent columns when the new lookup is resolved and
        the stored row is not — never downgrades a resolved parent to failed.
        """
        parent = msg.parent
        parent_id = parent.id if parent else None
        parent_author_id = parent.author.id if parent else None
        parent_author_name = parent.author.name if parent else None
        parent_text = parent.text if parent else None
        parent_url = parent.url if parent else None

        with self._uow.begin():
            try:
                cursor = self._conn.execute(
                    "INSERT INTO posts "
                    "(platform, platform_msg_id, channel_name, channel_id, "
                    "author_name, author_id, content, url, created_at, scan_id, "
                    "parent_lookup_status, parent_id, parent_author_id, "
                    "parent_author_name, parent_text, parent_url) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        msg.platform,
                        msg.platform_id,
                        msg.channel_name,
                        msg.channel_id,
                        msg.author_name,
                        msg.author_id,
                        msg.content,
                        msg.url,
                        msg.created_at.isoformat(),
                        scan_id,
                        msg.parent_lookup_status,
                        parent_id,
                        parent_author_id,
                        parent_author_name,
                        parent_text,
                        parent_url,
                    ),
                )
                assert cursor.lastrowid is not None
                return cursor.lastrowid
            except sqlite3.IntegrityError:
                row = self._conn.execute(
                    "SELECT id, parent_lookup_status FROM posts "
                    "WHERE platform = ? AND platform_msg_id = ?",
                    (msg.platform, msg.platform_id),
                ).fetchone()
                assert row is not None
                existing_id = int(row["id"])
                # Heal: upgrade to resolved only; never downgrade.
                if (
                    msg.parent_lookup_status == "resolved"
                    and msg.parent is not None
                    and row["parent_lookup_status"] != "resolved"
                ):
                    self._conn.execute(
                        "UPDATE posts SET parent_lookup_status = ?, parent_id = ?, "
                        "parent_author_id = ?, parent_author_name = ?, "
                        "parent_text = ?, parent_url = ? "
                        "WHERE id = ?",
                        (
                            "resolved",
                            parent_id,
                            parent_author_id,
                            parent_author_name,
                            parent_text,
                            parent_url,
                            existing_id,
                        ),
                    )
                return existing_id

    def _row_to_message(self, row: sqlite3.Row) -> Message:
        """Convert a DB row to a Message."""
        try:
            created_at = datetime.fromisoformat(row["created_at"])
        except (ValueError, TypeError):
            created_at = datetime.now(UTC)

        keys = row.keys() if hasattr(row, "keys") else []
        parent: SourceParent | None = None
        parent_lookup_status = "not_applicable"
        if "parent_lookup_status" in keys:
            parent_lookup_status = row["parent_lookup_status"] or "not_applicable"
            if (
                parent_lookup_status == "resolved"
                and row["parent_id"] is not None
                and row["parent_author_id"] is not None
                and row["parent_text"] is not None
            ):
                parent = SourceParent(
                    id=row["parent_id"],
                    author=SourceAuthor(
                        id=row["parent_author_id"],
                        name=row["parent_author_name"] or "",
                    ),
                    text=row["parent_text"],
                    url=row["parent_url"] or "",
                )

        return Message(
            platform=row["platform"],
            platform_id=row["platform_msg_id"],
            channel_name=row["channel_name"] or "",
            channel_id=row["channel_id"] or "",
            author_name=row["author_name"] or "unknown",
            author_id=row["author_id"] or "",
            content=row["content"] or "",
            created_at=created_at,
            url=row["url"] or "",
            parent=parent,
            parent_lookup_status=parent_lookup_status,
        )

    def load_posts(self, scan_id: int | None = None) -> list[Message]:
        """Load posts from the DB as Message objects.

        Args:
            scan_id: If set, only load posts from that scan. Otherwise loads all.
        """
        if scan_id:
            rows = self._conn.execute(
                "SELECT * FROM posts WHERE scan_id = ? ORDER BY created_at DESC",
                (scan_id,),
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM posts ORDER BY created_at DESC").fetchall()

        messages = [self._row_to_message(row) for row in rows]
        logger.info(
            "Loaded %d posts from DB%s",
            len(messages),
            f" (scan #{scan_id})" if scan_id else "",
        )
        return messages

    def load_post(self, post_id: int) -> Message | None:
        """Load one persisted post as the pipeline's Message value."""
        row = self._conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        return self._row_to_message(row) if row is not None else None

    def load_unevaluated_posts(self, scan_id: int | None = None) -> list[Message]:
        """Load posts that have no evaluation yet (e.g. from a failed scan).

        Args:
            scan_id: If set, only load from that scan. Otherwise loads all.
        """
        if scan_id:
            query = (
                "SELECT p.* FROM posts p "
                "WHERE p.scan_id = ? AND NOT EXISTS ("
                "SELECT 1 FROM evaluations e WHERE e.post_id = p.id) "
                "AND NOT EXISTS ("
                "SELECT 1 FROM blocked_authors b "
                "WHERE b.platform = p.platform AND b.author_id = p.author_id "
                "AND b.active = 1)"
            )
            params: tuple[int, ...] = (scan_id,)
        else:
            query = (
                "SELECT p.* FROM posts p "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM evaluations e WHERE e.post_id = p.id) "
                "AND NOT EXISTS ("
                "SELECT 1 FROM blocked_authors b "
                "WHERE b.platform = p.platform AND b.author_id = p.author_id "
                "AND b.active = 1)"
            )
            params = ()
        query += " ORDER BY p.created_at DESC"

        rows = self._conn.execute(query, params).fetchall()
        messages = [self._row_to_message(row) for row in rows]
        logger.info(
            "Loaded %d unevaluated posts from DB%s",
            len(messages),
            f" (scan #{scan_id})" if scan_id else "",
        )
        return messages

    def count_posts(self) -> int:
        """Total number of posts ever recorded — one leg of ScanStats."""
        return int(self._conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0])
