import { z } from "zod";
import type { FeedbackItemCursor, SnapshotListCursor } from "@/types/feedback";

export const utcInstant = z.string().datetime({ offset: true }).refine((value) => value.endsWith("Z"), {
  message: "must be an ISO-8601 UTC timestamp ending in Z",
});

export const snapshotListFiltersSchema = z.object({
  scanId: z.coerce.number().int().positive().optional(),
  mode: z.enum(["shadow", "active"]).optional(),
  limit: z.coerce.number().int().min(1).max(100).optional(),
  cursor: z.string().min(1).optional(),
});

export const snapshotDetailFiltersSchema = z.object({
  phase: z.enum(["relevance", "reply_draft", "critic"]).optional(),
  use: z.enum(["all", "included", "excluded"]).optional(),
  itemLimit: z.coerce.number().int().min(1).max(200).optional(),
  itemCursor: z.string().min(1).optional(),
  consumerLimit: z.coerce.number().int().min(1).max(100).optional(),
  consumerCursor: z.string().min(1).optional(),
});

export type SnapshotListFiltersInput = z.infer<typeof snapshotListFiltersSchema>;
export type SnapshotDetailFiltersInput = z.infer<typeof snapshotDetailFiltersSchema>;

export type ParseResult<T> =
  | { ok: true; data: T }
  | { ok: false; errors: string[] };

// Mirrors lib/filter-schemas.ts's parseSearchParams — empty-string query
// values are treated as absent so z.coerce.number() doesn't silently turn
// "?limit=" into 0.
export function parseSearchParams<T>(
  searchParams: URLSearchParams,
  schema: z.ZodType<T>
): ParseResult<T> {
  const obj: Record<string, string> = {};
  searchParams.forEach((v, k) => {
    if (v !== "") obj[k] = v;
  });
  const result = schema.safeParse(obj);
  if (result.success) return { ok: true, data: result.data };
  return {
    ok: false,
    errors: result.error.issues.map((i) => `${i.path.join(".")}: ${i.message}`),
  };
}

// Opaque cursor codec. Cursors are base64url-encoded JSON — clients treat
// them as an opaque token (pass back verbatim as ?cursor=/?itemCursor=),
// never construct or inspect them. Decoding failures (malformed base64,
// invalid JSON, wrong shape) return null so the route can 400.

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

export function encodeSnapshotListCursor(cursor: SnapshotListCursor): string {
  return encodeCursor(cursor);
}

export function decodeSnapshotListCursor(raw: string): SnapshotListCursor | null {
  const value = decodeCursor(raw);
  if (
    value === null ||
    typeof value !== "object" ||
    typeof (value as Record<string, unknown>).created_at !== "string" ||
    typeof (value as Record<string, unknown>).snapshot_id !== "number"
  ) {
    return null;
  }
  const { created_at, snapshot_id } = value as SnapshotListCursor;
  if (
    !Number.isFinite(Date.parse(created_at)) ||
    !Number.isInteger(snapshot_id) ||
    snapshot_id <= 0
  ) {
    return null;
  }
  return { created_at, snapshot_id };
}

export function encodeFeedbackItemCursor(cursor: FeedbackItemCursor): string {
  return encodeCursor(cursor);
}

export function decodeFeedbackItemCursor(raw: string): FeedbackItemCursor | null {
  const value = decodeCursor(raw);
  if (value === null || typeof value !== "object") return null;
  const obj = value as Record<string, unknown>;
  if (obj.primary_use !== "included" && obj.primary_use !== "excluded") return null;
  if (typeof obj.selected_note !== "boolean") return null;
  if (
    obj.example_rank !== null &&
    (typeof obj.example_rank !== "number" ||
      !Number.isInteger(obj.example_rank) ||
      obj.example_rank <= 0)
  ) {
    return null;
  }
  if (obj.selected_note !== (obj.example_rank !== null)) return null;
  if (typeof obj.graded_at !== "string" || !Number.isFinite(Date.parse(obj.graded_at))) return null;
  if (typeof obj.grade_id !== "number" || !Number.isInteger(obj.grade_id) || obj.grade_id <= 0) {
    return null;
  }
  return {
    primary_use: obj.primary_use,
    selected_note: obj.selected_note,
    example_rank: obj.example_rank,
    graded_at: obj.graded_at,
    grade_id: obj.grade_id,
  };
}
