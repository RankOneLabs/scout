import { getDb } from "@/lib/db";
import { validateGradeEnvelope } from "@/lib/grade-validation";
import {
  encodeGradeListCursor,
  encodeRevisionCursor,
  encodeSnapshotUseHistoryCursor,
} from "@/lib/feedback-grade-filters";
import {
  getEvaluationIdsWithLinkedPhaseRuns,
  hasLinkedPhaseRuns,
  listPhaseRunsForEvaluation,
} from "@/lib/feedback-phase-run-queries";
import type {
  CursorPage,
  EligibilityReason,
  GlobalExclusionReason,
  GradeDetailDraftEvidence,
  GradeDetailEvaluationEvidence,
  GradeDetailPagingOptions,
  GradeDetailPostEvidence,
  GradeDetailResponse,
  GradeExplorerRow,
  GradeListCursor,
  GradeListFilters,
  GradeListResponse,
  GradeRevisionSummary,
  OverrideMode,
  PhaseRunSummary,
  SnapshotUseHistoryEntry,
} from "@/types/feedback-grades";
import type { FeedbackPhase, FeedbackSnapshotMode } from "@/types/feedback";
import type { ActionJudgment, FailureDimension, Grade, RelevanceJudgment } from "@/types/schema";

// Mirrors config.HUMAN_GRADE_SCHEMA_VERSION — the schema version
// evaluation-feedback/v1 (and this cohort's reused classifier) treats as
// "current." Kept as a local literal since the web app has no import path
// into the Python config module.
export const HUMAN_GRADE_SCHEMA_VERSION = 3;

const DEFAULT_GRADE_LIST_LIMIT = 50;
const MAX_GRADE_LIST_LIMIT = 100;
const DEFAULT_SUBPAGE_LIMIT = 25;
const MAX_SUBPAGE_LIMIT = 100;

// Historical evaluations (predating phase-run linkage, or whose
// contributor phases never reached a durable trace) have no
// evaluation_phase_runs rows at all — an explicit absent state rather
// than a fuzzy inference.
export const TRACE_UNAVAILABLE_REASON = "no_linked_phase_runs";

// --------------------------------------------------------------------------
// evaluation-feedback/v1 policy + pure eligibility classifier, ported from
// evaluation_feedback.py's `_first_global_exclusion_reason` /
// `classify_feedback_eligibility`. Only the six global exclusion checks
// apply here — `not_draft_quality_population` is phase-scoped and belongs
// to the reply_draft/critic prompt population, not this cohort's
// phase-agnostic present-time eligibility.
// --------------------------------------------------------------------------

export interface ResolvedFeedbackPolicy {
  policy_version: string;
  lookback_days: number;
  max_grades: number;
}

interface PolicyRow {
  policy_version: string;
  lookback_days: number;
  max_grades: number;
}

// Resolved from the most recently created feedback_snapshots row — every
// snapshot stores its resolved policy verbatim, so the latest snapshot is
// the current policy. Null when no snapshot has ever been recorded (no
// scan has run evaluation-feedback/v1 selection yet).
export function resolveFeedbackPolicy(): ResolvedFeedbackPolicy | null {
  const db = getDb();
  const row = db
    .prepare(
      `SELECT policy_version, lookback_days, max_grades
       FROM feedback_snapshots
       ORDER BY created_at DESC, id DESC
       LIMIT 1`
    )
    .get() as PolicyRow | undefined;
  return row ?? null;
}

interface PopulationRow {
  grade_id: number;
  post_id: number;
  scan_id: number | null;
  graded_at: string;
  schema_version: number;
  needs_regrade: number;
  relevance_judgment: string;
  action_judgment: string | null;
  dimensions: string | null;
  failure_note: string | null;
  factual_offending_claim: string | null;
  factual_disposition: string | null;
  factual_contradicting_evidence: string | null;
  context_missing_input: string | null;
  posture_should_have_been: string | null;
  implication_implied_claim: string | null;
  implication_missing_support: string | null;
  evaluation_id: number | null;
  evaluation_post_id: number | null;
  evaluation_scan_id: number | null;
  evaluation_project_key: string | null;
  evaluation_dossier_summary_id: string | null;
  evaluation_dossier_revision: string | null;
  evaluation_posture: string | null;
  draft_comment_id: number | null;
  draft_project_key: string | null;
  draft_dossier_summary_id: string | null;
  draft_dossier_revision: string | null;
  draft_posture: string | null;
  override_mode: string;
}

