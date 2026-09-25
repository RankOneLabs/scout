import { z } from "zod";

import type { SurfaceStatus } from "@/types/schema";

export const postFiltersSchema = z.object({
  platform: z.enum(["discord", "farcaster", "bluesky"]).optional(),
  relevant: z
    .enum(["true", "false"])
    .optional()
    .transform((v) => (v === undefined ? undefined : v === "true")),
  score_min: z.coerce.number().min(0).max(1).optional(),
  score_max: z.coerce.number().min(0).max(1).optional(),
  scan_id: z.coerce.number().int().positive().optional(),
  before_id: z.coerce.number().int().positive().optional(),
  limit: z.coerce.number().int().min(1).max(200).optional(),
});

export const draftFiltersSchema = z.object({
  project_key: z.string().min(1).optional(),
  verdict: z.enum(["approve", "revise", "reject"]).optional(),
  scan_id: z.coerce.number().int().positive().optional(),
  before_id: z.coerce.number().int().positive().optional(),
  limit: z.coerce.number().int().min(1).max(200).optional(),
  include_grades: z
    .enum(["true", "false"])
    .optional()
    .transform((v) => v === "true"),
});

/** The complete `evaluations.surface_status` vocabulary as a runtime value,
 *  mirroring scout.storage.evaluations. 'held' is a value of its own and is
 *  never implied by another: anything that folded it into 'surfaced' or
 *  'drafting_failed' would report a hold as an outcome it never reached.
 *
 *  `SurfaceStatus` in types/schema.ts is the same vocabulary as a type, and
 *  `satisfies` is what keeps the two from drifting: a member here that the
 *  union does not name fails to compile, and the list is pinned member for
 *  member by __tests__/filter-schemas.test.ts. Import `SurfaceStatus` for the
 *  type — this module does not define a second one.
 *
 *  This is the vocabulary, not a filter. `evaluationFiltersSchema` below is
 *  deliberately unchanged — the evaluations route returns a scan's whole
 *  population and the status breakdown is derived from it by
 *  `selectSurfaceStatusCounts`, so no status is selected away before a
 *  status view can count it. */
export const SURFACE_STATUSES = [
  "surfaced",
  "low_relevance",
  "abstained",
  "critic_rejected",
  "gate_blocked",
  "not_relevant",
  "drafting_failed",
  "held",
] as const satisfies readonly SurfaceStatus[];

export const evaluationFiltersSchema = z.object({
  scan_id: z.coerce.number().int().positive(),
});

export const negativeGradingFiltersSchema = z.object({
  before_id: z.coerce.number().int().positive().optional(),
  limit: z.coerce.number().int().min(1).max(200).optional(),
});

export type PostFiltersInput = z.infer<typeof postFiltersSchema>;
export type DraftFiltersInput = z.infer<typeof draftFiltersSchema>;
export type EvaluationFiltersInput = z.infer<typeof evaluationFiltersSchema>;
export type NegativeGradingFiltersInput = z.infer<
  typeof negativeGradingFiltersSchema
>;

export type ParseResult<T> =
  | { ok: true; data: T }
  | { ok: false; errors: string[] };

export function parseSearchParams<T>(
  searchParams: URLSearchParams,
  schema: z.ZodType<T>
): ParseResult<T> {
  const obj: Record<string, string> = {};
  // Treat empty query values (e.g. "?score_min=") as absent. z.coerce.number()
  // would otherwise turn "" into 0, silently applying an unintended filter.
  searchParams.forEach((v, k) => {
    if (v !== "") obj[k] = v;
  });
  const result = schema.safeParse(obj);
  if (result.success) return { ok: true, data: result.data };
  return {
    ok: false,
    errors: result.error.issues.map(
      (i) => `${i.path.join(".")}: ${i.message}`
    ),
  };
}
