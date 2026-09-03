import { getTracesDb } from "./traces-db";
import { getDb } from "./db";
import type { TraceListItem, TraceSpan, TraceFilters } from "@/types/schema";
import type { ExperimentStatus, TraceComparisonBacklink } from "@/types/traces";

function safeParseJson(raw: string | null): unknown {
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export function getTraces(filters?: TraceFilters): TraceListItem[] {
  const db = getTracesDb();
  const limit = filters?.limit ?? 100;

  let query = `
    SELECT
      s.trace_id,
      s.name,
      s.started_at,
      s.duration_ms,
      (SELECT COUNT(*) FROM spans c WHERE c.trace_id = s.trace_id AND c.id != s.id) AS step_count,
      CASE WHEN s.error IS NOT NULL THEN 1 ELSE 0 END AS has_error,
      s.error
    FROM spans s
    WHERE s.kind = 'pipeline_run' AND s.parent_id IS NULL
  `;

  const params: unknown[] = [];

  if (filters?.error_only) {
    query += " AND s.error IS NOT NULL";
  }

  query += " ORDER BY s.started_at DESC LIMIT ?";
  params.push(limit);

  const rows = db.prepare(query).all(...params) as Array<{
    trace_id: string;
    name: string;
    started_at: string;
    duration_ms: number | null;
    step_count: number;
    has_error: number;
    error: string | null;
  }>;

  return rows.map((row) => ({
    ...row,
    has_error: row.has_error === 1,
  }));
}

export function getTraceSpans(traceId: string): TraceSpan[] {
  const db = getTracesDb();

  const rows = db
    .prepare(
      `SELECT id, trace_id, parent_id, kind, name, input, output,
              started_at, ended_at, duration_ms, metadata, error,
              usage_input_tokens, usage_output_tokens, usage_cost
       FROM spans
       WHERE trace_id = ?
       ORDER BY started_at ASC`
    )
    .all(traceId) as Array<{
    id: string;
    trace_id: string;
    parent_id: string | null;
    kind: string;
    name: string;
    input: string | null;
    output: string | null;
    started_at: string;
    ended_at: string | null;
    duration_ms: number | null;
    metadata: string | null;
    error: string | null;
    usage_input_tokens: number | null;
    usage_output_tokens: number | null;
    usage_cost: number | null;
  }>;

  return rows.map((row) => ({
    ...row,
    input: safeParseJson(row.input),
    output: safeParseJson(row.output),
    metadata: safeParseJson(row.metadata),
  }));
}

interface TraceComparisonBacklinkRow {
  comparison_id: number;
  experiment_id: number;
  experiment_name: string;
  experiment_status: string;
  role: "baseline" | "candidate";
}

// Trace Detail's second backlink relationship set: zero or more replay
// comparisons that used this trace as either side. Resolved only by the
// durable first-class comparison trace ids persisted at comparison time
// (trace_a_id = baseline, trace_b_id = candidate) — never
// by prompt hash, post identity, model, or time proximity. Independent of
// the single evaluation_phase_runs backlink: a baseline trace may be
// replayed into many experiments.
export function getTraceComparisonBacklinks(traceId: string): TraceComparisonBacklink[] {
  const db = getDb();
  const rows = db
    .prepare(
      `SELECT
         tc.id AS comparison_id, tc.experiment_id AS experiment_id,
         er.name AS experiment_name, e.status AS experiment_status,
         CASE WHEN tc.trace_a_id = ? THEN 'baseline' ELSE 'candidate' END AS role
       FROM trace_comparisons tc
       JOIN evaluation_experiments e ON e.id = tc.experiment_id
       JOIN experiment_runs er ON er.id = e.experiment_run_id
       WHERE tc.trace_a_id = ? OR tc.trace_b_id = ?
       ORDER BY tc.created_at DESC, tc.id DESC`
    )
    .all(traceId, traceId, traceId) as TraceComparisonBacklinkRow[];

  return rows.map((row) => ({
    experiment_id: row.experiment_id,
    comparison_id: row.comparison_id,
    role: row.role,
    experiment_name: row.experiment_name,
    experiment_status: row.experiment_status as ExperimentStatus,
    experiment_url: `/feedback/experiments/${row.experiment_id}`,
  }));
}
