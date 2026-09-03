import { z } from "zod";
import { EXPERIMENT_PHASES, EXPERIMENT_STATUSES } from "@/types/feedback-experiments";
import type { ExperimentListCursor } from "@/types/feedback-experiments";
import type { ExperimentRunCursor, ExperimentRunStatus } from "@/types/feedback-experiments";

const FILTER_KEYS = ["status", "phase", "limit", "cursor"] as const;

// Literal tuples (not the readonly array exports) so zod infers the exact
// ExperimentStatus/FeedbackPhase union rather than widening to `string`.
const STATUS_VALUES = ["queued", "running", "complete", "failed"] as const;
const PHASE_VALUES = ["relevance", "reply_draft", "critic"] as const;

export const experimentListFiltersSchema = z.object({
  status: z.enum(STATUS_VALUES).optional(),
  phase: z.enum(PHASE_VALUES).optional(),
  limit: z.coerce.number().int().positive().max(100).optional(),
  cursor: z.string().min(1).optional(),
});

export type ExperimentListFiltersInput = z.infer<typeof experimentListFiltersSchema>;

export type ParseResult<T> = { ok: true; data: T } | { ok: false; errors: string[] };

// Unlike lib/feedback-filters.ts's parseSearchParams (which treats an empty
// query value as "filter absent"), status/phase here are a closed allowlist
// over stored lifecycle/phase enums — an explicit but empty, repeated, or
// unrecognized value is caller error and must 400 rather than silently
// falling back to unfiltered or last-value-wins.
export function parseExperimentListSearchParams(
  searchParams: URLSearchParams
): ParseResult<ExperimentListFiltersInput> {
  const errors: string[] = [];
  const obj: Record<string, string> = {};

  for (const key of FILTER_KEYS) {
    const values = searchParams.getAll(key);
    if (values.length === 0) continue;
    if (values.length > 1) {
      errors.push(`${key}: must not be repeated`);
      continue;
    }
    if (values[0] === "") {
      errors.push(`${key}: must not be empty`);
      continue;
    }
    obj[key] = values[0];
  }

  if (errors.length > 0) return { ok: false, errors };

  const result = experimentListFiltersSchema.safeParse(obj);
  if (!result.success) {
    return { ok: false, errors: result.error.issues.map((i) => `${i.path.join(".")}: ${i.message}`) };
  }
  return { ok: true, data: result.data };
}

// --- Opaque cursor codec — base64url(JSON), filter-bound. The cursor
// carries the normalized status/phase filters that produced it so a
// cursor minted under one filter combination can never be replayed
// against a different one (silently skipping or duplicating rows). ---

function encodeCursor(value: unknown): string {
  return Buffer.from(JSON.stringify(value), "utf8").toString("base64url");
}

function decodeCursor(raw: string): unknown | null {
  try {
    const json = Buffer.from(raw, "base64url").toString("utf8");
    return JSON.parse(json);
  } catch {
    return null;
  }
}

export function encodeExperimentListCursor(cursor: ExperimentListCursor): string {
  return encodeCursor(cursor);
}

export function decodeExperimentListCursor(raw: string): ExperimentListCursor | null {
  const value = decodeCursor(raw);
  if (value === null || typeof value !== "object") return null;
  const obj = value as Record<string, unknown>;

  if (typeof obj.created_at !== "string" || !Number.isFinite(Date.parse(obj.created_at))) return null;
  if (typeof obj.id !== "number" || !Number.isInteger(obj.id) || obj.id <= 0) return null;
  if (!("status" in obj) || !("phase" in obj)) return null;

  const status = obj.status;
  if (status !== null && !EXPERIMENT_STATUSES.includes(status as (typeof EXPERIMENT_STATUSES)[number])) {
    return null;
  }
  const phase = obj.phase;
  if (phase !== null && !EXPERIMENT_PHASES.includes(phase as (typeof EXPERIMENT_PHASES)[number])) {
    return null;
  }

  return {
    created_at: obj.created_at,
    id: obj.id,
    status: status as ExperimentListCursor["status"],
    phase: phase as ExperimentListCursor["phase"],
  };
}

// True when a decoded cursor's bound filters match the request's own
// normalized filters — the boundary check that rejects reusing a cursor
// across a different filter combination.
export function cursorMatchesFilters(
  cursor: ExperimentListCursor,
  filters: { status?: string; phase?: string }
): boolean {
  return cursor.status === (filters.status ?? null) && cursor.phase === (filters.phase ?? null);
}

const RUN_STATUS_VALUES = ["queued", "running", "complete", "partial", "failed"] as const;
const runListFiltersSchema = z.object({
  status: z.enum(RUN_STATUS_VALUES).optional(),
  phase: z.enum(PHASE_VALUES).optional(),
  limit: z.coerce.number().int().positive().max(100).optional(),
  cursor: z.string().min(1).optional(),
});

export function parseExperimentRunListSearchParams(searchParams: URLSearchParams): ParseResult<{
  status?: ExperimentRunStatus;
  phase?: (typeof PHASE_VALUES)[number];
  limit?: number;
  cursor?: string;
}> {
  const errors: string[] = [];
  const obj: Record<string, string> = {};
  for (const key of FILTER_KEYS) {
    const values = searchParams.getAll(key);
    if (values.length > 1) errors.push(`${key}: must not be repeated`);
    else if (values.length === 1 && values[0] === "") errors.push(`${key}: must not be empty`);
    else if (values.length === 1) obj[key] = values[0];
  }
  if (errors.length > 0) return { ok: false, errors };
  const parsed = runListFiltersSchema.safeParse(obj);
  return parsed.success
    ? { ok: true, data: parsed.data }
    : { ok: false, errors: parsed.error.issues.map((issue) => `${issue.path.join(".")}: ${issue.message}`) };
}

export function encodeExperimentRunCursor(cursor: ExperimentRunCursor): string {
  return encodeCursor(cursor);
}

export function decodeExperimentRunCursor(raw: string): ExperimentRunCursor | null {
  const value = decodeCursor(raw);
  if (value === null || typeof value !== "object") return null;
  const obj = value as Record<string, unknown>;
  if (obj.version !== 1 || typeof obj.created_at !== "string" || !Number.isFinite(Date.parse(obj.created_at))) return null;
  if (typeof obj.id !== "number" || !Number.isInteger(obj.id) || obj.id <= 0) return null;
  if (!("status" in obj) || !("phase" in obj)) return null;
  if (obj.status !== null && !RUN_STATUS_VALUES.includes(obj.status as (typeof RUN_STATUS_VALUES)[number])) return null;
  if (obj.phase !== null && !PHASE_VALUES.includes(obj.phase as (typeof PHASE_VALUES)[number])) return null;
  if (Object.keys(obj).some((key) => !["version", "created_at", "id", "status", "phase"].includes(key))) return null;
  return obj as unknown as ExperimentRunCursor;
}

export function runCursorMatchesFilters(
  cursor: ExperimentRunCursor,
  filters: { status?: string; phase?: string }
): boolean {
  return cursor.status === (filters.status ?? null) && cursor.phase === (filters.phase ?? null);
}
