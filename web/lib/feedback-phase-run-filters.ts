import { z } from "zod";
import { parseSearchParams } from "@/lib/feedback-filters";
import type { PhaseRunCursor } from "@/types/feedback-grades";

export { parseSearchParams };

// Query params for a snapshot phase's consumer listing — the phase runs
// whose prompt was governed by one feedback_snapshot_phases row.
export const phaseRunConsumerFiltersSchema = z.object({
  limit: z.coerce.number().int().min(1).max(100).optional(),
  cursor: z.string().min(1).optional(),
});

export type PhaseRunConsumerFiltersInput = z.infer<typeof phaseRunConsumerFiltersSchema>;

// --- Opaque cursor codec — base64url(JSON), never constructed/inspected
// by clients. Shared by grade-detail phase_runs paging and snapshot-phase
// consumer paging: both order evaluation_phase_runs by created_at DESC,
// id DESC, so both use this one compound cursor shape. Decoding failures
// (malformed base64, invalid JSON, wrong shape) return null so the route
// can 400 rather than silently page from the start. ---

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

export function encodePhaseRunCursor(createdAt: string, id: number): string {
  return encodeCursor({ created_at: createdAt, id });
}

export function decodePhaseRunCursor(raw: string): PhaseRunCursor | null {
  const value = decodeCursor(raw);
  if (value === null || typeof value !== "object") return null;
  const obj = value as Record<string, unknown>;
  if (
    typeof obj.created_at !== "string" ||
    !Number.isFinite(Date.parse(obj.created_at)) ||
    typeof obj.id !== "number" ||
    !Number.isInteger(obj.id) ||
    obj.id <= 0
  )
    return null;
  return { created_at: obj.created_at, id: obj.id };
}
