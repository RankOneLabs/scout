"""Load and validate relevance catalogues before any provider dispatch."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jig.jev import NoulQuestion
from pydantic import ValidationError

from scout.relevance.yaml_loader import load_yaml
from scout.typesafe.catalogue import CatalogueDocument, CatalogueError

RELEVANCE_DECIDE = "agent_ops_route/v1"
REQUIRED_KEYS = ("id", "decide", "description", "state", "questions")
ROUTED_QUESTION_IDS = frozenset(
    {"needs_thread", "answerable_from_post", "about_agent_work", "points_somewhere"}
)
V2_FORMAT = "scout.typesafe-catalogue/v2"


@dataclass(frozen=True, slots=True)
class RelevanceCatalogue:
    document: CatalogueDocument
    questions: tuple[NoulQuestion, ...]
    file_sha256: str

    @property
    def id(self) -> str:
        return self.document.id

    @property
    def decide(self) -> str:
        return self.document.decide


class MalformedCatalogueError(CatalogueError):
    """A catalogue whose questions must not be dispatched to a provider."""


def _null_paths(value: Any, path: str) -> list[str]:
    if value is None:
        return [path]
    if isinstance(value, dict):
        return [
            found for key, child in value.items() for found in _null_paths(child, f"{path}.{key}")
        ]
    if isinstance(value, list):
        return [
            found
            for index, child in enumerate(value)
            for found in _null_paths(child, f"{path}[{index}]")
        ]
    return []


def check_dispatchable(questions: Mapping[str, Any]) -> None:
    """Refuse null criterion values while allowing null optional descriptions."""
    nulls: list[str] = []
    for key, question in questions.items():
        if not isinstance(question, Mapping):
            nulls.append(str(key))
            continue
        criteria = question.get("criteria", {})
        if criteria is None:
            nulls.append(f"{key}.criteria")
            continue
        entries = criteria.items() if isinstance(criteria, Mapping) else enumerate(criteria)
        for name, criterion in entries:
            if isinstance(criterion, Mapping):
                criterion = {
                    field: value
                    for field, value in criterion.items()
                    if not (field in ("not_for", "examples") and value is None)
                }
            nulls.extend(_null_paths(criterion, f"{key}.criteria.{name}"))
    if nulls:
        raise MalformedCatalogueError(
            f"{len(nulls)} null value(s) in catalogue criteria, first {nulls[0]!r}; "
            "quote criteria text that contains commas"
        )


def _questions(document: Mapping[str, Any]) -> tuple[NoulQuestion, ...]:
    raw_questions = document["questions"]
    if not isinstance(raw_questions, Mapping) or not raw_questions:
        raise CatalogueError("catalogue questions must be a non-empty object")
    check_dispatchable(raw_questions)

    question_ids = tuple(raw_questions)
    missing = ROUTED_QUESTION_IDS - set(question_ids)
    if missing:
        raise CatalogueError(f"catalogue is missing routed feature(s): {sorted(missing)}")
    if not any(question_id.startswith("excl_") for question_id in question_ids):
        raise CatalogueError("catalogue must contain at least one excl_* question")

    output: list[NoulQuestion] = []
    for question_id, raw_question in raw_questions.items():
        if not isinstance(question_id, str) or not isinstance(raw_question, Mapping):
            raise CatalogueError("catalogue question entries must be named objects")
        if raw_question.get("type") != "noul":
            raise CatalogueError(f"question {question_id!r} must have type 'noul'")
        output.append(
            NoulQuestion(
                id=question_id,
                instructions=raw_question.get("instructions"),
                criteria=raw_question.get("criteria"),
            )
        )
    return tuple(output)


def _normalize(document: Mapping[str, Any]) -> dict[str, Any]:
    raw_questions = document["questions"]
    assert isinstance(raw_questions, Mapping)
    return {
        **document,
        "format": V2_FORMAT,
        "questions": [
            {**raw_question, "id": question_id}
            for question_id, raw_question in raw_questions.items()
        ],
    }


def load_catalogue_bytes(source: bytes) -> RelevanceCatalogue:
    try:
        raw: Any = load_yaml(source)
        if not isinstance(raw, dict):
            raise CatalogueError("catalogue must be an object")
        for key in REQUIRED_KEYS:
            if key not in raw:
                raise CatalogueError(f"catalogue is missing {key}")
        if raw["decide"] != RELEVANCE_DECIDE:
            raise CatalogueError(f"catalogue decide must be {RELEVANCE_DECIDE!r}")
        questions = _questions(raw)
        document = CatalogueDocument.model_validate(_normalize(raw))
    except CatalogueError:
        raise
    except (UnicodeError, yaml.YAMLError, ValidationError, TypeError, ValueError) as exc:
        raise CatalogueError(str(exc)) from exc
    return RelevanceCatalogue(
        document=document,
        questions=questions,
        file_sha256=hashlib.sha256(source).hexdigest(),
    )


def load_catalogue(path: str | Path) -> RelevanceCatalogue:
    try:
        source = Path(path).read_bytes()
    except OSError as exc:
        raise CatalogueError(str(exc)) from exc
    return load_catalogue_bytes(source)


__all__ = [
    "MalformedCatalogueError",
    "RelevanceCatalogue",
    "check_dispatchable",
    "load_catalogue",
    "load_catalogue_bytes",
]
