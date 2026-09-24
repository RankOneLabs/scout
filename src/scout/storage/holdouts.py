"""Typed repository for relevance holdouts and decision provenance.

Two tables, one purpose: keep a relevance decision explainable after the fact,
and keep a held post releasable exactly once even across concurrent or
abandoned attempts.

`relevance_decisions` records what produced one evaluation — classifier, model,
catalogue and router identity, the action and its reason, and for JEV the full
validated answer vector and the complete router decision. Nothing pre-JEV has
a row, because no stored fact identifies which classifier produced it; an
absent row reads as unknown rather than as a guess.

`relevance_holdouts` records one held post. The hold references an immutable
source evaluation; a released outcome that drafts gets a distinct target
evaluation, uniquely linked back, so releasing never overwrites the decision
that was graded. Claims are compare-and-swap with a monotonic fence: a stale or
competing completion is rejected, and a completion that already committed is
readable again rather than applied twice.

Sampling, export and release themselves are not here. This is the storage and
the claim primitives they run on. See docs/relevance-holdouts.md.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from scout.result import Err, Ok, Result
from scout.storage.unit_of_work import UnitOfWork

ClassifierName = Literal["llm", "jev"]
ClassifierAction = Literal["respond", "review", "drop"]
HoldoutStatus = Literal["pending", "claimed", "released", "failed"]
HoldoutLabel = Literal["exclusion", "in_post", "pointer", "none"]
ReleaseAuthority = Literal["label", "recorded_action"]

#: How a stored label resolves to the action a release acts on. Confirmed in
#: the cutover specification; see docs/relevance-holdouts.md.
LABEL_ACTIONS: dict[HoldoutLabel, ClassifierAction] = {
    "exclusion": "drop",
    "in_post": "respond",
    "pointer": "review",
    "none": "drop",
}

DEFAULT_CLAIM_TTL_SECONDS = 900


@dataclass(frozen=True, slots=True)
class HoldoutStorageError:
    """A holdout write was refused. Carries enough to trace which and why."""

    operation: str
    detail: str
    holdout_id: int | None = None
    evaluation_id: int | None = None


@dataclass(frozen=True, slots=True)
class RelevanceDecisionWrite:
    """What produced one evaluation, as recorded beside it."""

    evaluation_id: int
    classifier: ClassifierName
    model: str
    action: ClassifierAction
    reason: str | None = None
    phase_run_id: int | None = None
    catalogue_id: str | None = None
    catalogue_version: str | None = None
    router_version: str | None = None
    answers: dict[str, Any] | None = None
    decision: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RelevanceDecision:
    """One recorded decision, read back."""

    id: int
    evaluation_id: int
    phase_run_id: int | None
    classifier: ClassifierName
    model: str
    catalogue_id: str | None
    catalogue_version: str | None
    router_version: str | None
    action: ClassifierAction
    reason: str | None
    answers: dict[str, Any] | None
    decision: dict[str, Any] | None
    created_at: str


@dataclass(frozen=True, slots=True)
class FrozenHoldoutInput:
    """The input identity a decision was made against, frozen at hold time.

    A label written weeks later joins to the case it was written for through
    this, not through a live read of a post or a project that may since have
    changed.
    """

    platform: str
    platform_id: str
    url: str | None = None
    channel: str | None = None
    text: str | None = None
    parent_author_name: str | None = None
    parent_text: str | None = None
    author_id: str | None = None
    author_name: str | None = None
    author_handle: str | None = None
    project_key: str | None = None
    project_name: str | None = None
    project_description: str | None = None
    keyword_route_id: int | None = None
    dossier_summary_id: str | None = None
    dossier_revision: str | None = None


@dataclass(frozen=True, slots=True)
class HoldoutWrite:
    """A post to hold back from surfacing, with its frozen input identity."""

    evaluation_id: int
    post_id: int
    scan_id: int
    frozen_input: FrozenHoldoutInput
    project_key: str | None = None


@dataclass(frozen=True, slots=True)
class Holdout:
    """One holdout row, read back."""

    id: int
    evaluation_id: int
    post_id: int
    scan_id: int
    project_key: str | None
    status: HoldoutStatus
    held_at: str
    released_at: str | None
    frozen_input: FrozenHoldoutInput
    claim_token: str | None
    claim_fence: int
    claim_owner: str | None
    claim_expires_at: str | None
    release_authority: ReleaseAuthority | None
    release_action: ClassifierAction | None
    label: HoldoutLabel | None
    label_source: str | None
    labelled_at: str | None
    target_evaluation_id: int | None
    attempts: int
    last_error: str | None


@dataclass(frozen=True, slots=True)
class HoldoutClaim:
    """An exclusive, fenced claim on one holdout.

    `fence` is monotonic per holdout. A completion presenting an older fence is
    a claim that was superseded while it was away, and is rejected.
    """

    holdout_id: int
    token: str
    fence: int
    owner: str
    expires_at: str


def _json(value: dict[str, Any] | None) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _decision_row(row: sqlite3.Row) -> RelevanceDecision:
    data = dict(row)
    answers_json = data.pop("answers_json")
    decision_json = data.pop("decision_json")
    return RelevanceDecision(
        **data,
        answers=None if answers_json is None else json.loads(answers_json),
        decision=None if decision_json is None else json.loads(decision_json),
    )


def _holdout_row(row: sqlite3.Row) -> Holdout:
    data = dict(row)
    frozen = json.loads(data.pop("frozen_input_json"))
    return Holdout(**data, frozen_input=FrozenHoldoutInput(**frozen))


class HoldoutStore:
    """Relevance decision provenance and holdout lifecycle."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    @property
    def _conn(self) -> sqlite3.Connection:
        return self._uow.db.conn

    # -- decision provenance ------------------------------------------------

    def record_decision(
        self, write: RelevanceDecisionWrite
    ) -> Result[RelevanceDecision, HoldoutStorageError]:
        """Record what produced one evaluation.

        Composable: called inside an already-open transaction it joins via
        savepoint, so a decision and the evaluation it explains roll back
        together.
        """
        try:
            with self._uow.begin_immediate():
                self._conn.execute(
                    "INSERT INTO relevance_decisions "
                    "(evaluation_id, phase_run_id, classifier, model, catalogue_id, "
                    "catalogue_version, router_version, action, reason, answers_json, "
                    "decision_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        write.evaluation_id,
                        write.phase_run_id,
                        write.classifier,
                        write.model,
                        write.catalogue_id,
                        write.catalogue_version,
                        write.router_version,
                        write.action,
                        write.reason,
                        _json(write.answers),
                        _json(write.decision),
                        _now(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            return Err(
                HoldoutStorageError(
                    operation="record_decision",
                    detail=str(exc),
                    evaluation_id=write.evaluation_id,
                )
            )
        recorded = self.get_decision(write.evaluation_id)
        assert recorded is not None
        return Ok(recorded)

    def get_decision(self, evaluation_id: int) -> RelevanceDecision | None:
        row = self._conn.execute(
            "SELECT * FROM relevance_decisions WHERE evaluation_id = ?",
            (evaluation_id,),
        ).fetchone()
        return None if row is None else _decision_row(row)

    # -- holds --------------------------------------------------------------

    def hold(self, write: HoldoutWrite) -> Result[Holdout, HoldoutStorageError]:
        """Hold one decided post back from surfacing.

        The UNIQUE on `evaluation_id` is what makes a second hold on the same
        source evaluation a refusal rather than a duplicate. Composable inside
        the transaction that wrote the evaluation.
        """
        try:
            with self._uow.begin_immediate():
                self._conn.execute(
                    "INSERT INTO relevance_holdouts "
                    "(evaluation_id, post_id, scan_id, project_key, status, held_at, "
                    "frozen_input_json) VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                    (
                        write.evaluation_id,
                        write.post_id,
                        write.scan_id,
                        write.project_key,
                        _now(),
                        json.dumps(
                            asdict(write.frozen_input),
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            return Err(
                HoldoutStorageError(
                    operation="hold",
                    detail=str(exc),
                    evaluation_id=write.evaluation_id,
                )
            )
        held = self.get_by_evaluation(write.evaluation_id)
        assert held is not None
        return Ok(held)

    def get(self, holdout_id: int) -> Holdout | None:
        row = self._conn.execute(
            "SELECT * FROM relevance_holdouts WHERE id = ?", (holdout_id,)
        ).fetchone()
        return None if row is None else _holdout_row(row)

    def get_by_evaluation(self, evaluation_id: int) -> Holdout | None:
        row = self._conn.execute(
            "SELECT * FROM relevance_holdouts WHERE evaluation_id = ?",
            (evaluation_id,),
        ).fetchone()
        return None if row is None else _holdout_row(row)

    def list_pending(self) -> list[Holdout]:
        """Every hold awaiting release, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM relevance_holdouts WHERE status IN ('pending', 'failed') "
            "ORDER BY held_at, id"
        ).fetchall()
        return [_holdout_row(row) for row in rows]

    # -- claims -------------------------------------------------------------

    def claim(
        self,
        holdout_id: int,
        *,
        owner: str,
        ttl_seconds: int = DEFAULT_CLAIM_TTL_SECONDS,
    ) -> Result[HoldoutClaim, HoldoutStorageError]:
        """Take an exclusive, fenced claim on one hold.

        Succeeds against a hold that is pending, previously failed, or held by
        a claim whose lease has expired — an abandoned attempt must not strand
        the post forever. Every success bumps the fence, which is what makes
        the superseded claim's later completion detectable.
        """
        now = datetime.now(UTC)
        token = secrets.token_urlsafe(16)
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
        with self._uow.begin_immediate():
            cursor = self._conn.execute(
                "UPDATE relevance_holdouts "
                "SET status = 'claimed', claim_token = ?, claim_owner = ?, "
                "    claim_expires_at = ?, claim_fence = claim_fence + 1, "
                "    attempts = attempts + 1 "
                "WHERE id = ? AND ("
                "  status IN ('pending', 'failed') "
                "  OR (status = 'claimed' AND claim_expires_at <= ?)"
                ")",
                (token, owner, expires_at, holdout_id, now.isoformat()),
            )
            if cursor.rowcount != 1:
                existing = self.get(holdout_id)
                detail = (
                    "no such holdout"
                    if existing is None
                    else f"holdout is {existing.status} and not claimable"
                )
                return Err(
                    HoldoutStorageError(
                        operation="claim", detail=detail, holdout_id=holdout_id
                    )
                )
            claimed = self.get(holdout_id)
        assert claimed is not None and claimed.claim_expires_at is not None
        return Ok(
            HoldoutClaim(
                holdout_id=holdout_id,
                token=token,
                fence=claimed.claim_fence,
                owner=owner,
                expires_at=claimed.claim_expires_at,
            )
        )

    def complete_release(
        self,
        claim: HoldoutClaim,
        *,
        release_authority: ReleaseAuthority,
        release_action: ClassifierAction,
        label: HoldoutLabel | None = None,
        label_source: str | None = None,
        labelled_at: str | None = None,
        target_evaluation_id: int | None = None,
    ) -> Result[Holdout, HoldoutStorageError]:
        """Finalize a release against a live claim.

        The compare-and-swap matches on token and fence together, so a claim
        that was superseded while it was away cannot complete. A claim whose
        completion already committed reads its own committed row back rather
        than applying a second time, which is what makes a retry after a lost
        response safe.
        """
        if release_authority == "label" and label is None:
            return Err(
                HoldoutStorageError(
                    operation="complete_release",
                    detail="release_authority='label' requires a label",
                    holdout_id=claim.holdout_id,
                )
            )
        if label is not None and LABEL_ACTIONS[label] != release_action:
            return Err(
                HoldoutStorageError(
                    operation="complete_release",
                    detail=(
                        f"label {label!r} resolves to {LABEL_ACTIONS[label]!r}, "
                        f"not {release_action!r}"
                    ),
                    holdout_id=claim.holdout_id,
                )
            )

        try:
            with self._uow.begin_immediate():
                cursor = self._conn.execute(
                    "UPDATE relevance_holdouts "
                    "SET status = 'released', released_at = ?, "
                    "    release_authority = ?, release_action = ?, label = ?, "
                    "    label_source = ?, labelled_at = ?, "
                    "    target_evaluation_id = ?, last_error = NULL, "
                    "    claim_expires_at = NULL "
                    "WHERE id = ? AND status = 'claimed' "
                    "  AND claim_token = ? AND claim_fence = ?",
                    (
                        _now(),
                        release_authority,
                        release_action,
                        label,
                        label_source,
                        labelled_at,
                        target_evaluation_id,
                        claim.holdout_id,
                        claim.token,
                        claim.fence,
                    ),
                )
                applied = cursor.rowcount == 1
        except sqlite3.IntegrityError as exc:
            return Err(
                HoldoutStorageError(
                    operation="complete_release",
                    detail=str(exc),
                    holdout_id=claim.holdout_id,
                )
            )

        current = self.get(claim.holdout_id)
        if applied:
            assert current is not None
            return Ok(current)
        return self._explain_refused_completion(claim, current)

    def fail_attempt(
        self, claim: HoldoutClaim, *, detail: str
    ) -> Result[Holdout, HoldoutStorageError]:
        """Record a failed release attempt and return the hold to the queue."""
        with self._uow.begin_immediate():
            cursor = self._conn.execute(
                "UPDATE relevance_holdouts "
                "SET status = 'failed', last_error = ?, claim_expires_at = NULL "
                "WHERE id = ? AND status = 'claimed' "
                "  AND claim_token = ? AND claim_fence = ?",
                (detail, claim.holdout_id, claim.token, claim.fence),
            )
            applied = cursor.rowcount == 1
        current = self.get(claim.holdout_id)
        if applied:
            assert current is not None
            return Ok(current)
        return self._explain_refused_completion(claim, current)

    def _explain_refused_completion(
        self, claim: HoldoutClaim, current: Holdout | None
    ) -> Result[Holdout, HoldoutStorageError]:
        """Decide whether a non-applying completion is idempotent or stale."""
        if current is None:
            return Err(
                HoldoutStorageError(
                    operation="complete_release",
                    detail="no such holdout",
                    holdout_id=claim.holdout_id,
                )
            )
        if (
            current.status in ("released", "failed")
            and current.claim_token == claim.token
            and current.claim_fence == claim.fence
        ):
            # This claim's own completion already committed. Reading it back
            # is the idempotent answer, not a second application.
            return Ok(current)
        if current.claim_fence > claim.fence:
            return Err(
                HoldoutStorageError(
                    operation="complete_release",
                    detail=(
                        f"claim fence {claim.fence} is stale; holdout is at "
                        f"{current.claim_fence}"
                    ),
                    holdout_id=claim.holdout_id,
                )
            )
        return Err(
            HoldoutStorageError(
                operation="complete_release",
                detail=f"holdout is {current.status} and does not match this claim",
                holdout_id=claim.holdout_id,
            )
        )

    # -- reads for export ---------------------------------------------------

    def pending_with_decisions(self) -> Sequence[tuple[Holdout, RelevanceDecision | None]]:
        """Every pending hold beside the decision that produced it.

        The decision is None for a hold on an evaluation that predates
        decision recording, which reads as unknown provenance rather than as
        an absent hold.
        """
        return [
            (holdout, self.get_decision(holdout.evaluation_id))
            for holdout in self.list_pending()
        ]
