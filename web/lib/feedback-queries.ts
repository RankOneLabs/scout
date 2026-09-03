import { getDb } from "@/lib/db";
import { encodeFeedbackItemCursor, encodeSnapshotListCursor } from "@/lib/feedback-filters";
import { listPhaseRunsForSnapshotPhase } from "@/lib/feedback-phase-run-queries";
import type { PhaseRunCursor } from "@/types/feedback-grades";
import {
  FEEDBACK_PHASES,
  type FeedbackExclusionReason,
  type FeedbackGradeUse,
  type FeedbackGradeUsePage,
  type FeedbackItemCursor,
  type FeedbackItemRole,
  type FeedbackPhase,
  type FeedbackPhaseSummary,
  type FeedbackSnapshotDetail,
  type FeedbackSnapshotHeader,
  type FeedbackSnapshotMode,
  type FeedbackUseFilter,
  type GradeRevisionMeta,
  type ScanFeedbackSummary,
  type SnapshotListCursor,
  type SnapshotListItem,
  type SnapshotListPage,
} from "@/types/feedback";

const DEFAULT_SNAPSHOT_LIMIT = 25;
const MAX_SNAPSHOT_LIMIT = 100;
const DEFAULT_ITEM_LIMIT = 100;
const MAX_ITEM_LIMIT = 200;
const DEFAULT_PHASE: FeedbackPhase = "relevance";
const DEFAULT_USE_FILTER: FeedbackUseFilter = "all";

// Grades not selected as an example sort after every ranked example within
// the same primary_use group — this sentinel must exceed any real
// example_rank (bounded by the tiny FEEDBACK_*_EXAMPLE_LIMIT config values).
const UNRANKED_NOTE_SENTINEL = 999999999;

const SORT_RANK_EXPR = "(CASE WHEN item.is_included = 1 THEN 0 ELSE 1 END)";
const NOTE_RANK_EXPR = `(CASE WHEN item.example_rank IS NOT NULL THEN item.example_rank ELSE ${UNRANKED_NOTE_SENTINEL} END)`;

interface SnapshotRow {
  id: number;
  scan_id: number;
  policy_version: string;
  mode: string;
  as_of: string;
  created_at: string;
  lookback_days: number;
  max_grades: number;
  segment_min_grades: number;
  note_max_chars: number;
  population_count: number;
  eligible_count: number;
  excluded_count: number;
}

function toSnapshotListItem(row: SnapshotRow): SnapshotListItem {
  return {
    snapshot_id: row.id,
    scan_id: row.scan_id,
    policy_version: row.policy_version,
    mode: row.mode as FeedbackSnapshotMode,
    as_of: row.as_of,
    created_at: row.created_at,
    population_count: row.population_count,
    eligible_count: row.eligible_count,
    excluded_count: row.excluded_count,
  };
}

function lookbackStartedAt(asOf: string, lookbackDays: number): string {
  const startMs = Date.parse(asOf) - lookbackDays * 24 * 60 * 60 * 1000;
  return new Date(startMs).toISOString();
}

export interface ListFeedbackSnapshotsFilters {
  scanId?: number;
  mode?: FeedbackSnapshotMode;
  limit?: number;
  cursor?: SnapshotListCursor;
}

// Ordered created_at DESC, id DESC — a stable compound key so new scans
// landing mid-page-through never duplicate or skip a row (unlike a plain
// offset, which would).
export function listFeedbackSnapshots(filters: ListFeedbackSnapshotsFilters): SnapshotListPage {
  const limit = Math.min(filters.limit ?? DEFAULT_SNAPSHOT_LIMIT, MAX_SNAPSHOT_LIMIT);

  const conditions: string[] = [];
  const params: (string | number)[] = [];

  if (filters.scanId !== undefined) {
    conditions.push("scan_id = ?");
    params.push(filters.scanId);
  }
  if (filters.mode !== undefined) {
    conditions.push("mode = ?");
    params.push(filters.mode);
  }
  if (filters.cursor !== undefined) {
    conditions.push("(created_at < ? OR (created_at = ? AND id < ?))");
    params.push(filters.cursor.created_at, filters.cursor.created_at, filters.cursor.snapshot_id);
  }

  const where = conditions.length > 0 ? `WHERE ${conditions.join(" AND ")}` : "";
  const db = getDb();
  const rows = db
    .prepare(
      `SELECT id, scan_id, policy_version, mode, as_of, created_at,
              lookback_days, max_grades, segment_min_grades, note_max_chars,
              population_count, eligible_count, excluded_count
       FROM feedback_snapshots
       ${where}
       ORDER BY created_at DESC, id DESC
       LIMIT ?`
    )
    .all(...params, limit + 1) as SnapshotRow[];

  const hasMore = rows.length > limit;
  const page = rows.slice(0, limit);
  const last = page[page.length - 1];

  return {
    data: page.map(toSnapshotListItem),
    has_more: hasMore,
    next_cursor:
      hasMore && last
        ? encodeSnapshotListCursor({ created_at: last.created_at, snapshot_id: last.id })
        : null,
  };
}

