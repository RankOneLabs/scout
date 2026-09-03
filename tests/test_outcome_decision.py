"""DB-free tests for scan_runner.classify_outcome — T-011.

classify_outcome is a pure, synchronous, DB-free transform: its only
collaborators are RELEVANCE_THRESHOLD, immutable input objects, and
verify_draft_content. No StateManager, no SQLite, no mocks of persistence.

Precedence (fixed):
    1. critic verdict reject
    2. structured posture abstain
    3. candidate not relevant
    4. relevant score below RELEVANCE_THRESHOLD
    5. missing project key or structured draft
    6. missing dossier
    7. content verifier rejection
    8. verifier success with missing or empty assembled text
    9. surfaced
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

import scout.scanning.runner as scan_runner
from scout.config import RELEVANCE_THRESHOLD, Message
from scout.dossiers.resolver import DossierFact, DossierResource, DossierSummary
from scout.scanning.runner import OutcomeDecision, classify_outcome
from scout.scanning.schemas import DeclarativeSegment, ReplyCandidate, StructuredDraftOutput
from scout.verifier import VerifyResult

_FACT_ID = "fact-001"
_SAFE_PHRASING = "Our gateway product improves agent signup security."
_RESOURCE_ID = "res-001"
_RESOURCE_URL = "https://gateway.example.com/docs"
_PROJECT_KEY = "gateway"


def _message() -> Message:
    return Message(
        platform="discord",
        platform_id="m1",
        channel_name="general",
        channel_id="c1",
        author_name="alice",
        author_id="author-1",
        content="tell me about the gateway",
        created_at=datetime(2026, 4, 18, tzinfo=UTC),
    )


def _dossier(project_key: str = _PROJECT_KEY) -> DossierSummary:
    return DossierSummary(
        project_key=project_key,
        last_reviewed=date.today(),
        reviewer="reviewer-1",
        facts=[
            DossierFact(
                id=_FACT_ID,
                text=f"Background: {_SAFE_PHRASING}",
                safe_phrasings=[_SAFE_PHRASING],
                immutable_evidence=["https://source.example.com/evidence"],
            )
        ],
        resources=[
            DossierResource(
                id=_RESOURCE_ID,
                label="Gateway Docs",
                canonical_url=_RESOURCE_URL,
                immutable_evidence=["https://source.example.com/evidence"],
            )
        ],
        prohibitions=[],
    )


def _passing_draft(**overrides: object) -> StructuredDraftOutput:
    defaults: dict[str, object] = dict(
        posture="answer",
        segments=[
            DeclarativeSegment(type="declarative", fact_id=_FACT_ID, text=_SAFE_PHRASING)
        ],
        claims=[_SAFE_PHRASING],
        resources_used=[],
    )
    defaults.update(overrides)
    return StructuredDraftOutput(**defaults)  # type: ignore[arg-type]


def _relevant_candidate(**overrides: object) -> ReplyCandidate:
    defaults: dict[str, object] = dict(
        relevant=True,
        score=0.9,
        reason="direct fit",
        relevant_to=[_PROJECT_KEY],
        project_key=_PROJECT_KEY,
        structured_draft=_passing_draft(),
    )
    defaults.update(overrides)
    return ReplyCandidate(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Terminal ladder — one case per outcome
# ---------------------------------------------------------------------------


def test_surfaced_carries_assembled_text_and_no_terminal_reason() -> None:
    candidate = _relevant_candidate()
    decision = classify_outcome(candidate, _message(), {_PROJECT_KEY: _dossier()})

    assert decision.status == "surfaced"
    assert decision.project_key == _PROJECT_KEY
    assert decision.posture == "answer"
    assert decision.validated_text == f"{_SAFE_PHRASING}"
    assert decision.terminal_reason is None
    assert decision.gate_violations == ()
    assert decision.structured_draft is not None


def test_not_relevant() -> None:
    candidate = ReplyCandidate(
        relevant=False,
        score=0.1,
        reason="off-topic",
        relevant_to=[],
    )
    decision = classify_outcome(candidate, _message(), {})

    assert decision.status == "not_relevant"
    assert decision.project_key is None
    assert decision.terminal_reason is None


def test_low_relevance_below_threshold() -> None:
    candidate = _relevant_candidate(score=max(RELEVANCE_THRESHOLD - 0.01, 0.0))
    decision = classify_outcome(candidate, _message(), {_PROJECT_KEY: _dossier()})

    assert decision.status == "low_relevance"


def test_drafting_failed_missing_project_key() -> None:
    """Neither project_key nor relevant_to[0] identifies a project."""
    candidate = _relevant_candidate(project_key=None, relevant_to=[])
    decision = classify_outcome(candidate, _message(), {})

    assert decision.status == "drafting_failed"
    assert decision.project_key is None
    assert decision.terminal_reason == "relevant evaluation did not identify a project"


def test_drafting_failed_missing_structured_draft() -> None:
    candidate = _relevant_candidate(structured_draft=None)
    decision = classify_outcome(candidate, _message(), {_PROJECT_KEY: _dossier()})

    assert decision.status == "drafting_failed"
    assert decision.project_key == _PROJECT_KEY
    assert decision.terminal_reason == "relevant evaluation did not produce a structured draft"


def test_drafting_failed_verifier_ok_with_no_assembled_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-abstain draft that passes every real gate always assembles
    non-empty text (platform_limits fails closed otherwise), so this ladder
    position is unreachable via the real verifier. Monkeypatch
    verify_draft_content to exercise the fail-closed contract directly."""
    monkeypatch.setattr(
        scan_runner,
        "verify_draft_content",
        lambda **kwargs: VerifyResult(ok=True, violations=[], assembled_text=None),
    )
    candidate = _relevant_candidate()
    decision = classify_outcome(candidate, _message(), {_PROJECT_KEY: _dossier()})

    assert decision.status == "drafting_failed"
    assert decision.project_key == _PROJECT_KEY
    assert decision.terminal_reason == "verified draft produced no publishable text"


