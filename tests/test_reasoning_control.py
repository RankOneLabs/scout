from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from jig import CompletionParams, LLMClient, LLMResponse

from scout.reasoning_control import ReasoningControlClient


class RecordingClient(LLMClient):
    def __init__(self) -> None:
        self.params: CompletionParams | None = None
        self.closed = False

    async def complete(self, params: CompletionParams) -> LLMResponse:
        self.params = params
        return LLMResponse(content="ok", tool_calls=[], usage=None, latency_ms=0, model="fake")

    async def stream(self, params: CompletionParams) -> AsyncIterator[str]:
        self.params = params
        yield "ok"

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("pinned", [True, False])
async def test_pinned_reasoning_reaches_provider_without_mutating_request(pinned: bool) -> None:
    inner = RecordingClient()
    params = CompletionParams(messages=[], reasoning=None)
    await ReasoningControlClient(inner, pinned).complete(params)
    assert inner.params.reasoning is pinned
    assert params.reasoning is None


async def test_pinned_value_overrides_whatever_the_runner_asked_for() -> None:
    inner = RecordingClient()
    await ReasoningControlClient(inner, False).complete(
        CompletionParams(messages=[], reasoning=True)
    )
    assert inner.params.reasoning is False


async def test_stream_and_close_pass_through() -> None:
    inner = RecordingClient()
    client = ReasoningControlClient(inner, True)
    chunks = [chunk async for chunk in client.stream(CompletionParams(messages=[]))]
    assert chunks == ["ok"] and inner.params.reasoning is True
    await client.aclose()
    assert inner.closed


@pytest.mark.parametrize("bad", [None, 1, "off"])
def test_non_boolean_switch_is_rejected(bad: object) -> None:
    with pytest.raises(ValueError, match="True or False"):
        ReasoningControlClient(RecordingClient(), bad)  # type: ignore[arg-type]
