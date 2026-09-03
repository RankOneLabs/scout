"""Gate verifier for structured draft output.

Runs checks in stable order; every policy violation becomes a GateViolation.
All violations are returned in VerifyResult.violations. This module is pure
content verification with no database access — it cannot read or write
mutable state. The author-rate gate and gate_blocks persistence are
StateManager's sole responsibility (see StateManager.persist_surfaced_outcome
and StateManager._save_gate_violations).

Verification order
------------------
1. blank_author_id       — author_id must be non-blank
2. structure_projections — claims count == declarative count;
                            resources_used set == resource segment ids
3. fact_ids              — each DeclarativeSegment.fact_id in dossier
4. safe_phrasing         — each claim backed by a dossier safe_phrasing
5. resource_ids          — each ResourceSegment.resource_id in dossier
6. url_allowlist         — every http URL in assembled text in dossier
7. prohibitions          — assembled text must not match any prohibition
8. platform_limits       — assembled text must pass platform constraints
9. posture               — questions only in engage/ask postures
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from scout.dossiers.contract import canonical_normalize, prohibition_matches
from scout.dossiers.resolver import DossierSummary, canonicalize_url
from scout.scanning.schemas import (
    DeclarativeSegment,
    QuestionSegment,
    ResourceSegment,
    StructuredDraftOutput,
)

# ── Result types ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class GateViolation:
    """A single gate policy violation."""

    reason_code: str
    offending_text: str | None
    segment_index: int | None


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Outcome of a gate verification pass."""

    ok: bool
    violations: list[GateViolation]
    assembled_text: str | None


# ── Module-level constants ─────────────────────────────────────────────────────

# PAA content_invariants producer version (paa/declarations.py resolves the
# content_invariants evaluator against this). Bump together with the PAA
# declaration reference whenever a check above changes what counts as a
# violation — evidence must stay interpretable against the exact contract
# that produced it.
CONTENT_INVARIANTS_EVALUATOR_VERSION = "1"

# assemble_draft_text producer version. Persisted verbatim wherever
# assembled text is used as scoring evidence (evaluation_experiments'
# reply-correction grading, via grading/correction.py) so stored
# comparison evidence identifies the exact assembly semantics that
# produced it. Bump together with assemble_draft_text whenever its output
# for a given (StructuredDraftOutput, DossierSummary) pair could change.
DRAFT_TEXT_ASSEMBLER_VERSION = "assemble_draft_text/v1"

_BLUESKY_MAX_CP: int = 300
_FARCASTER_MAX_UTF8_BYTES: int = 320
_DISCORD_MAX_CP: int = 2000

_KNOWN_PLATFORMS: frozenset[str] = frozenset({"bluesky", "farcaster", "discord"})

# Postures that permit QuestionSegments.
_QUESTION_POSTURES: frozenset[str] = frozenset({"engage", "ask"})

# C0 control chars (U+0000–U+001F) except LF (U+000A), plus C1 (U+0080–U+009F).
_CONTROL_CHAR_RE: re.Pattern[str] = re.compile(r"[\x00-\x09\x0b-\x1f\x80-\x9f]")

# Finds http(s) URLs in assembled text.
_URL_RE: re.Pattern[str] = re.compile(r"https?://\S+", re.IGNORECASE)


# ── Text assembly ──────────────────────────────────────────────────────────────


def assemble_draft_text(
    structured_draft: StructuredDraftOutput,
    dossier: DossierSummary,
) -> str | None:
    """Build the publish-ready string from draft segments.

    This is the only function allowed to turn a StructuredDraftOutput into
    publish-ready reply text. Declaratives and questions contribute their
    literal ``.text``. Resources expand to ``Resource: {label} —
    {canonical_url}``. Parts are joined with a single ASCII space, with no
    further normalization or trimming. Missing resource IDs (already flagged
    by check 5) are skipped silently to avoid raising here. Returns None
    only when no part resolves.
    """
    resource_by_id = {r.id: r for r in dossier.resources}
    parts: list[str] = []
    for seg in structured_draft.segments:
        if isinstance(seg, DeclarativeSegment):
            parts.append(seg.text)
        elif isinstance(seg, ResourceSegment):
            res = resource_by_id.get(seg.resource_id)
            if res is not None:
                parts.append(f"Resource: {res.label} — {res.canonical_url}")
        elif isinstance(seg, QuestionSegment):
            parts.append(seg.text)
    return " ".join(parts) or None


# ── Check 1: blank author ID ───────────────────────────────────────────────────


def _check_blank_author_id(author_id: str) -> list[GateViolation]:
    if not author_id.strip():
        return [
            GateViolation(
                reason_code="blank_author_id",
                offending_text=None,
                segment_index=None,
            )
        ]
    return []


# ── Check 2: structure projections ────────────────────────────────────────────


