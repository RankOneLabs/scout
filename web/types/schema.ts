import type { ScanFeedbackSummary } from "@/types/feedback";

// Raw table mirrors — 1:1 with SQLite tables

export type ScanStatus = "complete" | "partial" | "failed" | "interrupted";

export interface Scan {
  id: number;
  started_at: string;
  completed_at: string | null;
  messages_scanned: number;
  relevant_found: number;
  fetch_started_at: string | null;
  safe_watermark_at: string | null;
  status: ScanStatus | null;
  overflow_count: number;
}

export interface ScanFetchFailure {
  id: number;
  scan_id: number;
  platform: string;
  context: string | null;
  kind: string;
  message: string | null;
  http_status: number | null;
  retry_after: string | null;
  retryable: boolean;
  created_at: string;
}

export type ParentLookupStatus = "not_applicable" | "resolved" | "failed";

export interface SourceAuthor {
  id: string;
  name: string;
}

export interface SourceParent {
  id: string;
  author: SourceAuthor;
  text: string;
  url: string;
}

export interface Post {
  id: number;
  platform: string;
  platform_msg_id: string;
  channel_name: string | null;
  channel_id: string | null;
  author_name: string | null;
  author_id: string | null;
  content: string | null;
  url: string | null;
  created_at: string | null;
  scan_id: number | null;
  parent_lookup_status: ParentLookupStatus;
  parent: SourceParent | null;
}

export interface Evaluation {
  id: number;
  post_id: number;
  relevant: boolean;
  score: number;
  reason: string | null;
  relevant_to: string[];
  scan_id: number | null;
  surface_status?: string | null;
  posture?: string | null;
  dossier_revision?: string | null;
  dossier_summary_id?: string | null;
}

export interface DraftComment {
  id: number;
  post_id: number;
  evaluation_id: number;
  project_key: string | null;
  comment_text: string | null;
  created_at: string | null;
  scan_id: number | null;
  posture?: string | null;
  structured_output?: string | null;
  dossier_revision?: string | null;
  dossier_summary_id?: string | null;
}

export interface Critique {
  id: number;
  draft_id: number | null;
  evaluation_id: number | null;
  verdict: string;
  feedback: string | null;
  created_at: string | null;
  scan_id: number | null;
}

// Composite view types

export interface PromptBundle {
  evaluate: string | null;
  respond: string | null;
  critique: string | null;
}

export interface MatchedRoute {
  id: number;
  project_key: string;
  keyword: string;
  match_type: KeywordMatchType;
  intent: string | null;
  positive_context: string[];
  negative_context: string[];
  evaluate_prompt: string | null;
  respond_prompt: string | null;
  critique_prompt: string | null;
  resolved_prompt_bundle: PromptBundle | null;
}

export interface EvaluationWithRoute extends Evaluation {
  keyword_route_id: number | null;
  matched_route: MatchedRoute | null;
}

export type SurfaceStatus =
  | "surfaced"
  | "low_relevance"
  | "abstained"
  | "critic_rejected"
  | "gate_blocked"
  | "not_relevant"
  | "drafting_failed";

/** The complete, evaluation-scoped review population returned by the API. */
export interface ReviewEvaluation extends EvaluationWithRoute {
  surface_status: SurfaceStatus;
  failure_reason: string | null;
  project_key: string | null;
  post: Post;
  draft: DraftComment | null;
  critique: Critique | null;
  gate_violations: GateBlock[];
  grade: Grade | null;
}

export interface PostWithEvaluation extends Post {
  eval_id: number | null;
  relevant: boolean | null;
  score: number | null;
  reason: string | null;
  relevant_to: string[];
  keyword_route_id: number | null;
  matched_route: MatchedRoute | null;
}

export interface DraftWithContext {
  draft_id: number;
  evaluation_id?: number;
  project_key: string | null;
  comment_text: string | null;
  draft_created_at: string | null;
  verdict: string | null;
  feedback: string | null;
  post_id: number;
  platform: string;
  author_name: string | null;
  author_id: string | null;
  content: string | null;
  url: string | null;
  score: number | null;
  relevant: boolean;
  scan_id: number | null;
  keyword_route_id: number | null;
  matched_route: MatchedRoute | null;
  parent_lookup_status: ParentLookupStatus;
  parent: SourceParent | null;
  surface_status?: string | null;
  posture?: string | null;
  dossier_revision?: string | null;
}

export interface ScanStats {
  total_scans: number;
  total_posts: number;
  total_relevant: number;
  total_drafts: number;
  total_approved: number;
  total_rejected: number;
}

export interface ScanDetail extends Scan {
  post_count: number;
  eval_count: number;
  draft_count: number;
  approved_count: number;
  rejected_count: number;
  revised_count: number;
}

export interface ScanWithCounts extends Scan {
  post_count: number;
  eval_count: number;
  draft_count: number;
  critique_count: number;
}

export interface ScanDetailWithCounts extends ScanDetail {
  critique_count: number;
  gate_blocked_count?: number;
  failures: ScanFetchFailure[];
  // null when no feedback snapshot was recorded for this scan (pre-cohort-2
  // scans, or a scan that failed before the snapshot write).
  feedback?: ScanFeedbackSummary | null;
}

// Trace types

export interface TraceSpan {
  id: string;
  trace_id: string;
  parent_id: string | null;
  kind: string;
  name: string;
  input: unknown;
  output: unknown;
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
  metadata: unknown;
  error: string | null;
  usage_input_tokens: number | null;
  usage_output_tokens: number | null;
  usage_cost: number | null;
}

