// Read-only Experiments surface — experiment_runs (parent) /
// evaluation_experiments (per-baseline-case attempt) / trace_comparisons
// (v36's versioned replay-evidence domain, evaluation_experiments.py).
// Every shape here mirrors persisted authority: list/detail projections
// read stored columns and JSON verbatim, TraceDiff/domain_diff remain exact
// mirrors of Jig and Scout, and delta availability is derived only from the
// two stored traces so the browser never turns absent evidence into zero.

import type { FeedbackPhase } from "@/types/feedback";

// One evaluation_experiments attempt's own CAS lifecycle — unchanged since
// v30. The parent experiment_runs.status projection below adds a fifth
// "partial" value that never applies to an individual attempt.
export type ExperimentStatus = "queued" | "running" | "complete" | "failed";

export const EXPERIMENT_STATUSES: readonly ExperimentStatus[] = [
  "queued",
  "running",
  "complete",
  "failed",
];

// The parent run's transactionally consistent projection of every linked
// baseline case's latest attempt — see
// StateManager._recompute_experiment_run_status.
export type ExperimentRunStatus = "queued" | "running" | "complete" | "partial" | "failed";

export const EXPERIMENT_RUN_STATUSES: readonly ExperimentRunStatus[] = [
  "queued",
  "running",
  "complete",
  "partial",
  "failed",
];

export const EXPERIMENT_PHASES: readonly FeedbackPhase[] = [
  "relevance",
  "reply_draft",
  "critic",
];

export interface CursorPage<T> {
  data: T[];
  has_more: boolean;
  next_cursor: string | null;
}

// --- List -----------------------------------------------------------------

export interface ExperimentListCursor {
  created_at: string;
  id: number;
  status: ExperimentStatus | null;
  phase: FeedbackPhase | null;
}

export interface ExperimentListFilters {
  status?: ExperimentStatus;
  phase?: FeedbackPhase;
  limit?: number;
  cursor?: ExperimentListCursor;
}

export interface ExperimentListRow {
  id: number;
  experiment_run_id: number;
  name: string;
  status: ExperimentStatus;
  run_status: ExperimentRunStatus;
  phase: FeedbackPhase;
  attempt_number: number;
  supersedes_experiment_id: number | null;
  grader_attached: boolean;
  baseline_phase_run_id: number;
  candidate_trace_id: string | null;
  baseline_model: string;
  candidate_model: string;
  created_at: string;
  completed_at: string | null;
  candidate_llm_call_count: number | null;
  candidate_cost: number | null;
  // Null exactly when no comparison has been persisted yet (queued,
  // running, or failed before diff construction) — never coerced to
  // false/true.
  comparison_complete: boolean | null;
}

export type ExperimentListResponse = CursorPage<ExperimentListRow>;

// --- Parent-run summaries -------------------------------------------------

export type ExperimentRunVerdict =
  | "pending"
  | "failed"
  | "candidate_recommended"
  | "candidate_not_recommended"
  | "no_measurable_difference"
  | "not_graded"
  | "mixed";

export interface ExperimentRunCursor {
  version: 1;
  created_at: string;
  id: number;
  status: ExperimentRunStatus | null;
  phase: FeedbackPhase | null;
}

export interface AggregateMetric {
  available: boolean;
  case_count: number;
  baseline_mean: number | null;
  candidate_mean: number | null;
  mean_delta: number | null;
}

export interface ExperimentRunSummary {
  id: number;
  name: string;
  status: ExperimentRunStatus;
  phase: FeedbackPhase;
  grader_attached: boolean;
  created_at: string;
  completed_at: string | null;
  planned_case_count: number;
  attempted_case_count: number;
  skipped_case_count: number;
  current_case_count: number;
  retry_count: number;
  status_counts: Record<ExperimentStatus, number>;
  total_llm_call_count: number;
  total_cost: number | null;
  verdict: ExperimentRunVerdict;
  correction_distance: AggregateMetric;
  cost: AggregateMetric;
  latency: AggregateMetric;
}

export interface ExperimentRunCaseSummary {
  phase_run_id: number;
  current: ExperimentListRow;
  history: ExperimentListRow[];
}