interface PhaseRow {
  id: number;
  phase: FeedbackPhase;
  token_budget: number;
  token_estimate: number;
  truncated: number;
  structured_summary: string;
  rendered_text: string;
  rendered_sha256: string;
}

interface PhaseCountsRow {
  included_count: number | null;
  excluded_count: number | null;
  example_count: number | null;
}

function getPhaseCounts(snapshotPhaseId: number): {
  included_count: number;
  excluded_count: number;
  example_count: number;
} {
  const db = getDb();
  const row = db
    .prepare(
      `SELECT
         SUM(CASE WHEN role = 'aggregate' THEN 1 ELSE 0 END) AS included_count,
         SUM(CASE WHEN role = 'excluded' THEN 1 ELSE 0 END) AS excluded_count,
         SUM(CASE WHEN role = 'example' THEN 1 ELSE 0 END) AS example_count
       FROM feedback_snapshot_items WHERE snapshot_phase_id = ?`
    )
    .get(snapshotPhaseId) as PhaseCountsRow;
  return {
    included_count: row.included_count ?? 0,
    excluded_count: row.excluded_count ?? 0,
    example_count: row.example_count ?? 0,
  };
}

interface ItemRow {
  grade_id: number;
  post_id: number;
  graded_at: string;
  roles_concat: string;
  exclusion_reason: string | null;
  selection_reasons_concat: string;
  pinned_revision_id: number;
  is_included: number;
  example_rank: number | null;
  pinned_revision_payload: string;
  pinned_revision_recorded_at: string;
}

function toFeedbackGradeUse(row: ItemRow): FeedbackGradeUse {
  const roles = Array.from(new Set(row.roles_concat.split(","))) as FeedbackItemRole[];
  const exclusionReasons: FeedbackExclusionReason[] =
    row.exclusion_reason !== null ? [row.exclusion_reason as FeedbackExclusionReason] : [];
  const selectionReasons = Array.from(new Set(row.selection_reasons_concat.split(",")));
  return {
    grade_id: row.grade_id,
    post_id: row.post_id,
    graded_at: row.graded_at,
    primary_use: row.is_included === 1 ? "included" : "excluded",
    roles,
    selected_note: row.example_rank !== null,
    example_rank: row.example_rank,
    exclusion_reasons: exclusionReasons,
    selection_reasons: selectionReasons,
    pinned_grade_revision_id: row.pinned_revision_id,
    pinned_grade_revision_payload: row.pinned_revision_payload,
    pinned_grade_revision_recorded_at: row.pinned_revision_recorded_at,
  };
}

export interface FeedbackItemFilters {
  use?: FeedbackUseFilter;
  limit?: number;
  cursor?: FeedbackItemCursor;
}