def _check_structure_projections(
    structured_draft: StructuredDraftOutput,
) -> list[GateViolation]:
    """Verify claims and resources_used match their segment counterparts.

    One violation is emitted for the claims mismatch and another for the
    resources_used mismatch; callers can tell exactly which projection is off.
    """
    violations: list[GateViolation] = []

    declarative_count = sum(
        1 for s in structured_draft.segments if isinstance(s, DeclarativeSegment)
    )
    if len(structured_draft.claims) != declarative_count:
        violations.append(
            GateViolation(
                reason_code="structure_projections",
                offending_text=None,
                segment_index=None,
            )
        )

    resource_seg_ids = {
        s.resource_id
        for s in structured_draft.segments
        if isinstance(s, ResourceSegment)
    }
    if set(structured_draft.resources_used) != resource_seg_ids:
        violations.append(
            GateViolation(
                reason_code="structure_projections",
                offending_text=None,
                segment_index=None,
            )
        )

    return violations


# ── Check 3: fact ID referential integrity ─────────────────────────────────────


def _check_fact_ids(
    structured_draft: StructuredDraftOutput,
    dossier: DossierSummary,
) -> list[GateViolation]:
    """Flag DeclarativeSegments whose fact_id is absent from the dossier."""
    fact_id_set = frozenset(f.id for f in dossier.facts)
    violations: list[GateViolation] = []
    for idx, seg in enumerate(structured_draft.segments):
        if isinstance(seg, DeclarativeSegment) and seg.fact_id not in fact_id_set:
            violations.append(
                GateViolation(
                    reason_code="fact_ids",
                    offending_text=seg.fact_id,
                    segment_index=idx,
                )
            )
    return violations


# ── Check 4: safe phrasing ────────────────────────────────────────────────────


def _check_safe_phrasing(
    structured_draft: StructuredDraftOutput,
    dossier: DossierSummary,
) -> list[GateViolation]:
    """Each claim must normalize-match at least one safe_phrasing from any fact."""
    all_phrasings_norm = frozenset(
        canonical_normalize(sp) for f in dossier.facts for sp in f.safe_phrasings
    )
    violations: list[GateViolation] = []
    for claim in structured_draft.claims:
        if canonical_normalize(claim) not in all_phrasings_norm:
            violations.append(
                GateViolation(
                    reason_code="safe_phrasing",
                    offending_text=claim,
                    segment_index=None,
                )
            )
    return violations


# ── Check 5: resource ID referential integrity ─────────────────────────────────


def _check_resource_ids(
    structured_draft: StructuredDraftOutput,
    dossier: DossierSummary,
) -> list[GateViolation]:
    """Flag ResourceSegments whose resource_id is absent from the dossier."""
    resource_id_set = frozenset(r.id for r in dossier.resources)
    violations: list[GateViolation] = []
    for idx, seg in enumerate(structured_draft.segments):
        if isinstance(seg, ResourceSegment) and seg.resource_id not in resource_id_set:
            violations.append(
                GateViolation(
                    reason_code="resource_ids",
                    offending_text=seg.resource_id,
                    segment_index=idx,
                )
            )
    return violations


# ── Check 6: URL allowlist ─────────────────────────────────────────────────────


def _check_url_allowlist(
    assembled_text: str,
    dossier: DossierSummary,
) -> list[GateViolation]:
    """Flag http(s) URLs in assembled text that are not in the dossier allowlist.

    The allowlist is the canonicalized set of all resource canonical_urls.
    A dossier resource whose canonical_url itself fails canonicalization
    contributes no entry to the allowlist and instead yields a violation
    naming that resource URL — the verifier stays fail-closed even if
    malformed data reaches it. Every http(s) token in the assembled text
    that fails canonicalization (fragment, credentials, unparseable, or any
    other rejection) also yields a violation; it is never silently skipped.
    """
    violations: list[GateViolation] = []
    allowlist: set[str] = set()
    for r in dossier.resources:
        try:
            allowlist.add(canonicalize_url(r.canonical_url))
        except ValueError:
            violations.append(
                GateViolation(
                    reason_code="url_allowlist",
                    offending_text=r.canonical_url,
                    segment_index=None,
                )
            )

    for raw_url in _URL_RE.findall(assembled_text):
        # Strip common trailing punctuation absorbed by \\S+.
        raw_url = raw_url.rstrip(".,;:!)")
        try:
            canon = canonicalize_url(raw_url)
        except ValueError:
            violations.append(
                GateViolation(
                    reason_code="url_allowlist",
                    offending_text=raw_url,
                    segment_index=None,
                )
            )
            continue
        if canon not in allowlist:
            violations.append(
                GateViolation(
                    reason_code="url_allowlist",
                    offending_text=raw_url,
                    segment_index=None,
                )
            )
    return violations


# ── Check 7: prohibitions ─────────────────────────────────────────────────────


