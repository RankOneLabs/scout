// Feedback Overview (milestone 3b) — server-computed corpus/windowed
// analytics. Dedicated to this cohort; reuses evaluation-feedback/v1's
// persisted snapshot shape (types/feedback.ts) and the shared eligibility
// vocabulary (types/feedback-grades.ts) where the domain overlaps.

import type { FeedbackSnapshotMode } from "@/types/feedback";
import type { EligibilityReason } from "@/types/feedback-grades";

export interface FeedbackSummaryQuery {
  from?: string;
  to?: string;
}

export interface FeedbackSummaryPolicy {
  policy_version: string;
  lookback_days: number;
  max_grades: number;
}

export interface CorpusCard {
  count: number;
  // Every card shares the same denominator: COUNT(*) FROM grades — the
  // whole corpus, all time, regardless of window. Categories can and do
  // overlap (a legacy grade can also need a regrade); see decisions.
  denominator: number;
}

export interface FeedbackCorpusCards {
  total: CorpusCard;
  current: CorpusCard;
  legacy: CorpusCard;
  needs_regrade: CorpusCard;
}

export interface CoverageBucket {
  scan_id: number;
  day: string; // UTC day (YYYY-MM-DD) of the evaluation's scan
  linked: number;
  total: number;
}

export interface RelevanceMetrics {
  correct: number;
  false_positive: number;
  false_negative: number;
  // Contract-valid grades whose underlying evaluation predicted irrelevant.
  // Recall is not displayed until this population is non-zero.
  reviewed_model_negative: number;
  // relevance_judgment = 'correct' AND evaluation.relevant = 1 — the
  // precision/recall numerator (see decisions: "correct judgments on
  // model-relevant evaluations").
  correct_relevant: number;
  precision_denominator: number; // correct_relevant + false_positive
  recall_denominator: number; // correct_relevant + false_negative
}

export interface ResponseAcceptance {
  accept: number;
  fail: number;
  denominator: number; // accept + fail
  not_applicable: number;
}

export interface FailureDimensionBucket {
  day: string; // UTC day (YYYY-MM-DD)
  dimension: string;
  count: number;
  failed_draft_denominator: number;
}

export interface SegmentEntry {
  label: string;
  count: number;
  correct: number;
  false_positive: number;
  false_negative: number;
}

export interface SegmentSummary {
  entries: SegmentEntry[];
  other: SegmentEntry | null;
  denominator: number;
}

export interface FeedbackSegments {
  project: SegmentSummary;
  platform: SegmentSummary;
  posture: SegmentSummary;
  terminal_status: SegmentSummary;
}

export interface EligibilityReasonCount {
  reason: EligibilityReason;
  count: number;
}

export interface EligibilitySummary {
  in_lookback_population: number;
  eligible_after_cap: number;
  outside_lookback_count: number;
  by_reason: EligibilityReasonCount[];
  resolved_lookback_days: number;
  resolved_max_grades: number;
}

export interface LatestSnapshotSummary {
  snapshot_id: number;
  scan_id: number;
  policy_version: string;
  mode: FeedbackSnapshotMode;
  created_at: string;
  population_count: number;
  eligible_count: number;
  excluded_count: number;
  // Unique aggregate-role grade ids across all three phases when
  // mode === 'active'; 0 for a shadow snapshot (evidence stored, never
  // injected into a live prompt).
  used_grade_count: number;
}

export interface FeedbackSummaryResponse {
  as_of: string;
  from: string;
  to: string;
  timezone: "UTC";
  // Null only when no evaluation-feedback/v1 snapshot has ever been
  // recorded, so no policy is resolvable yet.
  policy: FeedbackSummaryPolicy | null;
  corpus: FeedbackCorpusCards;
  coverage: CoverageBucket[];
  relevance: RelevanceMetrics;
  response_acceptance: ResponseAcceptance;
  failure_dimension_trends: FailureDimensionBucket[];
  segments: FeedbackSegments;
  eligibility: EligibilitySummary;
  latest_snapshot: LatestSnapshotSummary | null;
}
