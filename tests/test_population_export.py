"""Population export transforms and stable JSON-lines rendering."""

from __future__ import annotations

import json
from datetime import UTC, datetime

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
    ]
    assert record.author_handle == "alice.bsky.social"
    assert record.snapshot_id == 11
    assert record.human_label is True


def test_jsonl_is_stable_and_ordered_by_evaluation_id() -> None:
    records = [
        record_from_frozen_input(_frozen_input(20, author="bob.bsky.social")),
        record_from_frozen_input(_frozen_input(10, author="alice.bsky.social")),
    ]

    first = render_population_jsonl(records)
    second = render_population_jsonl(reversed(records))

    assert first == second
    assert [json.loads(line)["evaluation_id"] for line in first.splitlines()] == [10, 20]


def test_live_population_includes_every_project_evaluation_and_only_finalized_labels() -> None:
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
            ],
        )
        state.conn.executemany(
            "INSERT INTO evaluations "
            "(id, post_id, relevant, score, project_key, surface_status) "
            "VALUES (?, ?, ?, ?, ?, 'surfaced')",
            [
                (20, 2, 0, 0.2, "agent-ops"),
                (10, 1, 1, 0.9, "agent-ops"),
                (30, 3, 1, 0.8, "other"),
                (40, 4, 0, 0.1, "agent-ops"),
                (50, 5, 1, 0.7, "agent-ops"),
            ],
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

    assert [record.evaluation_id for record in records] == [10, 20, 40, 50]
    assert [record.human_label for record in records] == [True, None, None, None]
    assert [record.author_handle for record in records] == [
        "alice",
        "bob",
        "mallory",
        "trent",
    ]
    assert all(record.snapshot_id is None for record in records)


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


def test_the_holdout_export_does_not_change_the_population_contract() -> None:
    """The holdout export extends this format; it never renames or drops a field.

    Assay's packet builder reads the population record. A field the holdout
    export quietly renamed would read as missing there, silently.
    """
    from scout.holdouts.export import HoldoutExportRecord

    population = PopulationExportRecord.model_fields
    holdout = HoldoutExportRecord.model_fields

    for name, field in population.items():
        assert name in holdout, f"{name} is missing from the holdout export"
        assert holdout[name].annotation == field.annotation, name


def test_live_population_export_is_unaffected_by_pending_holds(
    in_memory_state: StateManager,
) -> None:
    """A held evaluation is an ordinary evaluation to the population export."""
    from scout.config import Account, Message, RelevanceResult

    msg = Message(
        platform="farcaster",
        platform_id="0xheld",
        channel_name="agents",
        channel_id="agents",
        author=Account(platform="farcaster", id="42", name="Ada", handle="ada"),
        content="held post",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        url="https://warpcast.com/ada/0xheld",
    )
    scan_id = in_memory_state.start_scan(environment="test")
    post_id = in_memory_state.save_post(msg, scan_id)
    in_memory_state.evaluations.save_evaluation(
        RelevanceResult(
            message=msg, relevant=True, score=1.0, reason="r", relevant_to=("agent-ops",)
        ),
        post_id,
        scan_id,
        surface_status="held",
        project_key="agent-ops",
    )

    records = load_live_population(in_memory_state.conn, "agent-ops")

    assert [record.evaluation_id for record in records] == [1]
    assert records[0].text == "held post"
