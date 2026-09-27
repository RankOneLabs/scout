"""Validated YAML catalogue loading and content-addressed versioning."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from pydantic_core import PydanticSerializationError

from scout.relevance.binding import BINDABLE_STATE_SOURCES, STATE_FIELD_BINDINGS
from scout.typesafe.models import CatalogueVersion

LEGACY_ALLOWED_STATE_FIELDS = {
    "post": frozenset({"id", "platform", "channel_name", "content", "created_at", "url"}),
    "author": frozenset({"name", "handle"}),
    "project": frozenset({"key", "name", "description", "link"}),
}
ALLOWED_STATE_FIELDS = BINDABLE_STATE_SOURCES
UNAVAILABLE_STATE_FIELDS = frozenset({"bio", "followers", "following", "posts"})
REGISTERED_DECIDES = frozenset({"gate_v1", "account_annotation", "agent_ops_route/v1"})


class CatalogueError(ValueError):
    """A catalogue cannot be used by the production projection/decide path."""


class _FrozenDict(dict[str, Any]):
    """JSON-serializable mapping that rejects mutation at every public entry point."""

    @staticmethod
    def _immutable(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("catalogue question metadata is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable  # type: ignore[assignment]
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable  # type: ignore[assignment]


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


class _LegacyStateProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)
    post: tuple[str, ...] = ()
    parent_context_only: bool = False
    author: tuple[str, ...] = ()
    project: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_fields(self) -> _LegacyStateProjection:
        for group in ("post", "author", "project"):
            fields = set(getattr(self, group))
            unavailable = fields & UNAVAILABLE_STATE_FIELDS
            if unavailable:
                raise ValueError(
                    "state projection requests production-unavailable fields: "
                    f"{sorted(unavailable)}"
                )
            unknown = fields - LEGACY_ALLOWED_STATE_FIELDS[group]
            if unknown:
                raise ValueError(f"unknown {group} state fields: {sorted(unknown)}")
        return self


class StateProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    post: tuple[str, ...] = ()
    parent_context_only: tuple[str, ...] = ()
    author: tuple[str, ...] = ()
    project: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_bindings(self) -> StateProjection:
        for group in ("post", "parent_context_only", "author", "project"):
            bindings = STATE_FIELD_BINDINGS[group]
            unknown = set(getattr(self, group)) - set(bindings)
            if unknown:
                raise ValueError(
                    f"state name(s) bind to no available {group} source: {sorted(unknown)}"
                )
            unbindable = {bindings[name] for name in getattr(self, group)} - ALLOWED_STATE_FIELDS[
                group
            ]
            if unbindable:
                raise ValueError(f"unavailable {group} state source(s): {sorted(unbindable)}")
        return self


class Question(BaseModel):
    """Question metadata; unknown SDK fields are intentionally retained verbatim."""

    model_config = ConfigDict(frozen=True, extra="allow")
    id: str

    def model_post_init(self, context: Any, /) -> None:
        del context
        extras = self.__pydantic_extra__
        if extras:
            object.__setattr__(
                self,
                "__pydantic_extra__",
                _FrozenDict({key: _freeze(value) for key, value in extras.items()}),
            )


class CatalogueDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    format: Literal[
        "scout.typesafe-catalogue/v1", "scout.typesafe-catalogue/v2"
    ] = "scout.typesafe-catalogue/v1"
    id: str
    decide: str
    description: str
    state: _LegacyStateProjection | StateProjection
    questions: tuple[Question, ...]

    @model_validator(mode="after")
    def validate_decide(self) -> CatalogueDocument:
        if (
            self.format == "scout.typesafe-catalogue/v1"
            and not isinstance(self.state, _LegacyStateProjection)
        ):
            raise ValueError("v1 catalogue requires the legacy state projection")
        if (
            self.format == "scout.typesafe-catalogue/v2"
            and not isinstance(self.state, StateProjection)
        ):
            raise ValueError("v2 catalogue requires the declared state projection")
        if self.decide not in REGISTERED_DECIDES:
            raise ValueError(f"unknown decide function: {self.decide}")
        ids = [question.id for question in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("question ids must be unique")
        return self


class Catalogue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    document: CatalogueDocument
    version: CatalogueVersion

    @property
    def id(self) -> str:
        return self.document.id

    @property
    def decide(self) -> str:
        return self.document.decide


def _canonical(document: CatalogueDocument) -> bytes:
    if document.format == "scout.typesafe-catalogue/v1":
        payload = document.model_dump(mode="json", exclude={"format"})
    else:
        payload = document.model_dump(mode="json")
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def load_catalogue(path: str | Path) -> Catalogue:
    try:
        raw: Any = yaml.safe_load(Path(path).read_text())
        document = CatalogueDocument.model_validate(raw)
        version = CatalogueVersion(hashlib.sha256(_canonical(document)).hexdigest())
    except (
        OSError,
        UnicodeError,
        yaml.YAMLError,
        ValidationError,
        PydanticSerializationError,
        TypeError,
        ValueError,
    ) as exc:
        raise CatalogueError(str(exc)) from exc
    return Catalogue(document=document, version=version)