const POPULATION_SELECT = `
  SELECT
    g.id AS grade_id,
    g.post_id AS post_id,
    g.scan_id AS scan_id,
    g.graded_at AS graded_at,
    g.schema_version AS schema_version,
    g.needs_regrade AS needs_regrade,
    g.relevance_judgment AS relevance_judgment,
    g.action_judgment AS action_judgment,
    g.dimensions AS dimensions,
    g.failure_note AS failure_note,
    g.factual_offending_claim AS factual_offending_claim,
    g.factual_disposition AS factual_disposition,
    g.factual_contradicting_evidence AS factual_contradicting_evidence,
    g.context_missing_input AS context_missing_input,
    g.posture_should_have_been AS posture_should_have_been,
    g.implication_implied_claim AS implication_implied_claim,
    g.implication_missing_support AS implication_missing_support,
    e.id AS evaluation_id,
    e.post_id AS evaluation_post_id,
    e.scan_id AS evaluation_scan_id,
    e.project_key AS evaluation_project_key,
    e.dossier_summary_id AS evaluation_dossier_summary_id,
    e.dossier_revision AS evaluation_dossier_revision,
    e.posture AS evaluation_posture,
    dc.id AS draft_comment_id,
    dc.project_key AS draft_project_key,
    dc.dossier_summary_id AS draft_dossier_summary_id,
    dc.dossier_revision AS draft_dossier_revision,
    dc.posture AS draft_posture,
    COALESCE(o.mode, 'auto') AS override_mode
  FROM grades g
  LEFT JOIN evaluations e ON e.id = g.evaluation_id
  LEFT JOIN draft_comments dc ON dc.evaluation_id = e.id
  LEFT JOIN grade_usage_overrides o ON o.grade_id = g.id
`;

const POPULATION_SQL = `
  ${POPULATION_SELECT}
  WHERE g.graded_at >= ?
  ORDER BY g.graded_at DESC, g.id DESC
`;

function sharedContractInvalid(row: PopulationRow): boolean {
  let dimensions: unknown = null;
  if (row.dimensions !== null) {
    try {
      dimensions = JSON.parse(row.dimensions);
    } catch {
      dimensions = row.dimensions;
    }
  }
  const errors = validateGradeEnvelope(
    {
      relevance_judgment: row.relevance_judgment,
      action_judgment: row.action_judgment,
      dimensions,
      failure_note: row.failure_note,
      factual_offending_claim: row.factual_offending_claim,
      factual_disposition: row.factual_disposition,
      factual_contradicting_evidence: row.factual_contradicting_evidence,
      context_missing_input: row.context_missing_input,
      posture_should_have_been: row.posture_should_have_been,
      implication_implied_claim: row.implication_implied_claim,
      implication_missing_support: row.implication_missing_support,
    },
    row.evaluation_posture
  );
  if (errors.length > 0) return true;

  if (row.draft_comment_id === null) return false;
  return (
    row.draft_project_key !== row.evaluation_project_key ||
    row.draft_dossier_summary_id !== row.evaluation_dossier_summary_id ||
    row.draft_dossier_revision !== row.evaluation_dossier_revision ||
    row.draft_posture !== row.evaluation_posture
  );
}

function firstGlobalExclusionReason(row: PopulationRow): GlobalExclusionReason | null {
  if (row.schema_version !== HUMAN_GRADE_SCHEMA_VERSION) return "schema_version";
  if (row.needs_regrade) return "needs_regrade";
  if (row.evaluation_id === null) return "missing_evaluation_linkage";
  if (row.evaluation_post_id !== row.post_id) return "mismatched_evaluation_identity";
  if (row.scan_id !== null && row.evaluation_scan_id !== row.scan_id) {
    return "mismatched_evaluation_identity";
  }
  if (sharedContractInvalid(row)) return "shared_contract_invalid";
  if (row.override_mode === "exclude") return "manual_exclude";
  return null;
}