def test_gate_blocked_missing_dossier_retains_resolved_key() -> None:
    candidate = _relevant_candidate()
    decision = classify_outcome(candidate, _message(), {})

    assert decision.status == "gate_blocked"
    assert decision.project_key == _PROJECT_KEY
    assert len(decision.gate_violations) == 1
    assert decision.gate_violations[0].reason_code == "missing_dossier"


def test_gate_blocked_content_verifier_rejection_retains_assembled_text() -> None:
    bad_draft = _passing_draft(
        segments=[
            DeclarativeSegment(type="declarative", fact_id="unknown-fact", text=_SAFE_PHRASING)
        ],
        claims=[_SAFE_PHRASING],
    )
    candidate = _relevant_candidate(structured_draft=bad_draft)
    decision = classify_outcome(candidate, _message(), {_PROJECT_KEY: _dossier()})

    assert decision.status == "gate_blocked"
    assert any(v.reason_code == "fact_ids" for v in decision.gate_violations)
    # Assembled text is retained for audit even though the gate failed.
    assert decision.validated_text == _SAFE_PHRASING


def test_abstained_uses_structured_abstain_reason() -> None:
    draft = _passing_draft(
        posture="abstain",
        segments=[],
        claims=[],
        resources_used=[],
        abstain_reason="No dossier fact addresses this.",
    )
    candidate = _relevant_candidate(structured_draft=draft)
    decision = classify_outcome(candidate, _message(), {_PROJECT_KEY: _dossier()})

    assert decision.status == "abstained"
    assert decision.terminal_reason == "No dossier fact addresses this."
    assert decision.validated_text is None


def test_abstain_wins_over_relevant_false() -> None:
    """A malformed/legacy candidate shape: relevant=False but structured
    posture is abstain. classify_outcome checks posture before relevance."""
    draft = _passing_draft(
        posture="abstain",
        segments=[],
        claims=[],
        resources_used=[],
        abstain_reason="No contribution to make.",
    )
    candidate = ReplyCandidate(
        relevant=False,
        score=0.9,
        reason="direct fit",
        relevant_to=[_PROJECT_KEY],
        project_key=_PROJECT_KEY,
        structured_draft=draft,
    )
    decision = classify_outcome(candidate, _message(), {_PROJECT_KEY: _dossier()})

    assert decision.status == "abstained"
    assert decision.terminal_reason == "No contribution to make."