export interface ExperimentRunDetailResponse {
  run: ExperimentRunSummary;
  configuration: {
    version: 2 | 4;
    phase: FeedbackPhase;
    grader_attached: boolean;
    identity: string;
    plan_sha256: string | null;
  };
  cases: ExperimentRunCaseSummary[];
  skipped_pairs: CandidateConfigV4["skipped_pairs"];
}

export type ExperimentRunListResponse = CursorPage<ExperimentRunSummary>;

// --- Candidate config (v2, experiment_runs.candidate_config JSON column) --
// Candidate-only: phase/model/system_prompt/grader_attached, decided
// before any model call and shared across every baseline case replayed
// under this run. Per-baseline provenance (recorded_input_sha256,
// baseline_prompt_reused, and — for a grader_attached reply_draft attempt
// — the full correction-oracle pin) lives on each attempt's own
// baseline_evidence instead; see BaselineEvidenceV2.

export interface CandidateConfigV2 {
  version: 2;
  phase: FeedbackPhase;
  model: string;
  system_prompt: string;
  system_prompt_sha256: string;
  grader_attached: boolean;
}

export interface CandidateConfigV4 {
  version: 4;
  phase: FeedbackPhase;
  variant_name: string;
  model_override: string | null;
  system_prompt_override: string | null;
  system_prompt_override_sha256: string | null;
  grader_attached: boolean;
  sweep: { name: string; axis: "model" | "prompt"; version: 1 } | null;
  plan_sha256: string;
  phase_run_ids: number[];
  dropped_duplicate_phase_run_ids: number[];
  skipped_pairs: Array<{
    phase_run_id: number;
    classification: string;
    reason: string;
    baseline_model: string;
    baseline_prompt_sha256: string;
  }>;
}

export type CandidateConfig = CandidateConfigV2 | CandidateConfigV4;

// --- Baseline evidence (v2, evaluation_experiments.baseline_evidence) -----
// The base shape (recorded_input_sha256/baseline_prompt_reused) is always
// present. The extended reply-correction-oracle fields are present only on
// a grader_attached attempt — pinned once at insert and never changed.

export interface BaselineEvidenceV2 {
  version: 2;
  recorded_input_sha256: string;
  baseline_prompt_reused: boolean;
  baseline_model?: string;
  baseline_prompt_sha256?: string;
  reply_revision_id?: number;
  correction_sha256?: string;
  project_key?: string;
  dossier_summary_id?: string;
  dossier_revision?: string;
  grader_version?: string;
  assembler_version?: string;
}

export interface BatchCaseEvidenceV1 {
  version: 1;
  recorded_input_sha256: string;
  baseline_model: string;
  baseline_prompt_sha256: string;
  baseline_prompt_reused: boolean;
  candidate_model: string;
  candidate_prompt_sha256: string;
  estimated_usd: number | null;
  reply_revision_id: number;
  correction_sha256: string;
  project_key: string;
  dossier_summary_id: string;
  dossier_revision: string;
  grader_version: string;
  assembler_version: string;
}

export type BaselineEvidence = BaselineEvidenceV2 | BatchCaseEvidenceV1;

// --- Score evidence (trace_comparisons.score_evidence JSON column) --------
// Present only for a completed attempt that ran with Scout's
// ReplyCorrectionGrader attached; null for every ungraded (relevance/
// critic, or grader-ineligible) comparison. delta = candidate_distance -
// baseline_distance: negative unambiguously means the candidate is closer
// to the pinned correction than the historical baseline was.

export interface ScoreEvidence {
  grader_version: string;
  assembler_version: string;
  correction_sha256: string;
  reply_revision_id: number;
  baseline_distance: number;
  candidate_distance: number;
  delta: number;
  grader_attached: true;
}

// --- Jig TraceDiff (trace_comparisons.trace_diff JSON column) -------------
// One-to-one mirror of jig.replay.diff.TraceDiff via dataclasses.asdict +
// json.dumps — tuples become 2-element JSON arrays. Untouched by v36: Jig's
// own native trace comparison never learns about Scout's grader.

export type ToolDivergenceKind = "name" | "args" | "output" | "error" | "only_a" | "only_b";
export type AlignmentTier = "identity" | "anchor" | "ordinal";

export interface ToolEvent {
  name: string;
  args: unknown;
  output: string | null;
  error: string | null;
}

