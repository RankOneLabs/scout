// Mirrors grading/assistance_types.py, snapshots.py and review_types.py.
import { z } from "zod";
import type { Grade, GradeInput } from "@/types/schema";

// Mirror assistance_types.ASSISTANCE_PRODUCER_VERSIONS; checked against Python.
export const ASSISTANCE_PRODUCER_VERSIONS: readonly string[] = ["1", "2", "3"];

export const digestSchema = z.string().regex(/^[0-9a-f]{64}$/);
export type QueueDigest = z.infer<typeof digestSchema>;
export const queueSourceSchema = z.object({
  evaluation_id: z.number().int().positive(),
  ranked_position: z.number().int().nullable(),
  random_position: z.number().int().nullable(),
});
const scoreSchema = z.object({
  evaluation_id: z.number().int(), probability: z.number(),
  explanation: z.array(z.object({ term: z.string(), contribution: z.number() })),
});
const similarityScoreSchema = z.object({
  evaluation_id: z.number().int(), similarity: z.number().min(0).max(1),
  explanation: z.array(z.object({ term: z.string(), contribution: z.number() })),
});
const selectorSchema = z.object({
  kind: z.enum(["tfidf_logistic", "seeded_random"]),
  population_evaluation_ids: z.array(z.number().int()),
  selected_evaluation_ids: z.array(z.number().int()),
  scores: z.array(scoreSchema), explanation_method: z.string().nullable(),
});
const positiveSelectorSchema = z.object({
  kind: z.literal("tfidf_positive_similarity"),
  population_evaluation_ids: z.array(z.number().int()),
  selected_evaluation_ids: z.array(z.number().int()),
  scores: z.array(similarityScoreSchema),
  explanation_method: z.literal("tfidf-times-positive-centroid/v1"),
});
export const reviewQueueSchema = z.object({
  format: z.literal("scout.review-queue/v1"), project_key: z.string(),
  population_digest: digestSchema,
  items: z.array(z.object({ duplicate_key: digestSchema, sources: z.array(queueSourceSchema) })),
  ranked: z.union([selectorSchema, positiveSelectorSchema]).nullable(), random: selectorSchema,
});
export type ReviewQueue = z.infer<typeof reviewQueueSchema>;

export const recordedPostSchema = z.object({
  id: z.number().int(), content: z.string().nullable(), url: z.string().nullable(),
  author_name: z.string().nullable(), channel_name: z.string().nullable(),
  parent_text: z.string().nullable(), parent_author_name: z.string().nullable(),
});
export const recordedEvaluationSchema = z.object({
  id: z.number().int(), post_id: z.number().int(), scan_id: z.number().int().nullable(),
  project_key: z.string().nullable(), reason: z.string().nullable(),
  relevant: z.union([z.boolean(), z.number().int()]), posture: z.string().nullable(),
  dossier_revision: z.string().nullable(), dossier_summary_id: z.string().nullable(),
});
export const dossierContextSchema = z.object({
  summary: z.object({
    project_key: z.string(), last_reviewed: z.string(), reviewer: z.string(),
    facts: z.array(z.object({ id: z.string(), text: z.string(), safe_phrasings: z.array(z.string()), immutable_evidence: z.array(z.string()) })),
    resources: z.array(z.object({ id: z.string(), label: z.string(), canonical_url: z.string(), immutable_evidence: z.array(z.string()) })),
    prohibitions: z.array(z.object({ id: z.string(), mode: z.string(), pattern: z.string(), flags: z.string(), immutable_evidence: z.array(z.string()) })),
    references: z.array(z.string()),
  }),
  metadata: z.object({ project_key: z.string(), summary_id: z.string(), revision: z.string(), path: z.string() }),
  known_gaps: z.array(z.string()),
});
export const rejectedInputSchema = z.object({
  evaluation: recordedEvaluationSchema, post: recordedPostSchema.nullable(),
  context: dossierContextSchema.nullable(), has_grade: z.boolean(),
});
export type RejectedInput = z.infer<typeof rejectedInputSchema>;
export type ReviewAction = { kind: "grade"; grade: GradeInput } | { kind: "skip"; reason: string } | { kind: "reconcile" };
export interface ReviewTiming { elapsed_ms: number | null; method: "active-visible-idle60/v1" | null }
export interface ReviewPriceBasis { usd_per_hour: number; basis: string }
export interface ReviewRequest {
  action_id: string;
  expected_grade_revision_id: number | null;
  expected_action_id: string | null;
  action: ReviewAction;
  timing: ReviewTiming;
  pricing: ReviewPriceBasis | null;
}
export interface ReviewDisposition {
  format: "scout.review-disposition/v1";
  action_id: string; queue_digest: QueueDigest; evaluation_id: number;
  action: ReviewAction; recorded_at: string; timing: ReviewTiming;
  pricing: ReviewPriceBasis | null; grade_revision_id: number | null;
}
export type ReviewStatus = "pending" | "skipped" | "reviewed" | "graded_elsewhere" | "needs_regrade";
export interface QueueReviewItem {
  source: z.infer<typeof queueSourceSchema>;
  duplicate_key: string;
  recorded: RejectedInput;
  score: z.infer<typeof scoreSchema> | z.infer<typeof similarityScoreSchema> | null;
  grade: Grade | null;
  current_revision_id: number | null;
  disposition: ReviewDisposition | null;
  status: ReviewStatus;
}
export interface ReviewScorePresentation {
  summary: string;
  explanation: string;
}
export interface ReviewCosts {
  action_ids: string[]; measured_action_count: number; unavailable_action_count: number;
  elapsed_ms: number; priced_elapsed_ms: number; estimated_usd: number | null;
  purpose: "corpus_building";
}
export interface QueueSummary {
  digest: QueueDigest; project_key: string; source_count: number;
  reviewed: number; skipped: number; graded_elsewhere: number;
  recorded_at: string;
}
export interface QueueDetail {
  digest: QueueDigest; queue: ReviewQueue; lineage_digests: string[];
  items: QueueReviewItem[]; dispositions: ReviewDisposition[]; costs: ReviewCosts;
}
export type ReviewResult<T> = { ok: true; value: T } | { ok: false; error: string };