def test_critic_rejected_with_empty_segments_and_relevant_false() -> None:
    """A critic reject always wins, even when relevant=False and the
    structured draft has zero segments — it is not an irrelevance judgment."""
    empty_draft = _passing_draft(segments=[], claims=[])
    candidate = ReplyCandidate(
        relevant=False,
        score=0.2,
        reason="borderline",
        relevant_to=[_PROJECT_KEY],
        project_key=_PROJECT_KEY,
        critique_verdict="reject",
        critique_feedback="Draft invents an unsupported claim.",
        structured_draft=empty_draft,
    )
    decision = classify_outcome(candidate, _message(), {_PROJECT_KEY: _dossier()})

    assert decision.status == "critic_rejected"
    assert decision.terminal_reason == "Draft invents an unsupported claim."
    assert decision.validated_text is None
    assert decision.critique is not None
    assert decision.critique.verdict == "reject"


def test_critic_rejected_wins_over_not_relevant() -> None:
    candidate = ReplyCandidate(
        relevant=False,
        score=0.1,
        reason="off-topic per relevance phase",
        relevant_to=[],
        critique_verdict="reject",
        critique_feedback="rejected anyway",
    )
    decision = classify_outcome(candidate, _message(), {})

    assert decision.status == "critic_rejected"


def test_project_key_resolution_prefers_routed_over_relevant_to() -> None:
    candidate = _relevant_candidate(project_key="routed-key", relevant_to=["other-key"])
    decision = classify_outcome(candidate, _message(), {})

    assert decision.project_key == "routed-key"


def test_project_key_resolution_falls_back_to_first_relevant_to() -> None:
    """KEYWORD_PREFILTER=false path: no routed project_key, relevant_to[0] wins."""
    candidate = _relevant_candidate(project_key=None, relevant_to=["agent-ops", "other"])
    decision = classify_outcome(candidate, _message(), {})

    assert decision.project_key == "agent-ops"
    assert decision.status == "gate_blocked"  # no dossier loaded for agent-ops
    assert decision.gate_violations[0].reason_code == "missing_dossier"


def test_returns_outcome_decision_instance() -> None:
    decision = classify_outcome(_relevant_candidate(), _message(), {_PROJECT_KEY: _dossier()})
    assert isinstance(decision, OutcomeDecision)


# ---------------------------------------------------------------------------
# project_key resolution applies uniformly, not only to the "relevant" branches
# ---------------------------------------------------------------------------


def test_critic_rejected_resolves_project_key_from_relevant_to() -> None:
    """KEYWORD_PREFILTER=false: an unrouted critic-rejected candidate must
    still attribute the outcome to relevant_to[0], not persist a null key."""
    candidate = ReplyCandidate(
        relevant=False,
        score=0.2,
        reason="borderline",
        relevant_to=["agent-ops"],
        project_key=None,
        critique_verdict="reject",
        critique_feedback="Draft invents an unsupported claim.",
    )
    decision = classify_outcome(candidate, _message(), {})

    assert decision.status == "critic_rejected"
    assert decision.project_key == "agent-ops"


def test_abstained_resolves_project_key_from_relevant_to() -> None:
    draft = _passing_draft(
        posture="abstain",
        segments=[],
        claims=[],
        resources_used=[],
        abstain_reason="No dossier fact addresses this.",
    )
    candidate = ReplyCandidate(
        relevant=True,
        score=0.9,
        reason="direct fit",
        relevant_to=["agent-ops"],
        project_key=None,
        structured_draft=draft,
    )
    decision = classify_outcome(candidate, _message(), {})

    assert decision.status == "abstained"
    assert decision.project_key == "agent-ops"


def test_low_relevance_resolves_project_key_from_relevant_to() -> None:
    candidate = ReplyCandidate(
        relevant=True,
        score=max(RELEVANCE_THRESHOLD - 0.01, 0.0),
        reason="weak fit",
        relevant_to=["agent-ops"],
        project_key=None,
    )
    decision = classify_outcome(candidate, _message(), {})

    assert decision.status == "low_relevance"
    assert decision.project_key == "agent-ops"
