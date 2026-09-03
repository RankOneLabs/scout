// evaluation-feedback/v1 inspector — mirrors feedback_snapshots /
// feedback_snapshot_phases / feedback_snapshot_items. Read-only: these
// tables are immutable at the SQLite layer (no UPDATE/DELETE triggers
// permit it), so every type here describes a persisted, historical fact.

export type FeedbackPhase = "relevance" | "reply_draft" | "critic";

export const FEEDBACK_PHASES: readonly FeedbackPhase[] = [
  "relevance",
  "reply_draft",
  "critic",
];

export type FeedbackSnapshotMode = "shadow" | "active";

export type FeedbackExclusionReason =
  | "schema_version"
  | "needs_regrade"
  | "missing_evaluation_linkage"
  | "mismatched_evaluation_identity"
  | "shared_contract_invalid"
  | "manual_exclude"
  | "eligible_cap"
  | "not_draft_quality_population";

export type FeedbackItemRole = "excluded" | "aggregate" | "example";

export type FeedbackUseFilter = "all" | "included" | "excluded";

// One row per scan (feedback_snapshots.scan_id is UNIQUE) — the list-page
// projection, also embedded as `snapshot` on the detail response.
export interface SnapshotListItem {
  snapshot_id: number;
  scan_id: number;
  policy_version: string;
  mode: FeedbackSnapshotMode;
  as_of: string;
  created_at: string;
  population_count: number;
  eligible_count: number;
  excluded_count: number;
}

// Snapshot-wide policy/coverage header, returned once per detail response.
export interface FeedbackSnapshotHeader extends SnapshotListItem {
  lookback_days: number;
  // Derived (as_of - lookback_days), not itself a stored column — see
  // feedback-queries.ts for the exact computation.
  lookback_started_at: string;
  max_grades: number;
  segment_min_grades: number;
  note_max_chars: number;
}

// Per-phase counts. population_count/eligible_count are snapshot-wide
// (identical across all three phases); included/excluded/example counts
// are phase-specific because reply_draft/critic apply an additional
// not_draft_quality_population filter beyond global eligibility.
export interface FeedbackCounts {
  included_count: number;
  excluded_count: number;
  example_count: number;
  // included_count when mode === "active", else 0 — a shadow snapshot's
  // evidence was stored but never fed a live prompt.
  actually_used_count: number;
}

export interface FeedbackPhaseSummary {
  snapshot_phase_id: number;
  phase: FeedbackPhase;
  token_budget: number;
  token_estimate: number;
  truncated: boolean;
  // Raw persisted JSON text, verbatim — not re-parsed/re-derived.
  structured_summary: string;
  // Exact persisted prose, verbatim — no trimming or normalization.
  rendered_text: string;
  rendered_sha256: string;
  counts: FeedbackCounts;
}

// One normalized row per grade per phase, merging that grade's
// feedback_snapshot_items role rows (a grade is either wholly excluded,
// or in the aggregate population and optionally also selected as an
// example — never both excluded and included for the same phase).
export interface FeedbackGradeUse {
  grade_id: number;
  post_id: number;
  graded_at: string;
  primary_use: "included" | "excluded";
  roles: FeedbackItemRole[];
  selected_note: boolean;
  // 1-based rank among this phase's selected examples (insertion order,
  // i.e. selection order), null when selected_note is false.
  example_rank: number | null;
  // 0 or 1 entries in practice (a grade has at most one exclusion reason
  // per phase); kept as an array per the "all stored exclusion reasons"
  // contract so a future multi-reason model doesn't require a shape change.
  exclusion_reasons: FeedbackExclusionReason[];
  // Explicit persisted reasons for every role (phase_population,
  // selected_recent_note, or the concrete exclusion reason). This is audit
  // evidence, not a browser reconstruction from item IDs or role order.
  selection_reasons: string[];
  pinned_grade_revision_id: number;
  // Raw persisted JSON text of the pinned grade_revisions row.
  pinned_grade_revision_payload: string;
  pinned_grade_revision_recorded_at: string;
}

export interface FeedbackGradeUsePage {
  phase: FeedbackPhase;
  use_filter: FeedbackUseFilter;
  data: FeedbackGradeUse[];
  has_more: boolean;
  next_cursor: string | null;
}

export interface FeedbackPhaseRunConsumer {
  id: number;
  scan_id: number;
  post_id: number;
  evaluation_id: number | null;
  grade_id: number | null;
  phase: FeedbackPhase;
  trace_id: string;
  status: "complete" | "error" | "cancelled";
  created_at: string;
}

export interface FeedbackPhaseRunConsumerPage {
  phase: FeedbackPhase;
  data: FeedbackPhaseRunConsumer[];
  has_more: boolean;
  next_cursor: string | null;
}

export interface SnapshotListPage {
  data: SnapshotListItem[];
  has_more: boolean;
  next_cursor: string | null;
}

export interface FeedbackSnapshotDetail {
  snapshot: FeedbackSnapshotHeader;
  // All three phase summaries are always present, regardless of which
  // phase's item page was requested.
  phases: Record<FeedbackPhase, FeedbackPhaseSummary>;
  items: FeedbackGradeUsePage;
  consumers: FeedbackPhaseRunConsumerPage;
}

// Opaque cursor payloads — base64url(JSON) via feedback-filters.ts. Not
// meant to be constructed or read by clients; documented here only so the
// query layer and filter layer agree on shape.
export interface SnapshotListCursor {
  created_at: string;
  snapshot_id: number;
}

export interface FeedbackItemCursor {
  primary_use: "included" | "excluded";
  selected_note: boolean;
  example_rank: number | null;
  graded_at: string;
  grade_id: number;
}

// Scan-detail augmentation — one snapshot per scan (or none, for
// pre-cohort-2 scans / scans where the pipeline failed before the
// snapshot write).
export interface ScanFeedbackSummary {
  snapshot_id: number;
  policy_version: string;
  mode: FeedbackSnapshotMode;
  population_count: number;
  eligible_count: number;
  excluded_count: number;
}

// Draft/Evaluation card grade-save status — sourced from grade_revisions,
// independent of any feedback snapshot.
export interface GradeRevisionMeta {
  revision_count: number;
  latest_recorded_at: string;
}
