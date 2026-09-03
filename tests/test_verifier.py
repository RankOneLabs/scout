"""Tests for verifier.py — verify_draft_content gate checks.

verify_draft_content is pure content verification: no database parameter, no
mutable state, no persistence. It cannot be invoked with an author-rate check
or a gate_blocks writer — those live solely in
StateManager.persist_surfaced_outcome.

Verification order (stable):
    1. blank_author_id
    2. structure_projections
    3. fact_ids
    4. safe_phrasing
    5. resource_ids
    6. url_allowlist
    7. prohibitions
    8. platform_limits
    9. posture

Text assembly: declaratives → their text, resources → "Resource: {label} —
{canonical_url}", questions → their text, joined by a single ASCII space.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from scout.dossiers.resolver import (
    DossierFact,
    DossierProhibition,
    DossierResource,
    DossierSummary,
)
from scout.scanning.schemas import (
    DeclarativeSegment,
    DraftPosture,
    QuestionSegment,
    ResourceSegment,
    StructuredDraftOutput,
)
from scout.verifier import VerifyResult, assemble_draft_text, verify_draft_content

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_FACT_ID = "fact-001"
# Must not contain URLs or match any built-in prohibition; short enough to fit
# every platform limit without padding tests.
_SAFE_PHRASING = "Our gateway product improves agent signup security."

_RESOURCE_ID = "res-001"
_RESOURCE_LABEL = "Gateway Docs"
_RESOURCE_URL = "https://gateway.example.com/docs"

_PROJECT_KEY = "gateway"
_AUTHOR_ID = "user-1"
_PLATFORM = "discord"

_IMMUTABLE_EVIDENCE = ["https://source.example.com/evidence"]

_TODAY = date.today()


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def _make_fact(
    fact_id: str = _FACT_ID,
    safe_phrasing: str = _SAFE_PHRASING,
) -> DossierFact:
    return DossierFact(
        id=fact_id,
        text=f"Background: {safe_phrasing}",
        safe_phrasings=[safe_phrasing],
        immutable_evidence=_IMMUTABLE_EVIDENCE,
    )


def _make_resource(
    resource_id: str = _RESOURCE_ID,
    label: str = _RESOURCE_LABEL,
    canonical_url: str = _RESOURCE_URL,
) -> DossierResource:
    return DossierResource(
        id=resource_id,
        label=label,
        canonical_url=canonical_url,
        immutable_evidence=_IMMUTABLE_EVIDENCE,
    )


def _make_prohibition(
    mode: str, pattern: str, *, flags: str = "", prohibition_id: str = "proh-test"
) -> DossierProhibition:
    return DossierProhibition(
        id=prohibition_id,
        mode=mode,  # type: ignore[arg-type]
        pattern=pattern,
        flags=flags,
        immutable_evidence=_IMMUTABLE_EVIDENCE,
    )


def _make_dossier(
    *,
    facts: list[DossierFact] | None = None,
    resources: list[DossierResource] | None = None,
    prohibitions: list[DossierProhibition] | None = None,
    project_key: str = _PROJECT_KEY,
) -> DossierSummary:
    return DossierSummary(
        project_key=project_key,
        last_reviewed=_TODAY,
        reviewer="reviewer-1",
        facts=facts if facts is not None else [_make_fact()],
        resources=resources if resources is not None else [_make_resource()],
        prohibitions=prohibitions if prohibitions is not None else [],
    )


def _make_draft(
    *,
    posture: DraftPosture = "answer",
    segments: list[Any] | None = None,
    claims: list[str] | None = None,
    resources_used: list[str] | None = None,
) -> StructuredDraftOutput:
    """Return a StructuredDraftOutput that passes all gates by default."""
    if segments is None:
        segments = [
            DeclarativeSegment(type="declarative", fact_id=_FACT_ID, text=_SAFE_PHRASING)
        ]
    if claims is None:
        claims = [_SAFE_PHRASING]
    if resources_used is None:
        resources_used = []
    return StructuredDraftOutput(
        posture=posture,
        segments=segments,
        claims=claims,
        resources_used=resources_used,
    )


def _call_verify(
    dossier: DossierSummary,
    draft: StructuredDraftOutput,
    *,
    platform: str = _PLATFORM,
    author_id: str = _AUTHOR_ID,
) -> VerifyResult:
    """Thin wrapper so test bodies only pass what they care about."""
    return verify_draft_content(
        dossier=dossier,
        structured_draft=draft,
        platform=platform,
        author_id=author_id,
    )


# ---------------------------------------------------------------------------
# 0. PAA producer version
# ---------------------------------------------------------------------------


def test_content_invariants_evaluator_version_is_declared() -> None:
    """CONTENT_INVARIANTS_EVALUATOR_VERSION is the PAA content_invariants
    producer version paa_declarations.PRODUCER_REGISTRY resolves against."""
    from scout.verifier import CONTENT_INVARIANTS_EVALUATOR_VERSION

    assert CONTENT_INVARIANTS_EVALUATOR_VERSION == "1"


# ---------------------------------------------------------------------------
# 1. Happy path — all gates pass
# ---------------------------------------------------------------------------


def test_happy_path_returns_ok_true_with_assembled_text() -> None:
    """A fully valid structured draft passes every gate and returns assembled text."""
    dossier = _make_dossier()
    draft = _make_draft()

    result = _call_verify(dossier, draft)

    assert result.ok is True
    assert result.violations == []
    assert result.assembled_text == _SAFE_PHRASING


# ---------------------------------------------------------------------------
# 2. structure_projections — claims count doesn't match declarative segments
# ---------------------------------------------------------------------------


def test_claims_count_mismatch_triggers_structure_projections() -> None:
    """One declarative segment but zero claims: structure_projections fires."""
    dossier = _make_dossier()
    draft = _make_draft(claims=[])  # 1 declarative segment, 0 claims

    result = _call_verify(dossier, draft)

    assert result.ok is False
    assert any(v.reason_code == "structure_projections" for v in result.violations)


# ---------------------------------------------------------------------------
# 3. structure_projections — resources_used doesn't match resource segments
# ---------------------------------------------------------------------------


def test_resources_used_mismatch_triggers_structure_projections() -> None:
    """One resource segment present but resources_used is empty: structure_projections fires."""
    dossier = _make_dossier()
    draft = _make_draft(
        segments=[
            DeclarativeSegment(type="declarative", fact_id=_FACT_ID, text=_SAFE_PHRASING),
            ResourceSegment(type="resource", resource_id=_RESOURCE_ID),
        ],
        claims=[_SAFE_PHRASING],
        resources_used=[],  # should be [_RESOURCE_ID]
    )

    result = _call_verify(dossier, draft)

    assert result.ok is False
    assert any(v.reason_code == "structure_projections" for v in result.violations)


# ---------------------------------------------------------------------------
# 4. fact_ids — declarative references a fact not in the dossier
# ---------------------------------------------------------------------------


def test_unknown_fact_id_triggers_fact_ids_violation() -> None:
    """A declarative segment whose fact_id is absent from the dossier triggers fact_ids."""
    dossier = _make_dossier()
    draft = _make_draft(
        segments=[
            DeclarativeSegment(
                type="declarative",
                fact_id="nonexistent-fact-xyz",
                text=_SAFE_PHRASING,
            )
        ],
        claims=[_SAFE_PHRASING],
    )

    result = _call_verify(dossier, draft)

    assert result.ok is False
    assert any(v.reason_code == "fact_ids" for v in result.violations)


# ---------------------------------------------------------------------------
# 5. safe_phrasing — claim doesn't match fact's safe_phrasing after normalization
# ---------------------------------------------------------------------------


def test_claim_not_matching_safe_phrasing_triggers_violation() -> None:
    """A claim that diverges from the fact's safe_phrasings list triggers safe_phrasing."""
    dossier = _make_dossier()
    draft = _make_draft(
        segments=[DeclarativeSegment(type="declarative", fact_id=_FACT_ID, text=_SAFE_PHRASING)],
        claims=["This text is completely different and not an approved safe phrasing."],
    )

    result = _call_verify(dossier, draft)

    assert result.ok is False
    assert any(v.reason_code == "safe_phrasing" for v in result.violations)


