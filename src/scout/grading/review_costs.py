"""Scout review observations projected into existing operating cost components.

No new PAA contract or model-attribution claim. These are corpus-building costs;
attach each action once, never both its component and a priced aggregate.
"""

from __future__ import annotations

import sqlite3

from paa_runtime.operating import OperatingComponent, OperatingPrice
from pydantic import BaseModel

from scout.grading.artifacts import ArtifactDigest, ArtifactError, digest_artifact
from scout.grading.review_types import ReviewDisposition
from scout.result import Err, Ok, Result


class ReviewCostLine(BaseModel):
    action_id: str
    evaluation_id: int
    grade_revision_id: int | None
    source_references: tuple[str, ...]
    component: OperatingComponent


class ReviewCostReport(BaseModel):
    queue_digest: ArtifactDigest
    purpose: str = "corpus_building"
    lines: tuple[ReviewCostLine, ...]


def project_review_cost(disposition: ReviewDisposition) -> ReviewCostLine:
    elapsed = disposition.timing.elapsed_ms
    basis = disposition.pricing
    price = (
        OperatingPrice(
            currency="USD", amount=elapsed * basis.usd_per_hour / 3_600_000, basis=basis.basis
        )
        if elapsed is not None and basis is not None
        else None
    )
    return ReviewCostLine(
        action_id=disposition.action_id,
        evaluation_id=disposition.evaluation_id,
        grade_revision_id=disposition.grade_revision_id,
        source_references=(
            f"urn:scout:review-action:{disposition.action_id}",
            f"urn:scout:review-queue:sha256:{disposition.queue_digest}",
        ),
        component=OperatingComponent(
            kind="corpus_building_human_review", quantity=elapsed, unit="ms", price=price
        ),
    )


def read_review_costs(
    conn: sqlite3.Connection, queue_digest: ArtifactDigest
) -> Result[ReviewCostReport, ArtifactError]:
    try:
        from scout.grading.assistance_types import ReviewQueue

        row = conn.execute(
            "SELECT content FROM analysis_artifacts WHERE digest = ?", (queue_digest,)
        ).fetchone()
        if row is None or not isinstance(row[0], bytes) or digest_artifact(row[0]) != queue_digest:
            return Err(ArtifactError("review_costs", queue_digest, "Queue missing or corrupt"))
        ReviewQueue.model_validate_json(row[0])
        rows = conn.execute(
            "SELECT disposition_json FROM review_dispositions "
            "WHERE queue_digest = ? ORDER BY sequence",
            (queue_digest,),
        )
        observations = tuple(ReviewDisposition.model_validate_json(row[0]) for row in rows)
        return Ok(
            ReviewCostReport(
                queue_digest=queue_digest,
                lines=tuple(
                    project_review_cost(item)
                    for item in observations
                    if item.action.kind != "reconcile"
                ),
            )
        )
    except (sqlite3.Error, ValueError) as exc:
        return Err(ArtifactError("review_costs", queue_digest, f"Cannot read review costs: {exc}"))
