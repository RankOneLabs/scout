"""Population export transforms and stable JSON-lines rendering."""

from __future__ import annotations

import dataclasses
import json

import pytest

from scout.grading.feedback import GradePopulationRow
from scout.grading.snapshots import (
    FrozenGradeInput,
    RecordedEvaluation,
    RecordedExposure,
    RecordedPost,
)
from scout.replay.population_export import (
    PopulationExportRecord,
    load_live_population,
    record_from_frozen_input,
    record_from_held_row,
    render_population_jsonl,
)
from scout.scanning.author_class import handle_from_post_url
from scout.storage.grades import GradeRevision
from scout.storage.state import StateManager


@pytest.mark.parametrize(
    ("platform", "url", "expected"),
    [
        (
            "bluesky",
            "https://bsky.app/profile/alice.bsky.social/post/3abc",
            "alice.bsky.social",
        ),
        ("farcaster", "https://warpcast.com/alice/0xabc", "alice"),
        ("farcaster", "https://farcaster.xyz/@bob/0xdef", "bob"),
        ("discord", "https://discord.com/channels/1/2/3", None),
    ],
)
def test_handle_from_post_url(platform: str, url: str, expected: str | None) -> None:
    assert handle_from_post_url(platform, url) == expected


def _frozen_input(evaluation_id: int, *, author: str) -> FrozenGradeInput:
    grade = GradePopulationRow(
        grade_id=evaluation_id + 100,
        post_id=evaluation_id + 200,
        scan_id=1,
        graded_at="2026-09-01T00:00:00.000Z",
        schema_version=3,
        needs_regrade=False,
        relevance_judgment="correct",
        action_judgment=None,
        dimensions=None,
        failure_note=None,
        factual_disposition=None,
        factual_offending_claim=None,
        factual_contradicting_evidence=None,
        context_missing_input=None,
        posture_should_have_been=None,
        implication_implied_claim=None,
        implication_missing_support=None,
        platform="bluesky",
        evaluation_id=evaluation_id,
        evaluation_post_id=evaluation_id + 200,
        evaluation_scan_id=1,
        evaluation_relevant=1,
        evaluation_project_key="agent-ops",
        evaluation_dossier_summary_id=None,
        evaluation_dossier_revision=None,
        evaluation_posture=None,
        draft_comment_id=None,
        draft_project_key=None,
        draft_dossier_summary_id=None,
        draft_dossier_revision=None,
        draft_posture=None,
        override_mode="auto",
        override_reason=None,
        pinned_revision_id=evaluation_id + 300,
        pinned_revision_number=1,
    )
    post = RecordedPost(
        id=evaluation_id + 200,
        platform="bluesky",
        platform_msg_id=f"post-{evaluation_id}",
        channel_name="research",
        channel_id="research",
        author_name=author,
        author_id=f"did:plc:{author}",
        content=f"text {evaluation_id}",
        url=f"https://bsky.app/profile/{author}/post/{evaluation_id}",
        created_at=None,
        scan_id=1,
        parent_lookup_status="resolved",
        parent_id="parent",
        parent_author_id="did:plc:parent",
        parent_author_name="Parent",
        parent_text="Parent text",
        parent_url=None,
    )
    evaluation = RecordedEvaluation(
        id=evaluation_id,
        post_id=post.id,
        relevant=1,
        score=0.75,
        reason=None,
        relevant_to=None,
        keyword_route_id=None,
        scan_id=1,
        created_at=None,
        project_key="agent-ops",
        posture=None,
        surface_status="surfaced",
        failure_reason=None,
        dossier_summary_id=None,
        dossier_revision=None,
    )
    return FrozenGradeInput(
        grade=grade,
        revision=GradeRevision(
            id=evaluation_id + 300,
            grade_id=grade.grade_id,
            evaluation_id=evaluation_id,
            revision=1,
            schema_version=3,
            source="web",
            payload="{}",
            recorded_at="2026-09-01T00:00:00.000Z",
        ),
        revision_matches=True,
        post=post,
        evaluation=evaluation,
        phase_runs=(),
        context=None,
        exposures=(
            RecordedExposure(
                snapshot_id=11,
                scan_id=1,
                phase="relevance",
                grade_revision_id=evaluation_id + 300,
                role="selected",
            ),
        ),
    )


def test_frozen_transform_has_exact_contract() -> None:
    record = record_from_frozen_input(_frozen_input(20, author="alice.bsky.social"))

    assert list(record.model_dump()) == [
        "evaluation_id",
        "platform",
        "channel",
        "url",
        "text",
        "parent_author_name",
        "parent_text",
        "author_name",
        "author_handle",
        "snapshot_id",
        "human_label",
        "production_score",
        "production_decision",
        "production_action",
    ]
    assert record.author_handle == "alice.bsky.social"
    assert record.snapshot_id == 11
    assert record.human_label is True
    assert record.production_action is None


def test_optional_production_action_preserves_closed_legacy_contract() -> None:
    payload = record_from_frozen_input(_frozen_input(20, author="alice.bsky.social")).model_dump()
    payload.pop("production_action")
    assert PopulationExportRecord.model_validate(payload).production_action is None
    with pytest.raises(ValueError):
        PopulationExportRecord.model_validate({**payload, "unknown": True})