# ---------------------------------------------------------------------------
# 6. resource_ids — resource segment references id not in the dossier
# ---------------------------------------------------------------------------


def test_unknown_resource_id_triggers_resource_ids_violation() -> None:
    """A resource segment whose resource_id is absent from the dossier triggers resource_ids."""
    dossier = _make_dossier()
    draft = _make_draft(
        segments=[
            DeclarativeSegment(type="declarative", fact_id=_FACT_ID, text=_SAFE_PHRASING),
            ResourceSegment(type="resource", resource_id="nonexistent-resource"),
        ],
        claims=[_SAFE_PHRASING],
        resources_used=["nonexistent-resource"],
    )

    result = _call_verify(dossier, draft)

    assert result.ok is False
    assert any(v.reason_code == "resource_ids" for v in result.violations)


# ---------------------------------------------------------------------------
# 7. url_allowlist — URL in assembled text not covered by a resource
# ---------------------------------------------------------------------------


def test_url_in_declarative_not_in_resources_triggers_url_allowlist() -> None:
    """Reject a declarative URL that is not a resource canonical URL."""
    stray_url = "https://not-in-dossier.example.com/page"
    text_with_url = f"See {stray_url} for details."
    dossier = _make_dossier(
        facts=[_make_fact(safe_phrasing=text_with_url)],
    )
    draft = _make_draft(
        segments=[
            DeclarativeSegment(type="declarative", fact_id=_FACT_ID, text=text_with_url)
        ],
        claims=[text_with_url],
    )

    result = _call_verify(dossier, draft)

    assert result.ok is False
    assert any(v.reason_code == "url_allowlist" for v in result.violations)


