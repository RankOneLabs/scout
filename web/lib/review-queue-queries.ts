// Read-only projection of retained Scout artifacts and current revision links.
import { createHash } from "node:crypto";
import { isDeepStrictEqual } from "node:util";
import { z } from "zod";
import { getDb } from "@/lib/db";
import { getGradeByEvaluationId } from "@/lib/queries";
import { selectReviewCosts, selectReviewStatus } from "@/lib/review-selectors";
import {
  ASSISTANCE_PRODUCER_VERSIONS,
  digestSchema, dossierContextSchema, recordedEvaluationSchema, recordedPostSchema,
  rejectedInputSchema, reviewQueueSchema,
  type QueueDetail, type QueueReviewItem, type QueueSummary, type RejectedInput,
  type ReviewDisposition, type ReviewResult,
} from "@/types/review-queues";
import type { Grade } from "@/types/schema";

interface ArtifactRow { digest: string; content: Buffer; recorded_at: string }
// Both JSON documents mirror migrations.grade_revision_comparison_shape,
// including legacy fields and reply text resolved through its pinned revision.
interface RevisionRow { id: number | null; payload: string | null; current_payload: string }
interface DispositionRow { disposition_json: string }
interface CurrentReviewState {
  grade: Grade | null;
  current_revision_id: number | null;
  disposition: ReviewDisposition | null;
  status: QueueReviewItem["status"];
}
interface QueueStatusRow { evaluation_id: number; grade_id: number | null; needs_regrade: number | null; schema_version: number | null; revision_id: number | null; revision_payload: string | null; current_payload: string | null }
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
  return rows.flatMap((row) => {
    if (createHash("sha256").update(row.content).digest("hex") !== row.digest) {
      throw new Error(`Missing or corrupt artifact ${row.digest}`);
    }
    let parsed: unknown;
    try { parsed = JSON.parse(row.content.toString("utf8")) as unknown; } catch { return []; }
    const result = lineageSchema.safeParse(parsed);
    if (!result.success) return [];
    const lineage = result.data;
    return lineage.kind === "scout.grading.assistance" && lineage.process.id === lineage.kind
      && ASSISTANCE_PRODUCER_VERSIONS.includes(lineage.process.version) && lineage.outputs.length === 4
      ? [{ row, lineage }] : [];
  });
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
  const revision = getDb().prepare(`SELECT r.id, r.payload,
    json_object(
      'id', g.id, 'evaluation_id', g.evaluation_id, 'post_id', g.post_id,
      'scan_id', g.scan_id, 'source', g.source, 'graded_at', g.graded_at,
      'relevance_judgment', g.relevance_judgment, 'rejection_reason', g.rejection_reason,
      'comment_quality', g.comment_quality, 'comment_issue', g.comment_issue,
      'schema_version', g.schema_version, 'needs_regrade', g.needs_regrade,
      'action_judgment', g.action_judgment, 'dimensions', json(NULLIF(g.dimensions, '')),
      'failure_note', g.failure_note, 'factual_offending_claim', g.factual_offending_claim,
      'factual_disposition', g.factual_disposition,
      'factual_contradicting_evidence', g.factual_contradicting_evidence,
      'context_missing_input', g.context_missing_input,
      'posture_should_have_been', g.posture_should_have_been,
      'implication_implied_claim', g.implication_implied_claim,
      'implication_missing_support', g.implication_missing_support,
      'reply_revision_id', g.reply_revision_id, 'edited_text', reply.reply_text
    ) AS current_payload
    FROM grades g LEFT JOIN grade_revisions r ON r.grade_id = g.id AND r.evaluation_id = g.evaluation_id
    LEFT JOIN reply_draft_revisions reply ON reply.id = g.reply_revision_id
    WHERE g.evaluation_id = ? ORDER BY r.revision DESC LIMIT 1`).get(evaluationId) as RevisionRow | undefined;
  const revisionMatches = revision !== undefined && revision.payload !== null
    && isDeepStrictEqual(safeRevisionPayload(revision.payload), safeRevisionPayload(revision.current_payload));
  return { grade, current_revision_id: revision?.id ?? null, disposition,
    status: grade !== null && !revisionMatches ? "needs_regrade"
      : selectReviewStatus({ grade, revisionId: revision?.id ?? null, disposition }) };
}

