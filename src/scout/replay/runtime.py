"""Shared async resource lifetime for inbound replay/feedback operations.

Bundles a `StateManager`, `TracingLogger`, and the real pinned
`SQLiteFeedbackLoop` into one async context manager so `replay_cli`'s
preview, execute, retry, and report handlers stop repeating the same
nested `StateManager` / `SQLiteTracer` / `SQLiteFeedbackLoop` try/finally
pyramid.

Owned by the inbound replay/feedback domain. It began as a copy of the
outbound content engine's runtime bundle so replay would survive that
engine leaving this repository; the content engine has since left, and
this is now the only resource-lifetime context manager in Scout.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from jig import FeedbackLoop, SQLiteFeedbackLoop, SQLiteTracer, TracingLogger

from scout.config import DB_PATH, FEEDBACK_DB_PATH, TRACE_DB_PATH
from scout.storage.state import StateManager

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ReplayRuntime:
    """Per-use bundle of the resources a replay/feedback operation needs."""

    state: StateManager
    tracer: TracingLogger
    feedback: FeedbackLoop


@asynccontextmanager
async def replay_runtime(
    *,
    db_path: str = DB_PATH,
    trace_db_path: str = TRACE_DB_PATH,
    feedback_db_path: str = FEEDBACK_DB_PATH,
    state: StateManager | None = None,
) -> AsyncIterator[ReplayRuntime]:
    """Yield a `ReplayRuntime`, closing only the resources this call owns.

    When `state` is omitted, opens and owns a CLI-style `StateManager`
    (the normal schema-initializing constructor) and closes it on exit.
    When `state` is supplied, ownership stays with the caller: this
    context manager never closes or re-initializes it. Never constructs a
    `RecordingFeedbackLoop`; the tracer and feedback loop are always the
    real SQLite-backed implementations.

    Resources close in reverse acquisition order: feedback, then tracer,
    then state (if owned). Every cleanup failure is logged. A cleanup
    failure never replaces an exception already propagating out of the
    block — the original fault stays the visible one — but when the
    block exited cleanly, a cleanup failure propagates itself as an
    unexpected fault.
    """
    owns_state = state is None
    if state is None:
        state = StateManager(db_path=db_path)
    tracer = SQLiteTracer(db_path=trace_db_path)
    feedback = SQLiteFeedbackLoop(db_path=feedback_db_path)

    in_flight_error = False
    try:
        yield ReplayRuntime(state=state, tracer=tracer, feedback=feedback)
    except BaseException:
        in_flight_error = True
        raise
    finally:
        cleanup_error: BaseException | None = None
        try:
            await feedback.close()
        except Exception as exc:
            logger.exception("replay_runtime: feedback.close() failed")
            cleanup_error = cleanup_error or exc
        try:
            await tracer.close()
        except Exception as exc:
            logger.exception("replay_runtime: tracer.close() failed")
            cleanup_error = cleanup_error or exc
        if owns_state:
            try:
                state.close()
            except Exception as exc:
                logger.exception("replay_runtime: state.close() failed")
                cleanup_error = cleanup_error or exc
        if cleanup_error is not None and not in_flight_error:
            raise cleanup_error
