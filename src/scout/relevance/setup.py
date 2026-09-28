"""Acquire the scan-scoped resources needed by zero-shot relevance."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from jig.jev import JevClient, NoulQuestion

import scout.config as _config
from scout.registry import ProjectTarget, RuntimeRegistry
from scout.relevance.loader import RelevanceCatalogue, load_catalogue
from scout.relevance.models import ZEROSHOT_PROJECT_KEYS
from scout.result import Err, Ok, Result
from scout.typesafe.catalogue import CatalogueError


@dataclass(frozen=True, slots=True)
class ZeroShotScanContext:
    """Immutable configuration and the one provider client owned by a scan."""

    catalogue: RelevanceCatalogue
    questions: tuple[NoulQuestion, ...]
    client: JevClient
    projects: Mapping[str, ProjectTarget]


@dataclass(frozen=True, slots=True)
class ZeroShotSetupError:
    """A setup refusal with enough context to identify the failed entity."""

    operation: str
    entity: str
    detail: str


def setup_zeroshot_scan(
    registry: RuntimeRegistry,
) -> Result[ZeroShotScanContext, ZeroShotSetupError]:
    """Load and validate all zero-shot resources before a scan fetches any posts."""
    catalogue_path = _config.RELEVANCE_ZEROSHOT_CATALOGUE_PATH
    if not catalogue_path:
        return Err(
            ZeroShotSetupError(
                operation="load_relevance_catalogue",
                entity="RELEVANCE_ZEROSHOT_CATALOGUE_PATH",
                detail="catalogue path is empty",
            )
        )

    try:
        catalogue = load_catalogue(catalogue_path)
    except CatalogueError as exc:
        return Err(
            ZeroShotSetupError(
                operation="load_relevance_catalogue",
                entity=catalogue_path,
                detail=str(exc),
            )
        )

    routed_project_keys = frozenset(
        route.project_key
        for route in registry.keywords
        if route.project_key in ZEROSHOT_PROJECT_KEYS
    )
    for project_key in sorted(routed_project_keys):
        if project_key not in registry.projects:
            return Err(
                ZeroShotSetupError(
                    operation="resolve_relevance_project",
                    entity=project_key,
                    detail=f"active keyword routes reference missing project {project_key!r}",
                )
            )

    projects: Mapping[str, ProjectTarget] = MappingProxyType(
        {project_key: registry.projects[project_key] for project_key in routed_project_keys}
    )
    try:
        client = JevClient(
            model=_config.RELEVANCE_JEV_MODEL,
            api_key=_config.TYPESAFE_API_KEY,
            endpoint=_config.RELEVANCE_JEV_ENDPOINT,
        )
    except (TypeError, ValueError) as exc:
        return Err(
            ZeroShotSetupError(
                operation="open_jev_client",
                entity=_config.RELEVANCE_JEV_MODEL,
                detail=str(exc),
            )
        )

    return Ok(
        ZeroShotScanContext(
            catalogue=catalogue,
            questions=catalogue.questions,
            client=client,
            projects=projects,
        )
    )


__all__ = ["ZeroShotScanContext", "ZeroShotSetupError", "setup_zeroshot_scan"]