# ---------------------------------------------------------------------------
# 8. prohibitions — exact_phrase match
# ---------------------------------------------------------------------------


def test_exact_phrase_prohibition_triggers_violation() -> None:
    """Assembled text containing an exact_phrase prohibition is blocked."""
    forbidden = "buy our product now"
    phrasing = f"You should {forbidden} to stay secure."
    dossier = _make_dossier(
        facts=[_make_fact(safe_phrasing=phrasing)],
        prohibitions=[_make_prohibition("exact_phrase", forbidden)],
    )
    draft = _make_draft(
        segments=[DeclarativeSegment(type="declarative", fact_id=_FACT_ID, text=phrasing)],
        claims=[phrasing],
    )

    result = _call_verify(dossier, draft)

    assert result.ok is False
    assert any(v.reason_code == "prohibitions" for v in result.violations)


# ---------------------------------------------------------------------------
# 9. prohibitions — normalized_phrase match (case-insensitive after NFKC)
# ---------------------------------------------------------------------------


def test_normalized_phrase_prohibition_triggers_violation() -> None:
    """Assembled text matching a normalized_phrase prohibition is blocked case-insensitively."""
    phrasing = "Visit US Today for your security needs."
    dossier = _make_dossier(
        facts=[_make_fact(safe_phrasing=phrasing)],
        prohibitions=[_make_prohibition("normalized_phrase", "visit us today")],
    )
    draft = _make_draft(
        segments=[DeclarativeSegment(type="declarative", fact_id=_FACT_ID, text=phrasing)],
        claims=[phrasing],
    )

    result = _call_verify(dossier, draft)

    assert result.ok is False
    assert any(v.reason_code == "prohibitions" for v in result.violations)


# ---------------------------------------------------------------------------
# 10. prohibitions — regex match
# ---------------------------------------------------------------------------


def test_regex_prohibition_triggers_violation() -> None:
    """Assembled text matching a regex prohibition pattern is blocked."""
    # The regex matches bare email-style tokens (portable grammar: no \b or \w
    # shorthand classes); no http URL so url_allowlist stays clear.
    phrasing = "Send questions to support@example.com anytime."
    dossier = _make_dossier(
        facts=[_make_fact(safe_phrasing=phrasing)],
        prohibitions=[_make_prohibition("regex", r"[a-zA-Z0-9_]+@[a-zA-Z0-9_]+\.[a-zA-Z0-9_]+")],
    )
    draft = _make_draft(
        segments=[DeclarativeSegment(type="declarative", fact_id=_FACT_ID, text=phrasing)],
        claims=[phrasing],
    )

    result = _call_verify(dossier, draft)

    assert result.ok is False
    assert any(v.reason_code == "prohibitions" for v in result.violations)


