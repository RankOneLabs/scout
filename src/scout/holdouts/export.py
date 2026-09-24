"""The pending holdout population, projected for blind grading.

`scout holdout export` writes one JSON-lines record per unreleased hold. The
record extends the population export format assay already reads — every
`PopulationExportRecord` field is present and unchanged — with the holdout,
evaluation, post and frozen project identity a label joins back on, plus a
`private` envelope holding the production decision a blind reviewer must not
see.

Every identity field is read from the hold's frozen input rather than from
the live posts and projects rows. A post edited, a project renamed or a
route repointed after the decision must not change what the label was
written against.

See contracts/relevance/holdout-export.v1.schema.json and
docs/relevance-holdouts.md.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from scout.config import HUMAN_GRADE_SCHEMA_VERSION
from scout.result import Err, Ok, Result
from scout.scanning.author_class import handle_from_post_url
from scout.storage.holdouts import ClassifierAction, ClassifierName, FrozenHoldoutInput

HOLDOUT_EXPORT_SCHEMA_VERSION: Literal[1] = 1

#: Hold statuses the export covers. A release that failed and a claim still
#: in flight are both still pending work and belong in the population; only a
#: completed release is finished with it. No age, human-grade or exposure
#: filter applies — the export is the whole pending decision population.
UNRELEASED_STATUSES: tuple[str, ...] = ("pending", "failed", "claimed")

#: The only keys a blind reviewer may see. An allowlist, not a denylist: a
#: field added to the export record later is private until someone adds it
#: here on purpose.
BLIND_CASE_FIELDS: tuple[str, ...] = ("holdout_id", "text", "parent_text")


@dataclass(frozen=True, slots=True)
class HoldoutExportError:
    """An export was refused. Carries which hold and why."""

    operation: str
    detail: str
    holdout_id: int | None = None


class HoldoutExportClassifier(BaseModel):
    """What produced the decision. A null member means unknown, never a guess."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: ClassifierName | None
    model: str | None = None
    catalogue_id: str | None = None
    catalogue_version: str | None = None
    router_version: str | None = None


class HoldoutExportPrivate(BaseModel):
    """The production decision, isolated so a blind projection can drop it whole."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: ClassifierAction
    score: float
    reason: str | None
    classifier: HoldoutExportClassifier


class HoldoutExportProject(BaseModel):
    """The routed project frozen as it stood at decision time.

    Absent entirely on a hold that froze no project. A post the classifier
    reached without a keyword route carries no project, and holding it is a
    legitimate sampled decision — it can only ever be released as a drop.
    Refusing to export it would strand that hold, and because the export is
    all-or-nothing it would strand every other hold with it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    name: str | None
    description: str | None


class HoldoutExportRecord(BaseModel):
    """One pending held post.

    Mirrors contracts/relevance/holdout-export.v1.schema.json field for
    field. The population fields keep their population meanings so an assay
    packet builder that reads the population format reads this unchanged.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = HOLDOUT_EXPORT_SCHEMA_VERSION
    holdout_id: int
    evaluation_id: int
    post_id: int
    project: HoldoutExportProject | None
    platform: str
    channel: str | None
    url: str | None
    text: str | None
    parent_author_name: str | None
    parent_text: str | None
    author_name: str | None
    author_handle: str | None
    snapshot_id: int | None
    human_label: bool | None
    production_score: float
    production_decision: bool
    held_at: str
    private: HoldoutExportPrivate


class BlindHoldoutCase(BaseModel):
    """The allowlisted blind projection of one export record.

    Mirrors assay's `assay.label-packet/v1` blind case, with `holdout_id`
    standing in for its `case_id`: the reviewer sees the post and nothing
    about what production decided.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    holdout_id: int
    text: str | None
    parent_text: str | None


def surfaced_or_dropped(action: ClassifierAction) -> bool:
    """The assay decision boundary: respond and review against drop.

    `review` continues through drafting exactly as `respond` does, so both
    sit on the surfaced side of the boundary a grader is judging. This is
    the *intent* the classifier recorded, not what downstream actually did
    with it — a held post's actual surface_status is `held`, and a released
    one may still be stopped by the critic, the verifier or a rate cap.
    """
    return action in ("respond", "review")


def blind_case(record: HoldoutExportRecord) -> BlindHoldoutCase:
    """Project one export record down to what a blind reviewer may see."""
    return BlindHoldoutCase(
        holdout_id=record.holdout_id,
        text=record.text,
        parent_text=record.parent_text,
    )


