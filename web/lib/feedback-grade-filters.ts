import { z } from "zod";
import { parseSearchParams, utcInstant } from "@/lib/feedback-filters";
import { decodePhaseRunCursor, encodePhaseRunCursor } from "@/lib/feedback-phase-run-filters";
import type { GradeListCursor, SnapshotUseHistoryCursor } from "@/types/feedback-grades";

export { parseSearchParams, decodePhaseRunCursor, encodePhaseRunCursor };

export const gradeListFiltersSchema = z
  .object({
  gradedFrom: utcInstant.optional(),
  gradedTo: utcInstant.optional(),
  editedFrom: utcInstant.optional(),
  editedTo: utcInstant.optional(),
  schemaVersion: z
    .enum(["1", "2"])
    .optional()
    .transform((v) => (v === undefined ? undefined : (Number(v) as 1 | 2))),
  needsRegrade: z
    .enum(["true", "false"])
    .optional()
    .transform((v) => (v === undefined ? undefined : v === "true")),
  project: z.string().min(1).optional(),
  platform: z.string().min(1).optional(),
  posture: z.string().min(1).optional(),
  terminalStatus: z.string().min(1).optional(),
  relevanceJudgment: z.enum(["correct", "false_positive", "false_negative"]).optional(),
  actionJudgment: z.enum(["accept", "fail"]).optional(),
  failureDimension: z
    .enum([
      "contextual_understanding",
      "factual_support",
      "unsupported_implication",
      "posture",
      "tone",
      "wording",
      "usefulness",
    ])
    .optional(),
  eligibilityReason: z
    .enum([
      "eligible",
      "eligible_cap",
      "outside_lookback",
      "schema_version",
      "needs_regrade",
      "missing_evaluation_linkage",
      "mismatched_evaluation_identity",
      "shared_contract_invalid",
      "manual_exclude",
    ])
    .optional(),
  overrideMode: z.enum(["auto", "exclude"]).optional(),
  snapshotUse: z.enum(["never", "historical", "active"]).optional(),
  traceAvailability: z.enum(["available", "unavailable"]).optional(),
  scanId: z.coerce.number().int().positive().optional(),
  evaluationId: z.coerce.number().int().positive().optional(),
  limit: z.coerce.number().int().min(1).max(100).optional(),
  cursor: z.string().min(1).optional(),
  })
  .superRefine((value, context) => {
    for (const [fromKey, toKey] of [
      ["gradedFrom", "gradedTo"],
      ["editedFrom", "editedTo"],
    ] as const) {
      const from = value[fromKey];
      const to = value[toKey];
      if (from !== undefined && to !== undefined && Date.parse(from) > Date.parse(to)) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: [fromKey],
          message: `${fromKey} must be earlier than or equal to ${toKey}`,
        });
      }
    }
  });

export type GradeListFiltersInput = z.infer<typeof gradeListFiltersSchema>;

export const gradeDetailFiltersSchema = z.object({
  revisionLimit: z.coerce.number().int().min(1).max(100).optional(),
  revisionCursor: z.string().min(1).optional(),
  snapshotUseLimit: z.coerce.number().int().min(1).max(100).optional(),
  snapshotUseCursor: z.string().min(1).optional(),
  phaseRunLimit: z.coerce.number().int().min(1).max(100).optional(),
  phaseRunCursor: z.string().min(1).optional(),
});

export type GradeDetailFiltersInput = z.infer<typeof gradeDetailFiltersSchema>;

// --- Opaque cursor codecs — base64url(JSON), never constructed/inspected
// by clients. Decoding failures (malformed base64, invalid JSON, wrong
// shape) return null so the route can 400. ---

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

export function encodeGradeListCursor(cursor: GradeListCursor): string {
  return encodeCursor(cursor);
}

export function decodeGradeListCursor(raw: string): GradeListCursor | null {
  const value = decodeCursor(raw);
  if (value === null || typeof value !== "object") return null;
  const obj = value as Record<string, unknown>;
  if (
    typeof obj.last_edit_time !== "string" ||
    !Number.isFinite(Date.parse(obj.last_edit_time)) ||
    typeof obj.grade_id !== "number" ||
    !Number.isInteger(obj.grade_id) ||
    obj.grade_id <= 0
  )
    return null;
  return { last_edit_time: obj.last_edit_time, grade_id: obj.grade_id };
}

export function encodeRevisionCursor(revision: number): string {
  return encodeCursor({ revision });
}

export function decodeRevisionCursor(raw: string): number | null {
  const value = decodeCursor(raw);
  if (value === null || typeof value !== "object") return null;
  const obj = value as Record<string, unknown>;
  return typeof obj.revision === "number" && Number.isInteger(obj.revision) && obj.revision > 0
    ? obj.revision
    : null;
}

export function encodeSnapshotUseHistoryCursor(createdAt: string, id: number): string {
  return encodeCursor({ created_at: createdAt, id });
}

export function decodeSnapshotUseHistoryCursor(raw: string): SnapshotUseHistoryCursor | null {
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
