"""Classifier identity carried by relevance phase-run model payloads.

New Jev runs declare their classifier in the existing model field as
``jev:<model>``.  ``classifier_of`` is the compatibility transform for older
rows whose model field contains only an LLM model identifier; callers must
handle an unrecognised value instead of treating it as an LLM by default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, NewType

from scout.model_identity import ModelId, resolve_model_identity
from scout.result import Err, Ok, Result

Classifier = Literal["llm", "jev"]
# The value carried by evaluation_phase_runs.model.  Jev producers use the
# explicit ``jev:<model>`` namespace; historical LLM producers carry their
# reviewed Jig model identifier unchanged.
ClassifierIdentity = NewType("ClassifierIdentity", str)

_JEV_IDENTITY = re.compile(r"jev:[a-zA-Z0-9][a-zA-Z0-9._/-]*")
_EXPLICIT_LLM_ROUTE = re.compile(
    r"(?:dispatch|ollama)/[a-zA-Z0-9][a-zA-Z0-9._:/-]*"
    r"|openrouter/[a-zA-Z0-9][a-zA-Z0-9._-]*/[a-zA-Z0-9][a-zA-Z0-9._:-]*"
)


@dataclass(frozen=True, slots=True)
class UnknownClassifier:
    """An unrecognised phase-run model value with no safe classifier default."""

    model: str


def jev_classifier(model: str) -> ClassifierIdentity:
    """Build the namespaced classifier identity stored for a new Jev run."""
    identity = f"jev:{model}"
    if _JEV_IDENTITY.fullmatch(identity) is None:
        raise ValueError("Jev model must be a non-empty model identifier")
    return ClassifierIdentity(identity)


def classifier_of(model: str) -> Result[Classifier, UnknownClassifier]:
    """Classify a historical phase-run model without guessing.

    The explicit Jev namespace is authoritative.  LLM identities are accepted
    only when Scout's reviewed built-in model resolver knows them or when they
    use one of Jig's explicit routed-model namespaces.  Opaque aliases cannot
    be reconstructed from a historical row alone and are therefore unknown.
    """
    if _JEV_IDENTITY.fullmatch(model) is not None:
        return Ok("jev")
    if _EXPLICIT_LLM_ROUTE.fullmatch(model) is not None:
        return Ok("llm")
    if isinstance(resolve_model_identity(ModelId(model)), Ok):
        return Ok("llm")
    return Err(UnknownClassifier(model))


__all__ = [
    "Classifier",
    "ClassifierIdentity",
    "UnknownClassifier",
    "classifier_of",
    "jev_classifier",
]