# ---------------------------------------------------------------------------
# 10b. prohibitions — regex omitted flags mean none (case-sensitive,
# single-line, non-dotall), never a forced IGNORECASE|MULTILINE|DOTALL.
# ---------------------------------------------------------------------------


def test_regex_prohibition_omitted_flags_is_case_sensitive() -> None:
    """Omitted flags mean case-sensitive matching — no forced IGNORECASE."""
    dossier = _make_dossier(
        prohibitions=[_make_prohibition("regex", "FORBIDDEN")],
    )
    draft = _make_draft(
        segments=[
            DeclarativeSegment(
                type="declarative", fact_id=_FACT_ID, text="forbidden lowercase only"
            )
        ],
    )
    result = _call_verify(dossier, draft)
    assert result.ok is True


def test_regex_prohibition_authored_i_flag_is_ascii_fold_case_insensitive() -> None:
    """Authored i is ASCII-fold case-insensitive, unlike the omitted-flags
    (case-sensitive) case above."""
    dossier = _make_dossier(
        prohibitions=[_make_prohibition("regex", "FORBIDDEN", flags="i")],
    )
    draft = _make_draft(
        segments=[
            DeclarativeSegment(
                type="declarative", fact_id=_FACT_ID, text="forbidden lowercase only"
            )
        ],
    )
    result = _call_verify(dossier, draft)
    assert result.ok is False
    assert any(v.reason_code == "prohibitions" for v in result.violations)


def test_regex_prohibition_omitted_flags_is_not_multiline() -> None:
    """Omitted m flag: ^ anchors only the absolute start of text, not each line."""
    dossier = _make_dossier(
        prohibitions=[_make_prohibition("regex", "^bar")],
    )
    draft = _make_draft(
        segments=[DeclarativeSegment(type="declarative", fact_id=_FACT_ID, text="foo\nbar")],
    )
    result = _call_verify(dossier, draft)
    assert result.ok is True


def test_regex_prohibition_authored_m_flag_anchors_each_line() -> None:
    dossier = _make_dossier(
        prohibitions=[_make_prohibition("regex", "^bar", flags="m")],
    )
    draft = _make_draft(
        segments=[DeclarativeSegment(type="declarative", fact_id=_FACT_ID, text="foo\nbar")],
    )
    result = _call_verify(dossier, draft)
    assert result.ok is False
    assert any(v.reason_code == "prohibitions" for v in result.violations)


def test_regex_prohibition_omitted_flags_is_not_dotall() -> None:
    """Omitted s flag: dot does not match a newline."""
    dossier = _make_dossier(
        prohibitions=[_make_prohibition("regex", "foo.bar")],
    )
    draft = _make_draft(
        segments=[DeclarativeSegment(type="declarative", fact_id=_FACT_ID, text="foo\nbar")],
    )
    result = _call_verify(dossier, draft)
    assert result.ok is True


def test_regex_prohibition_authored_s_flag_dot_matches_newline() -> None:
    dossier = _make_dossier(
        prohibitions=[_make_prohibition("regex", "foo.bar", flags="s")],
    )
    draft = _make_draft(
        segments=[DeclarativeSegment(type="declarative", fact_id=_FACT_ID, text="foo\nbar")],
    )
    result = _call_verify(dossier, draft)
    assert result.ok is False
    assert any(v.reason_code == "prohibitions" for v in result.violations)


# ---------------------------------------------------------------------------
# 11. platform_limits — Bluesky > 300 Unicode code points
# ---------------------------------------------------------------------------


