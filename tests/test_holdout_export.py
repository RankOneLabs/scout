"""The pending holdout export: what it covers, what it freezes, what it hides."""

from __future__ import annotations

import json
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft7Validator

import scout.storage.holdouts as holdout_storage
from scout.config import Account, Message, RelevanceResult, SourceParent
from scout.holdouts.export import (
    BLIND_CASE_FIELDS,
    UNRELEASED_STATUSES,
    HoldoutExportRecord,
    blind_case,
    export_holdouts,
    load_unreleased_holdouts,
    render_blind_jsonl,
    render_holdout_jsonl,
    surfaced_or_dropped,
)
from scout.result import Err, Ok
from scout.storage.holdouts import (
    FrozenHoldoutInput,
    HoldoutWrite,
    KeyProvenance,
    LabelProvenance,
    RelevanceDecisionWrite,
)
from scout.storage.state import StateManager

_SCHEMA_PATH = (
    Path(__file__).parent.parent / "contracts" / "relevance" / "holdout-export.v1.schema.json"
)


def _sm() -> Generator[StateManager, None, None]:
    state = StateManager(db_path=":memory:")
    yield state
    state.commit()
    state.close()


sm = pytest.fixture(_sm)


def _message(platform_id: str) -> Message:
    return Message(
        platform="farcaster",
        platform_id=platform_id,
        channel_name="agents",
        channel_id="agents",
        author=Account(platform="farcaster", id="42", name="Ada", handle="ada"),
        content=f"post {platform_id}",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        url=f"https://warpcast.com/ada/{platform_id}",
        parent=SourceParent(
            id=f"{platform_id}-parent",
            author=Account(platform="farcaster", id="9", name="Bob", handle="bob"),
            text="the parent post",
            url="https://warpcast.com/bob/parent",
        ),
        parent_lookup_status="resolved",
    )


def _frozen(platform_id: str, *, project_key: str | None = "agent-ops") -> FrozenHoldoutInput:
    return FrozenHoldoutInput(
        platform="farcaster",
        platform_id=platform_id,
        url=f"https://warpcast.com/ada/{platform_id}",
        channel="agents",
        text=f"post {platform_id}",
        parent_author_name="Bob",
        parent_text="the parent post",
        author_id="42",
        author_name="Ada",
        author_handle="ada",
        project_key=project_key,
        project_name="Agent Ops" if project_key else None,
        project_description="A description." if project_key else None,
        keyword_route_id=None,
        dossier_summary_id="d1",
        dossier_revision="r1",
    )


def _hold(
    state: StateManager,
    platform_id: str,
    *,
    relevant: bool = True,
    score: float = 1.0,
    action: str = "respond",
    classifier: str = "jev",
    project_key: str | None = "agent-ops",
    record_decision: bool = True,
) -> tuple[int, int]:
    """Persist a held evaluation and its hold. Returns (holdout_id, evaluation_id)."""
    scan_id = state.start_scan(environment="test")
    msg = _message(platform_id)
    post_id = state.save_post(msg, scan_id)
    evaluation_id = state.evaluations.save_evaluation(
        RelevanceResult(
            message=msg,
            relevant=relevant,
            score=score,
            reason="the private reason",
            relevant_to=() if project_key is None else (project_key,),
        ),
        post_id,
        scan_id,
        surface_status="held",
        project_key=project_key,
        dossier_summary_id="d1",
        dossier_revision="r1",
    )
    if record_decision:
        written = state.holdouts.record_decision(
            RelevanceDecisionWrite(
                evaluation_id=evaluation_id,
                classifier=classifier,  # type: ignore[arg-type]
                model="jev-latest",
                action=action,  # type: ignore[arg-type]
                reason="the private reason",
                catalogue_id="cat",
                catalogue_version="v6",
                router_version="1",
            )
        )
        assert isinstance(written, Ok), written
    held = state.holdouts.hold(
        HoldoutWrite(
            evaluation_id=evaluation_id,
            post_id=post_id,
            scan_id=scan_id,
            project_key=project_key,
            frozen_input=_frozen(platform_id, project_key=project_key),
        )
    )
    assert isinstance(held, Ok), held
    return held.value.id, evaluation_id


