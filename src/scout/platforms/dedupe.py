"""Shared dedupe + since-filter + convert logic for platform scanners."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime

from scout.config import Message

logger = logging.getLogger(__name__)


def dedupe_and_filter[Item](
    items: Iterable[Item],
    *,
    id_of: Callable[[Item], str],
    text_of: Callable[[Item], str],
    created_at_of: Callable[[Item], datetime | None],
    to_message: Callable[[Item, str, datetime], Message],
    since: datetime | None,
    seen_ids: set[str],
    skip: Callable[[Item, str], bool] | None = None,
) -> Iterator[Message]:
    """Convert raw items to Messages, deduplicating solely on the platform stable ID.

    Text is content, not identity: distinct items with identical text but
    different stable IDs both survive. An empty stable ID is never assigned a
    fallback identity — it is skipped and logged as malformed. A None from
    created_at_of signals an unparseable/missing/naive platform timestamp;
    that item is skipped rather than assigned the current time.

    Mutates seen_ids so callers can dedupe across batches (and across query/
    language pairs, in the Bluesky search case).
    """
    for item in items:
        item_id = id_of(item)
        if not item_id:
            logger.warning("Skipping item with empty platform stable ID")
            continue
        if item_id in seen_ids:
            continue
        # Register as seen before the text/since/skip filters so a later page
        # carrying the same id doesn't get reprocessed just because this one
        # happened to be filtered out.
        seen_ids.add(item_id)
        text = text_of(item).strip()
        if not text:
            continue
        created_at = created_at_of(item)
        if created_at is None:
            continue
        if since is not None and created_at <= since:
            continue
        if skip is not None and skip(item, text):
            continue
        yield to_message(item, text, created_at)