def test_bluesky_over_300_codepoints_triggers_platform_limits() -> None:
    """Assembled text > 300 Unicode code points on Bluesky triggers platform_limits."""
    long_phrasing = "a" * 301
    dossier = _make_dossier(facts=[_make_fact(safe_phrasing=long_phrasing)])
    draft = _make_draft(
        segments=[DeclarativeSegment(type="declarative", fact_id=_FACT_ID, text=long_phrasing)],
        claims=[long_phrasing],
    )

    result = _call_verify(dossier, draft, platform="bluesky")

    assert result.ok is False
    assert any(v.reason_code == "platform_limits" for v in result.violations)


def test_bluesky_exactly_300_codepoints_passes() -> None:
    """Assembled text of exactly 300 Unicode code points on Bluesky passes platform_limits."""
    phrasing = "a" * 300
    dossier = _make_dossier(facts=[_make_fact(safe_phrasing=phrasing)])
    draft = _make_draft(
        segments=[DeclarativeSegment(type="declarative", fact_id=_FACT_ID, text=phrasing)],
        claims=[phrasing],
    )

    result = _call_verify(dossier, draft, platform="bluesky")

    assert result.ok is True


# ---------------------------------------------------------------------------
# 12. platform_limits — Farcaster > 320 UTF-8 bytes
# ---------------------------------------------------------------------------


def test_farcaster_over_320_utf8_bytes_triggers_platform_limits() -> None:
    """Assembled text > 320 UTF-8 bytes on Farcaster triggers platform_limits.

    107 em dashes (U+2014) encode to 321 UTF-8 bytes but are only 107 code
    points — well under Bluesky's 300-CP ceiling — proving the byte check is
    independent of the code-point check.
    """
    long_phrasing = "—" * 107  # 3 UTF-8 bytes each → 321 bytes, 107 code points
    assert len(long_phrasing.encode("utf-8")) == 321
    dossier = _make_dossier(facts=[_make_fact(safe_phrasing=long_phrasing)])
    draft = _make_draft(
        segments=[DeclarativeSegment(type="declarative", fact_id=_FACT_ID, text=long_phrasing)],
        claims=[long_phrasing],
    )

    result = _call_verify(dossier, draft, platform="farcaster")

    assert result.ok is False
    assert any(v.reason_code == "platform_limits" for v in result.violations)


# ---------------------------------------------------------------------------
# 13. platform_limits — Discord > 2000 Unicode code points
# ---------------------------------------------------------------------------


def test_discord_over_2000_codepoints_triggers_platform_limits() -> None:
    """Assembled text > 2000 Unicode code points on Discord triggers platform_limits."""
    long_phrasing = "b" * 2001
    dossier = _make_dossier(facts=[_make_fact(safe_phrasing=long_phrasing)])
    draft = _make_draft(
        segments=[DeclarativeSegment(type="declarative", fact_id=_FACT_ID, text=long_phrasing)],
        claims=[long_phrasing],
    )

    result = _call_verify(dossier, draft, platform="discord")

    assert result.ok is False
    assert any(v.reason_code == "platform_limits" for v in result.violations)


# ---------------------------------------------------------------------------
# 14. platform_limits — unknown platform is always blocked
# ---------------------------------------------------------------------------


def test_unknown_platform_triggers_platform_limits() -> None:
    """An unrecognised platform name is treated as an unsafe unknown and blocked."""
    dossier = _make_dossier()
    draft = _make_draft()

    result = _call_verify(dossier, draft, platform="myspace")

    assert result.ok is False
    assert any(v.reason_code == "platform_limits" for v in result.violations)


# ---------------------------------------------------------------------------
# 15. Blank author_id
# ---------------------------------------------------------------------------


def test_blank_author_id_triggers_violation() -> None:
    """An empty author_id string triggers a violation."""
    dossier = _make_dossier()
    draft = _make_draft()

    result = _call_verify(dossier, draft, author_id="")

    assert result.ok is False
    assert len(result.violations) > 0


# ---------------------------------------------------------------------------
# 16. Question segment containing a URL
# ---------------------------------------------------------------------------


