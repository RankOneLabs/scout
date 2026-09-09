from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from jig import CompletionParams, LLMClient, LLMResponse

from scout.completion_limits import BoundedCompletionClient


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


@pytest.mark.parametrize("requested,expected", [(None, 4096), (100352, 4096), (512, 512)])
async def test_bound_reaches_provider_without_mutating_request(requested, expected) -> None:
    inner = RecordingClient()
    params = CompletionParams(messages=[], max_tokens=requested)
    await BoundedCompletionClient(inner, 4096).complete(params)
    assert inner.params.max_tokens == expected
    assert params.max_tokens == requested


@pytest.mark.parametrize("key", ["max_tokens", "max_completion_tokens", "max_output_tokens"])
async def test_provider_override_cannot_escape_bound(key: str) -> None:
    inner = RecordingClient()
    params = CompletionParams(messages=[], provider_params={key: 131072, "seed": 7})
    await BoundedCompletionClient(inner, 4096).complete(params)
    assert inner.params.max_tokens == 4096
    assert inner.params.provider_params == {"seed": 7}
    assert params.provider_params[key] == 131072


async def test_stream_is_bounded_and_close_is_forwarded() -> None:
    inner = RecordingClient()
    client = BoundedCompletionClient(inner, 4096)
    assert [chunk async for chunk in client.stream(CompletionParams(messages=[]))] == ["ok"]
    assert inner.params.max_tokens == 4096
    await client.aclose()
    assert inner.closed


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_limit_is_rejected(limit) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        BoundedCompletionClient(RecordingClient(), limit)
