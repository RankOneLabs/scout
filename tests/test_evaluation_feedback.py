"""Tests for evaluation_feedback.py: the evaluation-feedback/v1 pipeline."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import scout.grading.feedback as ef
from scout.config import GradeRecord, Message, RelevanceResult
from scout.storage.state import StateManager

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

ELIGIBILITY_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "feedback_eligibility_cases.json"
)


def _config(**overrides: object) -> ef.FeedbackPolicyConfig:
    base = dict(
        policy_version="evaluation-feedback/v1",
        lookback_days=90,
        max_grades=200,
        segment_min_grades=5,
        relevance_example_limit=2,
        reply_draft_example_limit=2,
        critic_example_limit=3,
        note_max_chars=240,
        relevance_token_budget=800,
        reply_draft_token_budget=800,
        critic_token_budget=1000,
    )
    base.update(overrides)
    return ef.FeedbackPolicyConfig(**base)  # type: ignore[arg-type]


def _row(grade_id: int = 1, **overrides: object) -> ef.GradePopulationRow:
    """A fully valid, globally-eligible, draft-quality-population row by
    default: relevance correct, evaluation relevant=1, drafted, accepted,
    no draft/evaluation contract mismatch, no override."""
    base: dict[str, object] = dict(
        grade_id=grade_id,
        post_id=grade_id,
        scan_id=1,
        graded_at=f"2026-01-{grade_id:02d}T00:00:00.000Z",
        schema_version=3,
        needs_regrade=False,
        relevance_judgment="correct",
        action_judgment="accept",
        dimensions=None,
        failure_note=None,
        factual_disposition=None,
        factual_offending_claim=None,
        factual_contradicting_evidence=None,
        context_missing_input=None,
        posture_should_have_been=None,
        implication_implied_claim=None,
        implication_missing_support=None,
        platform="bluesky",
        evaluation_id=100 + grade_id,
        evaluation_post_id=grade_id,
        evaluation_scan_id=1,
        evaluation_relevant=1,
        evaluation_project_key="proj",
        evaluation_dossier_summary_id="sum-1",
        evaluation_dossier_revision="rev-1",
        evaluation_posture="answer",
        draft_comment_id=200 + grade_id,
        draft_project_key="proj",
        draft_dossier_summary_id="sum-1",
        draft_dossier_revision="rev-1",
        draft_posture="answer",
        override_mode="auto",
        override_reason=None,
        pinned_revision_id=grade_id,
        pinned_revision_number=1,
    )
    base.update(overrides)
    return ef.GradePopulationRow(**base)  # type: ignore[arg-type]


def _eligible(row: ef.GradePopulationRow) -> ef.EligibilityResult:
    return ef.EligibilityResult(row=row, status="eligible", reason=None)


def _relevance(agg: ef.PhaseAggregate) -> ef.RelevanceAggregate:
    assert isinstance(agg, ef.RelevanceAggregate)
    return agg


def _draft(agg: ef.PhaseAggregate) -> ef.DraftQualityAggregate:
    assert isinstance(agg, ef.DraftQualityAggregate)
    return agg


# --------------------------------------------------------------------------
# Cross-runtime eligibility conformance: tests/fixtures/feedback_eligibility_cases.json
# is the single language-neutral source of first-exclusion-reason precedence
# and cap-behavior cases, also loaded by
# web/__tests__/feedback-eligibility-conformance.test.ts. A change to
# precedence must update both runtimes' verdicts identically or one of the
# two suites fails.
# --------------------------------------------------------------------------


def _load_eligibility_fixture() -> dict[str, object]:
    with ELIGIBILITY_FIXTURE_PATH.open() as f:
        return json.load(f)


_ELIGIBILITY_FIXTURE = _load_eligibility_fixture()


class TestEligibilityFixtureConformance:
    """Deliberately does not pass the fixture's lookback_days/as_of fields
    into `_config()`: `classify_feedback_eligibility` is a pure
    post-selection classifier that never reads `config.lookback_days` —
    that boundary is applied earlier, by `load_grade_population`'s SQL
    query, which this fixture-driven suite intentionally bypasses (same
    as every other test in this file's `_row()`/`_config()` pattern). The
    fixture's `lookback_days`/`as_of` fields exist for the TypeScript
    conformance suite (`web/__tests__/feedback-eligibility-conformance.test.ts`),
    whose `computeEligibilityWindow` performs that boundary computation
    itself and does need them. `max_grades` is the only fixture policy
    field this suite's cap cases pass through, because it is the only one
    `classify_feedback_eligibility` actually consumes."""

    @pytest.mark.parametrize(
        "case",
        _ELIGIBILITY_FIXTURE["precedence_cases"],
        ids=lambda case: case["name"],
    )
    def test_precedence_case(self, case: dict[str, object]) -> None:
        row = ef.GradePopulationRow(**case["row"])
        result = ef.classify_feedback_eligibility([row], config=_config())[0]
        expected = case["expected"]
        assert result.status == expected["status"]
        assert result.reason == expected["reason"]

    @pytest.mark.parametrize(
        "case",
        _ELIGIBILITY_FIXTURE["cap_cases"],
        ids=lambda case: case["name"],
    )
    def test_cap_case(self, case: dict[str, object]) -> None:
        rows = [ef.GradePopulationRow(**r) for r in case["rows"]]
        results = ef.classify_feedback_eligibility(
            rows, config=_config(max_grades=case["max_grades"])
        )
        by_grade_id = {r.row.grade_id: r for r in results}
        for expected in case["expected"]:
            got = by_grade_id[expected["grade_id"]]
            assert got.status == expected["status"], expected
            assert got.reason == expected["reason"], expected


# --------------------------------------------------------------------------
# classify_feedback_eligibility: exclusion precedence
# --------------------------------------------------------------------------


class TestExclusionPrecedence:
    def test_valid_row_is_eligible(self) -> None:
        results = ef.classify_feedback_eligibility([_row()], config=_config())
        assert results[0].status == "eligible"
        assert results[0].reason is None

    def test_wrong_schema_version_excluded(self) -> None:
        results = ef.classify_feedback_eligibility(
            [_row(schema_version=1)], config=_config()
        )
        assert results[0].status == "excluded"
        assert results[0].reason == "schema_version"

    def test_needs_regrade_excluded(self) -> None:
        results = ef.classify_feedback_eligibility(
            [_row(needs_regrade=True)], config=_config()
        )
        assert results[0].reason == "needs_regrade"

    def test_missing_evaluation_linkage_excluded(self) -> None:
        results = ef.classify_feedback_eligibility(
            [_row(evaluation_id=None, evaluation_post_id=None, evaluation_scan_id=None,
                  evaluation_relevant=None, evaluation_project_key=None,
                  evaluation_dossier_summary_id=None, evaluation_dossier_revision=None,
                  evaluation_posture=None)],
            config=_config(),
        )
        assert results[0].reason == "missing_evaluation_linkage"

    def test_mismatched_post_id_excluded(self) -> None:
        results = ef.classify_feedback_eligibility(
            [_row(evaluation_post_id=999)], config=_config()
        )
        assert results[0].reason == "mismatched_evaluation_identity"

    def test_mismatched_scan_id_excluded(self) -> None:
        results = ef.classify_feedback_eligibility(
            [_row(evaluation_scan_id=999)], config=_config()
        )
        assert results[0].reason == "mismatched_evaluation_identity"

    def test_null_grade_scan_id_skips_scan_identity_check(self) -> None:
        """A grade with no scan_id (e.g. web-sourced) makes no claim about
        scan identity, so a mismatch there must not exclude it."""
        results = ef.classify_feedback_eligibility(
            [_row(scan_id=None, evaluation_scan_id=999)], config=_config()
        )
        assert results[0].status == "eligible"

    def test_shared_contract_invalid_on_project_key_mismatch(self) -> None:
        results = ef.classify_feedback_eligibility(
            [_row(draft_project_key="other-proj")], config=_config()
        )
        assert results[0].reason == "shared_contract_invalid"

    def test_shared_contract_invalid_on_dossier_summary_mismatch(self) -> None:
        results = ef.classify_feedback_eligibility(
            [_row(draft_dossier_summary_id="different-sum")], config=_config()
        )
        assert results[0].reason == "shared_contract_invalid"

    def test_shared_contract_invalid_on_dossier_revision_mismatch(self) -> None:
        results = ef.classify_feedback_eligibility(
            [_row(draft_dossier_revision="different-rev")], config=_config()
        )
        assert results[0].reason == "shared_contract_invalid"

    def test_shared_contract_invalid_on_posture_mismatch(self) -> None:
        results = ef.classify_feedback_eligibility(
            [_row(draft_posture="engage")], config=_config()
        )
        assert results[0].reason == "shared_contract_invalid"

    @pytest.mark.parametrize(
        ("overrides"),
        [
            {"action_judgment": "accept", "dimensions": ("tone",)},
            {"action_judgment": "fail", "dimensions": None, "failure_note": None},
            {"relevance_judgment": "false_positive", "action_judgment": "accept"},
            {
                "action_judgment": "fail",
                "dimensions": ("factual_support",),
                "failure_note": "bad fact",
                "factual_offending_claim": "claim",
                "factual_disposition": "contradicted",
                "factual_contradicting_evidence": None,
            },
            {
                "action_judgment": "fail",
                "dimensions": ("posture",),
                "failure_note": "wrong posture",
                "posture_should_have_been": "answer",
            },
            {"dimensions": "not-json-array"},
        ],
    )
    def test_shared_contract_invalid_uses_causal_grade_schema(
        self, overrides: dict[str, object]
    ) -> None:
        results = ef.classify_feedback_eligibility(
            [_row(**overrides)], config=_config()
        )
        assert results[0].reason == "shared_contract_invalid"

    def test_no_draft_comment_skips_shared_contract_check(self) -> None:
        results = ef.classify_feedback_eligibility(
            [_row(draft_comment_id=None, draft_project_key=None,
                  draft_dossier_summary_id=None, draft_dossier_revision=None,
                  draft_posture=None)],
            config=_config(),
        )
        assert results[0].status == "eligible"

    def test_manual_exclude_excluded(self) -> None:
        results = ef.classify_feedback_eligibility(
            [_row(override_mode="exclude", override_reason="operator flagged")],
            config=_config(),
        )
        assert results[0].reason == "manual_exclude"

    def test_precedence_schema_version_beats_needs_regrade(self) -> None:
        results = ef.classify_feedback_eligibility(
            [_row(schema_version=1, needs_regrade=True)], config=_config()
        )
        assert results[0].reason == "schema_version"

    def test_precedence_needs_regrade_beats_missing_linkage(self) -> None:
        results = ef.classify_feedback_eligibility(
            [_row(needs_regrade=True, evaluation_id=None)], config=_config()
        )
        assert results[0].reason == "needs_regrade"

    def test_precedence_linkage_beats_shared_contract(self) -> None:
        results = ef.classify_feedback_eligibility(
            [_row(evaluation_id=None, draft_project_key="other")], config=_config()
        )
        assert results[0].reason == "missing_evaluation_linkage"

    def test_precedence_shared_contract_beats_manual_exclude(self) -> None:
        results = ef.classify_feedback_eligibility(
            [_row(draft_project_key="other", override_mode="exclude", override_reason="x")],
            config=_config(),
        )
        assert results[0].reason == "shared_contract_invalid"


class TestGradeCap:
    def test_cap_marks_older_valid_rows_eligible_cap(self) -> None:
        # Population is already newest-first (grade_id descending order is
        # simulated by construction order here).
        rows = [_row(grade_id=3), _row(grade_id=2), _row(grade_id=1)]
        results = ef.classify_feedback_eligibility(rows, config=_config(max_grades=2))
        assert [r.status for r in results] == ["eligible", "eligible", "excluded"]
        assert results[2].reason == "eligible_cap"

    def test_excluded_rows_do_not_count_against_cap(self) -> None:
        rows = [
            _row(grade_id=3, schema_version=1),  # excluded, not counted
            _row(grade_id=2),
            _row(grade_id=1),
        ]
        results = ef.classify_feedback_eligibility(rows, config=_config(max_grades=2))
        assert results[0].reason == "schema_version"
        assert results[1].status == "eligible"
        assert results[2].status == "eligible"

    def test_population_eligible_excluded_counts_reconcile(self) -> None:
        rows = [_row(grade_id=i) for i in range(1, 6)]
        results = ef.classify_feedback_eligibility(rows, config=_config(max_grades=3))
        eligible = sum(1 for r in results if r.status == "eligible")
        excluded = sum(1 for r in results if r.status == "excluded")
        assert eligible == 3
        assert excluded == 2
        assert eligible + excluded == len(rows)


# --------------------------------------------------------------------------
# project_phase_evidence: draft-quality population, phase isolation
# --------------------------------------------------------------------------


class TestPhaseProjection:
    def test_relevance_includes_every_globally_eligible_row(self) -> None:
        eligibility = [
            _eligible(_row(grade_id=1)),
            _eligible(_row(grade_id=2, relevance_judgment="false_positive",
                            action_judgment="fail", dimensions=("usefulness",),
                            failure_note="not relevant", evaluation_relevant=0,
                            draft_comment_id=None, draft_project_key=None,
                            draft_dossier_summary_id=None, draft_dossier_revision=None,
                            draft_posture=None)),
        ]
        projections = ef.project_phase_evidence(eligibility)
        assert len(projections["relevance"].population) == 2

    def test_false_positive_excluded_from_draft_quality_population(self) -> None:
        """A false-positive relevance grade carries an action fail in its
        envelope but must never count as draft-quality failure evidence —
        it never represented a human-relevant drafting opportunity."""
        fp_row = _row(
            grade_id=1, relevance_judgment="false_positive", action_judgment="fail",
            dimensions=("usefulness",), failure_note="should not have drafted",
            evaluation_relevant=0, draft_comment_id=None, draft_project_key=None,
            draft_dossier_summary_id=None, draft_dossier_revision=None, draft_posture=None,
        )
        eligibility = [_eligible(fp_row)]
        projections = ef.project_phase_evidence(eligibility)

        assert projections["reply_draft"].population == ()
        assert projections["critic"].population == ()
        assert len(projections["reply_draft"].not_draft_quality) == 1
        assert projections["reply_draft"].not_draft_quality[0].row.grade_id == 1

    def test_draft_quality_requires_evaluation_relevant(self) -> None:
        row = _row(evaluation_relevant=0)
        projections = ef.project_phase_evidence([_eligible(row)])
        assert projections["reply_draft"].population == ()
        assert len(projections["reply_draft"].not_draft_quality) == 1

    def test_draft_quality_requires_draft_comment(self) -> None:
        row = _row(draft_comment_id=None, draft_project_key=None,
                    draft_dossier_summary_id=None, draft_dossier_revision=None,
                    draft_posture=None)
        projections = ef.project_phase_evidence([_eligible(row)])
        assert projections["reply_draft"].population == ()
        assert len(projections["reply_draft"].not_draft_quality) == 1

    def test_reply_draft_and_critic_share_identical_population(self) -> None:
        rows = [_eligible(_row(grade_id=i)) for i in range(1, 4)]
        projections = ef.project_phase_evidence(rows)
        reply_ids = [r.row.grade_id for r in projections["reply_draft"].population]
        critic_ids = [r.row.grade_id for r in projections["critic"].population]
        assert reply_ids == critic_ids == [1, 2, 3]

    def test_globally_excluded_rows_do_not_appear_in_any_projection(self) -> None:
        excluded = ef.EligibilityResult(
            row=_row(grade_id=1), status="excluded", reason="manual_exclude"
        )
        projections = ef.project_phase_evidence([excluded])
        assert projections["relevance"].population == ()
        assert projections["reply_draft"].population == ()
        assert projections["reply_draft"].not_draft_quality == ()


# --------------------------------------------------------------------------
# aggregate_phase_evidence: rates, denominators, segments, dimensions
# --------------------------------------------------------------------------


class TestAggregateRelevance:
    def test_counts_and_totals(self) -> None:
        rows = [
            _eligible(_row(grade_id=1, relevance_judgment="correct")),
            _eligible(_row(grade_id=2, relevance_judgment="false_positive")),
            _eligible(_row(grade_id=3, relevance_judgment="false_negative")),
            _eligible(_row(grade_id=4, relevance_judgment="correct")),
        ]
        projections = ef.project_phase_evidence(rows)
        aggregates = ef.aggregate_phase_evidence(projections, config=_config())
        agg = aggregates["relevance"]
        assert isinstance(agg, ef.RelevanceAggregate)
        assert agg.total_count == 4
        assert agg.correct_count == 2
        assert agg.false_positive_count == 1
        assert agg.false_negative_count == 1

    def test_segment_below_minimum_is_dropped(self) -> None:
        rows = [
            _eligible(_row(grade_id=i, evaluation_project_key="small"))
            for i in range(1, 4)  # only 3 grades, min is 5
        ]
        projections = ef.project_phase_evidence(rows)
        aggregates = ef.aggregate_phase_evidence(projections, config=_config(segment_min_grades=5))
        assert _relevance(aggregates["relevance"]).segments == ()

    def test_segment_at_minimum_is_kept_for_project_and_platform(self) -> None:
        rows = [
            _eligible(_row(grade_id=i, evaluation_project_key="proj", platform="bluesky"))
            for i in range(1, 6)
        ]
        projections = ef.project_phase_evidence(rows)
        aggregates = ef.aggregate_phase_evidence(projections, config=_config(segment_min_grades=5))
        segments = _relevance(aggregates["relevance"]).segments
        segment_types = {s.segment_type for s in segments}
        assert segment_types == {"project", "platform"}
        for segment in segments:
            assert segment.grade_count == 5

    def test_segments_sorted_by_count_desc_then_key_lexical(self) -> None:
        rows = (
            [_eligible(_row(grade_id=i, evaluation_project_key="big", platform="p"))
             for i in range(1, 6)]
            + [_eligible(_row(grade_id=i, evaluation_project_key="small", platform="p"))
               for i in range(6, 11)]
        )
        projections = ef.project_phase_evidence(rows)
        aggregates = ef.aggregate_phase_evidence(projections, config=_config(segment_min_grades=5))
        project_segments = [
            s for s in _relevance(aggregates["relevance"]).segments
            if s.segment_type == "project"
        ]
        assert [s.key for s in project_segments] == ["big", "small"]


class TestAggregateDraftQuality:
    def test_accept_fail_counts(self) -> None:
        rows = [
            _eligible(_row(grade_id=1, action_judgment="accept")),
            _eligible(_row(grade_id=2, action_judgment="fail", dimensions=("tone",),
                            failure_note="n")),
        ]
        projections = ef.project_phase_evidence(rows)
        aggregates = ef.aggregate_phase_evidence(projections, config=_config())
        agg = aggregates["reply_draft"]
        assert isinstance(agg, ef.DraftQualityAggregate)
        assert agg.total_count == 2
        assert agg.accept_count == 1
        assert agg.fail_count == 1

    def test_dimension_counts_ranked_desc_then_key(self) -> None:
        rows = [
            _eligible(_row(grade_id=1, action_judgment="fail",
                            dimensions=("tone", "usefulness"), failure_note="n1")),
            _eligible(_row(grade_id=2, action_judgment="fail",
                            dimensions=("tone",), failure_note="n2")),
            _eligible(_row(grade_id=3, action_judgment="fail",
                            dimensions=("posture",), failure_note="n3")),
        ]
        projections = ef.project_phase_evidence(rows)
        aggregates = ef.aggregate_phase_evidence(projections, config=_config())
        dims = _draft(aggregates["reply_draft"]).dimension_counts
        assert [(d.dimension, d.count) for d in dims] == [
            ("tone", 2), ("posture", 1), ("usefulness", 1),
        ]

    def test_posture_and_factual_correction_counts(self) -> None:
        rows = [
            _eligible(_row(grade_id=1, action_judgment="fail", dimensions=("posture",),
                            failure_note="n", posture_should_have_been="ask")),
            _eligible(_row(grade_id=2, action_judgment="fail", dimensions=("factual_support",),
                            failure_note="n", factual_disposition="unsupported")),
            _eligible(_row(grade_id=3, action_judgment="fail", dimensions=("factual_support",),
                            failure_note="n", factual_disposition="contradicted")),
        ]
        projections = ef.project_phase_evidence(rows)
        aggregates = ef.aggregate_phase_evidence(projections, config=_config())
        agg = _draft(aggregates["reply_draft"])
        assert agg.posture_correction_count == 1
        assert agg.factual_unsupported_count == 1
        assert agg.factual_contradicted_count == 1


# --------------------------------------------------------------------------
# select_phase_examples: ranking, trimming, critic dedup
# --------------------------------------------------------------------------


class TestSelectExamples:
    def test_relevance_examples_exclude_correct_judgments(self) -> None:
        rows = [
            _eligible(_row(grade_id=1, relevance_judgment="correct")),
            _eligible(_row(grade_id=2, relevance_judgment="false_positive",
                            failure_note="missed it", action_judgment="fail",
                            dimensions=("usefulness",))),
        ]
        projections = ef.project_phase_evidence(rows)
        examples = ef.select_phase_examples(projections, config=_config())
        assert [e.grade_id for e in examples["relevance"]] == [2]

    def test_examples_respect_configured_limit(self) -> None:
        rows = [
            _eligible(_row(grade_id=i, relevance_judgment="false_positive",
                            action_judgment="fail", dimensions=("usefulness",),
                            failure_note=f"note{i}"))
            for i in range(1, 6)
        ]
        projections = ef.project_phase_evidence(rows)
        examples = ef.select_phase_examples(projections, config=_config(relevance_example_limit=2))
        assert len(examples["relevance"]) == 2

    def test_examples_are_newest_first(self) -> None:
        # Population is already ordered newest-first by construction.
        rows = [
            _eligible(_row(grade_id=3, relevance_judgment="false_positive",
                            action_judgment="fail", dimensions=("usefulness",),
                            failure_note="newest")),
            _eligible(_row(grade_id=2, relevance_judgment="false_positive",
                            action_judgment="fail", dimensions=("usefulness",),
                            failure_note="middle")),
            _eligible(_row(grade_id=1, relevance_judgment="false_positive",
                            action_judgment="fail", dimensions=("usefulness",),
                            failure_note="oldest")),
        ]
        projections = ef.project_phase_evidence(rows)
        examples = ef.select_phase_examples(projections, config=_config(relevance_example_limit=2))
        assert [e.grade_id for e in examples["relevance"]] == [3, 2]

    def test_note_trimmed_to_max_chars(self) -> None:
        long_note = "x" * 500
        rows = [_eligible(_row(grade_id=1, relevance_judgment="false_positive",
                                action_judgment="fail", dimensions=("usefulness",),
                                failure_note=long_note))]
        projections = ef.project_phase_evidence(rows)
        examples = ef.select_phase_examples(projections, config=_config(note_max_chars=50))
        assert examples["relevance"][0].note == "x" * 50

    def test_reply_draft_examples_are_failures_only(self) -> None:
        rows = [
            _eligible(_row(grade_id=1, action_judgment="accept")),
            _eligible(_row(grade_id=2, action_judgment="fail", dimensions=("tone",),
                            failure_note="too blunt")),
        ]
        projections = ef.project_phase_evidence(rows)
        examples = ef.select_phase_examples(projections, config=_config())
        assert [e.grade_id for e in examples["reply_draft"]] == [2]

    def test_critic_examples_require_distinct_primary_dimensions(self) -> None:
        rows = [
            _eligible(_row(grade_id=3, action_judgment="fail", dimensions=("tone",),
                            failure_note="n3")),
            _eligible(_row(grade_id=2, action_judgment="fail", dimensions=("tone",),
                            failure_note="n2")),
            _eligible(_row(grade_id=1, action_judgment="fail", dimensions=("posture",),
                            failure_note="n1")),
        ]
        projections = ef.project_phase_evidence(rows)
        examples = ef.select_phase_examples(projections, config=_config(critic_example_limit=3))
        assert [e.grade_id for e in examples["critic"]] == [3, 1]

    def test_example_notes_are_trimmed_before_character_cap(self) -> None:
        rows = [
            _eligible(
                _row(
                    grade_id=1,
                    action_judgment="fail",
                    dimensions=("tone",),
                    failure_note="  operator note with padding  ",
                )
            )
        ]
        projections = ef.project_phase_evidence(rows)
        examples = ef.select_phase_examples(
            projections, config=_config(note_max_chars=13)
        )
        assert examples["critic"][0].note == "operator note"

    def test_zero_example_limit_selects_nothing(self) -> None:
        rows = [_eligible(_row(grade_id=1, relevance_judgment="false_positive",
                                action_judgment="fail", dimensions=("usefulness",),
                                failure_note="n"))]
        projections = ef.project_phase_evidence(rows)
        examples = ef.select_phase_examples(projections, config=_config(relevance_example_limit=0))
        assert examples["relevance"] == ()


# --------------------------------------------------------------------------
# render_feedback_sections: determinism, empty corpus, truncation
# --------------------------------------------------------------------------


class TestRenderFeedbackSections:
    def _sections_for(
        self, rows: list[ef.GradePopulationRow], config: ef.FeedbackPolicyConfig
    ) -> dict[ef.PhaseName, ef.RenderedFeedbackSection]:
        eligibility = [_eligible(r) for r in rows]
        projections = ef.project_phase_evidence(eligibility)
        aggregates = ef.aggregate_phase_evidence(projections, config=config)
        examples = ef.select_phase_examples(projections, config=config)
        return ef.render_feedback_sections(aggregates, examples, config=config)

    def test_empty_corpus_renders_empty_text_and_hash(self) -> None:
        sections = self._sections_for([], _config())
        for phase in ef.PHASE_NAMES:
            section = sections[phase]
            assert section.rendered_text == ""
            assert section.rendered_sha256 == _EMPTY_SHA256
            assert section.token_estimate == 0
            assert not section.truncated
            summary = json.loads(section.structured_summary)
            assert summary["reason"] == "no_eligible_feedback"

    def test_rendering_is_deterministic(self) -> None:
        rows = [_row(grade_id=1), _row(grade_id=2, action_judgment="fail",
                                        dimensions=("tone",), failure_note="n")]
        sections_a = self._sections_for(rows, _config())
        sections_b = self._sections_for(rows, _config())
        for phase in ef.PHASE_NAMES:
            assert sections_a[phase].structured_summary == sections_b[phase].structured_summary
            assert sections_a[phase].rendered_text == sections_b[phase].rendered_text
            assert sections_a[phase].rendered_sha256 == sections_b[phase].rendered_sha256

    def test_structured_summary_is_canonical_json(self) -> None:
        rows = [_row(grade_id=1)]
        sections = self._sections_for(rows, _config())
        raw = sections["relevance"].structured_summary
        assert "\n" not in raw
        assert ", " not in raw
        assert ": " not in raw
        reparsed = json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"))
        assert raw == reparsed

    def test_token_estimate_matches_ceil_utf8_over_4(self) -> None:
        rows = [_row(grade_id=1)]
        sections = self._sections_for(rows, _config())
        section = sections["relevance"]
        import math
        expected = math.ceil(len(section.rendered_text.encode("utf-8")) / 4)
        assert section.token_estimate == expected

    def test_hash_is_sha256_of_rendered_text_bytes(self) -> None:
        rows = [_row(grade_id=1)]
        sections = self._sections_for(rows, _config())
        section = sections["relevance"]
        assert section.rendered_sha256 == hashlib.sha256(
            section.rendered_text.encode("utf-8")
        ).hexdigest()

    def test_critic_emphasizes_failure_evidence_before_outcome_rate(self) -> None:
        rows = [
            _row(
                grade_id=1,
                action_judgment="fail",
                dimensions=("tone",),
                failure_note="too abrupt",
            ),
            _row(grade_id=2),
        ]
        text = self._sections_for(rows, _config())["critic"].rendered_text
        assert text.index("Failure dimensions:") < text.index("Recent failure notes:")
        assert text.index("Recent failure notes:") < text.index("Outcome context:")

    def test_truncation_drops_segments_before_examples_before_dimensions(self) -> None:
        # Build a draft-quality population with many dimensions and
        # failure notes, then squeeze the token budget down until
        # truncation must occur.
        rows = [
            _row(grade_id=i, action_judgment="fail",
                 dimensions=(f"dim{i}",), failure_note=f"note number {i} " * 5)
            for i in range(1, 8)
        ]
        tiny_config = _config(reply_draft_token_budget=20, critic_example_limit=3)
        sections = self._sections_for(rows, tiny_config)
        section = sections["reply_draft"]
        assert section.truncated
        summary = json.loads(section.structured_summary)
        # Mandatory totals always survive truncation.
        assert summary["totals"]["total_count"] == 7
        assert summary["totals"]["fail_count"] == 7

    def test_truncation_never_drops_mandatory_totals_even_far_over_budget(self) -> None:
        rows = [_row(grade_id=i, evaluation_project_key="proj", platform="bluesky")
                for i in range(1, 6)]
        sections = self._sections_for(rows, _config(relevance_token_budget=1))
        summary = json.loads(sections["relevance"].structured_summary)
        assert summary["totals"]["total_count"] == 5

    def test_segment_truncation_drops_global_minimum_not_last_in_type_group(self) -> None:
        """Regression: segments are display-sorted by (segment_type,
        -grade_count, key), which groups platform segments before project
        segments. A platform segment with the smallest count must still be
        dropped before a larger-count project segment, even though it
        isn't last in the type-grouped list."""
        platform_segment = ef.SegmentStat(
            segment_type="platform", key="bluesky", grade_count=5,
            correct_count=5, false_positive_count=0, false_negative_count=0,
        )
        big_project_segment = ef.SegmentStat(
            segment_type="project", key="big-proj", grade_count=100,
            correct_count=100, false_positive_count=0, false_negative_count=0,
        )
        small_project_segment = ef.SegmentStat(
            segment_type="project", key="small-proj", grade_count=6,
            correct_count=6, false_positive_count=0, false_negative_count=0,
        )
        # Display order (as produced by _aggregate_relevance's sort): all
        # platform segments first, then project segments by count desc.
        segments = (platform_segment, big_project_segment, small_project_segment)

        remaining, dropped = ef._pop_lowest_segment(segments)

        assert dropped == platform_segment
        assert set(remaining) == {big_project_segment, small_project_segment}


# --------------------------------------------------------------------------
# load_grade_population + persist_feedback_snapshot: DB-backed integration
# --------------------------------------------------------------------------


def _seed_grade(
    state: StateManager,
    *,
    scan_id: int,
    platform_id: str,
    relevance_judgment: str = "correct",
    action_judgment: str | None = "accept",
    dimensions: list[str] | None = None,
    failure_note: str | None = None,
    graded_at: datetime | None = None,
    with_draft: bool = True,
    project_key: str = "proj",
) -> tuple[int, int]:
    msg = Message(
        platform="bluesky", platform_id=platform_id, channel_name="",
        channel_id="", author_name="a", author_id="u",
        content="hello", created_at=datetime.now(UTC),
    )
    post_id = state.save_post(msg, scan_id)
    result = RelevanceResult(
        message=msg, relevant=(relevance_judgment != "false_positive"),
        score=0.9, reason="r", relevant_to=(project_key,),
    )
    eval_id = state.save_evaluation(
        result, post_id, scan_id, project_key=project_key, posture="answer",
        surface_status="surfaced",
    )
    if with_draft:
        state.save_draft(post_id, eval_id, project_key, "draft text", scan_id, posture="answer")
    grade_id = state.save_grade(GradeRecord(
        post_id=post_id, evaluation_id=eval_id, scan_id=scan_id, source="cli",
        graded_at=graded_at or datetime.now(UTC),
        relevance_judgment=relevance_judgment, action_judgment=action_judgment,
        dimensions=dimensions, failure_note=failure_note, schema_version=3,
    ))
    return post_id, grade_id


class TestLoadGradePopulationIntegration:
    def test_lookback_boundary_is_inclusive_and_excludes_older_rows(self) -> None:
        state = StateManager(db_path=":memory:")
        scan_id = state.start_scan()
        as_of = datetime(2026, 6, 1, tzinfo=UTC)
        _in_window_post, in_window_id = _seed_grade(
            state, scan_id=scan_id, platform_id="in-window",
            graded_at=datetime(2026, 3, 3, tzinfo=UTC),
        )
        _seed_grade(
            state, scan_id=scan_id, platform_id="too-old",
            graded_at=datetime(2026, 3, 2, 23, 59, 59, 999000, tzinfo=UTC),
        )
        config = _config(lookback_days=90)
        with state.db.begin_immediate():
            population = ef.load_grade_population(state.conn, as_of=as_of, config=config)
        assert [row.grade_id for row in population] == [in_window_id]
        state.commit()
        state.close()

    def test_ordering_is_graded_at_desc_then_grade_id_desc(self) -> None:
        state = StateManager(db_path=":memory:")
        scan_id = state.start_scan()
        same_instant = datetime(2026, 3, 1, tzinfo=UTC)
        _seed_grade(state, scan_id=scan_id, platform_id="a", graded_at=same_instant)
        _seed_grade(state, scan_id=scan_id, platform_id="b", graded_at=same_instant)
        _seed_grade(
            state, scan_id=scan_id, platform_id="c",
            graded_at=datetime(2026, 3, 5, tzinfo=UTC),
        )
        config = _config(lookback_days=200)
        with state.db.begin_immediate():
            population = ef.load_grade_population(
                state.conn, as_of=datetime(2026, 6, 1, tzinfo=UTC), config=config
            )
        # Newest graded_at first ("c"), then id-descending among ties.
        assert len(population) == 3
        assert population[0].graded_at.startswith("2026-03-05")
        assert population[1].grade_id > population[2].grade_id
        state.commit()
        state.close()

    def test_scan_recency_does_not_gate_selection(self) -> None:
        """The legacy bug: a human grade entered today on an evaluation
        from an old scan must still be selected by graded_at recency."""
        state = StateManager(db_path=":memory:")
        old_scan_id = state.start_scan()
        state.complete_scan(old_scan_id, 1, 1, status="complete")
        for _ in range(5):
            newer_scan_id = state.start_scan()
            state.complete_scan(newer_scan_id, 1, 1, status="complete")

        _post_id, grade_id = _seed_grade(
            state, scan_id=old_scan_id, platform_id="old-scan-recent-grade",
            graded_at=datetime.now(UTC),
        )
        config = _config()
        with state.db.begin_immediate():
            population = ef.load_grade_population(
                state.conn, as_of=datetime.now(UTC), config=config
            )
        assert grade_id in [row.grade_id for row in population]
        state.commit()
        state.close()


class TestPersistFeedbackSnapshotIntegration:
    def _run_pipeline(
        self,
        state: StateManager,
        scan_id: int,
        config: ef.FeedbackPolicyConfig,
        *,
        mode: ef.FeedbackMode = "shadow",
    ) -> ef.PersistedFeedbackSnapshot:
        as_of = datetime.now(UTC)
        population = ef.load_grade_population(state.conn, as_of=as_of, config=config)
        eligibility = ef.classify_feedback_eligibility(population, config=config)
        projections = ef.project_phase_evidence(eligibility)
        aggregates = ef.aggregate_phase_evidence(projections, config=config)
        examples = ef.select_phase_examples(projections, config=config)
        sections = ef.render_feedback_sections(aggregates, examples, config=config)
        return ef.persist_feedback_snapshot(
            state.conn, scan_id=scan_id, mode=mode, as_of=as_of, config=config,
            population=population, eligibility=eligibility, projections=projections,
            examples=examples, sections=sections, recorded_at=datetime.now(UTC),
        )

    def test_global_exclusion_recorded_in_all_three_phases(self) -> None:
        state = StateManager(db_path=":memory:")
        scan_id = state.start_scan()
        _post_id, grade_id = _seed_grade(
            state, scan_id=scan_id, platform_id="excluded",
            relevance_judgment="correct",
        )
        state.save_grade_usage_override(grade_id, mode="exclude", reason="op flag")

        with state.db.begin_immediate():
            snapshot = self._run_pipeline(state, scan_id, _config())

        rows = state.conn.execute(
            "SELECT fsp.phase, fsi.role, fsi.reason, fsi.selection_reason, fsi.rank "
            "FROM feedback_snapshot_items fsi "
            "JOIN feedback_snapshot_phases fsp ON fsp.id = fsi.snapshot_phase_id "
            "WHERE fsi.grade_id = ? ORDER BY fsp.phase",
            (grade_id,),
        ).fetchall()
        assert len(rows) == 3
        for row in rows:
            assert row["role"] == "excluded"
            assert row["reason"] == "manual_exclude"
            assert row["selection_reason"] == "manual_exclude"
            assert row["rank"] is None
        assert snapshot.eligible_count == 0
        state.commit()
        state.close()

    def test_not_draft_quality_reason_only_in_draft_phases(self) -> None:
        state = StateManager(db_path=":memory:")
        scan_id = state.start_scan()
        _post_id, grade_id = _seed_grade(
            state, scan_id=scan_id, platform_id="fp",
            relevance_judgment="false_positive", action_judgment="fail",
            dimensions=["usefulness"], failure_note="not relevant",
            with_draft=False,
        )

        with state.db.begin_immediate():
            self._run_pipeline(state, scan_id, _config())

        rows = state.conn.execute(
            "SELECT fsp.phase, fsi.role, fsi.reason FROM feedback_snapshot_items fsi "
            "JOIN feedback_snapshot_phases fsp ON fsp.id = fsi.snapshot_phase_id "
            "WHERE fsi.grade_id = ?",
            (grade_id,),
        ).fetchall()
        roles_by_phase: dict[str, set[str]] = {}
        reasons_by_phase: dict[str, set[str | None]] = {}
        for row in rows:
            roles_by_phase.setdefault(row["phase"], set()).add(row["role"])
            reasons_by_phase.setdefault(row["phase"], set()).add(row["reason"])

        # The relevance failure note also qualifies as a relevance example,
        # so relevance carries both an aggregate and an example row.
        assert roles_by_phase["relevance"] == {"aggregate", "example"}
        assert roles_by_phase["reply_draft"] == {"excluded"}
        assert reasons_by_phase["reply_draft"] == {"not_draft_quality_population"}
        assert roles_by_phase["critic"] == {"excluded"}
        assert reasons_by_phase["critic"] == {"not_draft_quality_population"}
        state.commit()
        state.close()

    def test_example_role_added_alongside_aggregate_role(self) -> None:
        state = StateManager(db_path=":memory:")
        scan_id = state.start_scan()
        _post_id, grade_id = _seed_grade(
            state, scan_id=scan_id, platform_id="failed-draft",
            action_judgment="fail", dimensions=["tone"], failure_note="too blunt",
        )
        with state.db.begin_immediate():
            self._run_pipeline(state, scan_id, _config())

        rows = state.conn.execute(
            "SELECT fsp.phase, fsi.role, fsi.selection_reason, fsi.rank "
            "FROM feedback_snapshot_items fsi "
            "JOIN feedback_snapshot_phases fsp ON fsp.id = fsi.snapshot_phase_id "
            "WHERE fsi.grade_id = ? AND fsp.phase = 'reply_draft' ORDER BY fsi.role",
            (grade_id,),
        ).fetchall()
        assert [row["role"] for row in rows] == ["aggregate", "example"]
        by_role = {row["role"]: row for row in rows}
        assert by_role["aggregate"]["selection_reason"] == "phase_population"
        assert by_role["aggregate"]["rank"] is None
        assert by_role["example"]["selection_reason"] == "selected_recent_note"
        assert by_role["example"]["rank"] == 1
        state.commit()
        state.close()

    def test_pinned_revision_id_matches_latest_grade_revision(self) -> None:
        state = StateManager(db_path=":memory:")
        scan_id = state.start_scan()
        _post_id, grade_id = _seed_grade(state, scan_id=scan_id, platform_id="p1")

        with state.db.begin_immediate():
            self._run_pipeline(state, scan_id, _config())

        expected_revision_id = state.conn.execute(
            "SELECT id FROM grade_revisions WHERE grade_id = ? "
            "ORDER BY revision DESC LIMIT 1",
            (grade_id,),
        ).fetchone()["id"]
        item_revision_id = state.conn.execute(
            "SELECT grade_revision_id FROM feedback_snapshot_items WHERE grade_id = ? LIMIT 1",
            (grade_id,),
        ).fetchone()["grade_revision_id"]
        assert item_revision_id == expected_revision_id
        state.commit()
        state.close()

    def test_snapshot_header_stores_resolved_policy_parameters(self) -> None:
        state = StateManager(db_path=":memory:")
        scan_id = state.start_scan()
        _seed_grade(state, scan_id=scan_id, platform_id="p1")
        config = _config(lookback_days=45, max_grades=10)

        with state.db.begin_immediate():
            snapshot = self._run_pipeline(state, scan_id, config)

        row = state.conn.execute(
            "SELECT * FROM feedback_snapshots WHERE id = ?", (snapshot.snapshot_id,)
        ).fetchone()
        assert row["lookback_days"] == 45
        assert row["max_grades"] == 10
        assert row["mode"] == "shadow"
        assert row["policy_version"] == "evaluation-feedback/v1"
        assert snapshot.mode == "shadow"
        state.commit()
        state.close()

    def test_snapshot_header_stores_active_mode_when_resolved_active(self) -> None:
        """mode is whatever the caller resolved and passed in — active mode
        must persist just as faithfully as the shadow default."""
        state = StateManager(db_path=":memory:")
        scan_id = state.start_scan()
        _seed_grade(state, scan_id=scan_id, platform_id="p1")

        with state.db.begin_immediate():
            snapshot = self._run_pipeline(state, scan_id, _config(), mode="active")

        row = state.conn.execute(
            "SELECT mode FROM feedback_snapshots WHERE id = ?", (snapshot.snapshot_id,)
        ).fetchone()
        assert row["mode"] == "active"
        assert snapshot.mode == "active"
        state.commit()
        state.close()


class TestLoadGradePopulationValidation:
    def test_naive_as_of_rejected(self) -> None:
        state = StateManager(db_path=":memory:")
        with state.db.begin_immediate(), pytest.raises(ValueError, match="timezone-aware"):
            ef.load_grade_population(
                state.conn, as_of=datetime(2026, 1, 1), config=_config()
            )
        state.commit()
        state.close()


class TestLoadCommittedPhaseBundle:
    """load_committed_phase_bundle reads the just-committed
    feedback_snapshot_phases rows back plainly and must never fall back to
    legacy feedback on any integrity failure."""

    def _committed_snapshot(
        self, state: StateManager, *, mode: str = "active"
    ) -> tuple[int, ef.PersistedFeedbackSnapshot]:
        scan_id = state.start_scan()
        _seed_grade(state, scan_id=scan_id, platform_id="p1")
        with state.db.begin_immediate():
            snapshot = TestPersistFeedbackSnapshotIntegration()._run_pipeline(
                state, scan_id, _config(), mode=mode  # type: ignore[arg-type]
            )
        state.commit()
        return scan_id, snapshot

    def test_happy_path_loads_stored_rendered_text_per_phase(self) -> None:
        state = StateManager(db_path=":memory:")
        _scan_id, snapshot = self._committed_snapshot(state)

        bundle = ef.load_committed_phase_bundle(
            state.conn, snapshot_id=snapshot.snapshot_id, expected_mode="active"
        )

        for phase_name, entry in (
            ("relevance", bundle.relevance),
            ("reply_draft", bundle.reply_draft),
            ("critic", bundle.critic),
        ):
            persisted_phase = next(p for p in snapshot.phases if p.phase == phase_name)
            row = state.conn.execute(
                "SELECT rendered_text FROM feedback_snapshot_phases WHERE id = ?",
                (persisted_phase.snapshot_phase_id,),
            ).fetchone()
            assert isinstance(entry, ef.SnapshotBody)
            assert entry.text == row["rendered_text"]
        state.close()

    def test_missing_snapshot_id_raises_integrity_error(self) -> None:
        state = StateManager(db_path=":memory:")
        with pytest.raises(ef.FeedbackBundleIntegrityError, match="not found"):
            ef.load_committed_phase_bundle(
                state.conn, snapshot_id=999999, expected_mode="active"
            )
        state.close()

    def test_mode_mismatch_raises_integrity_error(self) -> None:
        state = StateManager(db_path=":memory:")
        _scan_id, snapshot = self._committed_snapshot(state, mode="shadow")

        with pytest.raises(ef.FeedbackBundleIntegrityError, match="mode"):
            ef.load_committed_phase_bundle(
                state.conn, snapshot_id=snapshot.snapshot_id, expected_mode="active"
            )
        state.close()

    def _bare_snapshot_header(self, state: StateManager, *, scan_id: int) -> int:
        """Insert a feedback_snapshots header row directly, bypassing
        persist_feedback_snapshot, so its phase rows can be controlled by
        hand (feedback_snapshot_phases rejects UPDATE/DELETE once written
        via the normal pipeline, so corruption scenarios must be built
        from scratch rather than mutated after the fact)."""
        cursor = state.conn.execute(
            "INSERT INTO feedback_snapshots ("
            "  scan_id, policy_version, mode, as_of, lookback_days, max_grades, "
            "  segment_min_grades, note_max_chars, relevance_token_budget, "
            "  reply_draft_token_budget, critic_token_budget, "
            "  population_count, eligible_count, excluded_count, created_at"
            ") VALUES (?, 'evaluation-feedback/v1', 'active', '2026-01-01T00:00:00.000Z', "
            "  90, 200, 5, 240, 800, 800, 1000, 0, 0, 0, '2026-01-01T00:00:00.000Z')",
            (scan_id,),
        )
        snapshot_id = cursor.lastrowid
        assert snapshot_id is not None
        return snapshot_id

    def test_missing_phase_row_raises_integrity_error(self) -> None:
        state = StateManager(db_path=":memory:")
        scan_id = state.start_scan()
        with state.db.begin_immediate():
            snapshot_id = self._bare_snapshot_header(state, scan_id=scan_id)
        state.commit()

        with pytest.raises(ef.FeedbackBundleIntegrityError, match="missing phase"):
            ef.load_committed_phase_bundle(
                state.conn, snapshot_id=snapshot_id, expected_mode="active"
            )
        state.close()

    def test_hash_mismatch_raises_integrity_error(self) -> None:
        state = StateManager(db_path=":memory:")
        scan_id = state.start_scan()
        with state.db.begin_immediate():
            snapshot_id = self._bare_snapshot_header(state, scan_id=scan_id)
            now = "2026-01-01T00:00:00.000Z"
            for phase in ef.PHASE_NAMES:
                rendered_text = f"{phase} body"
                # Deliberately wrong hash — a fresh sha256 of rendered_text
                # would never equal this literal, so any real corruption of
                # the stored bytes (or of the recorded hash itself) is caught.
                state.conn.execute(
                    "INSERT INTO feedback_snapshot_phases ("
                    "  snapshot_id, phase, token_budget, token_estimate, truncated, "
                    "  structured_summary, rendered_text, rendered_sha256, created_at"
                    ") VALUES (?, ?, 800, 10, 0, '{}', ?, 'not-the-real-hash', ?)",
                    (snapshot_id, phase, rendered_text, now),
                )
        state.commit()

        with pytest.raises(ef.FeedbackBundleIntegrityError, match="hash"):
            ef.load_committed_phase_bundle(
                state.conn, snapshot_id=snapshot_id, expected_mode="active"
            )
        state.close()