// Windowed Overview analytics use the same schema/linkage/contract-valid
// population as prompt eligibility, but deliberately do not apply manual
// exclusions or the prompt cap: those are model-use policy, not human-quality
// metric semantics.
export function getContractValidLinkedGradeIds(from: string, to: string): number[] {
  const db = getDb();
  const rows = db
    .prepare(
      `${POPULATION_SELECT}
       WHERE g.graded_at >= ? AND g.graded_at <= ?
       ORDER BY g.graded_at DESC, g.id DESC`
    )
    .all(from, to) as PopulationRow[];
  return rows
    .filter(
      (row) =>
        row.schema_version === HUMAN_GRADE_SCHEMA_VERSION &&
        row.needs_regrade === 0 &&
        row.evaluation_id !== null &&
        row.evaluation_post_id === row.post_id &&
        (row.scan_id === null || row.evaluation_scan_id === row.scan_id) &&
        !sharedContractInvalid(row)
    )
    .map((row) => row.grade_id);
}

export interface EligibilityClassification {
  status: "eligible" | "excluded";
  reason: EligibilityReason | null; // null exactly when status === "eligible"
}

export interface EligibilityWindow {
  policy: ResolvedFeedbackPolicy;
  boundary: string;
  byGradeId: Map<number, EligibilityClassification>;
  populationCount: number;
  eligibleCount: number;
}

function lookbackBoundary(asOf: string, lookbackDays: number): string {
  return new Date(Date.parse(asOf) - lookbackDays * 24 * 60 * 60 * 1000).toISOString();
}

// Loads the in-window population and classifies every row with the fixed
// exclusion precedence, then caps the newest `max_grades` valid rows as
// eligible — the population is ordered graded_at DESC, grade_id DESC,
// exactly matching evaluation_feedback.py's classify_feedback_eligibility.
// Null when no policy has been resolved yet.
export function computeEligibilityWindow(asOf: string): EligibilityWindow | null {
  const policy = resolveFeedbackPolicy();
  if (policy === null) return null;
  const boundary = lookbackBoundary(asOf, policy.lookback_days);

  const db = getDb();
  const rows = db.prepare(POPULATION_SQL).all(boundary) as PopulationRow[];

  const byGradeId = new Map<number, EligibilityClassification>();
  let validCount = 0;
  for (const row of rows) {
    const reason = firstGlobalExclusionReason(row);
    if (reason !== null) {
      byGradeId.set(row.grade_id, { status: "excluded", reason });
      continue;
    }
    validCount += 1;
    if (validCount <= policy.max_grades) {
      byGradeId.set(row.grade_id, { status: "eligible", reason: null });
    } else {
      byGradeId.set(row.grade_id, { status: "excluded", reason: "eligible_cap" });
    }
  }

  return {
    policy,
    boundary,
    byGradeId,
    populationCount: rows.length,
    eligibleCount: validCount <= policy.max_grades ? validCount : policy.max_grades,
  };
}

// Present-time eligibility for one grade: the exact classifier result when
// in-window, `outside_lookback` when older than the resolved lookback, and
// null when no policy has ever been resolved (see computeEligibilityWindow).
export function resolveEligibilityReason(
  window: EligibilityWindow | null,
  gradeId: number,
  gradedAt: string
): EligibilityReason | null {
  if (window === null) return null;
  const hit = window.byGradeId.get(gradeId);
  if (hit) return hit.status === "eligible" ? "eligible" : hit.reason;
  return gradedAt < window.boundary ? "outside_lookback" : null;
}

// --------------------------------------------------------------------------
// Latest-snapshot "actively used" grade ids — a grade counts as actively
// used only when the single most recent snapshot is mode='active' and this
// grade has an aggregate/example role item under it, in any phase. A
// shadow snapshot's evidence was stored but never fed a live prompt.
// --------------------------------------------------------------------------

interface LatestSnapshotRow {
  id: number;
  scan_id: number;
  mode: string;
}

export function getLatestSnapshotRow(): LatestSnapshotRow | null {
  const db = getDb();
  const row = db
    .prepare(
      `SELECT id, scan_id, mode FROM feedback_snapshots ORDER BY created_at DESC, id DESC LIMIT 1`
    )
    .get() as LatestSnapshotRow | undefined;
  return row ?? null;
}

function getActivelyUsedGradeIds(latest: LatestSnapshotRow | null): Set<number> {
  if (latest === null || latest.mode !== "active") return new Set();
  const db = getDb();
  const rows = db
    .prepare(
      `SELECT DISTINCT fsi.grade_id AS grade_id
       FROM feedback_snapshot_items fsi
       JOIN feedback_snapshot_phases fsp ON fsp.id = fsi.snapshot_phase_id
       WHERE fsp.snapshot_id = ? AND fsi.role IN ('aggregate', 'example')`
    )
    .all(latest.id) as Array<{ grade_id: number }>;
  return new Set(rows.map((r) => r.grade_id));
}

