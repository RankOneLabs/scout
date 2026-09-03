// Feedback Overview / Grade Explorer (milestone 3b) — grade_id-addressed
// domain, dedicated to this cohort. Does not import or extend cohort 7
// experiment modules; does reuse evaluation-feedback/v1's persisted
// snapshot shapes from types/feedback.ts where the domain overlaps
// (FeedbackPhase, FeedbackSnapshotMode).

import type { FeedbackPhase, FeedbackSnapshotMode } from "@/types/feedback";
import type {
  ActionJudgment,
  FailureDimension,
  Grade,
  RelevanceJudgment,
} from "@/types/schema";

// The six global exclusion checks evaluation-feedback/v1 applies to every
// in-window grade (its `not_draft_quality_population` reason is phase-scoped
// and does not apply to this cohort's global, phase-agnostic eligibility).
export type GlobalExclusionReason =
  | "schema_version"
  | "needs_regrade"
  | "missing_evaluation_linkage"
  | "mismatched_evaluation_identity"
  | "shared_contract_invalid"
  | "manual_exclude";

// Present-time eligibility as evaluation-feedback/v1's classifier would
// resolve it right now: `outside_lookback` for grades older than the
// resolved lookback (the classifier never sees them at all), the exact
// classifier reason for in-window grades, or `eligible`/`eligible_cap`.
export type EligibilityReason =
  | "eligible"
  | "eligible_cap"
  | "outside_lookback"
  | GlobalExclusionReason;

export type OverrideMode = "auto" | "exclude";

export type SnapshotUseFilter = "never" | "historical" | "active";

export type TraceAvailabilityFilter = "available" | "unavailable";

export interface CursorPage<T> {
  data: T[];
  has_more: boolean;
  next_cursor: string | null;
}

// --- Grade Explorer list -----------------------------------------------

export interface GradeExplorerRow {
  grade_id: number;
  evaluation_id: number | null;
  post_id: number;
  scan_id: number | null;
  project_key: string | null;
  platform: string | null;
  posture: string | null;
  terminal_status: string | null;
  relevance_judgment: RelevanceJudgment;
  action_judgment: ActionJudgment | null;
  dimensions: FailureDimension[] | null;
  schema_version: number;
  needs_regrade: boolean;
  // Earliest graded_at recorded across this grade's revision envelopes —
  // distinct from last_edit_time, which is the newest revision's insert
  // time. See feedback-grade-queries.ts for why these are computed from
  // grade_revisions rather than the mutable grades row.
  grade_time: string;
  last_edit_time: string;
  revision_count: number;
  // Null only when no evaluation-feedback/v1 snapshot has ever been
  // recorded, so no lookback/cap policy is resolvable yet.
  eligibility_reason: EligibilityReason | null;
  override_mode: OverrideMode;
  override_reason: string | null;
  snapshot_use_first_at: string | null;
  snapshot_use_last_at: string | null;
  snapshot_use_count: number;
  snapshot_active_use: boolean;
  trace_available: boolean;
  trace_unavailable_reason: string | null;
}

export interface GradeListCursor {
  last_edit_time: string;
  grade_id: number;
}

export interface GradeListFilters {
  gradedFrom?: string;
  gradedTo?: string;
  editedFrom?: string;
  editedTo?: string;
  schemaVersion?: 1 | 2;
  needsRegrade?: boolean;
  project?: string;
  platform?: string;
  posture?: string;
  terminalStatus?: string;
  relevanceJudgment?: RelevanceJudgment;
  actionJudgment?: ActionJudgment;
  failureDimension?: FailureDimension;
  eligibilityReason?: EligibilityReason;
  overrideMode?: OverrideMode;
  snapshotUse?: SnapshotUseFilter;
  traceAvailability?: TraceAvailabilityFilter;
  scanId?: number;
  evaluationId?: number;
  limit?: number;
  cursor?: GradeListCursor;
}

export interface GradeListResponse {
  data: GradeExplorerRow[];
  has_more: boolean;
  next_cursor: string | null;
  total_matching: number;
}

// --- Grade detail --------------------------------------------------------

export interface GradeDetailPostEvidence {
  id: number;
  platform: string;
  author_name: string | null;
  author_id: string | null;
  content: string | null;
  url: string | null;
  created_at: string | null;
}

