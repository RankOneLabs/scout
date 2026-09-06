// Read-only projection of retained Scout artifacts and current revision links.
import { createHash } from "node:crypto";
import { z } from "zod";
import { getDb } from "@/lib/db";
import { getGradeByEvaluationId } from "@/lib/queries";
import { selectReviewCosts, selectReviewStatus } from "@/lib/review-selectors";
import {
  digestSchema, dossierContextSchema, recordedEvaluationSchema, recordedPostSchema,
  rejectedInputSchema, reviewQueueSchema,
  type QueueDetail, type QueueReviewItem, type QueueSummary, type RejectedInput,
  type ReviewDisposition, type ReviewResult,
} from "@/types/review-queues";
import type { Grade } from "@/types/schema";

interface ArtifactRow { digest: string; content: Buffer; recorded_at: string }
interface RevisionRow { id: number }
interface DispositionRow { disposition_json: string }
interface CurrentReviewState {
  grade: Grade | null;
  current_revision_id: number | null;
  disposition: ReviewDisposition | null;
  status: QueueReviewItem["status"];
}
const lineageSchema = z.object({
  kind: z.string(), inputs: z.array(digestSchema), outputs: z.array(digestSchema),
  process: z.object({ id: z.string(), version: z.string() }),
});
const manifestSchema = z.object({ format: z.literal("scout.rejected-population/v2"), items: z.array(digestSchema) });
const inputReferenceSchema = z.object({
  format: z.literal("scout.rejected-input/v2"), evaluation: recordedEvaluationSchema,
  post_digest: digestSchema.nullable(), context_digest: digestSchema.nullable(), has_grade: z.boolean(),
});
const legacyPopulationSchema = z.object({ format: z.literal("scout.rejected-population/v1"), items: z.array(rejectedInputSchema) });

function artifact(digest: string): unknown {
  const row = getDb().prepare("SELECT digest, content, recorded_at FROM analysis_artifacts WHERE digest = ?").get(digest) as ArtifactRow | undefined;
  if (!row || createHash("sha256").update(row.content).digest("hex") !== digest) throw new Error(`Missing or corrupt artifact ${digest}`);
  return JSON.parse(row.content.toString("utf8")) as unknown;
}

function queueProducers() {
  const rows = getDb().prepare("SELECT a.digest, a.content, a.recorded_at FROM analysis_lineage l JOIN analysis_artifacts a ON a.digest = l.digest ORDER BY a.recorded_at DESC, a.digest").all() as ArtifactRow[];
  return rows.map((row) => ({ row, lineage: lineageSchema.parse(artifact(row.digest)) }))
    .filter(({ lineage }) => lineage.kind === "scout.grading.assistance" && lineage.process.id === lineage.kind && ["1", "2"].includes(lineage.process.version) && lineage.outputs.length === 4);
}

function populationInputs(digest: string): RejectedInput[] {
  const value = artifact(digest);
  const legacy = legacyPopulationSchema.safeParse(value);
  if (legacy.success) return legacy.data.items;
  const manifest = manifestSchema.parse(value);
  const cache = new Map<string, unknown>();
  const read = (key: string) => {
    if (!cache.has(key)) cache.set(key, artifact(key));
    return cache.get(key);
  };
  return manifest.items.map((key) => {
    const item = inputReferenceSchema.parse(read(key));
    return {
      evaluation: item.evaluation, has_grade: item.has_grade,
      post: item.post_digest === null ? null : recordedPostSchema.parse(read(item.post_digest)),
      context: item.context_digest === null ? null : dossierContextSchema.parse(read(item.context_digest)),
    };
  });
}

function dispositionsForQueue(digest: string): ReviewDisposition[] {
  const rows = getDb().prepare("SELECT disposition_json FROM review_dispositions WHERE queue_digest = ? ORDER BY sequence").all(digest) as DispositionRow[];
  return rows.map((row) => JSON.parse(row.disposition_json) as ReviewDisposition);
}

