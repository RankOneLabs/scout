"""Model identity for preflight, separate from Jig's transport selection.

Routes mirror the pinned jig.llm.factory.from_model contract. OpenRouter
identities additionally require the developer namespace and a known family
prefix; a route or an opaque alias is never evidence of family diversity.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, NewType, TypedDict

from scout.result import Err, Ok, Result

ModelId = NewType("ModelId", str)
type ModelRoute = Literal["anthropic", "openai", "google", "openrouter"]
type ModelFamily = Literal["claude", "gpt", "o-series", "gemini", "kimi", "qwen"]
type ModelDeveloper = Literal["anthropic", "openai", "google", "moonshotai", "qwen"]
type PhaseRole = Literal["relevance", "reply_draft", "critic"]


class ModelIdentityJSON(TypedDict):
    model: str
    route: ModelRoute
    developer: ModelDeveloper
    family: ModelFamily


@dataclass(frozen=True, slots=True)
class ModelIdentity:
    """Identity inferred from a supported configured Jig model identifier."""

    model: ModelId
    route: ModelRoute
    developer: ModelDeveloper
    family: ModelFamily

    def to_json(self) -> ModelIdentityJSON:
        return {
            "model": self.model, "route": self.route,
            "developer": self.developer, "family": self.family,
        }


@dataclass(frozen=True, slots=True)
class PhaseModel:
    """One of Scout's configured relevance/drafting/critic model settings."""

    role: PhaseRole
    model: ModelId


@dataclass(frozen=True, slots=True)
class ModelIdentityError:
    operation: str
    model: ModelId
    detail: str


@dataclass(frozen=True, slots=True)
class FamilyRule:
    developer: ModelDeveloper
    family: ModelFamily
    slug_pattern: str
    direct_route: ModelRoute | None


# Namespace and slug must agree. Version/size suffixes do not create families.
# Kimi and Qwen namespaces: https://openrouter.ai/moonshotai/kimi-k2 and
# https://openrouter.ai/qwen/qwen3-235b-a22b. No inference API is added here.
_FAMILY_RULES = (
    FamilyRule("anthropic", "claude", r"claude-.+", "anthropic"),
    FamilyRule("openai", "gpt", r"(?:gpt|chatgpt)-.+", "openai"),
    FamilyRule("openai", "o-series", r"o[134](?:-.+)?", "openai"),
    FamilyRule("google", "gemini", r"gemini-.+", "google"),
    FamilyRule("moonshotai", "kimi", r"kimi-.+", None),
    FamilyRule("qwen", "qwen", r"qwen(?:[0-9].*|-.+)", None),
)


def resolve_model_identity(model: ModelId) -> Result[ModelIdentity, ModelIdentityError]:
    """Resolve a known identifier without creating a client or changing routing.

Dispatch and Ollama names can be arbitrary aliases; even a familiar-looking
alias needs separately declared identity metadata, which is not yet supported.
Unknown/malformed identifiers fail closed instead of inventing a family.
    """
    parts = model.split("/")
    is_openrouter = len(parts) == 3 and parts[0] == "openrouter"
    if len(parts) != 1 and not is_openrouter:
        return Err(ModelIdentityError(
            "resolve_model_identity", model,
            "unsupported route, malformed identifier, or opaque alias; "
            "use a recognized direct or openrouter/<developer>/<model> identifier",
        ))
    slug = parts[-1]
    if re.fullmatch(r"[a-z0-9][a-z0-9._:-]*", slug) is not None:
        for rule in _FAMILY_RULES:
            if re.fullmatch(rule.slug_pattern, slug) is None:
                continue
            if is_openrouter:
                if parts[1] != rule.developer:
                    continue
                route: ModelRoute = "openrouter"
            elif rule.direct_route is not None:
                route = rule.direct_route
            else:
                continue
            return Ok(ModelIdentity(model, route, rule.developer, rule.family))
    return Err(ModelIdentityError(
        "resolve_model_identity", model,
        "unknown model family or developer/slug mismatch; add a reviewed identity mapping",
    ))


@dataclass(frozen=True, slots=True)
class ModelDiversityError:
    operation: str
    families: tuple[ModelFamily, ...]
    detail: str


def model_families(identities: Sequence[ModelIdentity]) -> tuple[ModelFamily, ...]:
    return tuple(sorted({identity.family for identity in identities}))


def check_model_diversity(
    identities: Sequence[ModelIdentity],
) -> Result[None, ModelDiversityError]:
    """Preserve the existing pipeline-wide two-family bar, not independence."""
    families = model_families(identities)
    if len(families) < 2:
        return Err(ModelDiversityError(
            "check_model_diversity", families,
            "configured models must span at least two distinct model families",
        ))
    return Ok(None)