def _records(state: StateManager) -> tuple[HoldoutExportRecord, ...]:
    exported = export_holdouts(state.conn)
    assert isinstance(exported, Ok), exported
    return exported.value


# --- coverage ---------------------------------------------------------------


def test_the_export_covers_positives_and_drops_alike(sm: StateManager) -> None:
    _hold(sm, "0x1", relevant=True, score=1.0, action="respond")
    _hold(sm, "0x2", relevant=False, score=0.0, action="drop")

    assert {record.private.action for record in _records(sm)} == {"respond", "drop"}


def test_a_failed_release_stays_in_the_population(sm: StateManager) -> None:
    holdout_id, _evaluation_id = _hold(sm, "0x1")
    claim = sm.holdouts.claim(holdout_id, owner="worker")
    assert isinstance(claim, Ok)
    failed = sm.holdouts.fail_attempt(claim.value, detail="dossier unavailable")
    assert isinstance(failed, Ok)

    assert [record.holdout_id for record in _records(sm)] == [holdout_id]


def test_a_claim_in_flight_stays_in_the_population(sm: StateManager) -> None:
    holdout_id, _evaluation_id = _hold(sm, "0x1")
    assert isinstance(sm.holdouts.claim(holdout_id, owner="worker"), Ok)

    assert [record.holdout_id for record in _records(sm)] == [holdout_id]


def test_a_completed_release_is_excluded(sm: StateManager) -> None:
    holdout_id, _evaluation_id = _hold(sm, "0x1")
    claim = sm.holdouts.claim(holdout_id, owner="worker")
    assert isinstance(claim, Ok)
    released = sm.holdouts.complete_release(
        claim.value, release_authority="recorded_action", release_action="drop"
    )
    assert isinstance(released, Ok), released

    assert _records(sm) == ()


def test_every_unreleased_status_is_covered() -> None:
    assert set(UNRELEASED_STATUSES) == {"pending", "failed", "claimed"}