export interface TraceListItem {
  trace_id: string;
  name: string;
  started_at: string;
  duration_ms: number | null;
  step_count: number;
  has_error: boolean;
  error: string | null;
}

export interface TraceFilters {
  error_only?: boolean;
  limit?: number;
}

// Pagination

export interface Paginated<T> {
  data: T[];
  has_more: boolean;
}

// Filter types

export interface PostFilters {
  platform?: string;
  relevant?: boolean;
  score_min?: number;
  score_max?: number;
  scan_id?: number;
  limit?: number;
  before_id?: number;
}

export interface DraftFilters {
  project_key?: string;
  verdict?: string;
  scan_id?: number;
  limit?: number;
  before_id?: number;
}

export interface NegativeGradingFilters {
  limit?: number;
  before_id?: number;
}

// Grade types

export type RelevanceJudgment = "correct" | "false_positive" | "false_negative";
export type ActionJudgment = "accept" | "fail";
export type FailureDimension =
  | "contextual_understanding"
  | "factual_support"
  | "unsupported_implication"
  | "posture"
  | "tone"
  | "wording"
  | "usefulness";
export type Posture = "answer" | "engage" | "ask" | "abstain";

export interface Grade {
  id: number;
  evaluation_id: number | null;
  post_id: number;
  scan_id: number | null;
  source: string;
  graded_at: string;
  schema_version: number;
  needs_regrade: number;
  relevance_judgment: RelevanceJudgment;
  action_judgment: ActionJudgment | null;
  dimensions: FailureDimension[] | null;
  failure_note: string | null;
  factual_offending_claim: string | null;
  factual_disposition: "unsupported" | "contradicted" | null;
  factual_contradicting_evidence: string | null;
  context_missing_input: string | null;
  posture_should_have_been: Posture | null;
  implication_implied_claim: string | null;
  implication_missing_support: string | null;
  // Resolved reply text when the source includes the revision join (grade
  // write responses do; some read overlays intentionally omit it).
  edited_text?: string | null;
  // v3: pointer to the reply_draft_revisions row holding edited_text, if
  // any — server-assigned by save_grade, never client-settable.
  reply_revision_id: number | null;
  // Grade-save status, from grade_revisions — absent where the caller
  // doesn't resolve it (e.g. GradeInput echoes, not full Grade reads).
  revision_count?: number;
  latest_recorded_at?: string;
}

export interface GradeInput {
  relevance_judgment: RelevanceJudgment;
  action_judgment: ActionJudgment;
  dimensions?: FailureDimension[] | null;
  failure_note?: string | null;
  factual_offending_claim?: string | null;
  factual_disposition?: "unsupported" | "contradicted" | null;
  factual_contradicting_evidence?: string | null;
  context_missing_input?: string | null;
  posture_should_have_been?: Posture | null;
  implication_implied_claim?: string | null;
  implication_missing_support?: string | null;
  // v3: the grader's corrected reply text. Null/omitted on accept.
  edited_text?: string | null;
}

export interface DraftWithGrade extends DraftWithContext {
  grade: Grade | null;
}

export interface GradingProgress {
  graded: number;
  total: number;
}

// Feedback verbs — used by the annotation feedback-verb color palette in
// lib/design-tokens.ts.

export type FeedbackVerb =
  | "citation_needed"
  | "verify"
  | "soften"
  | "sharpen"
  | "expand"
  | "trim"
  | "custom";

export const FEEDBACK_VERBS: readonly FeedbackVerb[] = [
  "citation_needed",
  "verify",
  "soften",
  "sharpen",
  "expand",
  "trim",
  "custom",
] as const;

// Settings registry — raw table mirrors

export interface ProjectRow {
  key: string;
  name: string;
  description: string;
  link: string;
  active: boolean;
  dossier_summary_id?: string | null;
  created_at: string;
  updated_at: string;
}

export interface GateBlock {
  id: number;
  reason_code: string;
  offending_text: string | null;
  segment_index: number | null;
  project_key: string | null;
  dossier_summary_id: string | null;
  dossier_revision: string | null;
  scan_id: number | null;
  post_id: number | null;
  evaluation_id: number | null;
  context: string | null;
  created_at: string;
}

export interface SurfacedEvent {
  id: number;
  platform: string;
  author_id: string;
  surfaced_at: string;
  post_id: number | null;
  evaluation_id: number | null;
  draft_id: number | null;
  project_key: string | null;
  created_at: string;
}

export interface BlockedAuthorRow {
  id: number;
  platform: string;
  author_id: string;
  author_name: string | null;
  reason: string | null;
  active: boolean;
  created_at: string;
  updated_at: string;
}

export interface ProjectKeywordRow {
  id: number;
  project_key: string;
  keyword: string;
  match_type: KeywordMatchType;
  intent: string | null;
  positive_context: string[];
  negative_context: string[];
  notes: string | null;
  evaluate_prompt: string | null;
  respond_prompt: string | null;
  critique_prompt: string | null;
  active: boolean;
  priority: number;
  created_at: string;
  updated_at: string;
}

export type KeywordMatchType = "substring" | "phrase" | "exact" | "regex";

export type PromptKind = "evaluate" | "respond" | "critique" | "shared" | "custom";

export interface PromptTemplateRow {
  name: string;
  body: string;
  kind: PromptKind;
  active: boolean;
  created_at: string;
  updated_at: string;
}

// Composite list types

export interface ProjectListItem extends ProjectRow {
  keyword_count: number;
}

export interface PromptTemplateListItem extends PromptTemplateRow {
  referenced_keyword_count: number;
}
