"""Classifier identity carried by relevance phase-run model payloads.

New zero-shot runs declare their classifier in the existing model field as
``zeroshot:<model>``.  ``classifier_of`` is the compatibility transform for older
rows whose model field contains an LLM model identifier or opaque alias.
"""

from __future__ import annotations

import re
from typing import Literal, NewType

Classifier = Literal["llm", "zeroshot"]
# The value carried by evaluation_phase_runs.model.  Zero-shot producers use the
# explicit ``zeroshot:<model>`` namespace; historical LLM producers carry their
# reviewed Jig model identifier unchanged.
ClassifierIdentity = NewType("ClassifierIdentity", str)

_ZEROSHOT_IDENTITY = re.compile(r"zeroshot:[a-zA-Z0-9][a-zA-Z0-9._/-]*")
def zeroshot_classifier(model: str) -> ClassifierIdentity:
    """Build the namespaced classifier identity stored for a new zero-shot run."""
    identity = f"zeroshot:{model}"
    if _ZEROSHOT_IDENTITY.fullmatch(identity) is None:
        raise ValueError("Zero-shot model must be a non-empty model identifier")
    return ClassifierIdentity(identity)


def classifier_of(model: str) -> Classifier:
    """Classify the explicit zero-shot namespace and treat every other model as LLM."""
    if _ZEROSHOT_IDENTITY.fullmatch(model) is not None:
        return "zeroshot"
    return "llm"


__all__ = [
    "Classifier",
    "ClassifierIdentity",
    "classifier_of",
    "zeroshot_classifier",
]
