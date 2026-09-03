import { getDb } from "@/lib/db";
import { encodePhaseRunCursor } from "@/lib/feedback-phase-run-filters";
import type { FeedbackPhaseRunConsumer } from "@/types/feedback";
import type {
  CursorPage,
  PhaseRunCursor,
  PhaseRunDetail,
  PhaseRunSummary,
} from "@/types/feedback-grades";

const DEFAULT_LIMIT = 25;
const MAX_LIMIT = 100;

interface PhaseRunRow {
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

const PHASE_RUN_DETAIL_SELECT = `
  SELECT epr.id, epr.scan_id, epr.post_id, epr.evaluation_id,
         g.id AS grade_id, fsp.snapshot_id AS snapshot_id,
         epr.snapshot_phase_id, epr.phase, epr.trace_id, epr.model, epr.status, epr.created_at
  FROM evaluation_phase_runs epr
  JOIN feedback_snapshot_phases fsp ON fsp.id = epr.snapshot_phase_id
  LEFT JOIN grades g ON g.evaluation_id = epr.evaluation_id
`;

// Stored-key identity for GET /api/feedback/phase-runs/{phaseRunId} — never
// duplicates prompt content or grade evidence, which remain in the Jig
// trace store and feedback_snapshot_phases respectively.
export function getPhaseRunDetail(phaseRunId: number): PhaseRunDetail | null {
  const db = getDb();
  const row = db.prepare(`${PHASE_RUN_DETAIL_SELECT} WHERE epr.id = ?`).get(phaseRunId) as
    | PhaseRunRow
    | undefined;
  return row ?? null;
}

// The one approved unambiguous Trace Detail backlink: resolves a phase run
// by its exact trace_id, never by prompt hash, post identity, model, or
// time proximity.
export function getPhaseRunByTraceId(traceId: string): PhaseRunDetail | null {
  const db = getDb();
  const row = db.prepare(`${PHASE_RUN_DETAIL_SELECT} WHERE epr.trace_id = ?`).get(traceId) as
    | PhaseRunRow
    | undefined;
  return row ?? null;
}

// True when at least one phase run has been linked to this evaluation —
// the historical-trace-unavailable signal for evaluations that predate
// phase-run linkage (or whose contributor phase runs never made it to a
// durable trace).
export function hasLinkedPhaseRuns(evaluationId: number): boolean {
  const db = getDb();
  const row = db
    .prepare(`SELECT 1 FROM evaluation_phase_runs WHERE evaluation_id = ? LIMIT 1`)
    .get(evaluationId);
  return row !== undefined;
}

// Batched form of hasLinkedPhaseRuns for a page of rows — one query for
// the whole page instead of one per row (used by listGrades, which would
// otherwise issue an N+1 query per returned grade).
export function getEvaluationIdsWithLinkedPhaseRuns(evaluationIds: number[]): Set<number> {
  if (evaluationIds.length === 0) return new Set();
  const db = getDb();
  const placeholders = evaluationIds.map(() => "?").join(",");
  const rows = db
    .prepare(
      `SELECT DISTINCT evaluation_id FROM evaluation_phase_runs WHERE evaluation_id IN (${placeholders})`
    )
    .all(...evaluationIds) as Array<{ evaluation_id: number }>;
  return new Set(rows.map((r) => r.evaluation_id));
}

function pageQuery<T>(
  selectColumns: string,
  whereColumn: string,
  whereValue: number,
  paging: { limit?: number; cursor?: PhaseRunCursor }
): CursorPage<T> {
  const limit = Math.min(paging.limit ?? DEFAULT_LIMIT, MAX_LIMIT);
  const db = getDb();
  const conditions = [`${whereColumn} = ?`];
  const params: (string | number)[] = [whereValue];
  if (paging.cursor !== undefined) {
    conditions.push("(created_at < ? OR (created_at = ? AND id < ?))");
    params.push(paging.cursor.created_at, paging.cursor.created_at, paging.cursor.id);
  }
  const rows = db
    .prepare(
      `SELECT ${selectColumns} FROM evaluation_phase_runs
       WHERE ${conditions.join(" AND ")}
       ORDER BY created_at DESC, id DESC
       LIMIT ?`
    )
    .all(...params, limit + 1) as Array<T & { created_at: string; id: number }>;
  const hasMore = rows.length > limit;
  const page = rows.slice(0, limit);
  const last = page[page.length - 1];
  return {
    data: page,
    has_more: hasMore,
    next_cursor: hasMore && last ? encodePhaseRunCursor(last.created_at, last.id) : null,
  };
}

// Grade Detail's phase-run history: independent stable paging by
// created_at DESC, id DESC — this evaluation's own cursor space, never
// affected by another evaluation's or another phase's paging.
export function listPhaseRunsForEvaluation(
  evaluationId: number,
  paging: { limit?: number; cursor?: PhaseRunCursor }
): CursorPage<PhaseRunSummary> {
  return pageQuery<PhaseRunSummary>(
    "id, phase, trace_id, created_at",
    "evaluation_id",
    evaluationId,
    paging
  );
}

// Feedback snapshot phase detail's consumer listing: every phase run whose
// prompt was governed by this one feedback_snapshot_phases row, paginated
// independently of every other phase — a stable cursor keyed to this one
// snapshot_phase_id, so one phase's navigation never skips or repeats rows
// because of another phase's concurrent inserts.
export function listPhaseRunsForSnapshotPhase(
  snapshotPhaseId: number,
  paging: { limit?: number; cursor?: PhaseRunCursor }
): CursorPage<FeedbackPhaseRunConsumer> {
  return pageQuery<FeedbackPhaseRunConsumer>(
    "id, scan_id, post_id, evaluation_id, " +
      "(SELECT id FROM grades WHERE evaluation_id = evaluation_phase_runs.evaluation_id) " +
      "AS grade_id, phase, trace_id, status, created_at",
    "snapshot_phase_id",
    snapshotPhaseId,
    paging
  );
}