// --------------------------------------------------------------------------
// Grade Explorer list
// --------------------------------------------------------------------------

interface GradeListRow {
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
  dimensions: string | null;
  schema_version: number;
  needs_regrade: number;
  graded_at: string;
  grade_time: string;
  last_edit_time: string;
  revision_count: number;
  override_mode: string;
  override_reason: string | null;
  snapshot_use_first_at: string | null;
  snapshot_use_last_at: string | null;
  snapshot_use_count: number;
}

function parseDimensions(raw: string | null): FailureDimension[] | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as FailureDimension[]) : null;
  } catch {
    return null;
  }
}

export function listGrades(filters: GradeListFilters, asOf: string): GradeListResponse {
  const limit = Math.min(filters.limit ?? DEFAULT_GRADE_LIST_LIMIT, MAX_GRADE_LIST_LIMIT);

  const window = filters.eligibilityReason !== undefined ? computeEligibilityWindow(asOf) : null;
  const latestSnapshot =
    filters.snapshotUse === "active" ? getLatestSnapshotRow() : undefined;
  const activelyUsedIds =
    filters.snapshotUse === "active" ? getActivelyUsedGradeIds(latestSnapshot ?? null) : null;

  const conditions: string[] = [];
  const params: (string | number)[] = [];

  if (filters.gradedFrom !== undefined) {
    conditions.push("COALESCE(rm.grade_time, g.graded_at) >= ?");
    params.push(filters.gradedFrom);
  }
  if (filters.gradedTo !== undefined) {
    conditions.push("COALESCE(rm.grade_time, g.graded_at) <= ?");
    params.push(filters.gradedTo);
  }
  if (filters.editedFrom !== undefined) {
    conditions.push("COALESCE(rm.last_edit_time, g.graded_at) >= ?");
    params.push(filters.editedFrom);
  }
  if (filters.editedTo !== undefined) {
    conditions.push("COALESCE(rm.last_edit_time, g.graded_at) <= ?");
    params.push(filters.editedTo);
  }
  if (filters.schemaVersion !== undefined) {
    conditions.push("g.schema_version = ?");
    params.push(filters.schemaVersion);
  }
  if (filters.needsRegrade !== undefined) {
    conditions.push("g.needs_regrade = ?");
    params.push(filters.needsRegrade ? 1 : 0);
  }
  if (filters.project !== undefined) {
    conditions.push("e.project_key = ?");
    params.push(filters.project);
  }
  if (filters.platform !== undefined) {
    conditions.push("p.platform = ?");
    params.push(filters.platform);
  }
  if (filters.posture !== undefined) {
    conditions.push("e.posture = ?");
    params.push(filters.posture);
  }
  if (filters.terminalStatus !== undefined) {
    conditions.push("e.surface_status = ?");
    params.push(filters.terminalStatus);
  }
  if (filters.relevanceJudgment !== undefined) {
    conditions.push("g.relevance_judgment = ?");
    params.push(filters.relevanceJudgment);
  }
  if (filters.actionJudgment !== undefined) {
    conditions.push("g.action_judgment = ?");
    params.push(filters.actionJudgment);
  }
  if (filters.failureDimension !== undefined) {
    conditions.push("EXISTS (SELECT 1 FROM json_each(g.dimensions) WHERE json_each.value = ?)");
    params.push(filters.failureDimension);
  }
  if (filters.overrideMode !== undefined) {
    conditions.push("COALESCE(o.mode, 'auto') = ?");
    params.push(filters.overrideMode);
  }
  if (filters.scanId !== undefined) {
    conditions.push("g.scan_id = ?");
    params.push(filters.scanId);
  }
  if (filters.evaluationId !== undefined) {
    conditions.push("g.evaluation_id = ?");
    params.push(filters.evaluationId);
  }
  if (filters.traceAvailability !== undefined) {
    const linkedExists = "EXISTS (SELECT 1 FROM evaluation_phase_runs epr WHERE epr.evaluation_id = e.id)";
    conditions.push(filters.traceAvailability === "available" ? linkedExists : `NOT ${linkedExists}`);
  }
  if (filters.snapshotUse === "never") {
    conditions.push("su.grade_id IS NULL");
  } else if (filters.snapshotUse === "historical") {
    conditions.push("su.grade_id IS NOT NULL");
  } else if (filters.snapshotUse === "active") {
    const ids = activelyUsedIds ? Array.from(activelyUsedIds) : [];
    if (ids.length === 0) {
      conditions.push("1 = 0");
    } else {
      conditions.push(`g.id IN (${ids.map(() => "?").join(",")})`);
      params.push(...ids);
    }
  }
  if (filters.eligibilityReason !== undefined) {
    if (filters.eligibilityReason === "outside_lookback") {
      if (window === null) {
        conditions.push("1 = 0");
      } else {
        conditions.push("g.graded_at < ?");
        params.push(window.boundary);
      }
    } else {
      const ids: number[] = [];
      if (window !== null) {
        for (const [gradeId, classification] of window.byGradeId) {
          const reason = classification.status === "eligible" ? "eligible" : classification.reason;
          if (reason === filters.eligibilityReason) ids.push(gradeId);
        }
      }
      if (ids.length === 0) {
        conditions.push("1 = 0");
      } else {
        conditions.push(`g.id IN (${ids.map(() => "?").join(",")})`);
        params.push(...ids);
      }
    }
  }

  const baseWhere = conditions.length > 0 ? `WHERE ${conditions.join(" AND ")}` : "";

  const FROM_CLAUSE = `
    FROM grades g
    LEFT JOIN evaluations e ON e.id = g.evaluation_id
    LEFT JOIN posts p ON p.id = g.post_id
    LEFT JOIN grade_usage_overrides o ON o.grade_id = g.id
    LEFT JOIN (
      SELECT grade_id,
             MIN(json_extract(payload, '$.graded_at')) AS grade_time,
             MAX(recorded_at) AS last_edit_time,
             COUNT(*) AS revision_count
      FROM grade_revisions
      GROUP BY grade_id
    ) rm ON rm.grade_id = g.id
    LEFT JOIN (
      SELECT fsi.grade_id AS grade_id,
             MIN(fs.created_at) AS first_at,
             MAX(fs.created_at) AS last_at,
             COUNT(DISTINCT fsp.snapshot_id) AS use_count
      FROM feedback_snapshot_items fsi
      JOIN feedback_snapshot_phases fsp ON fsp.id = fsi.snapshot_phase_id
      JOIN feedback_snapshots fs ON fs.id = fsp.snapshot_id
      GROUP BY fsi.grade_id
    ) su ON su.grade_id = g.id
  `;

  const db = getDb();

  const totalRow = db
    .prepare(`SELECT COUNT(*) AS total ${FROM_CLAUSE} ${baseWhere}`)
    .get(...params) as { total: number };

  const pageConditions = [...conditions];
  const pageParams = [...params];
  if (filters.cursor !== undefined) {
    pageConditions.push(
      `(COALESCE(rm.last_edit_time, g.graded_at) < ? OR (COALESCE(rm.last_edit_time, g.graded_at) = ? AND g.id < ?))`
    );
    pageParams.push(filters.cursor.last_edit_time, filters.cursor.last_edit_time, filters.cursor.grade_id);
  }
  const pageWhere = pageConditions.length > 0 ? `WHERE ${pageConditions.join(" AND ")}` : "";

  const rows = db
    .prepare(
      `SELECT
         g.id AS grade_id,
         g.evaluation_id AS evaluation_id,
         g.post_id AS post_id,
         g.scan_id AS scan_id,
         e.project_key AS project_key,
         p.platform AS platform,
         e.posture AS posture,
         e.surface_status AS terminal_status,
         g.relevance_judgment AS relevance_judgment,
         g.action_judgment AS action_judgment,
         g.dimensions AS dimensions,
         g.schema_version AS schema_version,
         g.needs_regrade AS needs_regrade,
         g.graded_at AS graded_at,
         COALESCE(rm.grade_time, g.graded_at) AS grade_time,
         COALESCE(rm.last_edit_time, g.graded_at) AS last_edit_time,
         COALESCE(rm.revision_count, 0) AS revision_count,
         COALESCE(o.mode, 'auto') AS override_mode,
         o.reason AS override_reason,
         su.first_at AS snapshot_use_first_at,
         su.last_at AS snapshot_use_last_at,
         COALESCE(su.use_count, 0) AS snapshot_use_count
       ${FROM_CLAUSE}
       ${pageWhere}
       ORDER BY COALESCE(rm.last_edit_time, g.graded_at) DESC, g.id DESC
       LIMIT ?`
    )
    .all(...pageParams, limit + 1) as GradeListRow[];

  const hasMore = rows.length > limit;
  const page = rows.slice(0, limit);
  const last = page[page.length - 1];

  // Eligibility + active-use are computed once for the whole window/latest
  // snapshot above (when relevant filters are set) or lazily per-page here
  // (when no such filter narrowed the query, so the classifier hasn't run
  // yet but every row still needs its present-time reason/active-use flag).
  const eligibilityWindow = window ?? computeEligibilityWindow(asOf);
  const activeUseSet =
    activelyUsedIds ?? getActivelyUsedGradeIds(getLatestSnapshotRow());
  const linkedEvaluationIds = getEvaluationIdsWithLinkedPhaseRuns(
    page.flatMap((row) => (row.evaluation_id !== null ? [row.evaluation_id] : []))
  );

  const data: GradeExplorerRow[] = page.map((row) => {
    const traceAvailable = row.evaluation_id !== null && linkedEvaluationIds.has(row.evaluation_id);
    return {
      grade_id: row.grade_id,
      evaluation_id: row.evaluation_id,
      post_id: row.post_id,
      scan_id: row.scan_id,
      project_key: row.project_key,
      platform: row.platform,
      posture: row.posture,
      terminal_status: row.terminal_status,
      relevance_judgment: row.relevance_judgment,
      action_judgment: row.action_judgment,
      dimensions: parseDimensions(row.dimensions),
      schema_version: row.schema_version,
      needs_regrade: row.needs_regrade === 1,
      grade_time: row.grade_time,
      last_edit_time: row.last_edit_time,
      revision_count: row.revision_count,
      eligibility_reason: resolveEligibilityReason(eligibilityWindow, row.grade_id, row.graded_at),
      override_mode: row.override_mode as OverrideMode,
      override_reason: row.override_reason,
      snapshot_use_first_at: row.snapshot_use_first_at,
      snapshot_use_last_at: row.snapshot_use_last_at,
      snapshot_use_count: row.snapshot_use_count,
      snapshot_active_use: activeUseSet.has(row.grade_id),
      trace_available: traceAvailable,
      trace_unavailable_reason: traceAvailable ? null : TRACE_UNAVAILABLE_REASON,
    };
  });

  return {
    data,
    has_more: hasMore,
    next_cursor:
      hasMore && last
        ? encodeGradeListCursor({ last_edit_time: last.last_edit_time, grade_id: last.grade_id })
        : null,
    total_matching: totalRow.total,
  };
}

