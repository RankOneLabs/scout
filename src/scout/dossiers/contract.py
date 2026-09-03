"""Scout's sole implementation of the dossier-source dossier data contract:
canonical Unicode normalization, the portable regex grammar, prohibition
matching, and the producer's semantic validation pass. ``dossier.py`` uses
this module while resolving dossiers, ``verifier.py`` uses it while gating
draft text, and ``tests/test_dossier_conformance.py`` uses it as the shared
conformance corpus runner, so there is exactly one implementation of each of
these concerns in Scout.

This module is the stable facade for the dossier contract: it implements
``validate_dossier_semantics`` directly, and re-exports the cohesive
sub-concerns extracted into their own modules — ``dossier_normalization``
(Unicode normalization), ``portable_regex`` (grammar + compiler), and
``prohibition_matching`` (prohibition matcher dispatch) — so every existing
``from dossier_contract import ...`` caller keeps working unchanged.

Ported from the upstream producer implementation (pinned producer commit
44a70aa86d470e99c6315126ffdad5e1640d3f1c):
  - packages/core/src/unicode/normalize.ts    -> canonical_normalize / normalize_subject_for_regex
  - packages/core/src/regex/portable-regex.ts -> parse_portable_regex
  - packages/core/src/regex/compile.ts        -> compile_portable_regex / ascii_fold
  - packages/core/src/prohibition.ts          -> prohibition_matches
  - packages/core/src/validator.ts            -> validate_dossier_semantics

See Scout's ``docs/dossier-contract.md`` and the upstream producer's
``docs/regex-grammar.md`` and ``docs/unicode-normalization.md`` for the
authoritative contract this module implements.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from scout.dossiers.normalization import canonical_normalize, normalize_subject_for_regex
from scout.dossiers.prohibitions import ProhibitionMode, prohibition_matches
from scout.dossiers.regex import (
    AlternationNode,
    CompiledPortableRegex,
    PortableRegexError,
    ascii_fold,
    compile_portable_regex,
    parse_portable_regex,
)

__all__ = [
    "AlternationNode",
    "CompiledPortableRegex",
    "DossierContractViolation",
    "PortableRegexError",
    "ProhibitionMode",
    "ascii_fold",
    "canonical_normalize",
    "compile_portable_regex",
    "normalize_subject_for_regex",
    "parse_portable_regex",
    "prohibition_matches",
    "validate_dossier_semantics",
]


# ---------------------------------------------------------------------------
# Semantic validation (cross-record constraints JSON Schema cannot express)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DossierContractViolation:
    """One structured semantic-stage contract violation.

    ``rule_id`` is drawn from dossier-source's own conformance manifest
    (``conformance/v1/manifest.json``) — the identifier authority for both
    the upstream producer and this Scout consumer, never a second,
    Scout-local taxonomy. ``message`` is human-readable diagnostic text;
    callers that need a stable machine identity must key off ``rule_id``,
    not ``message``.
    """

    stage: Literal["semantic"]
    rule_id: str
    message: str


def _producer_canonical_url(raw: str) -> str:
    """Port of validator.ts's canonicalUrl: clear the fragment and strip
    exactly one trailing slash. Deliberately narrower than dossier.py's own
    canonicalize_url (which Scout uses for its URL allowlist) — this exists
    only to reproduce the producer's own duplicate-canonical-url check."""
    try:
        parsed = urllib.parse.urlparse(raw)
    except ValueError:
        return raw
    rebuilt = urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, "")
    )
    return rebuilt[:-1] if rebuilt.endswith("/") and rebuilt != "/" else rebuilt