def _check_prohibitions(
    assembled_text: str,
    dossier: DossierSummary,
) -> list[GateViolation]:
    """Apply dossier prohibitions to the assembled text.

    All prohibitions are checked regardless of earlier matches; the caller
    receives one violation per triggered prohibition. Matching is delegated
    to dossier_contract.prohibition_matches, the single execution path
    shared with dossier resolution and the shared conformance corpus:
    exact_phrase is case-sensitive substring, normalized_phrase is
    substring after canonical normalization, and regex honors only the
    prohibition's own authored i/m/s flags (never a forced IGNORECASE).
    """
    violations: list[GateViolation] = []

    for prohibition in dossier.prohibitions:
        triggered = prohibition_matches(
            prohibition.mode, prohibition.pattern, prohibition.flags, assembled_text
        )

        if triggered:
            violations.append(
                GateViolation(
                    reason_code="prohibitions",
                    offending_text=prohibition.pattern,
                    segment_index=None,
                )
            )

    return violations


# ── Check 8: platform limits ──────────────────────────────────────────────────


def _check_platform_limits(
    assembled_text: str | None,
    platform: str,
) -> list[GateViolation]:
    """Apply platform-specific format constraints.

    Unknown platform blocks unconditionally.  For known platforms: empty text,
    C0/C1 control characters, and length overflows are each checked in order.
    """
    if platform not in _KNOWN_PLATFORMS:
        return [
            GateViolation(
                reason_code="platform_limits",
                offending_text=platform,
                segment_index=None,
            )
        ]

    text = (assembled_text or "").strip()

    if not text:
        return [
            GateViolation(
                reason_code="platform_limits",
                offending_text=None,
                segment_index=None,
            )
        ]

    violations: list[GateViolation] = []

    if _CONTROL_CHAR_RE.search(text):
        violations.append(
            GateViolation(
                reason_code="platform_limits",
                offending_text=None,
                segment_index=None,
            )
        )

    if platform == "bluesky":
        cp_count = len(text)
        if cp_count > _BLUESKY_MAX_CP:
            violations.append(
                GateViolation(
                    reason_code="platform_limits",
                    offending_text=f"{cp_count} code points (limit {_BLUESKY_MAX_CP})",
                    segment_index=None,
                )
            )
    elif platform == "farcaster":
        byte_count = len(text.encode("utf-8"))
        if byte_count > _FARCASTER_MAX_UTF8_BYTES:
            violations.append(
                GateViolation(
                    reason_code="platform_limits",
                    offending_text=(
                        f"{byte_count} UTF-8 bytes (limit {_FARCASTER_MAX_UTF8_BYTES})"
                    ),
                    segment_index=None,
                )
            )
    elif platform == "discord":
        cp_count = len(text)
        if cp_count > _DISCORD_MAX_CP:
            violations.append(
                GateViolation(
                    reason_code="platform_limits",
                    offending_text=f"{cp_count} code points (limit {_DISCORD_MAX_CP})",
                    segment_index=None,
                )
            )

    return violations


# ── Check 9: question segment posture ─────────────────────────────────────────


def _check_question_posture(
    structured_draft: StructuredDraftOutput,
) -> list[GateViolation]:
    """QuestionSegments are only permitted in engage/ask postures."""
    has_questions = any(
        isinstance(s, QuestionSegment) for s in structured_draft.segments
    )
    if not has_questions:
        return []
    if structured_draft.posture not in _QUESTION_POSTURES:
        return [
            GateViolation(
                reason_code="posture",
                offending_text=str(structured_draft.posture),
                segment_index=None,
            )
        ]
    return []


# ── Public entry point ────────────────────────────────────────────────────────


def verify_draft_content(
    *,
    dossier: DossierSummary,
    structured_draft: StructuredDraftOutput,
    platform: str,
    author_id: str,
) -> VerifyResult:
    """Run immutable content gates without reading database state.

    This is the historical-replay API.  It contains every deterministic
    content gate (including author identity) but deliberately has no database
    parameter and therefore cannot invoke the mutable author-rate check or
    persist a gate block.
    """
    violations: list[GateViolation] = []

    # 1. blank author ID
    violations.extend(_check_blank_author_id(author_id))

    # 2. structure projections
    violations.extend(_check_structure_projections(structured_draft))

    # 3. fact ID foreign key
    violations.extend(_check_fact_ids(structured_draft, dossier))

    # 4. safe phrasing
    violations.extend(_check_safe_phrasing(structured_draft, dossier))

    # 5. resource ID foreign key
    violations.extend(_check_resource_ids(structured_draft, dossier))

    # Assemble text once; needed for checks 6–8.
    assembled_text: str | None = assemble_draft_text(structured_draft, dossier)

    # 6. URL allowlist
    if assembled_text is not None:
        violations.extend(_check_url_allowlist(assembled_text, dossier))

    # 7. prohibitions
    if assembled_text is not None:
        violations.extend(_check_prohibitions(assembled_text, dossier))

    # 8. platform limits — abstain+no-segments is intentional; skip platform check.
    is_abstain_empty = (
        structured_draft.posture == "abstain" and not structured_draft.segments
    )
    if not is_abstain_empty:
        violations.extend(_check_platform_limits(assembled_text, platform))

    # 9. question posture
    violations.extend(_check_question_posture(structured_draft))

    return VerifyResult(
        ok=not violations,
        violations=violations,
        assembled_text=assembled_text,
    )
