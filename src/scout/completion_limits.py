"""Explicit completion bounds for experiment clients using Jig's request contract."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace

from jig import CompletionParams, LLMClient, LLMResponse

EXPERIMENT_MAX_OUTPUT_TOKENS = 4096


class BoundedCompletionClient(LLMClient):  # type: ignore[misc]
    """Preserve Jig's client/trace identity while bounding provider output requests.

    This is an output-token limit, not a monetary budget. Production clients
    are not wrapped; experiment configuration records must capture the bound.
    """

    def __init__(self, inner: LLMClient, max_output_tokens: int) -> None:
        if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int):
            raise ValueError("max_output_tokens must be a positive integer")
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer")
        self._inner = inner
        self.max_output_tokens = max_output_tokens

    def _bounded_params(self, params: CompletionParams) -> CompletionParams:
        limit = (
            min(params.max_tokens, self.max_output_tokens)
            if params.max_tokens is not None
            else self.max_output_tokens
        )
        # Jig merges provider_params last. Do not let an alternate spelling
        # override the experiment bound; leave every unrelated option intact.
        provider_params = dict(params.provider_params) if params.provider_params else None
        if provider_params is not None:
            for name in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
                override = provider_params.pop(name, None)
                if override is not None:
                    if isinstance(override, bool) or not isinstance(override, int) or override <= 0:
                        raise ValueError(f"{name} must be a positive integer")
                    limit = min(limit, override)
        return replace(params, max_tokens=limit, provider_params=provider_params)

    async def complete(self, params: CompletionParams) -> LLMResponse:
        return await self._inner.complete(self._bounded_params(params))

    async def stream(self, params: CompletionParams) -> AsyncIterator[str]:
        async for chunk in self._inner.stream(self._bounded_params(params)):
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()