function queueStatuses(evaluationIds: number[], dispositions: Map<number, ReviewDisposition>): Map<number, QueueReviewItem["status"]> {
  if (evaluationIds.length === 0) return new Map();
  const placeholders = evaluationIds.map(() => "?").join(",");
  const rows = getDb().prepare(`SELECT g.evaluation_id, g.id AS grade_id, g.needs_regrade, g.schema_version,
    r.id AS revision_id, r.payload AS revision_payload,
    json_object(
      'id', g.id, 'evaluation_id', g.evaluation_id, 'post_id', g.post_id,
      'scan_id', g.scan_id, 'source', g.source, 'graded_at', g.graded_at,
      'relevance_judgment', g.relevance_judgment, 'rejection_reason', g.rejection_reason,
      'comment_quality', g.comment_quality, 'comment_issue', g.comment_issue,
      'schema_version', g.schema_version, 'needs_regrade', g.needs_regrade,
      'action_judgment', g.action_judgment, 'dimensions', json(NULLIF(g.dimensions, '')),
      'failure_note', g.failure_note, 'factual_offending_claim', g.factual_offending_claim,
      'factual_disposition', g.factual_disposition,
      'factual_contradicting_evidence', g.factual_contradicting_evidence,
      'context_missing_input', g.context_missing_input,
      'posture_should_have_been', g.posture_should_have_been,
      'implication_implied_claim', g.implication_implied_claim,
      'implication_missing_support', g.implication_missing_support,
      'reply_revision_id', g.reply_revision_id, 'edited_text', reply.reply_text
    ) AS current_payload
    FROM grades g
    LEFT JOIN grade_revisions r ON r.id = (SELECT latest.id FROM grade_revisions latest WHERE latest.grade_id = g.id AND latest.evaluation_id = g.evaluation_id ORDER BY latest.revision DESC LIMIT 1)
    LEFT JOIN reply_draft_revisions reply ON reply.id = g.reply_revision_id
    WHERE g.evaluation_id IN (${placeholders})`).all(...evaluationIds) as QueueStatusRow[];
  const byEvaluation = new Map(rows.map((row) => [row.evaluation_id, row]));
  return new Map(evaluationIds.map((evaluationId) => {
    const row = byEvaluation.get(evaluationId);
    const disposition = dispositions.get(evaluationId) ?? null;
    const revisionMatches = row?.revision_payload !== null && row?.revision_payload !== undefined
      && row.current_payload !== null && isDeepStrictEqual(safeRevisionPayload(row.revision_payload), safeRevisionPayload(row.current_payload));
    const status: QueueReviewItem["status"] = row === undefined
      ? disposition?.action.kind === "skip" ? "skipped" : "pending"
      : row.needs_regrade || row.schema_version !== 3 || !revisionMatches ? "needs_regrade"
      : disposition?.grade_revision_id === row.revision_id && row.revision_id !== null ? "reviewed" : "graded_elsewhere";
    return [evaluationId, status];
  }));
}

function safeRevisionPayload(payload: string): unknown {
  // Corrupt stored revision JSON is a remediation state, not a queue-read failure.
  try { return JSON.parse(payload) as unknown; }
  catch { return undefined; }
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
    if (!recorded || recorded.evaluation.project_key !== queue.project_key) throw new Error("Queue source lacks recorded context");
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
      const unique = new Map<string, string>();
      for (const { lineage, row } of queueProducers()) {
        if (!unique.has(lineage.outputs[2])) unique.set(lineage.outputs[2], row.recorded_at);
      }
      return [...unique].flatMap(([digest, recordedAt]) => {
        // The list never reads population blobs or repeated dossier contexts.
        const queue = reviewQueueSchema.parse(artifact(digest));
        if (project !== null && queue.project_key !== project) return [];
        const latest = new Map(dispositionsForQueue(digest).map((item) => [item.evaluation_id, item]));
        const evaluationIds = queue.items.flatMap((item) => item.sources.map((source) => source.evaluation_id));
        const statuses = queueStatuses(evaluationIds, latest);
        const states = evaluationIds.map((evaluationId) => statuses.get(evaluationId) ?? "pending");
        return [{ digest, project_key: queue.project_key, source_count: states.length,
          reviewed: states.filter((status) => status === "reviewed").length,
          skipped: states.filter((status) => status === "skipped").length,
          graded_elsewhere: states.filter((status) => status === "graded_elsewhere").length,
          recorded_at: recordedAt }];
      });
    }).deferred() };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : "Cannot list queues" };
  }
}
