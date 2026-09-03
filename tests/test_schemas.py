"""Tests for ReplyCandidate + unpack_candidate."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from scout.config import Message
from scout.scanning.schemas import (
    FARCASTER_POST_MAX_BYTES,
    CritiquePhaseOutput,
    DeclarativeSegment,
    ReplyCandidate,
    StructuredDraftOutput,
    unpack_candidate,
    validate_farcaster_byte_length,
)


def _make_message() -> Message:
    return Message(
        platform="discord",
        platform_id="1",
        channel_name="general",
        channel_id="123",
        author_name="alice",
        author_id="a1",
        content="hello",
        created_at=datetime(2026, 4, 18, tzinfo=UTC),
    )


def _make_structured_draft() -> StructuredDraftOutput:
    return StructuredDraftOutput(
        posture="answer",
        segments=[
            DeclarativeSegment(type="declarative", fact_id="fact-1", text="Our gateway helps.")
        ],
        claims=["Our gateway helps."],
        resources_used=[],
    )


def test_critique_revision_is_a_nested_structured_draft() -> None:
    critique = CritiquePhaseOutput(
        verdict="revise",
        feedback="Softened the wording.",
        revised_draft=_make_structured_draft(),
    )

    assert critique.revised_draft == _make_structured_draft()


def test_critique_revision_requires_a_revised_draft() -> None:
    with pytest.raises(ValidationError):
        CritiquePhaseOutput(verdict="revise", feedback="Needs softer wording.")


def test_relevant_candidate_round_trips() -> None:
    candidate = ReplyCandidate(
        relevant=True,
        score=0.9,
        reason="mentions the gateway project",
        relevant_to=["gateway"],
        project_key="gateway",
        structured_draft=_make_structured_draft(),
        critique_verdict="approve",
        critique_feedback="Looks good.",
    )

    relevance, critique = unpack_candidate(candidate, _make_message())

    assert relevance.relevant is True
    assert relevance.score == 0.9
    assert relevance.relevant_to == ("gateway",)
    assert critique is not None
    assert critique.verdict == "approve"


def test_not_relevant_short_circuits() -> None:
    candidate = ReplyCandidate(
        relevant=False,
        score=0.1,
        reason="off-topic",
        relevant_to=[],
    )

    relevance, critique = unpack_candidate(candidate, _make_message())

    assert relevance.relevant is False
    assert critique is None


def test_relevant_without_structured_draft_is_valid() -> None:
    """Classification, rather than model parsing, handles an incomplete draft."""
    candidate = ReplyCandidate(
        relevant=True,
        score=0.9,
        reason="hit",
        relevant_to=["gateway"],
        project_key="gateway",
        structured_draft=None,
    )
    assert candidate.structured_draft is None


def test_relevant_without_project_key_is_valid() -> None:
    """project_key is no longer schema-enforced when relevant=True.

    classify_outcome is the seam that turns an unresolved project into a
    terminal drafting_failed outcome; the schema must not raise first and
    turn a supported KEYWORD_PREFILTER=false configuration into an
    exception loop.
    """
    candidate = ReplyCandidate(
        relevant=True,
        score=0.9,
        reason="hit",
        relevant_to=["gateway"],
        project_key=None,
        structured_draft=_make_structured_draft(),
    )
    assert candidate.project_key is None


def test_score_must_be_in_range() -> None:
    with pytest.raises(ValidationError):
        ReplyCandidate(
            relevant=False,
            score=1.5,
            reason="bad",
            relevant_to=[],
        )


def test_missing_required_field_raises() -> None:
    with pytest.raises(ValidationError):
        ReplyCandidate.model_validate({"score": 0.5, "reason": "x", "relevant_to": []})


def test_relevant_with_no_critique_unpacks_cleanly() -> None:
    candidate = ReplyCandidate(
        relevant=True,
        score=0.8,
        reason="topical",
        relevant_to=["gateway"],
        project_key="gateway",
        structured_draft=_make_structured_draft(),
    )

    relevance, critique = unpack_candidate(candidate, _make_message())

    assert relevance.relevant is True
    assert critique is None


class TestAbstainReasonContract:
    def test_abstain_with_reason_and_empty_fields_is_valid(self) -> None:
        draft = StructuredDraftOutput(
            posture="abstain",
            segments=[],
            claims=[],
            resources_used=[],
            abstain_reason="No dossier fact addresses this topic.",
        )
        assert draft.abstain_reason == "No dossier fact addresses this topic."

    def test_abstain_preserves_reason_without_trimming(self) -> None:
        draft = StructuredDraftOutput(
            posture="abstain",
            segments=[],
            claims=[],
            resources_used=[],
            abstain_reason="  padded reason  ",
        )
        assert draft.abstain_reason == "  padded reason  "

    def test_abstain_without_reason_is_invalid(self) -> None:
        with pytest.raises(ValidationError):
            StructuredDraftOutput(
                posture="abstain",
                segments=[],
                claims=[],
                resources_used=[],
            )

    def test_abstain_with_whitespace_only_reason_is_invalid(self) -> None:
        with pytest.raises(ValidationError):
            StructuredDraftOutput(
                posture="abstain",
                segments=[],
                claims=[],
                resources_used=[],
                abstain_reason="   ",
            )

    def test_abstain_with_nonempty_segments_is_invalid(self) -> None:
        with pytest.raises(ValidationError):
            StructuredDraftOutput(
                posture="abstain",
                segments=[
                    DeclarativeSegment(type="declarative", fact_id="fact-1", text="x")
                ],
                claims=["x"],
                resources_used=[],
                abstain_reason="No contribution.",
            )

    def test_non_abstain_posture_with_reason_is_invalid(self) -> None:
        with pytest.raises(ValidationError):
            StructuredDraftOutput(
                posture="answer",
                segments=[
                    DeclarativeSegment(type="declarative", fact_id="fact-1", text="x")
                ],
                claims=["x"],
                resources_used=[],
                abstain_reason="should not be set",
            )

    def test_non_abstain_posture_defaults_reason_to_none(self) -> None:
        draft = _make_structured_draft()
        assert draft.abstain_reason is None


class TestValidateFarcasterByteLength:
    def test_exactly_320_ascii_bytes_passes(self) -> None:
        text = "a" * FARCASTER_POST_MAX_BYTES
        assert validate_farcaster_byte_length(text) is None

    def test_321_ascii_bytes_fails(self) -> None:
        text = "a" * (FARCASTER_POST_MAX_BYTES + 1)
        error = validate_farcaster_byte_length(text)
        assert error is not None
        assert "321" in error
        assert "320" in error

    def test_em_dash_counts_as_three_bytes(self) -> None:
        # U+2014 EM DASH encodes to 3 UTF-8 bytes
        # 105 em dashes = 315 bytes, plus 5 ASCII = 320 bytes total — passes
        text = "—" * 105 + "aaaaa"
        assert len(text.encode("utf-8")) == 320
        assert validate_farcaster_byte_length(text) is None

    def test_em_dash_string_exceeding_limit(self) -> None:
        # 107 em dashes = 321 bytes — fails, but only 107 code points (well under 320 chars)
        text = "—" * 107
        assert len(text) == 107  # code-point count is 107
        error = validate_farcaster_byte_length(text)
        assert error is not None
        assert "321" in error

    def test_curly_quotes_count_three_bytes_each(self) -> None:
        # U+201C and U+201D are 3 UTF-8 bytes each
        # 107 curly quotes = 321 bytes
        text = "“" * 107
        error = validate_farcaster_byte_length(text)
        assert error is not None

    def test_emoji_counts_four_bytes(self) -> None:
        # U+1F600 😀 encodes to 4 UTF-8 bytes
        # 80 emoji = 320 bytes — passes
        text = "\U0001f600" * 80
        assert len(text.encode("utf-8")) == 320
        assert validate_farcaster_byte_length(text) is None

    def test_emoji_one_over_limit(self) -> None:
        # 81 emoji = 324 bytes — fails; character count (81) is well under 320
        text = "\U0001f600" * 81
        assert len(text) == 81
        error = validate_farcaster_byte_length(text)
        assert error is not None
        assert "324" in error

    def test_mixed_unicode_under_char_limit_but_over_byte_limit(self) -> None:
        # Mix of ASCII and 3-byte characters; character count < 320, byte count > 320
        # 200 ASCII + 41 em dashes = 200 + 123 = 323 bytes, 241 characters
        text = "a" * 200 + "—" * 41
        assert len(text) == 241  # well under 320 chars
        assert len(text.encode("utf-8")) == 323
        error = validate_farcaster_byte_length(text)
        assert error is not None
        assert "323" in error

    def test_empty_string_passes(self) -> None:
        assert validate_farcaster_byte_length("") is None

    def test_error_message_mentions_utf8(self) -> None:
        error = validate_farcaster_byte_length("x" * 321)
        assert error is not None
        assert "UTF-8" in error
