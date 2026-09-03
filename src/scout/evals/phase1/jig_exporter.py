"""Effectful rebuild of a disposable Jig analysis database from Scout's
finalized human grades.

``scout.evals.phase1.export_adapter`` owns the pure projection (one exported
grade record -> ``EvalCase`` + Jig result content/metadata + causal
scores). This module owns every effect around it: reading Scout only
through ``StateManager.export_eval_cases`` (the sole read-only source
boundary), preflighting the configured Jig embedding provider, writing a
fresh uniquely named temporary sibling database, verifying it through
``SQLiteFeedbackLoop``'s public read surfaces, and atomically swapping it
in for the destination with ``os.replace``.

On any validation, provider, write, or interruption failure — including
``KeyboardInterrupt`` and task cancellation — only the temporary artifact
is removed. The preexisting destination database is never opened for
writing and is left byte-for-byte unchanged. A Jig database produced here
is disposable, replaceable analysis state: rebuilding again always starts
from a fresh temporary database, never an append or upsert onto the
existing one.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from jig import FeedbackLoop, FeedbackQuery, SQLiteFeedbackLoop
from jig._embed import ollama_embed

from scout.evals.phase1.export_adapter import (
    CANONICAL_FAILURE_DIMENSIONS,
    ExportedGradeRejected,
    ScoutJigProjection,
    project_exported_grades,
)
from scout.storage.state import StateManager

EmbeddingProvider = Callable[[str], Awaitable[np.ndarray]]
FeedbackLoopFactory = Callable[[str], FeedbackLoop]

# Fixed, content-free string embedded once during preflight — its only
# purpose is proving the configured provider is reachable and returns a
# usable vector before any temporary or destination database exists.
_PREFLIGHT_TEXT = "scout-finalized-grades-jig-rebuild-preflight"

# Matches SQLiteFeedbackLoop's own default embedding model, so preflight
# exercises exactly the client construction store_result will use.
_DEFAULT_EMBED_MODEL = "nomic-embed-text"

# FeedbackLoop.query() is a search surface, not an exhaustive export: Jig's
# SQLite implementation deliberately bounds its candidate window. Exercise
# that public surface on a representative sample, then use export_eval_set()
# below for exact whole-database result/score verification.
_QUERY_VERIFICATION_SAMPLE_LIMIT = 100


class JigRebuildError(RuntimeError):
    """Raised when the rebuild cannot produce a verified destination
    database. Whenever this is raised, the preexisting destination (if
    any) is left completely untouched."""


@dataclass(frozen=True, slots=True)
class JigRebuildResult:
    """Summary of a successful rebuild."""

    destination: str
    result_count: int
    score_count: int


async def _default_embedding_provider(text: str) -> np.ndarray:
    """The same embedding client construction SQLiteFeedbackLoop uses
    internally. Production wiring always uses this — never a fake."""
    vector: np.ndarray = await ollama_embed(text, _DEFAULT_EMBED_MODEL, None)
    return vector


def _default_feedback_loop_factory(db_path: str) -> FeedbackLoop:
    return SQLiteFeedbackLoop(db_path=db_path)


def _json_check(value: Any, *, where: str, evaluation_id: Any) -> None:
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise JigRebuildError(
            f"evaluation {evaluation_id}: {where} cannot be JSON-serialized: {exc}"
        ) from exc


def _validate_projection_serializable(projection: ScoutJigProjection) -> None:
    """Explicit JSON-serialization pass over every value this projection
    will hand to Jig, run entirely in memory before any database path is
    created or opened."""
    evaluation_id = projection.result_metadata.get("evaluation_id")
    _json_check(projection.eval_case.expected, where="case.expected", evaluation_id=evaluation_id)
    _json_check(projection.eval_case.context, where="case.context", evaluation_id=evaluation_id)
    _json_check(projection.eval_case.metadata, where="case.metadata", evaluation_id=evaluation_id)
    _json_check(projection.result_content, where="result content", evaluation_id=evaluation_id)
    _json_check(projection.result_metadata, where="result metadata", evaluation_id=evaluation_id)
    for score in projection.scores:
        _json_check(
            score.metadata,
            where=f"score[{score.dimension}].metadata",
            evaluation_id=evaluation_id,
        )


async def _preflight_embedding_provider(provider: EmbeddingProvider) -> None:
    """Fail fast, before any temporary or destination database path is
    created, when the configured Jig embedding provider is unavailable or
    misconfigured — store_result requires embeddings for every write."""
    try:
        vector = await provider(_PREFLIGHT_TEXT)
        array = np.asarray(vector, dtype=float)
    except Exception as exc:
        raise JigRebuildError(
            "Jig embedding provider preflight failed — check that the "
            "configured Ollama endpoint is reachable and the embedding "
            f"model is installed: {exc}"
        ) from exc
    if array.size == 0 or not np.all(np.isfinite(array)):
        raise JigRebuildError(
            "Jig embedding provider preflight returned an empty or "
            "non-finite vector — refusing to rebuild against it"
        )


async def _verify_rebuilt_database(
    feedback: FeedbackLoop, *, expected_result_count: int
) -> int:
    """Certify the temporary database through SQLiteFeedbackLoop's public
    query()/export_eval_set() surfaces before it may replace the
    destination. Returns the total verified score count.
    """
    expected_query_count = min(
        expected_result_count, _QUERY_VERIFICATION_SAMPLE_LIMIT
    )
    observed = await feedback.query(
        FeedbackQuery(limit=max(expected_query_count, 1))
    )
    if len(observed) != expected_query_count:
        raise JigRebuildError(
            f"verification failed: expected {expected_query_count} query-sampled "
            f"results, found {len(observed)}"
        )
    for result in observed:
        if not result.metadata or result.metadata.get("evaluation_id") is None:
            raise JigRebuildError(
                "verification failed: a query result is missing complete metadata"
            )
        expected_score_count = len(CANONICAL_FAILURE_DIMENSIONS)
        if len(result.scores) != expected_score_count or any(
            score.metadata is None for score in result.scores
        ):
            raise JigRebuildError(
                "verification failed: a query result does not have exactly "
                f"{expected_score_count} score metadata records"
            )

    cases = await feedback.export_eval_set()
    if len(cases) != expected_result_count:
        raise JigRebuildError(
            f"verification failed: export_eval_set returned {len(cases)} "
            f"cases, expected {expected_result_count}"
        )

    seen_evaluation_ids: set[Any] = set()
    total_scores = 0
    for case in cases:
        case_metadata = case.metadata or {}
        case_evaluation_id = case_metadata.get("evaluation_id")
        if case_evaluation_id is None:
            raise JigRebuildError(
                "verification failed: an exported result is missing evaluation_id metadata"
            )
        scores = case_metadata.get("scores") or []
        expected_score_count = len(CANONICAL_FAILURE_DIMENSIONS)
        if len(scores) != expected_score_count:
            raise JigRebuildError(
                f"verification failed: a result has {len(scores)} scores, "
                f"expected {expected_score_count}"
            )
        dims = [s.get("dimension") for s in scores]
        if dims != list(CANONICAL_FAILURE_DIMENSIONS):
            raise JigRebuildError(
                f"verification failed: score dimension order {dims} does not "
                f"match {list(CANONICAL_FAILURE_DIMENSIONS)}"
            )

        case_evaluation_ids: set[Any] = set()
        for s in scores:
            meta = s.get("metadata")
            if not meta or meta.get("evaluation_id") is None:
                raise JigRebuildError(
                    "verification failed: a score is missing complete metadata"
                )
            case_evaluation_ids.add(meta["evaluation_id"])
        if len(case_evaluation_ids) != 1:
            raise JigRebuildError(
                "verification failed: a single result's scores reference "
                f"more than one evaluation: {sorted(case_evaluation_ids)}"
            )
        evaluation_id = next(iter(case_evaluation_ids))
        if evaluation_id != case_evaluation_id:
            raise JigRebuildError(
                "verification failed: result and score metadata reference different "
                f"evaluations ({case_evaluation_id!r} vs {evaluation_id!r})"
            )
        if evaluation_id in seen_evaluation_ids:
            raise JigRebuildError(
                f"verification failed: evaluation {evaluation_id} is stored "
                "as more than one result"
            )
        seen_evaluation_ids.add(evaluation_id)
        total_scores += len(scores)

    expected_scores = len(CANONICAL_FAILURE_DIMENSIONS) * expected_result_count
    if total_scores != expected_scores:
        raise JigRebuildError(
            f"verification failed: found {total_scores} total scores, "
            f"expected {expected_scores}"
        )
    return total_scores


def _temp_sibling_path(destination: Path) -> Path:
    """A uniquely named temporary database in the destination's own
    directory. Same-directory replacement is what makes the final
    os.replace atomic on the target filesystem."""
    return destination.with_name(f".{destination.name}.rebuild-{uuid.uuid4().hex}.tmp")


async def rebuild_finalized_grades_to_jig(
    scout_db_path: str,
    jig_db_path: str,
    *,
    embedding_provider: EmbeddingProvider | None = None,
    feedback_loop_factory: FeedbackLoopFactory | None = None,
) -> JigRebuildResult:
    """Rebuild ``jig_db_path`` from every finalized grade in
    ``scout_db_path``.

    ``embedding_provider`` and ``feedback_loop_factory`` exist only for
    deterministic test injection; omit both in production so the real
    configured Jig/Ollama components are used.
    """
    provider = embedding_provider or _default_embedding_provider
    factory = feedback_loop_factory or _default_feedback_loop_factory

    # Read-only Scout boundary: export, then close before any Jig I/O.
    state = StateManager(scout_db_path)
    try:
        records = state.export_eval_cases()
    finally:
        state.close()

    # Project and validate the complete input sequence in memory —
    # including a JSON-serialization pass over every outgoing value —
    # before any temporary or destination database path is touched.
    try:
        projections = project_exported_grades(records)
    except ExportedGradeRejected as exc:
        raise JigRebuildError(str(exc)) from exc

    for projection in projections:
        _validate_projection_serializable(projection)

    await _preflight_embedding_provider(provider)

    destination = Path(jig_db_path)
    tmp_path = _temp_sibling_path(destination)

    feedback = factory(str(tmp_path))
    try:
        for projection in projections:
            result_id = await feedback.store_result(
                projection.result_content,
                projection.eval_case.input,
                projection.result_metadata,
            )
            await feedback.score(result_id, list(projection.scores))

        score_count = await _verify_rebuilt_database(
            feedback, expected_result_count=len(projections)
        )
        await feedback.close()
    except BaseException:
        with contextlib.suppress(Exception):
            await feedback.close()
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise

    try:
        os.replace(tmp_path, destination)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise

    return JigRebuildResult(
        destination=str(destination),
        result_count=len(projections),
        score_count=score_count,
    )


__all__ = [
    "EmbeddingProvider",
    "FeedbackLoopFactory",
    "JigRebuildError",
    "JigRebuildResult",
    "rebuild_finalized_grades_to_jig",
]
