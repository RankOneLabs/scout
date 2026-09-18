"""Non-gating orchestration for one typesafe shadow relevance call."""

from __future__ import annotations

import logging

from scout.config import Message
from scout.registry import ProjectTarget
from scout.result import Err, Ok
from scout.storage.shadow_relevance import ShadowRunWrite
from scout.storage.state import StateManager
from scout.typesafe.backend import Backend
from scout.typesafe.catalogue import Catalogue, load_catalogue
from scout.typesafe.compose import DECIDE_REGISTRY
from scout.typesafe.placeholder import PlaceholderBackend
from scout.typesafe.state import build_state

logger = logging.getLogger(__name__)


class ShadowRelevanceRunner:
    def __init__(self, catalogue: Catalogue, backend_name: str, backend: Backend) -> None:
        self.catalogue = catalogue
        self.backend_name = backend_name
        self.backend = backend

    @classmethod
    def placeholder(cls, catalogue_path: str, answers_path: str) -> ShadowRelevanceRunner:
        return cls(load_catalogue(catalogue_path), "placeholder", PlaceholderBackend(answers_path))

    async def run(
        self,
        *,
        state_manager: StateManager,
        scan_id: int,
        post_id: int,
        message: Message,
        project: ProjectTarget,
    ) -> int | None:
        projected = build_state(message, project, self.catalogue)
        fallback_request_id = f"error:{scan_id}:{post_id}"
        try:
            result = await self.backend(projected, self.catalogue)
            match result:
                case Ok(answers):
                    decision = DECIDE_REGISTRY[self.catalogue.decide](answers)
                    row = state_manager.shadow_relevance.record_shadow_run(
                        ShadowRunWrite(
                            scan_id=scan_id,
                            post_id=post_id,
                            backend=self.backend_name,
                            model=answers.model,
                            catalogue_id=self.catalogue.id,
                            catalogue_version=self.catalogue.version,
                            request_id=answers.request_id,
                            state=projected,
                            status="ok",
                            answers=answers.model_dump(mode="json"),
                            decision=decision.model_dump(mode="json"),
                            eligible=decision.eligible,
                            p_eligible=decision.p_eligible,
                            uncertain=decision.uncertain,
                            account_label=decision.account_label,
                            account_confidence=decision.account_confidence,
                            input_tokens=answers.usage.input_tokens,
                            output_tokens=answers.usage.output_tokens,
                            latency_ms=answers.latency_ms,
                        )
                    )
                    return row.id
                case Err(error):
                    request_id = error.request_id or fallback_request_id
                    detail = error.detail or f"{error.operation} failed"
                case _:
                    request_id = fallback_request_id
                    detail = "backend returned an invalid result"
        except Exception as exc:
            request_id = fallback_request_id
            detail = f"{type(exc).__name__}: {exc}"

        row = state_manager.shadow_relevance.record_shadow_run(
            ShadowRunWrite(
                scan_id=scan_id,
                post_id=post_id,
                backend=self.backend_name,
                model="unknown",
                catalogue_id=self.catalogue.id,
                catalogue_version=self.catalogue.version,
                request_id=request_id,
                state=projected,
                status="error",
                error_detail=detail,
            )
        )
        logger.warning("typesafe shadow evaluation failed for post %s: %s", post_id, detail)
        return row.id
