"""Structural interface implemented by shadow relevance backends."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Protocol

from scout.result import Result
from scout.typesafe.catalogue import Catalogue
from scout.typesafe.models import Answers, BackendError


class Backend(Protocol):
    def __call__(
        self, state: dict[str, object], catalogue: Catalogue
    ) -> Awaitable[Result[Answers, BackendError]]: ...