// --------------------------------------------------------------------------
// Grade detail
// --------------------------------------------------------------------------

interface GradeRow {
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
  dimensions: string | null;
  failure_note: string | null;
  factual_offending_claim: string | null;
  factual_disposition: "unsupported" | "contradicted" | null;
  factual_contradicting_evidence: string | null;
  context_missing_input: string | null;
  posture_should_have_been: string | null;
  implication_implied_claim: string | null;
  implication_missing_support: string | null;
}

function toGrade(row: GradeRow): Grade {
  return {
    ...row,
    dimensions: parseDimensions(row.dimensions),
  } as Grade;
}

export function getGradeById(gradeId: number): Grade | null {
  const db = getDb();
  const row = db.prepare("SELECT * FROM grades WHERE id = ?").get(gradeId) as GradeRow | undefined;
  return row ? toGrade(row) : null;
}

export function getGradeDetailByEvaluationId(
  evaluationId: number,
  asOf: string,
  paging: GradeDetailPagingOptions
): GradeDetailResponse | null {
  const db = getDb();
  const row = db
    .prepare(
      `SELECT id FROM grades WHERE evaluation_id = ?
       ORDER BY graded_at DESC, id DESC LIMIT 1`
    )
    .get(evaluationId) as { id: number } | undefined;
  return row ? getGradeDetail(row.id, asOf, paging) : null;
}

