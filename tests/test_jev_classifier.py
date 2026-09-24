"""The JEV adapter, catalogue loader, state projection, and JEV-only config.

The adapter tests drive a mock transport and assert the request Scout actually
puts on the wire: endpoint, bearer header, body, model, exactly one request,
the timeout, and that every failure mode comes back as a typed retryable
failure rather than an exception or a silent negative.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from scout.config import Account, Message, SourceParent
from scout.registry import ProjectTarget
from scout.result import Err, Ok
from scout.scanning.jev import (
    DEFAULT_MODEL,
    REQUEST_TIMEOUT_SECONDS,
    JevEndpoint,
    JevRequest,
    call_jev,
    validate_answers,
)
from scout.scanning.jev_catalogue import load_jev_catalogue
from scout.scanning.jev_router import route
from scout.scanning.jev_state import (
    JevProject,
    build_jev_state,
    project_from_target,
    state_input_from_message,
    state_input_from_record,
)

CATALOGUE = Path("tests/fixtures/relevance/routed-features.fixture.yaml")

PROJECT = JevProject(key="agent-ops", name="Agent Ops", description="A description.")


def _message(**overrides: Any) -> Message:
    defaults: dict[str, Any] = {
        "platform": "farcaster",
        "platform_id": "0xabc",
        "channel_name": "agents",
        "channel_id": "agents",
        "author": Account(
            platform="farcaster", id="42", name="Ada", handle="ada"
        ),
        "content": "our planner retries every failed step twice",
        "created_at": datetime(2026, 9, 1, tzinfo=UTC),
        "url": "https://warpcast.com/ada/0xabc",
    }
    return Message(**(defaults | overrides))


def _questions() -> Mapping[str, Any]:
    loaded = load_jev_catalogue(CATALOGUE)
    assert isinstance(loaded, Ok), loaded
    return loaded.value.questions


def _request(**endpoint_overrides: Any) -> JevRequest:
    endpoint = JevEndpoint(api_key="secret-key", **endpoint_overrides)
    state = build_jev_state(state_input_from_message(_message()), PROJECT)
    return JevRequest(endpoint=endpoint, state=state, questions=_questions())


def _noul_body(questions: Mapping[str, Any], value: float = 0.9) -> dict[str, Any]:
    return {"answers": {name: {"type": "noul", "noul": value} for name in questions}}


# --------------------------------------------------------------------------
# Catalogue loading
# --------------------------------------------------------------------------


def test_catalogue_version_is_a_sha256_digest() -> None:
    loaded = load_jev_catalogue(CATALOGUE)
    assert isinstance(loaded, Ok)
    assert len(loaded.value.version) == 64


def test_catalogue_version_is_stable_across_loads() -> None:
    first = load_jev_catalogue(CATALOGUE)
    second = load_jev_catalogue(CATALOGUE)
    assert isinstance(first, Ok) and isinstance(second, Ok)
    assert first.value.version == second.value.version


def test_catalogue_id_and_decide_read_from_the_document() -> None:
    loaded = load_jev_catalogue(CATALOGUE)
    assert isinstance(loaded, Ok)
    assert (loaded.value.id, loaded.value.decide) == (
        "routed-features-fixture",
        "agent_ops_route/v1",
    )


def test_missing_catalogue_returns_a_typed_error() -> None:
    result = load_jev_catalogue("tests/fixtures/relevance/does-not-exist.yaml")
    assert isinstance(result, Err)
    assert "could not read catalogue" in result.error.detail


def test_catalogue_missing_a_required_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "partial.yaml"
    path.write_text("id: x\ndecide: y\n", encoding="utf-8")
    result = load_jev_catalogue(path)
    assert isinstance(result, Err)
    assert "missing" in result.error.detail


def test_catalogue_with_empty_questions_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text(
        "id: x\ndecide: y\ndescription: z\nstate: {}\nquestions: {}\n", encoding="utf-8"
    )
    result = load_jev_catalogue(path)
    assert isinstance(result, Err)
    assert "non-empty" in result.error.detail


def test_catalogue_that_is_not_an_object_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "list.yaml"
    path.write_text("- one\n- two\n", encoding="utf-8")
    result = load_jev_catalogue(path)
    assert isinstance(result, Err)
    assert "must be an object" in result.error.detail


# --------------------------------------------------------------------------
# State projection
# --------------------------------------------------------------------------


def test_state_payload_matches_the_authoritative_projection() -> None:
    state = build_jev_state(state_input_from_message(_message()), PROJECT)
    payload = state.as_payload()
    assert set(payload) == {"post", "parent_context_only", "author", "project"}
    assert set(payload["post"]) == {"platform", "channel", "url", "text"}
    assert set(payload["author"]) == {"name", "handle"}
    assert set(payload["project"]) == {"key", "name", "description"}


def test_post_channel_comes_from_channel_name_and_text_from_content() -> None:
    payload = build_jev_state(
        state_input_from_message(_message()), PROJECT
    ).as_payload()
    assert payload["post"]["channel"] == "agents"
    assert payload["post"]["text"] == "our planner retries every failed step twice"


def test_parent_context_only_is_null_without_a_parent() -> None:
    payload = build_jev_state(
        state_input_from_message(_message()), PROJECT
    ).as_payload()
    assert payload["parent_context_only"] is None


def test_parent_context_only_carries_author_name_and_text() -> None:
    parent = SourceParent(
        id="0xparent",
        author=Account(platform="farcaster", id="7", name="Grace", handle="grace"),
        text="what retry budget do you use",
        url="https://warpcast.com/grace/0xparent",
    )
    payload = build_jev_state(
        state_input_from_message(_message(parent=parent)), PROJECT
    ).as_payload()
    assert payload["parent_context_only"] == {
        "author_name": "Grace",
        "text": "what retry budget do you use",
    }


def test_an_empty_url_projects_as_absent() -> None:
    payload = build_jev_state(
        state_input_from_message(_message(url="")), PROJECT
    ).as_payload()
    assert payload["post"]["url"] is None


def test_an_empty_post_text_stays_an_empty_string() -> None:
    payload = build_jev_state(
        state_input_from_message(_message(content="")), PROJECT
    ).as_payload()
    assert payload["post"]["text"] == ""


def test_record_projection_reads_the_population_export_fields() -> None:
    record = {
        "platform": "bluesky",
        "channel": "feed",
        "url": "https://bsky.app/x",
        "text": "a post",
        "parent_author_name": "Grace",
        "parent_text": "a parent",
        "author_name": "Ada",
        "author_handle": "ada",
    }
    payload = build_jev_state(state_input_from_record(record), PROJECT).as_payload()
    assert payload["post"] == {
        "platform": "bluesky",
        "channel": "feed",
        "url": "https://bsky.app/x",
        "text": "a post",
    }
    assert payload["parent_context_only"] == {
        "author_name": "Grace",
        "text": "a parent",
    }


def test_record_projection_treats_absent_fields_as_null() -> None:
    payload = build_jev_state(
        state_input_from_record({"platform": "discord"}), PROJECT
    ).as_payload()
    assert payload["post"] == {
        "platform": "discord",
        "channel": None,
        "url": None,
        "text": None,
    }
    assert payload["parent_context_only"] is None
    assert payload["author"] == {"name": None, "handle": None}


def test_a_parent_with_only_one_field_still_carries_context() -> None:
    payload = build_jev_state(
        state_input_from_record({"platform": "discord", "parent_text": "a parent"}),
        PROJECT,
    ).as_payload()
    assert payload["parent_context_only"] == {"author_name": None, "text": "a parent"}


def test_project_from_target_drops_link_and_dossier_id() -> None:
    target = ProjectTarget(
        key="agent-ops",
        name="Agent Ops",
        description="A description.",
        link="https://example.invalid",
        dossier_summary_id="d1",
    )
    assert project_from_target(target) == PROJECT


# --------------------------------------------------------------------------
# Answer validation
# --------------------------------------------------------------------------


def test_valid_answers_become_probabilities() -> None:
    questions = _questions()
    result = validate_answers(_noul_body(questions, 0.25), questions)
    assert isinstance(result, Ok)
    assert set(result.value.probabilities) == set(questions)
    assert all(value == 0.25 for value in result.value.probabilities.values())


def test_answers_may_arrive_as_the_body_itself() -> None:
    questions = _questions()
    body = {name: {"type": "noul", "noul": 0.5} for name in questions}
    result = validate_answers(body, questions)
    assert isinstance(result, Ok)
    assert set(result.value.probabilities) == set(questions)


def test_raw_answers_are_retained_whole() -> None:
    questions = _questions()
    body = {
        "answers": {
            name: {"type": "noul", "noul": 0.5, "rationale": "because"}
            for name in questions
        }
    }
    result = validate_answers(body, questions)
    assert isinstance(result, Ok)
    assert all(
        answer["rationale"] == "because" for answer in result.value.raw.values()
    )


def test_a_short_answer_vector_is_rejected() -> None:
    questions = _questions()
    body = _noul_body(questions)
    body["answers"].pop("needs_thread")
    result = validate_answers(body, questions)
    assert isinstance(result, Err)
    assert "needs_thread" in result.error.detail


def test_a_non_noul_answer_type_is_rejected() -> None:
    questions = _questions()
    body = _noul_body(questions)
    body["answers"]["needs_thread"] = {"type": "choice", "choice": "yes"}
    result = validate_answers(body, questions)
    assert isinstance(result, Err)
    assert "expected 'noul'" in result.error.detail


def test_a_non_numeric_noul_is_rejected() -> None:
    questions = _questions()
    body = _noul_body(questions)
    body["answers"]["needs_thread"] = {"type": "noul", "noul": "0.9"}
    result = validate_answers(body, questions)
    assert isinstance(result, Err)
    assert "non-numeric" in result.error.detail


def test_a_boolean_noul_is_rejected() -> None:
    """bool is an int in Python; a true/false answer is not a probability."""
    questions = _questions()
    body = _noul_body(questions)
    body["answers"]["needs_thread"] = {"type": "noul", "noul": True}
    result = validate_answers(body, questions)
    assert isinstance(result, Err)
    assert "non-numeric" in result.error.detail


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_noul_is_rejected(value: float) -> None:
    body = _noul_body(_questions())
    body["answers"]["needs_thread"] = {"type": "noul", "noul": value}
    result = validate_answers(body, _questions())
    assert isinstance(result, Err)
    assert "non-finite" in result.error.detail


@pytest.mark.parametrize("value", [-0.1, 1.1, 10**1000])
def test_an_out_of_range_noul_is_rejected(value: float | int) -> None:
    body = _noul_body(_questions())
    body["answers"]["needs_thread"] = {"type": "noul", "noul": value}
    result = validate_answers(body, _questions())
    assert isinstance(result, Err)
    assert "out-of-range" in result.error.detail


def test_an_extra_exclusion_is_preserved_for_the_router() -> None:
    body = _noul_body(_questions(), 0.0)
    body["answers"]["excl_new"] = {"type": "noul", "noul": 0.9}
    result = validate_answers(body, _questions())
    assert isinstance(result, Ok)
    assert result.value.probabilities["excl_new"] == 0.9


def test_a_malformed_extra_exclusion_is_rejected() -> None:
    body = _noul_body(_questions(), 0.0)
    body["answers"]["excl_new"] = {"type": "noul", "noul": float("inf")}
    assert isinstance(validate_answers(body, _questions()), Err)


def test_a_non_object_body_is_rejected() -> None:
    result = validate_answers(["not", "an", "object"], _questions())
    assert isinstance(result, Err)
    assert "expected an object" in result.error.detail


# --------------------------------------------------------------------------
# The request on the wire
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_targets_the_system_one_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_noul_body(_questions()))

    await call_jev(_request(), transport=httpx.MockTransport(handler))
    assert [str(request.url) for request in seen] == [
        "https://api.typesafe.ai/v1/systemone"
    ]


@pytest.mark.asyncio
async def test_request_carries_the_bearer_credential() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_noul_body(_questions()))

    await call_jev(_request(), transport=httpx.MockTransport(handler))
    assert seen[0].headers["authorization"] == "Bearer secret-key"


@pytest.mark.asyncio
async def test_request_body_is_state_model_and_questions() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_noul_body(_questions()))

    request = _request()
    await call_jev(request, transport=httpx.MockTransport(handler))
    body = json.loads(seen[0].content)
    assert set(body) == {"state", "model", "questions"}
    assert body["model"] == DEFAULT_MODEL
    assert body["state"] == request.state.as_payload()
    assert body["questions"] == dict(request.questions)


@pytest.mark.asyncio
async def test_questions_are_sent_verbatim_with_literal_criteria_keys() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_noul_body(_questions()))

    await call_jev(_request(), transport=httpx.MockTransport(handler))
    questions = json.loads(seen[0].content)["questions"]
    assert {key for q in questions.values() for key in q["criteria"]} == {
        "true",
        "false",
    }


@pytest.mark.asyncio
async def test_a_configured_base_url_replaces_the_default() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_noul_body(_questions()))

    await call_jev(
        _request(base_url="https://typesafe.internal/"),
        transport=httpx.MockTransport(handler),
    )
    assert str(seen[0].url) == "https://typesafe.internal/v1/systemone"


@pytest.mark.asyncio
async def test_the_client_timeout_is_sixty_seconds() -> None:
    timeouts: list[httpx.Timeout | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions.get("timeout"))
        return httpx.Response(200, json=_noul_body(_questions()))

    await call_jev(_request(), transport=httpx.MockTransport(handler))
    assert timeouts[0] == {
        "connect": REQUEST_TIMEOUT_SECONDS,
        "read": REQUEST_TIMEOUT_SECONDS,
        "write": REQUEST_TIMEOUT_SECONDS,
        "pool": REQUEST_TIMEOUT_SECONDS,
    }


@pytest.mark.asyncio
async def test_a_failing_status_makes_exactly_one_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="unavailable")

    result = await call_jev(_request(), transport=httpx.MockTransport(handler))
    assert calls == 1
    assert isinstance(result, Err)
    assert result.error.status == 503


@pytest.mark.asyncio
async def test_a_transport_error_becomes_a_typed_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    result = await call_jev(_request(), transport=httpx.MockTransport(handler))
    assert isinstance(result, Err)
    assert "request failed" in result.error.detail


@pytest.mark.asyncio
async def test_a_timeout_becomes_a_typed_failure_naming_the_budget() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    result = await call_jev(_request(), transport=httpx.MockTransport(handler))
    assert isinstance(result, Err)
    assert "timed out after 60s" in result.error.detail


@pytest.mark.asyncio
async def test_a_non_json_body_becomes_a_typed_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>nope</html>")

    result = await call_jev(_request(), transport=httpx.MockTransport(handler))
    assert isinstance(result, Err)
    assert "not JSON" in result.error.detail


@pytest.mark.asyncio
async def test_a_malformed_answer_vector_becomes_a_typed_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answers": {"needs_thread": {"type": "noul"}}})

    result = await call_jev(_request(), transport=httpx.MockTransport(handler))
    assert isinstance(result, Err)
    assert result.error.status == 200


@pytest.mark.asyncio
async def test_transport_preserves_extra_exclusion_that_changes_reference_route() -> None:
    """Pinned assay route() drops this vector; filtering to asked names responded."""
    body = _noul_body(_questions(), 0.0)
    body["answers"]["answerable_from_post"]["noul"] = 0.9
    body["answers"]["about_agent_work"]["noul"] = 0.9
    body["answers"]["excl_new"] = {"type": "noul", "noul": 0.95}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    received = await call_jev(_request(), transport=httpx.MockTransport(handler))
    assert isinstance(received, Ok)
    decision = route(received.value.probabilities)
    assert isinstance(decision, Ok)
    assert (decision.value.action, decision.value.exclusion) == ("drop", "new")
    assert decision.value.features["excl_new"] == 0.95


@pytest.mark.asyncio
async def test_no_failure_detail_carries_the_credential() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    result = await call_jev(_request(), transport=httpx.MockTransport(handler))
    assert isinstance(result, Err)
    assert "secret-key" not in result.error.detail