def test_an_old_hold_is_not_filtered_out(
    sm: StateManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No age cutoff: a hold from years ago is still pending work."""
    monkeypatch.setattr(holdout_storage, "_now", lambda: "2020-01-01T00:00:00+00:00")
    holdout_id, _evaluation_id = _hold(sm, "0x1")
    monkeypatch.undo()

    records = _records(sm)
    assert [record.holdout_id for record in records] == [holdout_id]
    assert records[0].held_at == "2020-01-01T00:00:00+00:00"


def test_an_ungraded_record_carries_a_null_human_label(sm: StateManager) -> None:
    _hold(sm, "0x1")

    assert _records(sm)[0].human_label is None


def test_records_are_ordered_oldest_first_with_unique_identities(
    sm: StateManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _ = _hold(sm, "0x1")
    second, _ = _hold(sm, "0x2")
    monkeypatch.setattr(holdout_storage, "_now", lambda: "2020-01-01T00:00:00+00:00")
    third, _ = _hold(sm, "0x3")
    monkeypatch.undo()

    identities = [record.holdout_id for record in _records(sm)]
    assert identities == [third, first, second]
    assert len(set(identities)) == 3


def test_a_hold_with_no_recorded_decision_is_refused_not_guessed(sm: StateManager) -> None:
    holdout_id, _evaluation_id = _hold(sm, "0x1", record_decision=False)

    exported = export_holdouts(sm.conn)

    assert isinstance(exported, Err)
    assert exported.error.holdout_id == holdout_id
    assert "no recorded relevance decision" in exported.error.detail


# --- frozen identity --------------------------------------------------------


def test_the_export_uses_the_frozen_post_not_the_live_one(sm: StateManager) -> None:
    _hold(sm, "0x1")
    sm.conn.execute("UPDATE posts SET content = ?, url = ?", ("edited later", "https://new"))

    record = _records(sm)[0]
    assert record.text == "post 0x1"
    assert record.url == "https://warpcast.com/ada/0x1"


def test_the_export_uses_the_frozen_project_not_the_live_one(sm: StateManager) -> None:
    _hold(sm, "0x1")

    record = _records(sm)[0]
    assert record.project is not None
    assert record.project.key == "agent-ops"
    assert record.project.name == "Agent Ops"
    assert record.project.description == "A description."


# --- a hold that froze no project -------------------------------------------


def test_a_hold_that_froze_no_project_is_exported_with_a_null_project(
    sm: StateManager,
) -> None:
    """A post reached without a keyword route carries no project.

    Holding it is a legitimate sampled decision — release lands it as a
    drop — so the export represents it rather than refusing it.
    """
    _hold(sm, "0x1", project_key=None, relevant=False, score=0.0, action="drop")

    assert _records(sm)[0].project is None


def test_a_projectless_record_still_validates_against_the_v1_schema(
    sm: StateManager,
) -> None:
    _hold(sm, "0x1", project_key=None, relevant=False, score=0.0, action="drop")
    validator = Draft7Validator(json.loads(_SCHEMA_PATH.read_text()))

    validator.validate(json.loads(_records(sm)[0].model_dump_json()))


def test_a_projectless_hold_does_not_strand_the_rest_of_the_population(
    sm: StateManager,
) -> None:
    """The export is all-or-nothing, so one refused hold refuses every hold."""
    _hold(sm, "0x1", project_key=None, relevant=False, score=0.0, action="drop")
    _hold(sm, "0x2")

    assert len(_records(sm)) == 2


def test_the_export_carries_unambiguous_identity(sm: StateManager) -> None:
    holdout_id, evaluation_id = _hold(sm, "0x1")

    record = _records(sm)[0]
    assert record.holdout_id == holdout_id
    assert record.evaluation_id == evaluation_id
    assert record.post_id > 0


# --- the decision boundary --------------------------------------------------


@pytest.mark.parametrize(
    ("action", "expected"),
    [("respond", True), ("review", True), ("drop", False)],
)
def test_the_decision_boundary_is_respond_or_review_against_drop(
    action: str, expected: bool
) -> None:
    assert surfaced_or_dropped(action) is expected  # type: ignore[arg-type]


def test_a_low_score_llm_drop_reads_as_dropped_despite_a_relevant_evaluation(
    sm: StateManager,
) -> None:
    """Recorded action and the evaluation's `relevant` can legitimately differ."""
    _hold(sm, "0x1", relevant=True, score=0.2, action="drop", classifier="llm")

    record = _records(sm)[0]
    assert record.production_decision is False
    assert record.production_score == 0.2
    assert record.private.action == "drop"


def test_recorded_action_is_kept_apart_from_the_actual_surface_status(
    sm: StateManager,
) -> None:
    _hold(sm, "0x1", action="respond")

    record = _records(sm)[0]
    surface_status = sm.conn.execute(
        "SELECT surface_status FROM evaluations WHERE id = ?", (record.evaluation_id,)
    ).fetchone()["surface_status"]
    assert record.private.action == "respond"
    assert surface_status == "held"


# --- the blind projection ---------------------------------------------------


def test_the_blind_projection_is_exactly_the_allowlist(sm: StateManager) -> None:
    _hold(sm, "0x1")

    projected = json.loads(blind_case(_records(sm)[0]).model_dump_json())

    assert tuple(sorted(projected)) == tuple(sorted(BLIND_CASE_FIELDS))


def test_no_private_value_survives_the_blind_projection(sm: StateManager) -> None:
    """Adversarial: every private field carries a sentinel, none may appear."""
    scan_id = sm.start_scan(environment="test")
    msg = _message("0x1")
    post_id = sm.save_post(msg, scan_id)
    evaluation_id = sm.evaluations.save_evaluation(
        RelevanceResult(
            message=msg,
            relevant=True,
            score=0.123456,
            reason="SENTINEL-REASON",
            relevant_to=("agent-ops",),
        ),
        post_id,
        scan_id,
        surface_status="held",
        project_key="agent-ops",
        dossier_summary_id="d1",
        dossier_revision="r1",
    )
    written = sm.holdouts.record_decision(
        RelevanceDecisionWrite(
            evaluation_id=evaluation_id,
            classifier="jev",
            model="SENTINEL-MODEL",
            action="review",
            reason="SENTINEL-REASON",
            catalogue_id="SENTINEL-CATALOGUE",
            catalogue_version="SENTINEL-CATALOGUE-VERSION",
            router_version="SENTINEL-ROUTER",
            answers={"excl_spam": 0.9},
            decision={"line": "SENTINEL-LINE", "exclusion": "SENTINEL-EXCLUSION"},
        )
    )
    assert isinstance(written, Ok), written
    held = sm.holdouts.hold(
        HoldoutWrite(
            evaluation_id=evaluation_id,
            post_id=post_id,
            scan_id=scan_id,
            project_key="agent-ops",
            frozen_input=_frozen("0x1"),
        )
    )
    assert isinstance(held, Ok), held

    blind = render_blind_jsonl(_records(sm)).decode("utf-8")

    for sentinel in (
        "SENTINEL-REASON",
        "SENTINEL-MODEL",
        "SENTINEL-CATALOGUE",
        "SENTINEL-CATALOGUE-VERSION",
        "SENTINEL-ROUTER",
        "SENTINEL-LINE",
        "SENTINEL-EXCLUSION",
        "0.123456",
        "review",
        "excl_spam",
    ):
        assert sentinel not in blind, f"{sentinel} leaked into the blind projection"


def test_the_full_export_still_carries_the_private_envelope(sm: StateManager) -> None:
    _hold(sm, "0x1", action="review")

    line = json.loads(render_holdout_jsonl(_records(sm)).decode("utf-8").splitlines()[0])

    assert line["private"]["action"] == "review"
    assert line["private"]["classifier"]["name"] == "jev"
    assert line["private"]["reason"] == "the private reason"


def test_the_private_envelope_is_one_droppable_key(sm: StateManager) -> None:
    """A denylist check only works if the private data has exactly one home."""
    _hold(sm, "0x1")
    line = json.loads(render_holdout_jsonl(_records(sm)).decode("utf-8").splitlines()[0])

    del line["private"]

    assert "the private reason" not in json.dumps(line)


# --- the contract ------------------------------------------------------------


def test_every_exported_record_validates_against_the_v1_schema(sm: StateManager) -> None:
    _hold(sm, "0x1", action="respond")
    _hold(sm, "0x2", relevant=False, score=0.0, action="drop", classifier="llm")
    validator = Draft7Validator(json.loads(_SCHEMA_PATH.read_text()))

    for line in render_holdout_jsonl(_records(sm)).decode("utf-8").splitlines():
        validator.validate(json.loads(line))


def test_a_synthetic_golden_record_matches_the_contract() -> None:
    """A hand-written record, so a field renamed in the model is caught here."""
    golden: dict[str, Any] = {
        "schema_version": 1,
        "holdout_id": 7,
        "evaluation_id": 41,
        "post_id": 12,
        "project": {"key": "agent-ops", "name": "Agent Ops", "description": "A description."},
        "platform": "farcaster",
        "channel": "agents",
        "url": "https://warpcast.com/ada/0xabc",
        "text": "our planner retries every failed step twice",
        "parent_author_name": "Bob",
        "parent_text": "the parent post",
        "author_name": "Ada",
        "author_handle": "ada",
        "snapshot_id": None,
        "human_label": None,
        "production_score": 1.0,
        "production_decision": True,
        "held_at": "2026-09-24T00:00:00+00:00",
        "private": {
            "action": "respond",
            "score": 1.0,
            "reason": "the line that decided it",
            "classifier": {
                "name": "jev",
                "model": "jev-latest",
                "catalogue_id": "agent-ops-relevance-features",
                "catalogue_version": "v6",
                "router_version": "1",
            },
        },
    }
    Draft7Validator(json.loads(_SCHEMA_PATH.read_text())).validate(golden)

    assert json.loads(HoldoutExportRecord(**golden).model_dump_json()) == golden


def test_every_population_export_field_is_present_and_unchanged(sm: StateManager) -> None:
    """The holdout export extends the population format; it never drops a field."""
    from scout.replay.population_export import PopulationExportRecord

    _hold(sm, "0x1")
    record = _records(sm)[0]

    for field in PopulationExportRecord.model_fields:
        assert field in HoldoutExportRecord.model_fields, field
        assert hasattr(record, field)


class _CountingConnection:
    """Counts statements without touching the real connection's attributes."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self.statements: list[str] = []

    def execute(self, sql: str, *args: Any) -> Any:
        self.statements.append(sql)
        return self._conn.execute(sql, *args)


def test_the_snapshot_is_read_in_one_statement(sm: StateManager) -> None:
    """One query, so no record can come from a different view of the database."""
    _hold(sm, "0x1")
    counting = _CountingConnection(sm.conn)

    assert isinstance(load_unreleased_holdouts(counting), Ok)  # type: ignore[arg-type]
    assert len(counting.statements) == 1


def test_a_graded_hold_reports_its_human_label(sm: StateManager) -> None:
    _holdout_id, evaluation_id = _hold(sm, "0x1", relevant=False, score=0.0, action="drop")
    post_id = sm.conn.execute(
        "SELECT post_id FROM evaluations WHERE id = ?", (evaluation_id,)
    ).fetchone()["post_id"]
    sm.conn.execute(
        "INSERT INTO grades (evaluation_id, post_id, scan_id, source, graded_at, "
        "relevance_judgment, schema_version, needs_regrade) "
        "VALUES (?, ?, 1, 'cli', '2026-09-24T00:00:00.000Z', 'false_negative', 3, 0)",
        (evaluation_id, post_id),
    )

    assert _records(sm)[0].human_label is True


def test_label_and_key_provenance_types_are_the_join_the_export_offers() -> None:
    """The export's holdout_id/evaluation_id/project are what a key maps to."""
    label = LabelProvenance(
        format="assay.label-packet-labels/v3",
        packet="round-6",
        packet_digest="a" * 64,
        plan_digest="b" * 64,
        reviewer="reviewer",
        saved_at="2026-09-24T00:00:00Z",
        case_id=3,
    )
    key = KeyProvenance(
        format="assay.label-packet-key/v2",
        name="round-6",
        digest="a" * 64,
        plan_digest="b" * 64,
        sitting="first",
        case_id=3,
        evaluation_id=41,
        project_key="agent-ops",
    )
    assert label.case_id == key.case_id
    assert label.packet_digest == key.digest


# --- the command -------------------------------------------------------------


def test_the_export_command_writes_the_population_to_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from argparse import Namespace

    from scout.cli.holdout import export_holdout_population

    db_path = str(tmp_path / "scout.db")
    state = StateManager(db_path=db_path)
    _hold(state, "0x1")
    state.commit()
    state.close()

    output = tmp_path / "holdouts.jsonl"
    export_holdout_population(Namespace(db=db_path, output=str(output), blind=False))

    lines = output.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["private"]["action"] == "respond"


def test_the_blind_flag_writes_only_allowlisted_fields(tmp_path: Path) -> None:
    from argparse import Namespace

    from scout.cli.holdout import export_holdout_population

    db_path = str(tmp_path / "scout.db")
    state = StateManager(db_path=db_path)
    _hold(state, "0x1")
    state.commit()
    state.close()

    output = tmp_path / "blind.jsonl"
    export_holdout_population(Namespace(db=db_path, output=str(output), blind=True))

    case = json.loads(output.read_text().splitlines()[0])
    assert tuple(sorted(case)) == tuple(sorted(BLIND_CASE_FIELDS))


def test_the_export_command_fails_rather_than_writing_a_partial_file(
    tmp_path: Path,
) -> None:
    from argparse import Namespace

    from scout.cli.holdout import export_holdout_population

    db_path = str(tmp_path / "scout.db")
    state = StateManager(db_path=db_path)
    _hold(state, "0x1", record_decision=False)
    state.commit()
    state.close()

    output = tmp_path / "holdouts.jsonl"
    with pytest.raises(SystemExit):
        export_holdout_population(Namespace(db=db_path, output=str(output), blind=False))

    assert not output.exists()


def test_the_export_command_refuses_a_database_that_does_not_exist(
    tmp_path: Path,
) -> None:
    """A mistyped `--db` is a mistake, not a first run.

    Creating one here would migrate an empty database and write a
    zero-record export that reads exactly like a fully released population.
    """
    from argparse import Namespace

    from scout.cli.holdout import export_holdout_population

    missing = tmp_path / "typo.db"
    output = tmp_path / "holdouts.jsonl"

    with pytest.raises(Exception, match="(?i)does not exist|unable to open"):
        export_holdout_population(Namespace(db=str(missing), output=str(output), blind=False))

    assert not missing.exists()
    assert not output.exists()