export function getGradeDetail(
  gradeId: number,
  asOf: string,
  paging: GradeDetailPagingOptions
): GradeDetailResponse | null {
  const db = getDb();
  const gradeRow = db.prepare("SELECT * FROM grades WHERE id = ?").get(gradeId) as
    | GradeRow
    | undefined;
  if (gradeRow === undefined) return null;
  const grade = toGrade(gradeRow);

  const post = db
    .prepare(
      `SELECT id, platform, author_name, author_id, content, url, created_at
       FROM posts WHERE id = ?`
    )
    .get(grade.post_id) as GradeDetailPostEvidence | undefined;
  if (post === undefined) {
    throw new Error(`grade ${gradeId} references missing post ${grade.post_id}`);
  }

  let evaluation: GradeDetailEvaluationEvidence | null = null;
  let draft: GradeDetailDraftEvidence | null = null;
  let projectKey: string | null = null;
  let posture: string | null = null;
  let terminalStatus: string | null = null;

  if (grade.evaluation_id !== null) {
    const evalRow = db
      .prepare(
        `SELECT id, relevant, score, reason, project_key, posture, surface_status, failure_reason
         FROM evaluations WHERE id = ?`
      )
      .get(grade.evaluation_id) as
      | (Omit<GradeDetailEvaluationEvidence, "relevant"> & { relevant: number })
      | undefined;
    if (evalRow !== undefined) {
      evaluation = { ...evalRow, relevant: evalRow.relevant === 1 };
      projectKey = evalRow.project_key;
      posture = evalRow.posture;
      terminalStatus = evalRow.surface_status;
    }

    const draftRow = db
      .prepare(
        `SELECT id, comment_text, posture, created_at FROM draft_comments WHERE evaluation_id = ?`
      )
      .get(grade.evaluation_id) as GradeDetailDraftEvidence | undefined;
    draft = draftRow ?? null;
  }

  let critique = null as GradeDetailResponse["evidence"]["critique"];
  if (grade.evaluation_id !== null) {
    const row = db
      .prepare(
        `SELECT id, verdict, feedback, created_at FROM critiques
         WHERE evaluation_id = ? ORDER BY id DESC LIMIT 1`
      )
      .get(grade.evaluation_id) as GradeDetailResponse["evidence"]["critique"] | undefined;
    critique = row ?? null;
  }
  if (critique === null && draft !== null) {
    const row = db
      .prepare(
        `SELECT id, verdict, feedback, created_at FROM critiques
         WHERE draft_id = ? ORDER BY id DESC LIMIT 1`
      )
      .get(draft.id) as GradeDetailResponse["evidence"]["critique"] | undefined;
    critique = row ?? null;
  }

  const overrideRow = db
    .prepare(`SELECT mode, reason, updated_at FROM grade_usage_overrides WHERE grade_id = ?`)
    .get(gradeId) as { mode: OverrideMode; reason: string | null; updated_at: string } | undefined;

  const window = computeEligibilityWindow(asOf);
  const eligibilityReason = resolveEligibilityReason(window, gradeId, grade.graded_at);

  const snapshotUseRow = db
    .prepare(
      `SELECT MIN(fs.created_at) AS first_at, MAX(fs.created_at) AS last_at,
              COUNT(DISTINCT fsp.snapshot_id) AS use_count
       FROM feedback_snapshot_items fsi
       JOIN feedback_snapshot_phases fsp ON fsp.id = fsi.snapshot_phase_id
       JOIN feedback_snapshots fs ON fs.id = fsp.snapshot_id
       WHERE fsi.grade_id = ?`
    )
    .get(gradeId) as { first_at: string | null; last_at: string | null; use_count: number };

  const latestSnapshot = getLatestSnapshotRow();
  const activeUse = getActivelyUsedGradeIds(latestSnapshot).has(gradeId);

  const revisionLimit = Math.min(paging.revisionLimit ?? DEFAULT_SUBPAGE_LIMIT, MAX_SUBPAGE_LIMIT);
  const revisionConditions = ["grade_id = ?"];
  const revisionParams: (string | number)[] = [gradeId];
  if (paging.revisionCursor !== undefined) {
    revisionConditions.push("revision < ?");
    revisionParams.push(paging.revisionCursor);
  }
  const revisionRows = db
    .prepare(
      `SELECT id, revision, schema_version, source, recorded_at, payload FROM grade_revisions
       WHERE ${revisionConditions.join(" AND ")}
       ORDER BY revision DESC
       LIMIT ?`
    )
    .all(...revisionParams, revisionLimit + 1) as GradeRevisionSummary[];
  const revisionHasMore = revisionRows.length > revisionLimit;
  const revisionPage = revisionRows.slice(0, revisionLimit);
  const revisionLast = revisionPage[revisionPage.length - 1];

  const snapshotUseLimit = Math.min(
    paging.snapshotUseLimit ?? DEFAULT_SUBPAGE_LIMIT,
    MAX_SUBPAGE_LIMIT
  );
  const snapshotUseHistoryConditions = ["fsi.grade_id = ?"];
  const snapshotUseHistoryParams: (string | number)[] = [gradeId];
  if (paging.snapshotUseCursor !== undefined) {
    snapshotUseHistoryConditions.push("(fsi.created_at < ? OR (fsi.created_at = ? AND fsi.id < ?))");
    snapshotUseHistoryParams.push(
      paging.snapshotUseCursor.created_at,
      paging.snapshotUseCursor.created_at,
      paging.snapshotUseCursor.id
    );
  }
  const snapshotUseHistoryRows = db
    .prepare(
      `SELECT fsi.id AS id, fsp.snapshot_id AS snapshot_id, fs.scan_id AS scan_id,
              fsp.phase AS phase, fsi.role AS role, fsi.reason AS reason,
              fs.mode AS mode, fsi.created_at AS created_at
       FROM feedback_snapshot_items fsi
       JOIN feedback_snapshot_phases fsp ON fsp.id = fsi.snapshot_phase_id
       JOIN feedback_snapshots fs ON fs.id = fsp.snapshot_id
       WHERE ${snapshotUseHistoryConditions.join(" AND ")}
       ORDER BY fsi.created_at DESC, fsi.id DESC
       LIMIT ?`
    )
    .all(...snapshotUseHistoryParams, snapshotUseLimit + 1) as Array<{
    id: number;
    snapshot_id: number;
    scan_id: number;
    phase: FeedbackPhase;
    role: "excluded" | "aggregate" | "example";
    reason: string | null;
    mode: FeedbackSnapshotMode;
    created_at: string;
  }>;
  const snapshotUseHistoryHasMore = snapshotUseHistoryRows.length > snapshotUseLimit;
  const snapshotUseHistoryPage = snapshotUseHistoryRows.slice(0, snapshotUseLimit);
  const snapshotUseHistoryLast = snapshotUseHistoryPage[snapshotUseHistoryPage.length - 1];

  const traceAvailable = grade.evaluation_id !== null && hasLinkedPhaseRuns(grade.evaluation_id);
  const phaseRuns: CursorPage<PhaseRunSummary> =
    grade.evaluation_id !== null
      ? listPhaseRunsForEvaluation(grade.evaluation_id, {
          limit: paging.phaseRunLimit,
          cursor: paging.phaseRunCursor,
        })
      : { data: [], has_more: false, next_cursor: null };

  return {
    grade,
    identity: {
      evaluation_id: grade.evaluation_id,
      post_id: grade.post_id,
      scan_id: grade.scan_id,
    },
    segment: {
      project_key: projectKey,
      platform: post.platform,
      posture,
      terminal_status: terminalStatus,
    },
    evidence: { post, evaluation, draft, critique },
    eligibility: {
      reason: eligibilityReason,
      policy_version: window?.policy.policy_version ?? null,
      resolved_lookback_days: window?.policy.lookback_days ?? null,
      resolved_max_grades: window?.policy.max_grades ?? null,
    },
    override: overrideRow ?? null,
    snapshot_use: {
      first_at: snapshotUseRow.first_at,
      last_at: snapshotUseRow.last_at,
      count: snapshotUseRow.use_count,
      active_use: activeUse,
    },
    trace_available: traceAvailable,
    trace_unavailable_reason: traceAvailable ? null : TRACE_UNAVAILABLE_REASON,
    revision_history: {
      data: revisionPage,
      has_more: revisionHasMore,
      next_cursor: revisionHasMore && revisionLast ? encodeRevisionCursor(revisionLast.revision) : null,
    },
    snapshot_use_history: {
      data: snapshotUseHistoryPage as SnapshotUseHistoryEntry[],
      has_more: snapshotUseHistoryHasMore,
      next_cursor:
        snapshotUseHistoryHasMore && snapshotUseHistoryLast
          ? encodeSnapshotUseHistoryCursor(snapshotUseHistoryLast.created_at, snapshotUseHistoryLast.id)
          : null,
    },
    phase_runs: phaseRuns,
  };
}

export type { GradeListCursor };
