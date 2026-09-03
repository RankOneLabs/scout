"""FastAPI sidecar exposing Scout's grading write path over HTTP.

The web dashboard reads SQLite directly (`web/lib/db.ts`); the only write
entry points that need the state machine, the jig drafter, or feedback-loop
side effects live here: recording a grade, promoting a model-negative case
into the draft/critic flow, and setting a grade's usage override.

Bind address is controlled by `SCOUT_SIDECAR_HOST` (default `127.0.0.1`).
Set it to `0.0.0.0` inside a container so sibling containers on the same
docker network can reach the sidecar. Binding to a non-loopback address
exposes write endpoints to any peer on that network — ensure
`SCOUT_SIDECAR_TOKEN` is set when doing so.

Protected by the `X-Scout-Sidecar-Token` header so casual misuse on a
multi-tenant dev box can't drive writes.

Schema DDL and migrations run exactly once, in the FastAPI lifespan
bootstrap, before the app is marked ready. Every request handler then
opens its own fresh `StateManager(DB_PATH, init_schema=False)` and closes
it on exit — SQLite + WAL handles the concurrent open connections;
per-request isolation keeps transaction scope tight and matches every
other call site in the codebase. Handlers that do SQLite work are ordinary
`def` functions so FastAPI runs them in its threadpool instead of blocking
the event loop; `/healthz` alone stays `async` and constructs no
StateManager, so it keeps responding while a write is in flight. The
promotion handler needs to await genuinely-async domain work (the jig
drafter) and drives it with `asyncio.run` inside its `def` body — the
threadpool thread has no event loop of its own to conflict with.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import anyio.from_thread
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from scout.config import DB_PATH, FEEDBACK_DB_PATH, TRACE_DB_PATH, GradeRecord
from scout.storage.state import (
    GradeValidationError,
    HumanPositivePromotionInProgressError,
    StateManager,
)

logger = logging.getLogger(__name__)

TOKEN_HEADER = "X-Scout-Sidecar-Token"
DEFAULT_PORT = 8799

_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
_BIND_WILDCARDS = frozenset({"0.0.0.0", "::", "0:0:0:0:0:0:0:0"})


def _bare_host(host: str) -> str:
    """Strip port and IPv6 brackets from a host string."""
    host = host.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end != -1 else host[1:]
    if host.count(":") == 1:
        return host.rsplit(":", 1)[0]
    return host


def _is_loopback(host: str) -> bool:
    """Return True if host is a loopback address or name."""
    bare = _bare_host(host)
    if bare in _LOOPBACK_NAMES:
        return True
    parts = bare.split(".")
    if len(parts) == 4:
        try:
            if int(parts[0]) == 127:
                return True
        except ValueError:
            pass
    return False


def _expected_token() -> str | None:
    """Return the configured shared secret, or None if SCOUT_SIDECAR_TOKEN is unset."""
    raw = os.getenv("SCOUT_SIDECAR_TOKEN", "").strip()
    return raw or None


def _is_bind_wildcard(host: str) -> bool:
    """Return True for bind-all wildcard addresses (0.0.0.0, ::, etc.)."""
    return _bare_host(host) in _BIND_WILDCARDS


def _trusted_host_value() -> str:
    """Return the host value used for Host/Origin trust checks.

    Explicit SCOUT_SIDECAR_TRUSTED_HOST takes precedence.  Falls back to
    SCOUT_SIDECAR_HOST only when it is a real hostname, not a wildcard.
    Returns an empty string when the bind address is a wildcard and no
    explicit trusted host is configured; in that case only loopback is trusted.
    """
    explicit = os.getenv("SCOUT_SIDECAR_TRUSTED_HOST", "").strip()
    if explicit:
        return explicit
    bind = os.getenv("SCOUT_SIDECAR_HOST", "127.0.0.1").strip() or "127.0.0.1"
    return "" if _is_bind_wildcard(bind) else bind


def _validate_startup_config(host: str) -> None:
    """Raise SystemExit on configurations that would create an unsafe startup.

    An empty/whitespace token is always an operator mistake.  A non-loopback
    host without any token exposes write endpoints without authorization.
    An explicitly set but empty SCOUT_SIDECAR_TRUSTED_HOST is also an error.
    """
    raw = os.getenv("SCOUT_SIDECAR_TOKEN")
    if raw is not None and not raw.strip():
        raise SystemExit(
            "SCOUT_SIDECAR_TOKEN is set but empty or whitespace-only — "
            "refusing to start with a blank token"
        )
    if not _is_loopback(host) and (raw is None or not raw.strip()):
        raise SystemExit(
            f"SCOUT_SIDECAR_HOST={host!r} is non-loopback; "
            "SCOUT_SIDECAR_TOKEN must be set to a non-empty value"
        )
    trusted_raw = os.getenv("SCOUT_SIDECAR_TRUSTED_HOST")
    if trusted_raw is not None and not trusted_raw.strip():
        raise SystemExit(
            "SCOUT_SIDECAR_TRUSTED_HOST is set but empty or whitespace-only — "
            "refusing to start with a blank trusted host"
        )


def _is_trusted_host(host_header: str, configured_host: str) -> bool:
    """Return True if the Host header value is a trusted endpoint."""
    bare = _bare_host(host_header)
    if _is_loopback(bare):
        return True
    return bare == _bare_host(configured_host)


def _is_trusted_browser_origin(origin_or_referer: str, configured_host: str) -> bool:
    """Return True if a browser Origin or Referer header points to a trusted host.

    Uses urlparse so userinfo attacks (http://localhost@evil.example.com) are
    rejected: parsed.hostname always returns the real authority hostname.
    Fails closed on any malformed or scheme-less value.
    """
    value = origin_or_referer.strip()
    try:
        parsed = urlparse(value)
        host = parsed.hostname  # strips userinfo, lowercases, returns None on failure
    except Exception:
        return False
    if not host:
        return False
    return _is_trusted_host(host, configured_host)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Run schema DDL/migrations exactly once, before the app is ready.

    Opens a bootstrap StateManager (default `init_schema=True`), lets it
    apply the baseline schema and any pending migrations, then closes it.
    Every per-request StateManager afterward opens with
    `init_schema=False` and skips straight to using the already-current
    schema. A failure here aborts startup — no request is served against
    a database whose schema initialization didn't succeed.
    """
    bootstrap = StateManager(db_path=DB_PATH)
    bootstrap.close()
    yield


app = FastAPI(title="scout-grading-api", version="0.1.0", lifespan=_lifespan)


@app.exception_handler(GradeValidationError)
async def _grade_validation_error_handler(
    request: Request, exc: GradeValidationError
) -> JSONResponse:
    return JSONResponse(status_code=400, content={"errors": exc.errors})


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Enforce token auth, Host validation, and browser-origin checks on write requests.

    /healthz is always open. Non-browser clients (no Origin/Referer) pass
    through after token and Host checks.
    """
    if request.url.path == "/healthz":
        return await call_next(request)

    # --- Token auth ---
    expected = _expected_token()
    if expected is not None:
        supplied = request.headers.get(TOKEN_HEADER, "")
        if not secrets.compare_digest(supplied, expected):
            return JSONResponse(
                status_code=401,
                content={"detail": "missing or invalid sidecar token"},
            )

    # --- Host validation ---
    host_header = request.headers.get("host", "")
    configured_host = _trusted_host_value()
    if not host_header or not _is_trusted_host(host_header, configured_host):
        return JSONResponse(
            status_code=403,
            content={"detail": "untrusted Host header"},
        )

    # --- Origin / Referer (browser defense-in-depth) ---
    origin = request.headers.get("origin", "")
    referer = request.headers.get("referer", "")
    for header_value in (origin, referer):
        if header_value and not _is_trusted_browser_origin(header_value, configured_host):
            return JSONResponse(
                status_code=403,
                content={"detail": "untrusted Origin or Referer"},
            )

    return await call_next(request)


# ----- Request models -----


class UsageOverrideRequest(BaseModel):
    """`mode`/`reason` are `Any` on purpose: StateManager.save_grade_usage_override
    is the sole domain validator (mode membership, exclude's non-blank reason
    requirement), so a wrong type here must reach it unchanged rather than
    being coerced or rejected by pydantic first."""

    mode: Any = None
    reason: Any = None


# ----- Helpers -----


def _state_for_request() -> StateManager:
    # Schema DDL/migrations already ran once in the lifespan bootstrap;
    # each request just needs its own connection with PRAGMAs applied.
    return StateManager(db_path=DB_PATH, init_schema=False)


def _grade_row_to_response(row: Any) -> dict[str, Any]:
    """Serialize a raw `grades` row for the response body.

    `dimensions` is stored as a JSON string; every other column already
    matches the wire shape the web client expects.
    """
    data = dict(row)
    raw_dims = data.get("dimensions")
    if raw_dims:
        try:
            data["dimensions"] = json.loads(raw_dims)
        except (json.JSONDecodeError, TypeError):
            data["dimensions"] = None
    return data


def _grade_record_from_body(
    evaluation: Any, evaluation_id: int, body: dict[str, Any]
) -> GradeRecord:
    return GradeRecord(
        post_id=evaluation["post_id"],
        evaluation_id=evaluation_id,
        scan_id=evaluation["scan_id"],
        source="web",
        graded_at=datetime.now(UTC),
        needs_regrade=False,
        relevance_judgment=body["relevance_judgment"],
        action_judgment=body["action_judgment"],
        dimensions=body.get("dimensions"),
        failure_note=body.get("failure_note"),
        factual_offending_claim=body.get("factual_offending_claim"),
        factual_disposition=body.get("factual_disposition"),
        factual_contradicting_evidence=body.get("factual_contradicting_evidence"),
        context_missing_input=body.get("context_missing_input"),
        posture_should_have_been=body.get("posture_should_have_been"),
        implication_implied_claim=body.get("implication_implied_claim"),
        implication_missing_support=body.get("implication_missing_support"),
        edited_text=body.get("edited_text"),
    )


# ----- Routes -----


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/grades/{evaluation_id}")
def grade_endpoint(evaluation_id: int, request: Request) -> dict[str, Any]:
    """Accepts the grade payload as a plain JSON object (not a typed
    pydantic request model) so validate_grade_envelope inspects the exact
    bytes the client sent — a typed model would silently drop or coerce
    unknown/malformed fields before the shared contract ever saw them.

    validate_grade_envelope runs the same Draft 2020-12 schema Python,
    TypeScript, and the eval corpus all consume; save_grade below
    revalidates the constructed GradeRecord once more with full stored
    context (schema_version, source, evaluation identity) as the
    authoritative persistence-time check.

    This is a `def` handler like every other stateful route, so the
    synchronous SQLite work below runs in FastAPI's threadpool rather
    than the event loop. `request.json()` is a coroutine bound to the
    ASGI connection's receive channel, which belongs to the server's
    running event loop — not this threadpool thread. `asyncio.run` would
    spin up an unrelated loop with no connection to that channel, so the
    body is read via `anyio.from_thread.run`, which schedules the
    coroutine back on the same event loop that dispatched this worker
    thread and waits for the result.
    """
    from scout.grading.service import validate_grade_envelope

    state = _state_for_request()
    try:
        evaluation = state.get_evaluation(evaluation_id)
        if evaluation is None:
            raise HTTPException(
                status_code=404, detail=f"evaluation {evaluation_id} not found"
            )

        try:
            body = anyio.from_thread.run(request.json)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid JSON body") from exc
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400, detail="request body must be a JSON object"
            )

        envelope_errors = validate_grade_envelope(body, evaluation["posture"])
        if envelope_errors:
            raise GradeValidationError(envelope_errors)

        grade = _grade_record_from_body(evaluation, evaluation_id, body)
        grade_id = state.save_grade(grade)
        row = state.get_grade_row_by_id(grade_id)
        if row is None:
            raise HTTPException(
                status_code=500,
                detail=f"grade {grade_id} not found immediately after save",
            )
    finally:
        state.close()
    return _grade_row_to_response(row)


@app.post("/grades/{evaluation_id}/promote")
def promote_negative_grade_endpoint(
    evaluation_id: int, request: Request
) -> dict[str, Any]:
    """Grade a model-negative case as positive, then run draft + critic.

    The source false-negative grade and promotion state are durable before
    inference begins. A successful response is stored under a new evaluation
    and returned to the ordinary Drafts queue for its own grade.
    """
    from scout.grading.promotion import (
        NegativeCasePromotionError,
        promote_negative_case,
    )
    from scout.grading.service import validate_grade_envelope
    from scout.replay.runtime import replay_runtime

    state = _state_for_request()
    try:
        evaluation = state.get_evaluation(evaluation_id)
        if evaluation is None:
            raise HTTPException(
                status_code=404, detail=f"evaluation {evaluation_id} not found"
            )
        if bool(evaluation["relevant"]):
            raise HTTPException(
                status_code=409,
                detail="only model-negative evaluations can enter the promotion flow",
            )
        try:
            body = anyio.from_thread.run(request.json)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid JSON body") from exc
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400, detail="request body must be a JSON object"
            )
        envelope_errors = validate_grade_envelope(body, evaluation["posture"])
        if envelope_errors:
            raise GradeValidationError(envelope_errors)
        if body.get("relevance_judgment") != "false_negative":
            raise HTTPException(
                status_code=400,
                detail="promotion requires relevance_judgment=false_negative",
            )

        grade = _grade_record_from_body(evaluation, evaluation_id, body)

        async def _promote() -> Any:
            async with replay_runtime(
                state=state,
                trace_db_path=TRACE_DB_PATH,
                feedback_db_path=FEEDBACK_DB_PATH,
            ) as runtime:
                return await promote_negative_case(
                    state=runtime.state,
                    tracer=runtime.tracer,
                    feedback=runtime.feedback,
                    source_evaluation_id=evaluation_id,
                    grade=grade,
                )

        try:
            outcome = asyncio.run(_promote())
        except HumanPositivePromotionInProgressError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except NegativeCasePromotionError as exc:
            status = {
                "validation": 422,
                "config": 422,
                "generation": 502,
                "persistence": 500,
            }[exc.category]
            raise HTTPException(status_code=status, detail=exc.detail) from exc

        row = state.get_grade_row_by_id(outcome.source_grade_id)
        if row is None:
            raise HTTPException(status_code=500, detail="source grade was not persisted")
        response = _grade_row_to_response(row)
        response["promotion"] = {
            "scan_id": outcome.scan_id,
            "target_evaluation_id": outcome.target_evaluation_id,
            "surface_status": outcome.surface_status,
            "already_completed": outcome.already_completed,
        }
        return response
    finally:
        state.close()


@app.post("/grades/{evaluation_id}/usage-override")
def grade_usage_override_endpoint(
    evaluation_id: int, req: UsageOverrideRequest
) -> dict[str, Any]:
    """Resolves the current grade for `evaluation_id` server-side — the
    browser never selects a grade_id directly — and 404s when no grade
    exists yet. StateManager.save_grade_usage_override is the sole domain
    validator; a GradeValidationError there maps to 400 via the handler
    registered above."""
    state = _state_for_request()
    try:
        grade_id = state.get_grade_id_for_evaluation(evaluation_id)
        if grade_id is None:
            raise HTTPException(
                status_code=404,
                detail=f"no grade found for evaluation {evaluation_id}",
            )
        row = state.save_grade_usage_override(grade_id, mode=req.mode, reason=req.reason)
    finally:
        state.close()
    return dict(row)


def main() -> None:
    """Launch uvicorn on $SCOUT_SIDECAR_HOST:$SCOUT_SIDECAR_PORT (default 127.0.0.1)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    port_raw = os.getenv("SCOUT_SIDECAR_PORT", str(DEFAULT_PORT)).strip()
    try:
        port = int(port_raw)
    except ValueError:
        logger.warning(
            "SCOUT_SIDECAR_PORT=%r not an int; falling back to %d", port_raw, DEFAULT_PORT
        )
        port = DEFAULT_PORT
    host = os.getenv("SCOUT_SIDECAR_HOST", "127.0.0.1").strip() or "127.0.0.1"
    _validate_startup_config(host)
    if _expected_token() is None:
        logger.warning(
            "SCOUT_SIDECAR_TOKEN is unset — sidecar accepts unauthenticated requests"
        )
    if not _is_loopback(host):
        logger.warning(
            "SCOUT_SIDECAR_HOST=%s — sidecar is reachable beyond loopback", host
        )
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