export interface ToolDiff {
  index: number;
  divergence: ToolDivergenceKind;
  a: ToolEvent | null;
  b: ToolEvent | null;
  tier: AlignmentTier | null;
  index_a: number | null;
  index_b: number | null;
}

export interface TraceDiff {
  trace_a_id: string;
  trace_b_id: string;
  tool_divergence: ToolDiff[];
  output_diff: [string, string] | null;
  error_category_change: [string | null, string | null] | null;
  score_deltas: Record<string, number>;
  score_details: Record<string, [number | null, number | null]>;
  cost_delta: number;
  latency_ms_delta: number;
  comparison_complete: boolean;
  comparison_incomplete_reason: string | null;
  a_output_preview: string;
  b_output_preview: string;
  a_output_hash: string | null;
  b_output_hash: string | null;
  a_output_byte_length: number | null;
  b_output_byte_length: number | null;
  a_output_complete: unknown;
  b_output_complete: unknown;
}

// --- Scout domain_diff (trace_comparisons.domain_diff JSON column) --------

export interface DomainDiffSide {
  complete: boolean;
  sha256: string | null;
  utf8_byte_length: number | null;
  incomplete_reason: string | null;
  value?: unknown;
}

export interface DomainDiff {
  baseline: DomainDiffSide;
  candidate: DomainDiffSide;
  grader_not_attached: boolean;
  // Present only when both sides are complete — never filled with an
  // empty array to signal "no comparison was possible".
  additions?: string[];
  removals?: string[];
  changes?: string[];
}

export interface ExperimentComparison {
  id: number;
  trace_a_id: string;
  trace_b_id: string;
  jig_revision: string;
  created_at: string;
  // Derived server-side from the two persisted traces. The native Jig
  // numeric deltas remain untouched even when their evidence is absent.
  cost_delta_available: boolean;
  latency_delta_available: boolean;
  trace_diff: TraceDiff;
  domain_diff: DomainDiff;
  score_evidence: ScoreEvidence | null;
}

// --- Detail -----------------------------------------------------------------

export interface ExperimentRunDetail {
  id: number;
  name: string;
  status: ExperimentRunStatus;
  candidate_config: CandidateConfig;
  created_at: string;
  completed_at: string | null;
}

export interface ExperimentBaselineDetail {
  phase_run_id: number;
  phase: FeedbackPhase;
  trace_id: string;
  model: string;
  system_prompt: string;
  system_prompt_sha256: string;
  phase_run_url: string;
  trace_url: string;
}

export interface ExperimentSnapshotDetail {
  snapshot_phase_id: number;
  snapshot_id: number;
  phase: FeedbackPhase;
  policy_version: string;
  lookback_days: number;
  max_grades: number;
  snapshot_url: string;
}

export interface ExperimentCandidateDetail {
  trace_id: string | null;
  trace_url: string | null;
  model: string;
  system_prompt: string;
  system_prompt_sha256: string;
  llm_call_count: number | null;
  cost: number | null;
}

export interface ExperimentDetailResponse {
  id: number;
  experiment_run: ExperimentRunDetail;
  attempt_number: number;
  supersedes_experiment_id: number | null;
  status: ExperimentStatus;
  error_detail: string | null;
  created_at: string;
  completed_at: string | null;
  baseline: ExperimentBaselineDetail;
  baseline_evidence: BaselineEvidence;
  evaluation_id: number | null;
  snapshot: ExperimentSnapshotDetail;
  candidate: ExperimentCandidateDetail;
  // Null exactly when status is not yet 'complete' (or completion failed
  // before the comparison was persisted).
  comparison: ExperimentComparison | null;
  reply_evidence?: ReplyEvidence;
}

export type ReplyEvidenceUnavailableReason =
  | "not_reply_draft"
  | "grader_not_attached"
  | "attempt_not_complete"
  | "comparison_unavailable"
  | "output_incomplete";

export type ReplyEvidence =
  | { available: false; reason: ReplyEvidenceUnavailableReason }
  | {
      available: true;
      correction: string;
      baseline_output: unknown;
      candidate_output: unknown;
      baseline_distance: number;
      candidate_distance: number;
      delta: number;
    };