def test_question_with_url_triggers_violation() -> None:
    """A question segment that embeds an http URL is not permitted."""
    dossier = _make_dossier()
    draft = _make_draft(
        posture="engage",
        segments=[
            QuestionSegment(type="question", text="Have you read https://example.com/page?")
        ],
        claims=[],
        resources_used=[],
    )

    result = _call_verify(dossier, draft)

    assert result.ok is False
    assert len(result.violations) > 0


# ---------------------------------------------------------------------------
# 17. Question segment in 'answer' posture
# ---------------------------------------------------------------------------


def test_question_in_answer_posture_triggers_posture_violation() -> None:
    """Questions are only permitted in 'engage' or 'ask' postures; 'answer' is not allowed."""
    dossier = _make_dossier()
    draft = StructuredDraftOutput(
        posture="answer",
        segments=[
            DeclarativeSegment(type="declarative", fact_id=_FACT_ID, text=_SAFE_PHRASING),
            QuestionSegment(type="question", text="Does this make sense to you?"),
        ],
        claims=[_SAFE_PHRASING],
        resources_used=[],
    )

    result = _call_verify(dossier, draft)

    assert result.ok is False
    assert any(v.reason_code == "posture" for v in result.violations)


# ---------------------------------------------------------------------------
# 18. Abstain posture — non-reply decision produces no assembled text
# ---------------------------------------------------------------------------


def test_abstain_posture_with_no_segments_returns_ok_and_no_text() -> None:
    """Treat an empty abstention as a successful no-reply decision."""
    dossier = _make_dossier()
    draft = StructuredDraftOutput(
        posture="abstain",
        segments=[],
        claims=[],
        resources_used=[],
        abstain_reason="No dossier fact addresses this question.",
    )

    result = _call_verify(dossier, draft)

    assert result.ok is True
    assert result.violations == []
    assert result.assembled_text is None


# ---------------------------------------------------------------------------
# 19. Assembled text empty — platform_limits blocks non-abstain empty drafts
# ---------------------------------------------------------------------------


def test_empty_assembled_text_on_non_abstain_posture_triggers_platform_limits() -> None:
    """An 'answer' draft with no segments assembles to empty text; platform_limits blocks it."""
    dossier = _make_dossier()
    draft = StructuredDraftOutput(
        posture="answer",
        segments=[],
        claims=[],
        resources_used=[],
    )

    result = _call_verify(dossier, draft)

    assert result.ok is False
    assert any(v.reason_code == "platform_limits" for v in result.violations)


# ---------------------------------------------------------------------------
# Structural invariants on VerifyResult and GateViolation
# ---------------------------------------------------------------------------


def test_verify_result_ok_implies_empty_violations() -> None:
    """When ok=True the violations list must be empty."""
    dossier = _make_dossier()
    draft = _make_draft()

    result = _call_verify(dossier, draft)

    if result.ok:
        assert result.violations == []


def test_gate_violation_fields_have_correct_types() -> None:
    """GateViolation fields expose their documented types."""
    dossier = _make_dossier()
    draft = _make_draft(claims=[])  # triggers structure_projections

    result = _call_verify(dossier, draft)

    assert not result.ok
    assert len(result.violations) >= 1
    v = result.violations[0]
    assert isinstance(v.reason_code, str) and v.reason_code
    assert v.offending_text is None or isinstance(v.offending_text, str)
    assert v.segment_index is None or isinstance(v.segment_index, int)


# ---------------------------------------------------------------------------
# Text assembly integration
# ---------------------------------------------------------------------------


def test_resource_segment_assembles_to_label_and_url() -> None:
    """ResourceSegment assembles as 'Resource: {label} — {canonical_url}'."""
    dossier = _make_dossier()
    draft = StructuredDraftOutput(
        posture="answer",
        segments=[
            DeclarativeSegment(type="declarative", fact_id=_FACT_ID, text=_SAFE_PHRASING),
            ResourceSegment(type="resource", resource_id=_RESOURCE_ID),
        ],
        claims=[_SAFE_PHRASING],
        resources_used=[_RESOURCE_ID],
    )

    result = _call_verify(dossier, draft)

    assert result.ok is True
    expected_chunk = f"Resource: {_RESOURCE_LABEL} — {_RESOURCE_URL}"
    assert result.assembled_text is not None
    assert expected_chunk in result.assembled_text
    assert result.assembled_text.startswith(_SAFE_PHRASING)