// Normalizes feedback_snapshot_items to one row per grade for this phase
// (a grade is either wholly excluded, or in the aggregate population and
// optionally also selected as an example — never both), then orders
// included before excluded, selected-note rank, graded_at desc, grade_id
// desc, with a matching opaque keyset cursor.
function getFeedbackGradeUsePage(
  phase: FeedbackPhase,
  snapshotPhaseId: number,
  filters: FeedbackItemFilters
): FeedbackGradeUsePage {
  const useFilter = filters.use ?? DEFAULT_USE_FILTER;
  const limit = Math.min(filters.limit ?? DEFAULT_ITEM_LIMIT, MAX_ITEM_LIMIT);

  const whereParts: string[] = [];
  const params: (string | number)[] = [snapshotPhaseId];

  if (useFilter === "included") {
    whereParts.push("item.is_included = 1");
  } else if (useFilter === "excluded") {
    whereParts.push("item.is_included = 0");
  }

  if (filters.cursor) {
    const c = filters.cursor;
    const sortRank = c.primary_use === "included" ? 0 : 1;
    const noteRank =
      c.selected_note && c.example_rank !== null ? c.example_rank : UNRANKED_NOTE_SENTINEL;
    whereParts.push(`(
      ${SORT_RANK_EXPR} > ?
      OR (${SORT_RANK_EXPR} = ? AND ${NOTE_RANK_EXPR} > ?)
      OR (${SORT_RANK_EXPR} = ? AND ${NOTE_RANK_EXPR} = ? AND item.graded_at < ?)
      OR (${SORT_RANK_EXPR} = ? AND ${NOTE_RANK_EXPR} = ? AND item.graded_at = ? AND item.grade_id < ?)
    )`);
    params.push(
      sortRank,
      sortRank,
      noteRank,
      sortRank,
      noteRank,
      c.graded_at,
      sortRank,
      noteRank,
      c.graded_at,
      c.grade_id
    );
  }

  const where = whereParts.length > 0 ? `WHERE ${whereParts.join(" AND ")}` : "";

  const db = getDb();
  const rows = db
    .prepare(
      `WITH merged AS (
       SELECT
           fsi.grade_id AS grade_id,
           GROUP_CONCAT(DISTINCT fsi.role) AS roles_concat,
           MAX(CASE WHEN fsi.role = 'excluded' THEN fsi.reason END) AS exclusion_reason,
           GROUP_CONCAT(DISTINCT fsi.selection_reason) AS selection_reasons_concat,
           MAX(fsi.grade_revision_id) AS pinned_revision_id,
           MAX(CASE WHEN fsi.role IN ('aggregate', 'example') THEN 1 ELSE 0 END) AS is_included,
           MAX(CASE WHEN fsi.role = 'example' THEN fsi.rank END) AS example_rank
         FROM feedback_snapshot_items fsi
         WHERE fsi.snapshot_phase_id = ?
         GROUP BY fsi.grade_id
       ), item AS (
         SELECT
           merged.*,
           CAST(json_extract(gr.payload, '$.post_id') AS INTEGER) AS post_id,
           CAST(json_extract(gr.payload, '$.graded_at') AS TEXT) AS graded_at,
           gr.payload AS pinned_revision_payload,
           gr.recorded_at AS pinned_revision_recorded_at
         FROM merged
         JOIN grade_revisions gr ON gr.id = merged.pinned_revision_id
       )
       SELECT
         item.grade_id AS grade_id,
         item.post_id AS post_id,
         item.graded_at AS graded_at,
         item.roles_concat AS roles_concat,
         item.exclusion_reason AS exclusion_reason,
         item.selection_reasons_concat AS selection_reasons_concat,
         item.pinned_revision_id AS pinned_revision_id,
         item.is_included AS is_included,
         item.example_rank AS example_rank,
         item.pinned_revision_payload AS pinned_revision_payload,
         item.pinned_revision_recorded_at AS pinned_revision_recorded_at
       FROM item
       ${where}
       ORDER BY
         ${SORT_RANK_EXPR} ASC,
         ${NOTE_RANK_EXPR} ASC,
         item.graded_at DESC,
         item.grade_id DESC
       LIMIT ?`
    )
    .all(...params, limit + 1) as ItemRow[];

  const hasMore = rows.length > limit;
  const page = rows.slice(0, limit);
  const last = page[page.length - 1];

  return {
    phase,
    use_filter: useFilter,
    data: page.map(toFeedbackGradeUse),
    has_more: hasMore,
    next_cursor:
      hasMore && last
        ? encodeFeedbackItemCursor({
            primary_use: last.is_included === 1 ? "included" : "excluded",
            selected_note: last.example_rank !== null,
            example_rank: last.example_rank,
            graded_at: last.graded_at,
            grade_id: last.grade_id,
          })
        : null,
  };
}

export interface GetFeedbackSnapshotDetailOptions {
  phase?: FeedbackPhase;
  use?: FeedbackUseFilter;
  itemLimit?: number;
  itemCursor?: FeedbackItemCursor;
  consumerLimit?: number;
  consumerCursor?: PhaseRunCursor;
}

