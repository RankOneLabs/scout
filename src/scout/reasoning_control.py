"""Pin a replay candidate's reasoning ("thinking") switch on every request.

Jig's ``CompletionParams.reasoning`` is portable: ``None`` leaves the
provider's own default untouched, ``True``/``False`` asks the adapter to
turn the model's reasoning mode on or off (Ollama ``think``, OpenRouter
``reasoning.enabled``), and an adapter that cannot honour a non-None
value raises before any request is made. Replay records the switch in
the candidate plan, so every request the candidate makes must carry the
same value regardless of what the agent runner puts in the params.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace

from jig import CompletionParams, LLMClient, LLMResponse


class ReasoningControlClient(LLMClient):  # type: ignore[misc]
    """Preserve Jig's client/trace identity while pinning ``reasoning``.

    Wrap only when the plan sets an explicit value; a plan with
    ``reasoning=None`` must leave the request byte-identical to today.
    """

    def __init__(self, inner: LLMClient, reasoning: bool) -> None:
        if not isinstance(reasoning, bool):
            raise ValueError("reasoning must be True or False")
        self._inner = inner
        self.reasoning = reasoning

    def _pinned(self, params: CompletionParams) -> CompletionParams:
        return replace(params, reasoning=self.reasoning)

    async def complete(self, params: CompletionParams) -> LLMResponse:
        return await self._inner.complete(self._pinned(params))

    async def stream(self, params: CompletionParams) -> AsyncIterator[str]:
        async for chunk in self._inner.stream(self._pinned(params)):
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


__all__ = ["ReasoningControlClient"]
