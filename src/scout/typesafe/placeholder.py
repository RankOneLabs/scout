"""Fixture-backed deterministic backend for shadow-node wiring tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from scout.result import Err, Ok, Result
from scout.typesafe.catalogue import Catalogue
from scout.typesafe.models import Answers, BackendError


class PlaceholderBackend:
    def __init__(self, path: str | Path) -> None:
        raw: Any = yaml.safe_load(Path(path).read_text())
        if not isinstance(raw, dict) or "default" not in raw:
            raise ValueError("placeholder fixture must be a mapping with a default answer")
        self._answers = raw

    async def __call__(
        self, state: dict[str, object], catalogue: Catalogue
    ) -> Result[Answers, BackendError]:
        del catalogue
        post = state.get("post")
        post_id = post.get("id") if isinstance(post, dict) else None
        known = str(post_id) in self._answers
        payload = self._answers.get(str(post_id), self._answers["default"])
        try:
            answers = Answers.model_validate(payload)
            if not known:
                answers = answers.model_copy(
                    update={"request_id": f"{answers.request_id}:{post_id}"}
                )
            return Ok(answers)
        except Exception as exc:
            return Err(
                BackendError(
                    operation="placeholder.validate",
                    entity_id=str(post_id),
                    detail=str(exc),
                )
            )
