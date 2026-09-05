"""Dossier resolution and readiness checking for Scout.

Read-only: this module never writes to the dossier root.
The dossier root is the dossier-source checkout; index.yaml is the canonical
resolution path.  Every resolution retrieves and validates the pinned
schemas/index.v1.schema.json and schemas/summary.v1.schema.json from the
*same* requested revision via ``git show`` before any semantic check runs,
so Scout consumes exactly the contract published alongside the data rather
than trusting the working tree or a locally installed schema copy.

The public boundary is ``resolve_dossier`` at an immutable dossier-source
revision, plus the normalization and readiness helpers it composes.
"""
# ruff: noqa: E501

from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, Field, field_validator, model_validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from scout.dossiers.contract import validate_dossier_semantics
from scout.resources import runtime_resource

_INDEX_SCHEMA_PATH = "schemas/index.v1.schema.json"
_SUMMARY_SCHEMA_PATH = "schemas/summary.v1.schema.json"


def _is_v1_index_version(value: object) -> bool:
    """Accept only the canonical v1 spelling the upstream producer emits.

    The upstream index contract always writes semantic version ``"1.0.0"``;
    it never emits the compact integer spelling ``1``,
    so accepting it would let a fixture-only convention define production
    behavior. Any other major, minor, or malformed version fails closed.
    """
    return value == "1.0.0"


# ---------------------------------------------------------------------------
# URL canonicalization
# ---------------------------------------------------------------------------