export interface GradeDetailEvaluationEvidence {
  id: number;
  relevant: boolean;
  score: number;
  reason: string | null;
  project_key: string | null;
  posture: string | null;
  surface_status: string | null;
  failure_reason: string | null;
}

export interface GradeDetailDraftEvidence {
  id: number;
  comment_text: string | null;
  posture: string | null;
  created_at: string | null;
}

export interface GradeDetailCritiqueEvidence {
  id: number;
  verdict: string;
  feedback: string | null;
  created_at: string | null;
}

export interface GradeDetailEvidence {
  post: GradeDetailPostEvidence;
  evaluation: GradeDetailEvaluationEvidence | null;
  draft: GradeDetailDraftEvidence | null;
  critique: GradeDetailCritiqueEvidence | null;
}

export interface GradeDetailOverride {
  mode: OverrideMode;
  reason: string | null;
  updated_at: string;
}

export interface GradeDetailEligibility {
  reason: EligibilityReason | null;
  policy_version: string | null;
  resolved_lookback_days: number | null;
  resolved_max_grades: number | null;
}

export interface GradeDetailSnapshotUse {
  first_at: string | null;
  last_at: string | null;
  count: number;
  active_use: boolean;
}

export interface GradeRevisionSummary {
  id: number;
  revision: number;
  schema_version: number;
  source: string;
  recorded_at: string;
  payload: string;
}

export interface SnapshotUseHistoryEntry {
  id: number;
  snapshot_id: number;
  scan_id: number;
  phase: FeedbackPhase;
  role: "excluded" | "aggregate" | "example";
  reason: string | null;
  mode: FeedbackSnapshotMode;
  created_at: string;
}

// One row per relevance/reply_draft/critic phase attempt that this
// evaluation's terminal outcome actually depended on — joins only
// evaluation_phase_runs.id/trace_id, never prompt content or grade
// evidence (those remain in the Jig trace store and
// feedback_snapshot_phases respectively).
export interface PhaseRunSummary {
  id: number;
  phase: string;
  trace_id: string;
  created_at: string;
}

// Full stored-key identity for one evaluation_phase_runs row — the
// response shape for GET /api/feedback/phase-runs/{phaseRunId} and for
// resolving a Trace Detail backlink by trace_id. Never duplicates prompt
// or grade content.
export interface PhaseRunDetail {
  id: number;
  scan_id: number;
  post_id: number;
  evaluation_id: number | null;
  grade_id: number | null;
  snapshot_id: number;
  snapshot_phase_id: number;
  phase: string;
  trace_id: string;
  model: string;
  status: "complete" | "error" | "cancelled";
  created_at: string;
}

export interface SnapshotUseHistoryCursor {
  created_at: string;
  id: number;
}

// Shared by both grade-detail phase_runs paging and snapshot-phase
// consumer paging — both order evaluation_phase_runs by created_at DESC,
// id DESC, so both use the exact same compound cursor shape.
export interface PhaseRunCursor {
  created_at: string;
  id: number;
}

// Cursors here are already decoded by the route layer (mirrors
// feedback-filters.ts's convention: routes decode the opaque query param,
// query functions take the structured cursor).
export interface GradeDetailPagingOptions {
  revisionLimit?: number;
  revisionCursor?: number;
  snapshotUseLimit?: number;
  snapshotUseCursor?: SnapshotUseHistoryCursor;
  phaseRunLimit?: number;
  phaseRunCursor?: PhaseRunCursor;
}

export interface GradeDetailResponse {
  grade: Grade;
  identity: { evaluation_id: number | null; post_id: number; scan_id: number | null };
  segment: {
    project_key: string | null;
    platform: string | null;
    posture: string | null;
    terminal_status: string | null;
  };
  evidence: GradeDetailEvidence;
  eligibility: GradeDetailEligibility;
  override: GradeDetailOverride | null;
  snapshot_use: GradeDetailSnapshotUse;
  trace_available: boolean;
  trace_unavailable_reason: string | null;
  revision_history: CursorPage<GradeRevisionSummary>;
  snapshot_use_history: CursorPage<SnapshotUseHistoryEntry>;
  phase_runs: CursorPage<PhaseRunSummary>;
}