def test_question_segment_assembles_in_ask_posture() -> None:
    """QuestionSegment text appears verbatim in assembled text under 'ask' posture."""
    question_text = "What authentication flow does your team use?"
    dossier = _make_dossier()
    draft = StructuredDraftOutput(
        posture="ask",
        segments=[QuestionSegment(type="question", text=question_text)],
        claims=[],
        resources_used=[],
    )

    result = _call_verify(dossier, draft)

    assert result.ok is True
    assert result.assembled_text == question_text


def test_multiple_segments_joined_by_single_space() -> None:
    """Multiple segments are joined by exactly one ASCII space in the assembled text."""
    question_text = "Does your team face similar challenges?"
    dossier = _make_dossier()
    draft = StructuredDraftOutput(
        posture="engage",
        segments=[
            DeclarativeSegment(type="declarative", fact_id=_FACT_ID, text=_SAFE_PHRASING),
            QuestionSegment(type="question", text=question_text),
        ],
        claims=[_SAFE_PHRASING],
        resources_used=[],
    )

    result = _call_verify(dossier, draft)

    assert result.ok is True
    assert result.assembled_text == f"{_SAFE_PHRASING} {question_text}"


# ---------------------------------------------------------------------------
# C0/C1 control character gate (platform_limits)
# ---------------------------------------------------------------------------


def test_control_character_in_text_triggers_platform_limits() -> None:
    """Assembled text containing a C0 control character (except LF) triggers platform_limits."""
    # U+0007 BELL is a C0 control character prohibited in any post.
    phrasing = "Security matters.\x07"
    dossier = _make_dossier(facts=[_make_fact(safe_phrasing=phrasing)])
    draft = _make_draft(
        segments=[DeclarativeSegment(type="declarative", fact_id=_FACT_ID, text=phrasing)],
        claims=[phrasing],
    )

    result = _call_verify(dossier, draft)

    assert result.ok is False
    assert any(v.reason_code == "platform_limits" for v in result.violations)


# ---------------------------------------------------------------------------
# url_allowlist — canonicalization failures must fail closed, never be skipped
# ---------------------------------------------------------------------------


def test_fragment_url_triggers_url_allowlist_violation() -> None:
    """A fragment-bearing URL is rejected by canonicalize_url and must not be skipped."""
    raw_url = "https://gateway.example.com/docs#section"
    text = f"See {raw_url} for details."
    dossier = _make_dossier(facts=[_make_fact(safe_phrasing=text)])
    draft = _make_draft(
        segments=[DeclarativeSegment(type="declarative", fact_id=_FACT_ID, text=text)],
        claims=[text],
    )

    result = _call_verify(dossier, draft)

    assert result.ok is False
    url_violations = [v for v in result.violations if v.reason_code == "url_allowlist"]
    assert len(url_violations) == 1
    assert url_violations[0].offending_text == raw_url


def test_credential_bearing_url_triggers_url_allowlist_violation() -> None:
    """A URL with embedded credentials is rejected by canonicalize_url."""
    raw_url = "https://user:pass@gateway.example.com/docs"
    text = f"See {raw_url} for details."
    dossier = _make_dossier(facts=[_make_fact(safe_phrasing=text)])
    draft = _make_draft(
        segments=[DeclarativeSegment(type="declarative", fact_id=_FACT_ID, text=text)],
        claims=[text],
    )

    result = _call_verify(dossier, draft)

    assert result.ok is False
    url_violations = [v for v in result.violations if v.reason_code == "url_allowlist"]
    assert len(url_violations) == 1
    assert url_violations[0].offending_text == raw_url