def canonicalize_url(url: str) -> str:
    """Canonicalize an http(s) URL.

    Lowercases scheme/host, removes default ports (80 for http, 443 for https),
    resolves dot segments in path, preserves path/query, rejects fragments and
    credentials.  Raises ValueError on non-http(s), credentials, or fragments.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception as exc:
        raise ValueError(f"unparseable URL {url!r}: {exc}") from exc

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"URL scheme must be http or https, got {scheme!r} in {url!r}")
    if parsed.username or parsed.password:
        raise ValueError(f"URL must not contain credentials: {url!r}")
    if parsed.fragment:
        raise ValueError(f"URL must not contain a fragment: {url!r}")

    host = parsed.hostname or ""
    port = parsed.port
    default_ports = {"http": 80, "https": 443}
    if port is not None and port == default_ports.get(scheme):
        port = None

    netloc = host.lower()
    if port is not None:
        netloc = f"{netloc}:{port}"

    # Resolve dot segments
    path = urllib.parse.urljoin("/", parsed.path)

    return urllib.parse.urlunparse((scheme, netloc, path, parsed.params, parsed.query, ""))


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class DossierFact(BaseModel):
    id: str
    text: str
    safe_phrasings: list[str]
    immutable_evidence: list[str]

    @field_validator("id", "text")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be non-empty")
        return v

    @field_validator("safe_phrasings", "immutable_evidence")
    @classmethod
    def _non_empty_list(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("must be non-empty list")
        for item in v:
            if not item.strip():
                raise ValueError("items must be non-empty strings")
        return v


class DossierResource(BaseModel):
    id: str
    label: str
    canonical_url: str
    immutable_evidence: list[str]

    @field_validator("id", "label")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be non-empty")
        return v

    @field_validator("canonical_url")
    @classmethod
    def _valid_url(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("canonical_url must be non-empty")
        try:
            canonicalize_url(v)
        except ValueError as exc:
            raise ValueError(f"invalid canonical_url: {exc}") from exc
        return v

    @field_validator("immutable_evidence")
    @classmethod
    def _non_empty_list(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("must be non-empty list")
        for item in v:
            if not item.strip():
                raise ValueError("items must be non-empty strings")
        return v


class DossierProhibition(BaseModel):
    id: str
    mode: Literal["exact_phrase", "normalized_phrase", "regex"]
    pattern: str
    flags: str = ""
    immutable_evidence: list[str]

    @field_validator("id", "pattern")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be non-empty")
        return v

    @field_validator("immutable_evidence")
    @classmethod
    def _non_empty_list(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("must be non-empty list")
        for item in v:
            if not item.strip():
                raise ValueError("items must be non-empty strings")
        return v


class DossierSummary(BaseModel):
    project_key: str
    last_reviewed: date  # Pydantic v2 coerces ISO 8601 strings automatically
    reviewer: str
    facts: list[DossierFact] = Field(default_factory=list)
    resources: list[DossierResource] = Field(default_factory=list)
    prohibitions: list[DossierProhibition] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)

    @field_validator("project_key", "reviewer")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be non-empty")
        return v

    @model_validator(mode="after")
    def _unique_ids(self) -> DossierSummary:
        fact_ids = [f.id for f in self.facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("fact ids must be unique within the summary")
        resource_ids = [r.id for r in self.resources]
        if len(resource_ids) != len(set(resource_ids)):
            raise ValueError("resource ids must be unique within the summary")
        return self


# ---------------------------------------------------------------------------
# Canonical dossier-source v1 boundary
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolutionMetadata:
    """Immutable identity of a canonical dossier read."""

    project_key: str
    summary_id: str
    revision: str
    path: str


class DossierResolutionError(ValueError):
    """A reproducible, attributable failure at the dossier-source boundary."""

    def __init__(
        self,
        message: str,
        *,
        project_key: str,
        summary_id: str,
        revision: str | None = None,
        path: str | None = None,
    ) -> None:
        self.project_key = project_key
        self.summary_id = summary_id
        self.revision = revision
        self.path = path
        identity = [f"project_key={project_key!r}", f"summary_id={summary_id!r}"]
        if revision is not None:
            identity.append(f"revision={revision!r}")
        if path is not None:
            identity.append(f"path={path!r}")
        super().__init__(f"{message} ({', '.join(identity)})")


@dataclass(frozen=True, slots=True)
class DossierResolution:
    """Read-only canonical resolution result consumed by Scout callers."""

    summary: DossierSummary
    metadata: ResolutionMetadata
    known_gaps: tuple[str, ...]


_FULL_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")


def _boundary_error(
    message: str, project_key: str, summary_id: str, revision: str | None, path: str | None = None
) -> DossierResolutionError:
    return DossierResolutionError(
        message, project_key=project_key, summary_id=summary_id, revision=revision, path=path
    )


def _git_show(root: Path, revision: str, path: str, project_key: str, summary_id: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{revision}:{path}"],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise _boundary_error(
            result.stderr.strip() or "git object cannot be read",
            project_key,
            summary_id,
            revision,
            path,
        )
    return result.stdout


def _required_text(value: Any, name: str, error: DossierResolutionError) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _boundary_error(f"{name} must be a non-empty string", error.project_key,
                              error.summary_id, error.revision, error.path)
    return value


# ---------------------------------------------------------------------------
# Pinned-schema retrieval and validation
# ---------------------------------------------------------------------------


def _load_pinned_schema(
    root: Path, revision: str, path: str, project_key: str, summary_id: str
) -> dict[str, Any]:
    """Read *path* at *revision* via git show, parse as JSON, and validate it
    is itself a well-formed Draft 2020-12 schema whose ``$id`` identifies it as
    the versioned schema at *path* (the ``$id`` must end in ``/<path>``; the
    producer's host is not part of the contract)."""
    raw = _git_show(root, revision, path, project_key, summary_id)
    try:
        schema = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _boundary_error(f"{path} is invalid JSON: {exc}", project_key, summary_id,
                              revision, path) from exc
    if not isinstance(schema, dict):
        raise _boundary_error(f"{path} must be a JSON object", project_key, summary_id,
                              revision, path)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise _boundary_error(f"{path} is not a valid Draft 2020-12 schema: {exc.message}",
                              project_key, summary_id, revision, path) from exc
    expected_suffix = f"/{path}"
    schema_id = schema.get("$id")
    if not isinstance(schema_id, str) or not schema_id.endswith(expected_suffix):
        raise _boundary_error(f"{path} must declare $id ending in {expected_suffix!r}",
                              project_key, summary_id, revision, path)
    return schema


def _offline_registry(index_schema: dict[str, Any], summary_schema: dict[str, Any]) -> Registry[Any]:
    """Build a referencing.Registry containing only the two pinned schemas,
    so validation can never resolve a remote $ref."""
    registry: Registry[Any] = Registry()
    for schema in (index_schema, summary_schema):
        resource = Resource.from_contents(schema, default_specification=DRAFT202012)
        registry = registry.with_resource(schema["$id"], resource)
    return registry


def _validate_instance(
    instance: Any,
    schema: dict[str, Any],
    registry: Registry[Any],
    project_key: str,
    summary_id: str,
    revision: str,
    path: str,
) -> None:
    """Validate *instance* against *schema*; raise on the first deterministic
    error (sorted by instance path, then schema path) with its keyword."""
    validator = Draft202012Validator(schema, registry=registry)
    errors = sorted(
        validator.iter_errors(instance),
        key=lambda e: ([str(p) for p in e.absolute_path], [str(p) for p in e.schema_path]),
    )
    if errors:
        first = errors[0]
        instance_loc = "/" + "/".join(str(p) for p in first.absolute_path)
        raise _boundary_error(
            f"schema validation failed at {instance_loc}: {first.message} (keyword={first.validator})",
            project_key, summary_id, revision, path,
        )


def resolve_dossier(
    repository: Path | str,
    revision: str,
    project_key: str,
    dossier_summary_id: str,
    *,
    max_age_days: int | None = None,
    min_entries: int = 0,
) -> DossierResolution:
    """Resolve one dossier-source v1 summary from a clean, full git revision.

    This is Scout's only canonical dossier reader.  It reads git objects at
    *revision* — never the working tree — and performs no writes.  Validation
    order: retrieve and validate the pinned index/summary JSON Schemas, then
    validate the index document, select and path-check the requested entry,
    validate the summary document, then run Scout's identity, readiness, and
    cross-reference checks (``_build_resolution``). A schema failure or a
    semantic failure both raise the same ``DossierResolutionError`` boundary.
    """
    root = Path(repository)
    if not _FULL_REVISION_RE.fullmatch(revision):
        raise _boundary_error("revision must be a full 40-character lowercase SHA", project_key,
                              dossier_summary_id, revision)
    status = subprocess.run(["git", "-C", str(root), "status", "--porcelain"], capture_output=True,
                            text=True)
    if status.returncode or status.stdout.strip():
        detail = status.stderr.strip() or "checkout has uncommitted changes"
        raise _boundary_error(detail, project_key, dossier_summary_id, revision)
    verify = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", f"{revision}^{{commit}}"],
                            capture_output=True, text=True)
    if verify.returncode or verify.stdout.strip() != revision:
        raise _boundary_error("revision is not a resolvable full commit", project_key,
                              dossier_summary_id, revision)

    index_schema = _load_pinned_schema(root, revision, _INDEX_SCHEMA_PATH,
                                       project_key, dossier_summary_id)
    summary_schema = _load_pinned_schema(root, revision, _SUMMARY_SCHEMA_PATH,
                                         project_key, dossier_summary_id)
    registry = _offline_registry(index_schema, summary_schema)

    try:
        index = yaml.safe_load(_git_show(root, revision, "index.yaml", project_key, dossier_summary_id))
    except yaml.YAMLError as exc:
        raise _boundary_error(f"index.yaml is invalid YAML: {exc}", project_key,
                              dossier_summary_id, revision) from exc
    _validate_instance(index, index_schema, registry, project_key, dossier_summary_id, revision, "index.yaml")
    if not isinstance(index, Mapping) or not _is_v1_index_version(index.get("version")):
        raise _boundary_error("index.yaml must declare version 1.0.0", project_key,
                              dossier_summary_id, revision)
    entries = index.get("entries")
    if not isinstance(entries, Mapping):
        raise _boundary_error("index entries must be a mapping", project_key,
                              dossier_summary_id, revision, "index.yaml")
    entry = entries.get(dossier_summary_id)
    if not isinstance(entry, Mapping):
        raise _boundary_error("summary id is absent from index", project_key, dossier_summary_id, revision)
    if entry.get("type") != "summary":
        raise _boundary_error("index entry is not an allowed summary", project_key, dossier_summary_id, revision)
    path = entry.get("path")
    if not isinstance(path, str):
        raise _boundary_error("index entry path is invalid", project_key, dossier_summary_id, revision)
    pure_path = PurePosixPath(path)
    if pure_path.is_absolute() or ".." in pure_path.parts or path != pure_path.as_posix():
        raise _boundary_error("index path escapes repository", project_key, dossier_summary_id, revision, path)

    try:
        document = yaml.safe_load(_git_show(root, revision, path, project_key, dossier_summary_id))
    except yaml.YAMLError as exc:
        raise _boundary_error(f"summary is invalid YAML: {exc}", project_key, dossier_summary_id, revision, path) from exc
    _validate_instance(document, summary_schema, registry, project_key, dossier_summary_id, revision, path)
    if not isinstance(document, Mapping):
        raise _boundary_error("summary must be a mapping", project_key,
                              dossier_summary_id, revision, path)

    try:
        return _build_resolution(
            document, project_key, dossier_summary_id, revision, path,
            max_age_days=max_age_days, min_entries=min_entries,
        )
    except DossierResolutionError:
        raise
    except (KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
        # A permissive pinned schema must not leak implementation exceptions
        # from projection of untrusted dossier data across this IO boundary.
        raise _boundary_error("invalid canonical dossier structure", project_key,
                              dossier_summary_id, revision, path) from exc


def _build_resolution(
    document: Mapping[str, Any],
    project_key: str,
    dossier_summary_id: str,
    revision: str,
    path: str,
    *,
    max_age_days: int | None,
    min_entries: int,
) -> DossierResolution:
    """Project a schema-validated summary document into a ``DossierResolution``.

    Pure function: JSON Schema has already established shape, and
    ``dossier_contract.validate_dossier_semantics`` — the single production
    authority also shared by ``scripts/check_dossiers.py`` (via
    ``resolve_dossier``) and ``tests/test_dossier_conformance.py`` (the
    conformance corpus runner) — establishes every cross-record constraint
    dossier-source's manifest declares (id uniqueness, non-future review date,
    references, phrasing/URL duplication, regex portability). What remains
    here is Scout's own consumer-side identity/readiness checks (does the
    document match what was requested, is it fresh enough, does it meet
    ``min_entries``) plus projecting the now-known-valid document into
    Scout's pydantic models. Operates on an already-parsed document, so it
    needs neither git nor the pinned schemas and can be exercised directly
    against dossier fixtures that do not themselves carry a schema copy.
    """
    marker = _boundary_error("invalid canonical dossier", project_key, dossier_summary_id, revision)
    if not isinstance(document, Mapping) or document.get("id") != dossier_summary_id:
        raise _boundary_error("document id does not match index summary id", project_key, dossier_summary_id, revision, path)
    if document.get("type") != "summary" or not isinstance(document.get("dossier"), Mapping):
        raise _boundary_error("document is not a dossier summary", project_key, dossier_summary_id, revision, path)
    dossier = document["dossier"]

    if dossier.get("project_key") != project_key:
        raise _boundary_error("dossier project_key does not match requested project", project_key,
                              dossier_summary_id, revision, path)
    reviewer = dossier["reviewer"]
    reviewer_id = _required_text(reviewer.get("id"), "reviewer.id", marker)
    reviewer_name = _required_text(reviewer.get("display_name"), "reviewer.display_name", marker)
    try:
        reviewed = date.fromisoformat(_required_text(dossier.get("last_reviewed"), "last_reviewed", marker))
    except ValueError as exc:
        raise _boundary_error("last_reviewed must be an ISO date", project_key, dossier_summary_id, revision, path) from exc
    if max_age_days is not None:
        age = (date.today() - reviewed).days
        if age < 0 or age > max_age_days:
            raise _boundary_error(f"dossier review age {age} exceeds allowed range", project_key,
                                  dossier_summary_id, revision, path)

    violations = validate_dossier_semantics(dossier)
    if violations:
        first = violations[0]
        raise _boundary_error(f"{first.rule_id}: {first.message}", project_key,
                              dossier_summary_id, revision, path)

    facts: list[DossierFact] = []
    for fact in dossier["facts"]:
        fid = _required_text(fact.get("id"), "fact.id", marker)
        facts.append(DossierFact(id=fid, text=_required_text(fact.get("claim"), "fact.claim", marker),
                                 safe_phrasings=list(fact.get("safe_phrasings", [])),
                                 immutable_evidence=list(fact.get("evidence_ids", []))))

    resources: list[DossierResource] = []
    for resource in dossier["resources"]:
        rid = _required_text(resource.get("id"), "resource.id", marker)
        resources.append(DossierResource(id=rid, label=_required_text(resource.get("label"), "resource.label", marker),
                                         canonical_url=_required_text(resource.get("canonical_url"), "resource.canonical_url", marker),
                                         immutable_evidence=list(resource.get("evidence_ids", []))))

    prohibitions: list[DossierProhibition] = []
    for prohibition in dossier["prohibitions"]:
        pid = _required_text(prohibition.get("id"), "prohibition.id", marker)
        if "exact_phrase" in prohibition:
            mode: Literal["exact_phrase", "normalized_phrase", "regex"] = "exact_phrase"
            pattern = _required_text(prohibition.get("exact_phrase"), "prohibition.exact_phrase", marker)
        elif "normalized_phrase" in prohibition:
            mode = "normalized_phrase"
            pattern = _required_text(prohibition.get("normalized_phrase"), "prohibition.normalized_phrase", marker)
        else:
            mode = "regex"
            pattern = _required_text(prohibition.get("regex"), "prohibition.regex", marker)
        flags = prohibition.get("flags", "")
        prohibitions.append(DossierProhibition(id=pid, mode=mode, pattern=pattern, flags=flags,
                                               immutable_evidence=list(prohibition.get("evidence_ids", []))))

    if len(facts) + len(resources) < min_entries:
        raise _boundary_error("dossier does not meet minimum fact/resource entries", project_key,
                              dossier_summary_id, revision, path)

    gap_questions = [
        _required_text(gap.get("question"), "known_gap.question", marker)
        for gap in dossier["known_gaps"]
    ]

    summary = DossierSummary(project_key=project_key, last_reviewed=reviewed,
                             reviewer=f"{reviewer_id}: {reviewer_name}", facts=facts,
                             resources=resources, prohibitions=prohibitions, references=[])
    return DossierResolution(summary, ResolutionMetadata(project_key, dossier_summary_id, revision, path),
                             tuple(gap_questions))


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


#: Scout's sole dossier-source contract pin. The same file the conformance
#: suite reads, so runtime and CI ground on one revision instead of two.
DOSSIER_SOURCE_PIN_PATH: Path = runtime_resource("contracts", "dossier-source-v1.json")


def get_pinned_dossier_revision(root: Path, *, pin_path: Path | None = None) -> str:
    """Return the dossier-source revision Scout pins, verified present in *root*.

    This is the revision every runtime dossier read resolves at. It comes
    from ``contracts/dossier-source-v1.json`` — not from the checkout's HEAD —
    because the checkout does not belong to Scout.

    The upstream repository authors dossier-source and Scout only consumes it, pinned; that
    is what ``docs/dossier-contract.md`` describes and what the conformance
    suite tests. Resolving at HEAD instead quietly broke the arrangement in
    both directions: CI validated one revision while production read
    whatever the producer had most recently committed, so a commit in a repo
    Scout does not own could stop Scout at any hour, and the conformance
    suite could not fail in a way that predicted it. Reading the pin here is
    what makes the pin load-bearing rather than documentation.

    Unlike :func:`get_dossier_revision` this does not require a clean
    checkout. That check existed to stop the operator-visible files from
    disagreeing with the resolved artifact, which only made sense while the
    two were meant to be the same commit. Under a pin they are expected to
    differ — the producer checkout sits at its own HEAD — so refusing a
    dirty tree would reject a state that is now entirely normal. ``git
    show`` reads objects, not the working tree, so the worktree's condition
    cannot affect what is resolved.

    Raises RuntimeError if the pin is missing or malformed, or if the pinned
    commit is not present in *root* — the case a shallow or stale clone
    produces, and one worth naming precisely, because the symptom is
    otherwise a confusing "path does not exist at revision".
    """
    path = pin_path or DOSSIER_SOURCE_PIN_PATH
    try:
        pin = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"dossier-source pin unreadable at {path}: {exc}") from exc

    revision = pin.get("revision")
    if not isinstance(revision, str) or len(revision) != 40 or not all(
        c in "0123456789abcdef" for c in revision
    ):
        raise RuntimeError(
            f"dossier-source pin at {path} has no valid 40-character revision: {revision!r}"
        )

    try:
        subprocess.run(
            ["git", "-C", str(root), "cat-file", "-e", f"{revision}^{{commit}}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"pinned dossier-source revision {revision} is not present in {root}; "
            f"fetch it in that checkout ({exc.stderr.strip()})"
        ) from exc

    return revision


def get_dossier_revision(root: Path) -> str:
    """Return the clean dossier checkout's 40-character HEAD commit SHA.

    Not what runtime resolution uses — see
    :func:`get_pinned_dossier_revision`. Retained for diagnostics that need
    to report where the shared checkout actually sits, which is how pin
    drift becomes visible to an operator instead of only to CI.

    Raises RuntimeError if root is not a git repo, git fails, or the checkout
    has staged, unstaged, or untracked changes.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"git rev-parse HEAD failed in {root}: {exc.stderr.strip()}"
        ) from exc
    sha = result.stdout.strip()
    if len(sha) != 40 or not all(c in "0123456789abcdef" for c in sha):
        raise RuntimeError(f"unexpected git HEAD output: {sha!r}")
    try:
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"git status failed in {root}: {exc.stderr.strip()}"
        ) from exc
    if status.stdout.strip():
        raise RuntimeError(
            f"dossier checkout has uncommitted changes: {status.stdout.strip()!r}"
        )
    return sha
