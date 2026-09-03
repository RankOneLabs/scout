"""Scout's binding of the paa_runtime declaration loader to its own inputs.

The Progressive Autonomy Architecture task-declaration contract — schema,
vocabulary, cross-field semantics — is owned by RankOneLabs/paa and
implemented by ``paa_runtime.declarations``. Scout used to carry a second
implementation of that loader. This module is what remains: the two
Scout-specific inputs the runtime deliberately refuses to guess, bound
once so callers don't repeat them.

Those two inputs are ``DEFAULT_DECLARATIONS_DIR`` (where Scout's
declarations live) and ``PRODUCER_REGISTRY`` (which Scout code produces
each declared verdict — see paa_registry). ``paa_runtime`` takes both as
required parameters and holds no module-global state, which is what lets
one process serve several consumers with different declarations; Scout is
one consumer, so it supplies its own defaults here rather than upstream.

Everything else is re-exported unchanged from the runtime. The types are
the runtime's types, not copies — an ``isinstance`` check or a match
against ``paa_runtime.PaaTaskDeclaration`` succeeds on what this module
returns.
"""

from __future__ import annotations

from pathlib import Path

from paa_runtime.declarations import (
    AutonomyPosition,
    Deployment,
    PaaDeclarationError,
    PaaDemotion,
    PaaEvaluationBasis,
    PaaEvaluator,
    PaaEvaluatorSelector,
    PaaPlacement,
    PaaPlacementOverride,
    PaaPositionPolicy,
    PaaPromotion,
    PaaTaskDeclaration,
    PaaWindow,
    PositionPolicyMode,
    ProducerRegistration,
    PromotionExecution,
    WindowKind,
)
from paa_runtime.declarations import (
    get_paa_declaration as _get_paa_declaration,
)
from paa_runtime.declarations import (
    load_paa_declarations as _load_paa_declarations,
)

from scout.paa.registry import PRODUCER_REGISTRY
from scout.resources import runtime_resource

DEFAULT_DECLARATIONS_DIR = runtime_resource("contracts", "paa")


def load_paa_declarations(
    directory: Path | str = DEFAULT_DECLARATIONS_DIR,
) -> dict[str, PaaTaskDeclaration]:
    """Load every checked-in PAA task declaration in *directory*.

    Fails closed (raising PaaDeclarationError) on a missing or empty
    directory, a file that fails to parse as YAML, a malformed or missing
    access field, an unsupported position/deployment/execution value, an
    evaluator that doesn't resolve against PRODUCER_REGISTRY, or a second
    file declaring a task already seen.
    """
    return _load_paa_declarations(directory, registry=PRODUCER_REGISTRY)


def get_paa_declaration(
    task: str, *, directory: Path | str = DEFAULT_DECLARATIONS_DIR,
) -> PaaTaskDeclaration:
    """Return the one checked-in declaration for *task*, requiring exact identity.

    Raises PaaDeclarationError for an unknown task rather than returning
    None or an invented default — callers must handle absence explicitly.
    """
    return _get_paa_declaration(task, directory=directory, registry=PRODUCER_REGISTRY)


__all__ = [
    "DEFAULT_DECLARATIONS_DIR",
    "PRODUCER_REGISTRY",
    "AutonomyPosition",
    "Deployment",
    "PaaDeclarationError",
    "PaaDemotion",
    "PaaEvaluationBasis",
    "PaaEvaluator",
    "PaaEvaluatorSelector",
    "PaaPlacement",
    "PaaPlacementOverride",
    "PaaPositionPolicy",
    "PaaPromotion",
    "PaaTaskDeclaration",
    "PaaWindow",
    "PositionPolicyMode",
    "PromotionExecution",
    "ProducerRegistration",
    "WindowKind",
    "get_paa_declaration",
    "load_paa_declarations",
]