def _export_record(row: sqlite3.Row) -> Result[HoldoutExportRecord, HoldoutExportError]:
    """Project one joined row, or refuse it.

    A hold with no recorded decision cannot be exported: its released action
    would have to be invented, and an invented action is worse than a
    refused export. Capture writes the decision and the hold in one
    transaction, so this is an impossible state rather than an ordinary one.
    """
    holdout_id = int(row["holdout_id"])
    action = row["action"]
    if action is None:
        return Err(
            HoldoutExportError(
                operation="export",
                detail="hold has no recorded relevance decision to export",
                holdout_id=holdout_id,
            )
        )
    frozen = FrozenHoldoutInput(**json.loads(row["frozen_input_json"]))
    human_label = row["human_label"]
    return Ok(
        HoldoutExportRecord(
            holdout_id=holdout_id,
            evaluation_id=int(row["evaluation_id"]),
            post_id=int(row["post_id"]),
            project=(
                None
                if frozen.project_key is None
                else HoldoutExportProject(
                    key=frozen.project_key,
                    name=frozen.project_name,
                    description=frozen.project_description,
                )
            ),
            platform=frozen.platform,
            channel=frozen.channel,
            url=frozen.url,
            text=frozen.text,
            parent_author_name=frozen.parent_author_name,
            parent_text=frozen.parent_text,
            author_name=frozen.author_name,
            # The decision-time handle wins; the URL derivation is the
            # fallback for a platform that never carried one, and matches
            # what the population export computes.
            author_handle=frozen.author_handle
            or handle_from_post_url(frozen.platform, frozen.url),
            # A post held at decision time has never been exposed for
            # grading, so there is no snapshot to cite. Matches
            # load_live_population, which reports the same for a live read.
            snapshot_id=None,
            human_label=None if human_label is None else bool(human_label),
            production_score=float(row["production_score"]),
            # What production decided to do, not what downstream did with
            # it. A held LLM case that was relevant but under the threshold
            # recorded `drop`, and reads false here even though its
            # evaluation says relevant.
            production_decision=surfaced_or_dropped(action),
            held_at=str(row["held_at"]),
            private=HoldoutExportPrivate(
                action=action,
                score=float(row["production_score"]),
                reason=row["decision_reason"],
                classifier=HoldoutExportClassifier(
                    name=row["classifier"],
                    model=row["model"],
                    catalogue_id=row["catalogue_id"],
                    catalogue_version=row["catalogue_version"],
                    router_version=row["router_version"],
                ),
            ),
        )
    )


_UNRELEASED_QUERY = """
    SELECT
        h.id                   AS holdout_id,
        h.evaluation_id        AS evaluation_id,
        h.post_id              AS post_id,
        h.held_at              AS held_at,
        h.frozen_input_json    AS frozen_input_json,
        e.score                AS production_score,
        e.relevant             AS evaluation_relevant,
        d.action               AS action,
        d.reason               AS decision_reason,
        d.classifier           AS classifier,
        d.model                AS model,
        d.catalogue_id         AS catalogue_id,
        d.catalogue_version    AS catalogue_version,
        d.router_version       AS router_version,
        CASE WHEN g.schema_version = ? AND g.needs_regrade = 0 THEN
            CASE
                WHEN g.relevance_judgment = 'correct' AND e.relevant IN (0, 1)
                    THEN e.relevant
                WHEN g.relevance_judgment = 'false_positive' AND e.relevant = 1 THEN 0
                WHEN g.relevance_judgment = 'false_negative' AND e.relevant = 0 THEN 1
            END
        END                    AS human_label
    FROM relevance_holdouts h
    JOIN evaluations e ON e.id = h.evaluation_id
    LEFT JOIN relevance_decisions d ON d.evaluation_id = h.evaluation_id
    LEFT JOIN grades g ON g.evaluation_id = h.evaluation_id
    WHERE h.status IN ({placeholders})
    ORDER BY h.held_at, h.id
"""


def load_unreleased_holdouts(
    conn: sqlite3.Connection,
) -> Result[tuple[HoldoutExportRecord, ...], HoldoutExportError]:
    """Read every unreleased hold as one consistent snapshot.

    One statement, so every record comes from the same view of the database
    — a release committing mid-export cannot produce a file that holds one
    record from before it and the next from after. `grades` joins at most
    one row per evaluation (grades_evaluation_id_unique), so the join cannot
    duplicate a hold.

    Ordered by (held_at, id): stable across runs, and oldest first, because
    the oldest pending hold is the one an operator most wants to see.
    """
    placeholders = ", ".join("?" for _ in UNRELEASED_STATUSES)
    rows = conn.execute(
        _UNRELEASED_QUERY.format(placeholders=placeholders),
        (HUMAN_GRADE_SCHEMA_VERSION, *UNRELEASED_STATUSES),
    ).fetchall()
    records: list[HoldoutExportRecord] = []
    for row in rows:
        match _export_record(row):
            case Ok(record):
                records.append(record)
            case Err(error):
                return Err(error)
    return Ok(tuple(records))


def render_holdout_jsonl(records: Iterable[HoldoutExportRecord]) -> bytes:
    """Render canonical UTF-8 JSON lines in the order given."""
    return b"".join(record.model_dump_json().encode("utf-8") + b"\n" for record in records)


def render_blind_jsonl(records: Iterable[HoldoutExportRecord]) -> bytes:
    """Render the blind projection as JSON lines."""
    return b"".join(
        blind_case(record).model_dump_json().encode("utf-8") + b"\n" for record in records
    )


def export_holdouts(
    conn: sqlite3.Connection,
) -> Result[tuple[HoldoutExportRecord, ...], HoldoutExportError]:
    """Load and validate the pending population, refusing a duplicate identity."""
    loaded = load_unreleased_holdouts(conn)
    if isinstance(loaded, Err):
        return loaded
    records = loaded.value
    identities = {record.holdout_id for record in records}
    if len(identities) != len(records):
        return Err(
            HoldoutExportError(
                operation="export",
                detail="holdout identities are not unique across the export",
            )
        )
    return Ok(records)


__all__: Sequence[str] = (
    "BLIND_CASE_FIELDS",
    "BlindHoldoutCase",
    "HoldoutExportClassifier",
    "HoldoutExportError",
    "HoldoutExportPrivate",
    "HoldoutExportProject",
    "HoldoutExportRecord",
    "UNRELEASED_STATUSES",
    "blind_case",
    "export_holdouts",
    "load_unreleased_holdouts",
    "render_blind_jsonl",
    "render_holdout_jsonl",
    "surfaced_or_dropped",
)