def test_held_constructor_does_not_relax_frozen_grade_validation() -> None:
    item = _frozen_input(20, author="alice.bsky.social")
    item = item.model_copy(
        update={"grade": dataclasses.replace(item.grade, relevance_judgment=None)}
    )
    with pytest.raises(ValueError, match="no valid human label"):
        record_from_frozen_input(item)
    record = record_from_held_row(
        {
            "evaluation_id": 20,
            "platform": "bluesky",
            "channel": "research",
            "url": "https://bsky.app/profile/alice.test/post/20",
            "text": "held",
            "parent_author_name": None,
            "parent_text": None,
            "author_name": "Alice",
            "production_score": 0.7,
            "production_decision": 1,
            "production_action": "review",
        }
    )
    assert (record.human_label, record.production_action) == (None, "review")


def test_jsonl_is_stable_and_ordered_by_evaluation_id() -> None:
    records = [
        record_from_frozen_input(_frozen_input(20, author="bob.bsky.social")),
        record_from_frozen_input(_frozen_input(10, author="alice.bsky.social")),
    ]

    first = render_population_jsonl(records)
    second = render_population_jsonl(reversed(records))

    assert first == second
    assert [json.loads(line)["evaluation_id"] for line in first.splitlines()] == [10, 20]


def test_live_population_filters_human_and_held_rows_and_exports_actions() -> None:
    with StateManager(db_path=":memory:") as state:
        state.conn.executemany(
            "INSERT INTO posts "
            "(id, platform, platform_msg_id, channel_name, author_name, content, url) "
            "VALUES (?, 'farcaster', ?, 'agent-ops', ?, ?, ?)",
            [
                (1, "cast-1", "Alice", "first", "https://warpcast.com/alice/0x1"),
                (2, "cast-2", "Bob", "second", "https://warpcast.com/bob/0x2"),
                (3, "cast-3", "Eve", "other", "https://warpcast.com/eve/0x3"),
                (4, "cast-4", "Mallory", "fourth", "https://warpcast.com/mallory/0x4"),
                (5, "cast-5", "Trent", "fifth", "https://warpcast.com/trent/0x5"),
                (6, "cast-6", "Uma", "sixth", "https://warpcast.com/uma/0x6"),
            ],
        )
        state.conn.executemany(
            "INSERT INTO evaluations "
            "(id, post_id, relevant, score, project_key, surface_status, "
            "relevance_classifier, relevance_action) "
            "VALUES (?, ?, ?, ?, ?, 'surfaced', ?, ?)",
            [
                (20, 2, 0, 0.2, "agent-ops", "zeroshot", "review"),
                (10, 1, 1, 0.9, "agent-ops", "llm", None),
                (30, 3, 1, 0.8, "other", "llm", None),
                (40, 4, 0, 0.1, "agent-ops", "zeroshot", "drop"),
                (50, 5, 1, 0.7, "agent-ops", "human", "respond"),
                (60, 6, 0, 0.4, "agent-ops", "zeroshot", "review"),
            ],
        )
        state.conn.execute(
            "INSERT INTO relevance_holdouts "
            "(evaluation_id, production_action, held, registry_state, created_at) "
            "VALUES (60, 'review', 1, '{}', '2026-09-01T00:00:00.000Z')"
        )
        state.conn.executemany(
            "INSERT INTO grades "
            "(evaluation_id, post_id, source, graded_at, relevance_judgment, "
            "schema_version, needs_regrade) VALUES (?, ?, 'web', ?, ?, 3, ?)",
            [
                (10, 1, "2026-09-01T00:00:00.000Z", "correct", 0),
                (20, 2, "2026-09-01T00:00:00.000Z", "false_negative", 1),
                (40, 4, "2026-09-01T00:00:00.000Z", "false_positive", 0),
                (50, 5, "2026-09-01T00:00:00.000Z", "false_negative", 0),
            ],
        )

        records = load_live_population(state.conn, "agent-ops")

    assert [record.evaluation_id for record in records] == [10, 20, 40]
    assert [record.human_label for record in records] == [True, None, None]
    assert [record.author_handle for record in records] == [
        "alice",
        "bob",
        "mallory",
    ]
    assert all(record.snapshot_id is None for record in records)
    lines = render_population_jsonl(records).splitlines()
    assert lines[1] == (
        b'{"evaluation_id":20,"platform":"farcaster","channel":"agent-ops",'
        b'"url":"https://warpcast.com/bob/0x2","text":"second",'
        b'"parent_author_name":null,"parent_text":null,"author_name":"Bob",'
        b'"author_handle":"bob","snapshot_id":null,"human_label":null,'
        b'"production_score":0.2,"production_decision":false,'
        b'"production_action":"review"}'
    )
    assert lines[2] == (
        b'{"evaluation_id":40,"platform":"farcaster","channel":"agent-ops",'
        b'"url":"https://warpcast.com/mallory/0x4","text":"fourth",'
        b'"parent_author_name":null,"parent_text":null,"author_name":"Mallory",'
        b'"author_handle":"mallory","snapshot_id":null,"human_label":null,'
        b'"production_score":0.1,"production_decision":false,'
        b'"production_action":"drop"}'
    )


def test_live_population_rejects_invalid_production_decision() -> None:
    with StateManager(db_path=":memory:") as state:
        state.conn.execute(
            "INSERT INTO posts "
            "(id, platform, platform_msg_id, channel_name, author_name, content, url) "
            "VALUES (1, 'discord', 'message-1', 'agent-ops', 'Alice', 'first', "
            "'https://discord.com/channels/1/2/3')"
        )
        state.conn.execute(
            "INSERT INTO evaluations "
            "(id, post_id, relevant, score, project_key, surface_status) "
            "VALUES (10, 1, 2, 0.9, 'agent-ops', 'surfaced')"
        )

        with pytest.raises(ValueError, match="evaluation 10 decision must be boolean"):
            load_live_population(state.conn, "agent-ops")
