"""Tests for the shared scanner dedupe helper."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TypedDict

from scout.config import Message
from scout.platforms.dedupe import dedupe_and_filter


class Item(TypedDict):
    id: str
    text: str
    created_at: datetime | None


def _id_of(item: Item) -> str:
    return item["id"]


def _text_of(item: Item) -> str:
    return item["text"]


def _ts_of(item: Item) -> datetime | None:
    return item["created_at"]


def _to_message(item: Item, text: str, created_at: datetime) -> Message:
    return Message(
        platform="test",
        platform_id=item["id"],
        channel_name="test-channel",
        channel_id="test-channel",
        author_name="tester",
        author_id="tester",
        content=text,
        created_at=created_at,
        url="",
    )


def _item(item_id: str, text: str, created_at: datetime | None = None) -> Item:
    return {
        "id": item_id,
        "text": text,
        "created_at": created_at or datetime.now(UTC),
    }


def test_dedupes_by_id() -> None:
    seen_ids = {"a"}

    result = list(
        dedupe_and_filter(
            [_item("a", "first"), _item("b", "second")],
            id_of=_id_of,
            text_of=_text_of,
            created_at_of=_ts_of,
            to_message=_to_message,
            since=None,
            seen_ids=seen_ids,
        )
    )

    assert len(result) == 1
    assert result[0].platform_id == "b"


def test_identical_text_with_distinct_ids_both_survive() -> None:
    """Text is content, not identity — byte-identical text under different
    stable IDs (e.g. different authors) must not be merged."""
    seen_ids: set[str] = set()

    text = "Hello World " + "x" * 200
    result = list(
        dedupe_and_filter(
            [_item("a", text), _item("b", text.upper())],
            id_of=_id_of,
            text_of=_text_of,
            created_at_of=_ts_of,
            to_message=_to_message,
            since=None,
            seen_ids=seen_ids,
        )
    )

    assert len(result) == 2
    assert {m.platform_id for m in result} == {"a", "b"}


def test_skips_empty_text() -> None:
    seen_ids: set[str] = set()

    result = list(
        dedupe_and_filter(
            [_item("a", ""), _item("b", "   "), _item("c", "keep me")],
            id_of=_id_of,
            text_of=_text_of,
            created_at_of=_ts_of,
            to_message=_to_message,
            since=None,
            seen_ids=seen_ids,
        )
    )

    assert len(result) == 1
    assert result[0].content == "keep me"


def test_skips_empty_stable_id_without_fabricating_identity() -> None:
    seen_ids: set[str] = set()

    result = list(
        dedupe_and_filter(
            [_item("", "no id here"), _item("b", "has id")],
            id_of=_id_of,
            text_of=_text_of,
            created_at_of=_ts_of,
            to_message=_to_message,
            since=None,
            seen_ids=seen_ids,
        )
    )

    assert len(result) == 1
    assert result[0].platform_id == "b"
    assert "" not in seen_ids


def test_skips_malformed_timestamp_without_fabricating_now() -> None:
    seen_ids: set[str] = set()
    no_ts: Item = {"id": "a", "text": "bad ts", "created_at": None}

    result = list(
        dedupe_and_filter(
            [no_ts, _item("b", "good ts")],
            id_of=_id_of,
            text_of=_text_of,
            created_at_of=_ts_of,
            to_message=_to_message,
            since=None,
            seen_ids=seen_ids,
        )
    )

    assert len(result) == 1
    assert result[0].platform_id == "b"


def test_skips_items_before_since() -> None:
    seen_ids: set[str] = set()

    now = datetime.now(UTC)
    since = now - timedelta(hours=1)

    result = list(
        dedupe_and_filter(
            [
                _item("old", "old post", now - timedelta(hours=2)),
                _item("boundary", "boundary", since),  # equals since → excluded
                _item("new", "new post", now),
            ],
            id_of=_id_of,
            text_of=_text_of,
            created_at_of=_ts_of,
            to_message=_to_message,
            since=since,
            seen_ids=seen_ids,
        )
    )

    assert len(result) == 1
    assert result[0].platform_id == "new"


def test_skip_callback_invoked_and_respected() -> None:
    seen_ids: set[str] = set()
    skip_calls: list[tuple[str, str]] = []

    def _skip(item: Item, text: str) -> bool:
        skip_calls.append((item["id"], text))
        return item["id"] == "spam"

    result = list(
        dedupe_and_filter(
            [_item("spam", "spammy"), _item("ok", "legit")],
            id_of=_id_of,
            text_of=_text_of,
            created_at_of=_ts_of,
            to_message=_to_message,
            since=None,
            seen_ids=seen_ids,
            skip=_skip,
        )
    )

    assert skip_calls == [("spam", "spammy"), ("ok", "legit")]
    assert len(result) == 1
    assert result[0].platform_id == "ok"


def test_seen_ids_mutate_across_calls() -> None:
    seen_ids: set[str] = set()

    first_batch = list(
        dedupe_and_filter(
            [_item("a", "hello")],
            id_of=_id_of,
            text_of=_text_of,
            created_at_of=_ts_of,
            to_message=_to_message,
            since=None,
            seen_ids=seen_ids,
        )
    )
    assert len(first_batch) == 1
    assert "a" in seen_ids

    # Second batch: "a" repeats by id (dropped); "b" has different id and
    # identical text to "a" (kept — text is not identity); "c" is new.
    second_batch = list(
        dedupe_and_filter(
            [_item("a", "different"), _item("b", "hello"), _item("c", "fresh")],
            id_of=_id_of,
            text_of=_text_of,
            created_at_of=_ts_of,
            to_message=_to_message,
            since=None,
            seen_ids=seen_ids,
        )
    )

    assert [m.platform_id for m in second_batch] == ["b", "c"]
    assert seen_ids == {"a", "b", "c"}
