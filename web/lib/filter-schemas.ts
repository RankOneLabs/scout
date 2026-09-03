import { z } from "zod";

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
