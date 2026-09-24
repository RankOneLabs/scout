"""The JEV adapter: one bearer-authenticated request to Typesafe System One.

JEV was graded on this exact request, so the request is a contract:

    POST {base}/v1/systemone
    Authorization: Bearer <key>
    {"state": <state>, "model": "jev-latest", "questions": <questions>}

One request, a 60-second timeout, no retry inside the adapter, ``httpx``
directly with no Typesafe SDK import. A failed attempt stays visible and
retryable rather than being papered over by a second call or by handing the
post to the other classifier.

The bearer credential is carried in a header and nowhere else. It is never
returned in an error, never stored on a decision, and never logged.

Answer validation happens here, before ``route`` is called: every question
asked must come back as ``{"type": "noul", "noul": <number>}``. A short,
mistyped or malformed answer vector is a failure, never a silent negative.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from scout.result import Err, Ok, Result
from scout.scanning.jev_state import JevState

logger = logging.getLogger("scout.scanning.jev")

SYSTEM_ONE_PATH = "/v1/systemone"
DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"
REQUEST_TIMEOUT_SECONDS = 60.0
ANSWER_TYPE = "noul"


@dataclass(frozen=True, slots=True)
class JevFailure:
    """A JEV attempt failed. Retryable: the post stays unevaluated.

    ``detail`` is built from the response status and body shape only. No header
    and no credential ever reaches it.
    """

    operation: str
    detail: str
    status: int | None = None


@dataclass(frozen=True, slots=True)
class JevEndpoint:
    """Where a request goes and what answers it. Holds the credential."""

    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL

    @property
    def url(self) -> str:
        return f"{self.base_url.rstrip('/')}{SYSTEM_ONE_PATH}"


@dataclass(frozen=True, slots=True)
class JevRequest:
    """One post's request: the projected state and the catalogue's questions."""

    endpoint: JevEndpoint
    state: JevState
    questions: Mapping[str, Any]

    def body(self) -> dict[str, Any]:
        """The exact JSON body. Key order is the graded order."""
        return {
            "state": self.state.as_payload(),
            "model": self.endpoint.model,
            "questions": dict(self.questions),
        }

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.endpoint.api_key}",
            "Content-Type": "application/json",
        }


@dataclass(frozen=True, slots=True)
class JevAnswers:
    """One validated answer vector.

    ``probabilities`` is what ``route`` consumes. ``raw`` is every answer as
    returned, kept whole so a stored decision can be re-explained against the
    answers that produced it rather than against a lossy projection of them.
    """

    probabilities: Mapping[str, float]
    raw: Mapping[str, Any]


def _answers_payload(body: Any) -> Mapping[str, Any] | None:
    """Locate the answers mapping in a response body.

    The wire contract fixes the answer shape but not its container. A body that
    names its answers under ``answers`` is read that way; a body that is itself
    the mapping of question name to answer is read as-is. Anything else is a
    failure rather than a guess.
    """
    if not isinstance(body, Mapping):
        return None
    nested = body.get("answers")
    if isinstance(nested, Mapping):
        return nested
    return body


def validate_answers(
    body: Any, questions: Mapping[str, Any]
) -> Result[JevAnswers, JevFailure]:
    """Require a well-formed noul answer for every question asked."""
    payload = _answers_payload(body)
    if payload is None:
        return Err(
            JevFailure(
                operation="validate_answers",
                detail=f"response body is {type(body).__name__}, expected an object",
            )
        )

    probabilities: dict[str, float] = {}
    names = list(questions) + [
        name for name in payload if isinstance(name, str)
        and name.startswith("excl_") and name not in questions
    ]
    for name in names:
        answer = payload.get(name)
        if not isinstance(answer, Mapping):
            return Err(
                JevFailure(
                    operation="validate_answers",
                    detail=f"missing or non-object answer for {name!r}",
                )
            )
        if answer.get("type") != ANSWER_TYPE:
            return Err(
                JevFailure(
                    operation="validate_answers",
                    detail=(
                        f"answer for {name!r} has type {answer.get('type')!r}, "
                        f"expected {ANSWER_TYPE!r}"
                    ),
                )
            )
        value = answer.get(ANSWER_TYPE)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value)):
            return Err(
                JevFailure(
                    operation="validate_answers",
                    detail=f"answer for {name!r} has a non-numeric or non-finite {ANSWER_TYPE}",
                )
            )
        probabilities[name] = float(value)

    return Ok(
        JevAnswers(
            probabilities=probabilities,
            raw={name: payload[name] for name in names},
        )
    )


async def call_jev(
    request: JevRequest,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Result[JevAnswers, JevFailure]:
    """Make the one request and validate its answers.

    The IO boundary for the JEV path: every transport and decoding failure is
    caught here and converted to a ``JevFailure``. Nothing above this deals in
    exceptions. ``transport`` is a seam for tests; production passes none.
    """
    try:
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SECONDS, transport=transport
        ) as client:
            response = await client.post(
                request.endpoint.url,
                json=request.body(),
                headers=request.headers(),
            )
    except httpx.TimeoutException as exc:
        return Err(
            JevFailure(
                operation="call_jev",
                detail=f"request timed out after {REQUEST_TIMEOUT_SECONDS:g}s: {exc!s}",
            )
        )
    except httpx.HTTPError as exc:
        return Err(JevFailure(operation="call_jev", detail=f"request failed: {exc!s}"))

    if response.status_code >= 400:
        logger.warning(
            "JEV request failed with status %s for %s",
            response.status_code,
            request.endpoint.url,
        )
        return Err(
            JevFailure(
                operation="call_jev",
                detail=f"HTTP {response.status_code}",
                status=response.status_code,
            )
        )

    try:
        body: Any = response.json()
    except ValueError as exc:
        return Err(
            JevFailure(
                operation="call_jev",
                detail=f"response body is not JSON: {exc!s}",
                status=response.status_code,
            )
        )

    validated = validate_answers(body, request.questions)
    if isinstance(validated, Err):
        return Err(
            JevFailure(
                operation=validated.error.operation,
                detail=validated.error.detail,
                status=response.status_code,
            )
        )
    return validated