function currentReviewState(evaluationId: number, disposition: ReviewDisposition | null): CurrentReviewState {
  const grade = getGradeByEvaluationId(evaluationId);
  const revision = getDb().prepare("SELECT r.id FROM grade_revisions r JOIN grades g ON g.id = r.grade_id WHERE g.evaluation_id = ? AND r.evaluation_id = ? ORDER BY r.revision DESC LIMIT 1").get(evaluationId, evaluationId) as RevisionRow | undefined;
  return { grade, current_revision_id: revision?.id ?? null, disposition,
    status: selectReviewStatus({ grade, revisionId: revision?.id ?? null, disposition }) };
}

function queueDetail(digest: string): QueueDetail {
  const producers = queueProducers().filter(({ lineage }) => lineage.outputs[2] === digest);
  if (producers.length === 0) throw new Error("Queue has no supported producer");
  const queue = reviewQueueSchema.parse(artifact(digest));
  const population = new Map(populationInputs(queue.population_digest).map((item) => [item.evaluation.id, item]));
  // Only the Python writer creates these typed observations; the DB enforces append-only rows.
  const dispositions = dispositionsForQueue(digest);
  const latest = new Map(dispositions.map((item) => [item.evaluation_id, item]));
  const scores = new Map(queue.ranked?.scores.map((item) => [item.evaluation_id, item]) ?? []);
  const items: QueueReviewItem[] = queue.items.flatMap((item) => item.sources.map((source) => {
    const recorded = population.get(source.evaluation_id);
    if (!recorded?.post || !recorded.context || recorded.evaluation.project_key !== queue.project_key) throw new Error("Queue source lacks recorded context");
    const disposition = latest.get(source.evaluation_id) ?? null;
    return {
      source, duplicate_key: item.duplicate_key, recorded, score: scores.get(source.evaluation_id) ?? null,
      ...currentReviewState(source.evaluation_id, disposition),
    };
  }));
  return { digest, queue, lineage_digests: producers.map(({ row }) => row.digest), items, dispositions, costs: selectReviewCosts(dispositions) };
}

export function getReviewQueue(digest: string): ReviewResult<QueueDetail> {
  try {
    return { ok: true, value: getDb().transaction(() => queueDetail(digest)).deferred() };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : "Cannot read queue" };
  }
}

export function listEvaluationReviews(evaluationId: number): ReviewResult<ReviewDisposition[]> {
  try {
    const rows = getDb().prepare("SELECT disposition_json FROM review_dispositions WHERE evaluation_id = ? ORDER BY sequence DESC").all(evaluationId) as DispositionRow[];
    return { ok: true, value: rows.map((row) => JSON.parse(row.disposition_json) as ReviewDisposition) };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : "Cannot read review provenance" };
  }
}

export function listReviewQueues(project: string | null): ReviewResult<QueueSummary[]> {
  try {
    return { ok: true, value: getDb().transaction(() => {
      const unique = new Map(queueProducers().map(({ lineage, row }) => [lineage.outputs[2], row.recorded_at]));
      return [...unique].flatMap(([digest, recordedAt]) => {
        // The list never reads population blobs or repeated dossier contexts.
        const queue = reviewQueueSchema.parse(artifact(digest));
        if (project !== null && queue.project_key !== project) return [];
        const latest = new Map(dispositionsForQueue(digest).map((item) => [item.evaluation_id, item]));
        const states = queue.items.flatMap((item) => item.sources.map((source) =>
          currentReviewState(source.evaluation_id, latest.get(source.evaluation_id) ?? null)));
        return [{ digest, project_key: queue.project_key, source_count: states.length,
          reviewed: states.filter((item) => item.status === "reviewed").length,
          skipped: states.filter((item) => item.status === "skipped").length,
          graded_elsewhere: states.filter((item) => item.status === "graded_elsewhere").length,
          recorded_at: recordedAt }];
      });
    }).deferred() };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : "Cannot list queues" };
  }
}