def test_unparseable_port_url_triggers_url_allowlist_violation() -> None:
    """A URL that matches the http(s) regex but has a non-numeric port fails closed.

    ``urllib.parse.urlparse`` accepts this string without raising, but the
    ``.port`` property raises ``ValueError`` lazily when canonicalize_url reads
    it — proving canonicalizer exceptions outside the initial parse call are
    still caught rather than crashing verification.
    """
    raw_url = "https://gateway.example.com:abc/docs"
    text = f"See {raw_url} for details."
    dossier = _make_dossier(facts=[_make_fact(safe_phrasing=text)])
    draft = _make_draft(
        segments=[DeclarativeSegment(type="declarative", fact_id=_FACT_ID, text=text)],
        claims=[text],
    )

    result = _call_verify(dossier, draft)

    assert result.ok is False
    url_violations = [v for v in result.violations if v.reason_code == "url_allowlist"]
    assert len(url_violations) == 1
    assert url_violations[0].offending_text == raw_url


def test_invalid_dossier_resource_canonical_url_yields_violation_not_crash() -> None:
    """A malformed dossier resource canonical_url fails closed, not a crash.

    Loader validation should reject this before it ever reaches the verifier,
    but the verifier must remain fail-closed and auditable if malformed data
    reaches it anyway (e.g. via a bypassed constructor).
    """
    bad_resource = DossierResource.model_construct(
        id=_RESOURCE_ID,
        label=_RESOURCE_LABEL,
        canonical_url="ftp://gateway.example.com/docs",
        immutable_evidence=_IMMUTABLE_EVIDENCE,
    )
    dossier = _make_dossier(resources=[bad_resource])
    draft = _make_draft()

    result = _call_verify(dossier, draft)

    assert result.ok is False
    url_violations = [v for v in result.violations if v.reason_code == "url_allowlist"]
    assert len(url_violations) == 1
    assert url_violations[0].offending_text == "ftp://gateway.example.com/docs"


# ---------------------------------------------------------------------------
# platform_limits — canonical resource expansion, not a short placeholder,
# must be what gets length-checked
# ---------------------------------------------------------------------------


def test_resource_expansion_exceeds_bluesky_limit_though_placeholder_would_not() -> None:
    """Canonical resource expansion must be validated, not a short placeholder.

    The old ``[resource:id]`` representation would total 299 code points (at
    or under Bluesky's 300-code-point limit), but the canonical
    ``Resource: {label} — {canonical_url}`` expansion pushes the assembled
    text past it — proving validation must run after dossier-aware expansion.
    """
    declarative_text = "a" * 280
    dossier = _make_dossier(facts=[_make_fact(safe_phrasing=declarative_text)])
    draft = StructuredDraftOutput(
        posture="answer",
        segments=[
            DeclarativeSegment(type="declarative", fact_id=_FACT_ID, text=declarative_text),
            ResourceSegment(type="resource", resource_id=_RESOURCE_ID),
        ],
        claims=[declarative_text],
        resources_used=[_RESOURCE_ID],
    )

    placeholder_total = len(declarative_text) + 1 + len(f"[resource:{_RESOURCE_ID}]")
    assert placeholder_total <= 300

    result = _call_verify(dossier, draft, platform="bluesky")

    expected_text = f"{declarative_text} Resource: {_RESOURCE_LABEL} — {_RESOURCE_URL}"
    assert len(expected_text) > 300
    assert result.ok is False
    assert any(v.reason_code == "platform_limits" for v in result.violations)
    assert result.assembled_text == expected_text


# ---------------------------------------------------------------------------
# assemble_draft_text — public pure function
# ---------------------------------------------------------------------------


def test_assemble_draft_text_matches_verify_result() -> None:
    """assemble_draft_text is the same value verify_draft_content places on VerifyResult."""
    dossier = _make_dossier()
    draft = _make_draft()

    assert assemble_draft_text(draft, dossier) == _SAFE_PHRASING


def test_assemble_draft_text_returns_none_when_no_part_resolves() -> None:
    """An unresolvable resource segment with no other segments assembles to None."""
    dossier = _make_dossier()
    draft = StructuredDraftOutput(
        posture="answer",
        segments=[ResourceSegment(type="resource", resource_id="nonexistent-resource")],
        claims=[],
        resources_used=["nonexistent-resource"],
    )

    assert assemble_draft_text(draft, dossier) is None
