"""Loading the JEV feature catalogue, and its content-addressed version.

Ported from ``RankOneLabs/assay``,
``experiments/typesafe_relevance/catalogue.py`` at commit
``282479cdc877d1d470840684e8cbea891f9c54d3``.

The load-bearing detail is the loader subclass. YAML 1.1 resolves bare ``true``
and ``false`` to booleans, and this catalogue's question criteria are keyed by
those two literal strings. A plain ``yaml.safe_load`` turns those keys into
Python ``True``/``False``, and the questions mapping sent to the classifier
stops matching what the graded runs sent. Dropping ``tag:yaml.org,2002:bool``
from every implicit resolver keeps them strings.

The questions mapping is passed to the classifier verbatim as loaded. Nothing
here re-renders, re-keys or normalises it.

This is deliberately separate from ``scout.typesafe.catalogue``, which serves
the disabled shadow node and models a different document shape (questions as a
list of objects, ``parent_context_only`` as a boolean, a closed ``decide``
vocabulary the JEV catalogue is not in). See docs/relevance-holdouts.md.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from scout.result import Err, Ok, Result

REQUIRED_KEYS: tuple[str, ...] = ("id", "decide", "description", "state", "questions")


@dataclass(frozen=True, slots=True)
class JevCatalogueError:
    """A catalogue could not be loaded or is not usable by the JEV path."""

    operation: str
    path: str
    detail: str


class _LiteralKeyLoader(yaml.SafeLoader):
    """YAML 1.2-style booleans: ``true``/``false`` mapping keys stay strings."""


_LiteralKeyLoader.yaml_implicit_resolvers = {
    key: [item for item in values if item[0] != "tag:yaml.org,2002:bool"]
    for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


@dataclass(frozen=True, slots=True)
class JevCatalogue:
    """One loaded feature catalogue, addressed by the digest of its content."""

    path: Path
    document: Mapping[str, Any]
    version: str

    @property
    def id(self) -> str:
        return str(self.document["id"])

    @property
    def decide(self) -> str:
        return str(self.document["decide"])

    @property
    def questions(self) -> dict[str, Any]:
        """The questions mapping exactly as loaded, for the request body."""
        return dict(self.document["questions"])


def _canonical(document: Mapping[str, Any]) -> bytes:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def load_jev_catalogue(path: str | Path) -> Result[JevCatalogue, JevCatalogueError]:
    """Read and validate a catalogue. The one IO boundary in the JEV loader."""
    catalogue_path = Path(path)

    def _fail(detail: str) -> Err[JevCatalogueError]:
        return Err(
            JevCatalogueError(
                operation="load_jev_catalogue",
                path=str(catalogue_path),
                detail=detail,
            )
        )

    try:
        text = catalogue_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return _fail(f"could not read catalogue: {exc}")
    try:
        document: Any = yaml.load(text, Loader=_LiteralKeyLoader)
    except yaml.YAMLError as exc:
        return _fail(f"catalogue is not valid YAML: {exc}")

    if not isinstance(document, dict):
        return _fail("catalogue must be an object")
    missing = [key for key in REQUIRED_KEYS if key not in document]
    if missing:
        return _fail(f"catalogue is missing {', '.join(missing)}")
    questions = document["questions"]
    if not isinstance(questions, dict) or not questions:
        return _fail("catalogue questions must be a non-empty object")

    try:
        version = hashlib.sha256(_canonical(document)).hexdigest()
    except (TypeError, ValueError) as exc:
        return _fail(f"catalogue is not serializable for versioning: {exc}")
    return Ok(JevCatalogue(path=catalogue_path, document=document, version=version))