def validate_dossier_semantics(dossier: dict[str, Any]) -> tuple[DossierContractViolation, ...]:
    """Run every cross-record semantic check dossier-source's manifest declares.

    This is Scout's sole semantic-stage implementation of the producer's
    ``validateDossier()`` — the single production authority shared by
    ``dossier.resolve_dossier`` (live resolution), ``scripts/check_dossiers.py``
    (readiness verification, via ``resolve_dossier``), and
    ``tests/test_dossier_conformance.py`` (the shared conformance corpus
    runner). Schema-stage validation (structural shape, enums, additional
    properties) runs separately via jsonschema before this function is ever
    called; this function assumes its input already passed that pass and
    only expresses constraints JSON Schema cannot: global id uniqueness,
    non-future review dates, cross-references, phrasing/URL duplication
    after normalization, and portable-regex grammar validity.

    Returns a deterministic ordered tuple, empty when *dossier* is fully
    valid. Iteration order is evidence, then facts (id, safe_phrasings,
    evidence_ids), then resources (id, canonical_url, evidence_ids), then
    prohibitions (id, regex, forbidden_phrasings, evidence_ids,
    resource_ids), then known_gaps (id, related_fact_ids,
    related_resource_ids) — each in the dossier's own list order.
    """
    violations: list[DossierContractViolation] = []
    all_ids: dict[str, str] = {}
    today = date.today().isoformat()

    if str(dossier.get("last_reviewed", "")) > today:
        violations.append(DossierContractViolation(
            "semantic", "scout.dossier.future-last-reviewed",
            f"last_reviewed ({dossier['last_reviewed']}) is in the future",
        ))

    evidence_ids: set[str] = set()
    for i, evidence in enumerate(dossier.get("evidence", [])):
        p = f"/dossier/evidence/{i}"
        eid = evidence["id"]
        if eid in all_ids:
            violations.append(DossierContractViolation(
                "semantic", "scout.dossier.duplicate-id",
                f"duplicate dossier id {eid!r} ({p}/id, also at {all_ids[eid]!r})",
            ))
        else:
            all_ids[eid] = f"{p}/id"
        evidence_ids.add(eid)

    fact_ids: set[str] = set()
    seen_safe_phrasings: set[str] = set()
    for i, fact in enumerate(dossier.get("facts", [])):
        p = f"/dossier/facts/{i}"
        fid = fact["id"]
        if fid in all_ids:
            violations.append(DossierContractViolation(
                "semantic", "scout.dossier.duplicate-id",
                f"duplicate dossier id {fid!r} ({p}/id, also at {all_ids[fid]!r})",
            ))
        else:
            all_ids[fid] = f"{p}/id"
        fact_ids.add(fid)
        for phrasing in fact.get("safe_phrasings", []):
            norm = canonical_normalize(phrasing)
            if norm in seen_safe_phrasings:
                violations.append(DossierContractViolation(
                    "semantic", "scout.dossier.duplicate-safe-phrasing",
                    f"duplicate safe phrasing after normalization: {norm!r} (fact {fid!r})",
                ))
            seen_safe_phrasings.add(norm)
        for eid in fact.get("evidence_ids", []):
            if eid not in evidence_ids:
                violations.append(DossierContractViolation(
                    "semantic", "scout.dossier.broken-evidence-reference",
                    f"evidence_id {eid!r} does not reference any dossier evidence record ({p})",
                ))

    resource_ids: set[str] = set()
    seen_canonical_urls: dict[str, str] = {}
    for i, resource in enumerate(dossier.get("resources", [])):
        p = f"/dossier/resources/{i}"
        rid = resource["id"]
        if rid in all_ids:
            violations.append(DossierContractViolation(
                "semantic", "scout.dossier.duplicate-id",
                f"duplicate dossier id {rid!r} ({p}/id, also at {all_ids[rid]!r})",
            ))
        else:
            all_ids[rid] = f"{p}/id"
        resource_ids.add(rid)
        canon_url = _producer_canonical_url(resource.get("canonical_url", ""))
        if canon_url in seen_canonical_urls:
            violations.append(DossierContractViolation(
                "semantic", "scout.dossier.duplicate-canonical-url",
                f"duplicate canonical_url after normalization: {canon_url!r} "
                f"({p}, also at {seen_canonical_urls[canon_url]!r})",
            ))
        seen_canonical_urls[canon_url] = p
        for eid in resource.get("evidence_ids", []):
            if eid not in evidence_ids:
                violations.append(DossierContractViolation(
                    "semantic", "scout.dossier.broken-evidence-reference",
                    f"evidence_id {eid!r} does not reference any dossier evidence record ({p})",
                ))

    seen_forbidden_phrasings: set[str] = set()
    for i, prohibition in enumerate(dossier.get("prohibitions", [])):
        p = f"/dossier/prohibitions/{i}"
        pid = prohibition["id"]
        if pid in all_ids:
            violations.append(DossierContractViolation(
                "semantic", "scout.dossier.duplicate-id",
                f"duplicate dossier id {pid!r} ({p}/id, also at {all_ids[pid]!r})",
            ))
        else:
            all_ids[pid] = f"{p}/id"
        if "regex" in prohibition:
            try:
                parse_portable_regex(prohibition["regex"])
            except PortableRegexError as exc:
                violations.append(DossierContractViolation(
                    "semantic", "scout.dossiers.resolver.nonportable-regex",
                    f"prohibition regex rejected: {exc}",
                ))
        for phrasing in prohibition.get("forbidden_phrasings", []):
            norm = canonical_normalize(phrasing)
            if norm in seen_forbidden_phrasings:
                violations.append(DossierContractViolation(
                    "semantic", "scout.dossier.duplicate-forbidden-phrasing",
                    f"duplicate forbidden phrasing after normalization: {norm!r} "
                    f"(prohibition {pid!r})",
                ))
            seen_forbidden_phrasings.add(norm)
        for eid in prohibition.get("evidence_ids", []):
            if eid not in evidence_ids:
                violations.append(DossierContractViolation(
                    "semantic", "scout.dossier.broken-evidence-reference",
                    f"evidence_id {eid!r} does not reference any dossier evidence record ({p})",
                ))
        for rid in prohibition.get("resource_ids") or []:
            if rid not in resource_ids:
                violations.append(DossierContractViolation(
                    "semantic", "scout.dossier.broken-resource-reference",
                    f"resource_id {rid!r} does not reference any dossier resource ({p})",
                ))

    for i, gap in enumerate(dossier.get("known_gaps", [])):
        p = f"/dossier/known_gaps/{i}"
        gid = gap["id"]
        if gid in all_ids:
            violations.append(DossierContractViolation(
                "semantic", "scout.dossier.duplicate-id",
                f"duplicate dossier id {gid!r} ({p}/id, also at {all_ids[gid]!r})",
            ))
        else:
            all_ids[gid] = f"{p}/id"
        for fid in gap.get("related_fact_ids") or []:
            if fid not in fact_ids:
                violations.append(DossierContractViolation(
                    "semantic", "scout.dossier.broken-gap-fact-reference",
                    f"related_fact_id {fid!r} does not reference any dossier fact ({p})",
                ))
        for rid in gap.get("related_resource_ids") or []:
            if rid not in resource_ids:
                violations.append(DossierContractViolation(
                    "semantic", "scout.dossier.broken-gap-resource-reference",
                    f"related_resource_id {rid!r} does not reference any dossier resource ({p})",
                ))

    return tuple(violations)