// Returns the snapshot header and all three phase summaries, plus one
// paginated normalized item page for the requested phase (default
// relevance) — the public endpoint set stays fixed while the UI loads
// each phase's items independently.
export function getFeedbackSnapshotDetail(
  snapshotId: number,
  options: GetFeedbackSnapshotDetailOptions
): FeedbackSnapshotDetail | null {
  const db = getDb();
  const header = db
    .prepare(
      `SELECT id, scan_id, policy_version, mode, as_of, created_at,
              lookback_days, max_grades, segment_min_grades, note_max_chars,
              population_count, eligible_count, excluded_count
       FROM feedback_snapshots WHERE id = ?`
    )
    .get(snapshotId) as SnapshotRow | undefined;
  if (header === undefined) return null;

  const phaseRows = db
    .prepare(
      `SELECT id, phase, token_budget, token_estimate, truncated,
              structured_summary, rendered_text, rendered_sha256
       FROM feedback_snapshot_phases WHERE snapshot_id = ?`
    )
    .all(snapshotId) as PhaseRow[];
  const phaseRowByName = new Map(phaseRows.map((r) => [r.phase, r]));

  const phases = {} as Record<FeedbackPhase, FeedbackPhaseSummary>;
  for (const phaseName of FEEDBACK_PHASES) {
    const row = phaseRowByName.get(phaseName);
    if (!row) {
      throw new Error(`feedback snapshot ${snapshotId} is missing its ${phaseName} phase row`);
    }
    const counts = getPhaseCounts(row.id);
    phases[phaseName] = {
      snapshot_phase_id: row.id,
      phase: phaseName,
      token_budget: row.token_budget,
      token_estimate: row.token_estimate,
      truncated: row.truncated === 1,
      structured_summary: row.structured_summary,
      rendered_text: row.rendered_text,
      rendered_sha256: row.rendered_sha256,
      counts: {
        included_count: counts.included_count,
        excluded_count: counts.excluded_count,
        example_count: counts.example_count,
        actually_used_count: header.mode === "active" ? counts.included_count : 0,
      },
    };
  }

  const requestedPhase = options.phase ?? DEFAULT_PHASE;
  const requestedPhaseRow = phaseRowByName.get(requestedPhase);
  if (!requestedPhaseRow) {
    throw new Error(`feedback snapshot ${snapshotId} is missing its ${requestedPhase} phase row`);
  }
  const items = getFeedbackGradeUsePage(requestedPhase, requestedPhaseRow.id, {
    use: options.use,
    limit: options.itemLimit,
    cursor: options.itemCursor,
  });
  const consumerPage = listPhaseRunsForSnapshotPhase(requestedPhaseRow.id, {
    limit: options.consumerLimit,
    cursor: options.consumerCursor,
  });

  const snapshot: FeedbackSnapshotHeader = {
    ...toSnapshotListItem(header),
    lookback_days: header.lookback_days,
    lookback_started_at: lookbackStartedAt(header.as_of, header.lookback_days),
    max_grades: header.max_grades,
    segment_min_grades: header.segment_min_grades,
    note_max_chars: header.note_max_chars,
  };

  return {
    snapshot,
    phases,
    items,
    consumers: { phase: requestedPhase, ...consumerPage },
  };
}

interface ScanSnapshotRow {
  id: number;
  policy_version: string;
  mode: string;
  population_count: number;
  eligible_count: number;
  excluded_count: number;
}

// One snapshot per scan (feedback_snapshots.scan_id is UNIQUE) — null for
// scans that predate cohort 2 or that failed before the snapshot write.
export function getFeedbackSnapshotSummaryForScan(scanId: number): ScanFeedbackSummary | null {
  const db = getDb();
  const row = db
    .prepare(
      `SELECT id, policy_version, mode, population_count, eligible_count, excluded_count
       FROM feedback_snapshots WHERE scan_id = ?`
    )
    .get(scanId) as ScanSnapshotRow | undefined;
  if (row === undefined) return null;
  return {
    snapshot_id: row.id,
    policy_version: row.policy_version,
    mode: row.mode as FeedbackSnapshotMode,
    population_count: row.population_count,
    eligible_count: row.eligible_count,
    excluded_count: row.excluded_count,
  };
}

interface RevisionMetaRow {
  revision_count: number;
  latest_recorded_at: string | null;
}

export function getGradeRevisionMeta(gradeId: number): GradeRevisionMeta | null {
  const db = getDb();
  const row = db
    .prepare(
      `SELECT COUNT(*) AS revision_count, MAX(recorded_at) AS latest_recorded_at
       FROM grade_revisions WHERE grade_id = ?`
    )
    .get(gradeId) as RevisionMetaRow;
  if (row.revision_count === 0 || row.latest_recorded_at === null) return null;
  return { revision_count: row.revision_count, latest_recorded_at: row.latest_recorded_at };
}

interface RevisionMetaBatchRow {
  grade_id: number;
  revision_count: number;
  latest_recorded_at: string;
}

export function getGradeRevisionMetaBatch(gradeIds: number[]): Map<number, GradeRevisionMeta> {
  const result = new Map<number, GradeRevisionMeta>();
  if (gradeIds.length === 0) return result;
  const db = getDb();
  const placeholders = gradeIds.map(() => "?").join(",");
  const rows = db
    .prepare(
      `SELECT grade_id, COUNT(*) AS revision_count, MAX(recorded_at) AS latest_recorded_at
       FROM grade_revisions WHERE grade_id IN (${placeholders}) GROUP BY grade_id`
    )
    .all(...gradeIds) as RevisionMetaBatchRow[];
  for (const row of rows) {
    result.set(row.grade_id, {
      revision_count: row.revision_count,
      latest_recorded_at: row.latest_recorded_at,
    });
  }
  return result;
}
