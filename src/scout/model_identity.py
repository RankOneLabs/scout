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

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scout.result import Err, Ok, Result

ModelId = NewType("ModelId", str)
type ModelRoute = Literal["anthropic", "openai", "google", "openrouter", "dispatch", "ollama"]
ModelFamily = NewType("ModelFamily", str)
ModelDeveloper = NewType("ModelDeveloper", str)
type PhaseRole = Literal["relevance", "reply_draft", "critic"]


class ModelIdentityJSON(TypedDict):
    model: str
    route: ModelRoute
    developer: ModelDeveloper
    family: ModelFamily
    source: Literal["builtin", "declared"]


@dataclass(frozen=True, slots=True)
class ModelIdentity:
    """Identity inferred from a supported configured Jig model identifier."""

    model: ModelId
    route: ModelRoute
    developer: ModelDeveloper
    family: ModelFamily
    source: Literal["builtin", "declared"] = "builtin"

    def to_json(self) -> ModelIdentityJSON:
        return {
            "model": self.model, "route": self.route,
            "developer": self.developer, "family": self.family,
            "source": self.source,
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
    FamilyRule(ModelDeveloper("anthropic"), ModelFamily("claude"), r"claude-.+", "anthropic"),
    FamilyRule(ModelDeveloper("openai"), ModelFamily("gpt"), r"(?:gpt|chatgpt)-.+", "openai"),
    FamilyRule(ModelDeveloper("openai"), ModelFamily("o-series"), r"o[134](?:-.+)?", "openai"),
    FamilyRule(ModelDeveloper("google"), ModelFamily("gemini"), r"gemini-.+", "google"),
    FamilyRule(ModelDeveloper("moonshotai"), ModelFamily("kimi"), r"kimi-.+", None),
    FamilyRule(ModelDeveloper("qwen"), ModelFamily("qwen"), r"qwen(?:[0-9].*|-.+)", None),
)


def _resolve_builtin_identity(model: ModelId) -> Result[ModelIdentity, ModelIdentityError]:
    """Resolve only built-in, namespace-checked identifiers."""
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


def resolve_model_identity(
    model: ModelId, declared_identities: Sequence[ModelIdentity] = (),
) -> Result[ModelIdentity, ModelIdentityError]:
    """Resolve built-ins or validated exact local declarations; never reroute."""
    builtin = _resolve_builtin_identity(model)
    if isinstance(builtin, Ok):
        return builtin
    for identity in declared_identities:
        if identity.model == model:
            return Ok(identity)
    return builtin


class ModelDiversityPolicy(BaseModel):
    """Deployment-local pipeline-wide preflight policy; three configured roles."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    minimum_families: int = Field(default=2, ge=1, le=3)


DEFAULT_MODEL_DIVERSITY_POLICY = ModelDiversityPolicy()


class DeclaredModelIdentity(BaseModel):
    """Exact configured Jig identifier and operator-declared family metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    model: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]*$")
    developer: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    family: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")


class ModelIdentityConfig(BaseModel):
    """JSON shape of SCOUT_MODEL_IDENTITY_CONFIG, stored in local .env only."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    policy: ModelDiversityPolicy = Field(default_factory=ModelDiversityPolicy)
    identities: tuple[DeclaredModelIdentity, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelIdentitySettings:
    policy: ModelDiversityPolicy
    identities: tuple[ModelIdentity, ...]


@dataclass(frozen=True, slots=True)
class ModelIdentityConfigError:
    operation: str
    detail: str


def _resolve_declared_identity(
    declaration: DeclaredModelIdentity,
) -> Result[ModelIdentity, ModelIdentityConfigError]:
    model = ModelId(declaration.model)
    if any(
        rule.family == declaration.family and rule.developer != declaration.developer
        for rule in _FAMILY_RULES
    ):
        return Err(ModelIdentityConfigError(
            "resolve_declared_identity", f"developer conflicts with known family: {model}",
        ))
    builtin = _resolve_builtin_identity(model)
    if isinstance(builtin, Ok):
        if (builtin.value.developer, builtin.value.family) != (
            declaration.developer, declaration.family,
        ):
            return Err(ModelIdentityConfigError(
                "resolve_declared_identity",
                f"declaration conflicts with built-in identity: {model}",
            ))
        return builtin
    prefix, separator, name = model.partition("/")
    route: ModelRoute
    if separator and name and prefix in {"dispatch", "ollama"}:
        route = "dispatch" if prefix == "dispatch" else "ollama"
    elif prefix == "openrouter" and len(name.split("/")) == 2 and all(name.split("/")):
        # Existing namespaces cannot be used to relabel a known family slug.
        namespace, slug = name.split("/")
        if any(re.fullmatch(rule.slug_pattern, slug) for rule in _FAMILY_RULES):
            return Err(ModelIdentityConfigError(
                "resolve_declared_identity", f"developer/slug mismatch: {model}",
            ))
        if namespace != declaration.developer:
            return Err(ModelIdentityConfigError(
                "resolve_declared_identity", f"developer must match OpenRouter namespace: {model}",
            ))
        route = "openrouter"
    else:
        return Err(ModelIdentityConfigError(
            "resolve_declared_identity", f"unsupported or malformed Jig model route: {model}",
        ))
    return Ok(ModelIdentity(
        model, route, ModelDeveloper(declaration.developer), ModelFamily(declaration.family),
        source="declared",
    ))


def parse_model_identity_config(
    raw: str | None,
) -> Result[ModelIdentitySettings, ModelIdentityConfigError]:
    """Validate the local JSON boundary without logging its rejected contents."""
    try:
        config = (
            ModelIdentityConfig() if raw is None else ModelIdentityConfig.model_validate_json(raw)
        )
    except ValidationError:
        return Err(ModelIdentityConfigError(
            "parse_model_identity_config",
            "invalid SCOUT_MODEL_IDENTITY_CONFIG: expected policy.minimum_families (integer 1–3) "
            "and identities with model, developer, family; extra fields are forbidden",
        ))
    identities: list[ModelIdentity] = []
    seen: set[str] = set()
    for declaration in config.identities:
        if declaration.model in seen:
            return Err(ModelIdentityConfigError(
                "parse_model_identity_config", f"duplicate model declaration: {declaration.model}",
            ))
        seen.add(declaration.model)
        match _resolve_declared_identity(declaration):
            case Err(error):
                return Err(error)
            case Ok(identity):
                identities.append(identity)
    return Ok(ModelIdentitySettings(config.policy, tuple(identities)))


@dataclass(frozen=True, slots=True)
class ModelDiversityError:
    operation: str
    families: tuple[ModelFamily, ...]
    detail: str


def model_families(identities: Sequence[ModelIdentity]) -> tuple[ModelFamily, ...]:
    return tuple(sorted({identity.family for identity in identities}))


def check_model_diversity(
    identities: Sequence[ModelIdentity],
    policy: ModelDiversityPolicy = DEFAULT_MODEL_DIVERSITY_POLICY,
) -> Result[None, ModelDiversityError]:
    """Apply a declared pipeline-wide diversity bar, not independence."""
    families = model_families(identities)
    if len(families) < policy.minimum_families:
        return Err(ModelDiversityError(
            "check_model_diversity", families,
            f"configured models must span at least {policy.minimum_families} "
            "distinct model families",
        ))
    return Ok(None)
