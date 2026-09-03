import { createHash } from "node:crypto";
import { z } from "zod";
import { getDb } from "@/lib/db";
import { getTraceSpans } from "@/lib/trace-queries";
import { getTracesDb } from "@/lib/traces-db";
import { cursorMatchesFilters, encodeExperimentListCursor, encodeExperimentRunCursor } from "@/lib/feedback-experiment-filters";
import { aggregateExperimentRun, type RunAttemptEvidence } from "@/lib/experiment-run-aggregation";
import type { FeedbackPhase } from "@/types/feedback";
import type { TraceSpan } from "@/types/schema";
import type {
  BaselineEvidence,
  BatchCaseEvidenceV1,
  CandidateConfig,
  CandidateConfigV4,
  DomainDiff,
  ExperimentCandidateDetail,
  ExperimentComparison,
  ExperimentDetailResponse,
  ExperimentListCursor,
  ExperimentListFilters,
  ExperimentListResponse,
  ExperimentListRow,
  ExperimentStatus,
  ExperimentRunCursor,
  ExperimentRunDetailResponse,
  ExperimentRunListResponse,
  ExperimentRunStatus,
  ReplyEvidence,
  ScoreEvidence,
  TraceDiff,
} from "@/types/feedback-experiments";

const DEFAULT_LIST_LIMIT = 50;
const MAX_LIST_LIMIT = 100;

// Thrown when a persisted candidate_config/baseline_evidence/trace_diff/
// domain_diff/score_evidence JSON column, or a baseline trace's recorded
// config snapshot, does not match its expected shape. This is storage
// corruption, not caller error — the API layer surfaces it as an internal
// data-integrity error rather than coercing, filling defaults, or silently
// omitting the corrupt evidence.
export class DataIntegrityError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DataIntegrityError";
  }
}

function parseJsonColumn<T>(schema: z.ZodType<T>, raw: string, context: string): T {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new DataIntegrityError(`${context}: stored value is not valid JSON`);
  }
  const result = schema.safeParse(parsed);
  if (!result.success) {
    throw new DataIntegrityError(`${context}: stored JSON does not match the expected shape`);
  }
  return result.data;
}

// The same UTF-8 SHA-256 helper evaluation_experiments.py's
// `_sha256_utf8` uses to hash both system prompts at replay time — the
// detail route re-derives the baseline hash from the trusted trace with
// this exact helper so it matches the candidate hash's provenance.
export function sha256Utf8(text: string): string {
  return createHash("sha256").update(text, "utf8").digest("hex");
}

// --- candidate_config (v2, experiment_runs.candidate_config) --------------
// .strict() throughout this section: an unrecognized field on a versioned
// evidence document is exactly the kind of storage corruption/drift this
// decoder must fail closed on, not silently ignore.

const candidateConfigV2Schema = z
  .object({
    version: z.literal(2),
    phase: z.enum(["relevance", "reply_draft", "critic"]),
    model: z.string().min(1),
    system_prompt: z.string(),
    system_prompt_sha256: z.string().min(1),
    grader_attached: z.boolean(),
  })
  .strict();

const candidateConfigV4Schema: z.ZodType<CandidateConfigV4> = z
  .object({
    version: z.literal(4),
    phase: z.enum(["relevance", "reply_draft", "critic"]),
    variant_name: z.string().min(1),
    model_override: z.string().min(1).nullable(),
    system_prompt_override: z.string().nullable(),
    system_prompt_override_sha256: z.string().min(1).nullable(),
    grader_attached: z.boolean(),
    sweep: z
      .object({
        name: z.string().min(1),
        axis: z.enum(["model", "prompt"]),
        version: z.literal(1),
      })
      .strict()
      .nullable(),
    plan_sha256: z.string().min(1),
    phase_run_ids: z.array(z.number().int().positive()),
    dropped_duplicate_phase_run_ids: z.array(z.number().int().positive()),
    skipped_pairs: z.array(
      z
        .object({
          phase_run_id: z.number().int().positive(),
          classification: z.string().min(1),
          reason: z.string(),
          baseline_model: z.string().min(1),
          baseline_prompt_sha256: z.string().min(1),
        })
        .strict()
    ),
  })
  .strict();

const candidateConfigSchema: z.ZodType<CandidateConfig> = z.union([
  candidateConfigV2Schema,
  candidateConfigV4Schema,
]);

function parseCandidateConfig(raw: string, experimentRunId: number): CandidateConfig {
  return parseJsonColumn(
    candidateConfigSchema,
    raw,
    `experiment_runs ${experimentRunId} candidate_config`
  );
}

function assertCandidateConfigPhaseMatches(
  candidateConfig: CandidateConfig,
  phaseRunPhase: FeedbackPhase,
  experimentId: number
): void {
  if (candidateConfig.phase !== phaseRunPhase) {
    throw new DataIntegrityError(
      `experiment ${experimentId}: candidate_config.phase (${candidateConfig.phase}) does not ` +
        `match its baseline phase run's phase (${phaseRunPhase})`
    );
  }
}

// --- baseline_evidence (v2, evaluation_experiments.baseline_evidence) -----
// The base shape is always present; the extended reply-correction-oracle
// fields are present only on a grader_attached attempt.

const baselineEvidenceV2Schema = z
  .object({
    version: z.literal(2),
    recorded_input_sha256: z.string().min(1),
    baseline_prompt_reused: z.boolean(),
    baseline_model: z.string().min(1).optional(),
    baseline_prompt_sha256: z.string().min(1).optional(),
    reply_revision_id: z.number().int().optional(),
    correction_sha256: z.string().min(1).optional(),
    project_key: z.string().min(1).optional(),
    dossier_summary_id: z.string().min(1).optional(),
    dossier_revision: z.string().min(1).optional(),
    grader_version: z.string().min(1).optional(),
    assembler_version: z.string().min(1).optional(),
  })
  .strict();

const batchCaseEvidenceV1Schema: z.ZodType<BatchCaseEvidenceV1> = z
  .object({
    version: z.literal(1),
    recorded_input_sha256: z.string().min(1),
    baseline_model: z.string().min(1),
    baseline_prompt_sha256: z.string().min(1),
    baseline_prompt_reused: z.boolean(),
    candidate_model: z.string().min(1),
    candidate_prompt_sha256: z.string().min(1),
    estimated_usd: z.number().nullable(),
    reply_revision_id: z.number().int(),
    correction_sha256: z.string().min(1),
    project_key: z.string().min(1),
    dossier_summary_id: z.string().min(1),
    dossier_revision: z.string().min(1),
    grader_version: z.string().min(1),
    assembler_version: z.string().min(1),
  })
  .strict();

const baselineEvidenceSchema: z.ZodType<BaselineEvidence> = z.union([
  baselineEvidenceV2Schema,
  batchCaseEvidenceV1Schema,
]);

function parseBaselineEvidence(raw: string, experimentId: number): BaselineEvidence {
  return parseJsonColumn(
    baselineEvidenceSchema,
    raw,
    `evaluation_experiments ${experimentId} baseline_evidence`
  );
}

// The correction-oracle pin is all-or-nothing: every field below is written
// atomically by evaluation_experiments.py's build_baseline_evidence, keyed
// only on whether a ReplyCorrectionOracle was resolved — never a subset.
const ORACLE_PIN_FIELDS = [
  "baseline_model",
  "baseline_prompt_sha256",
  "reply_revision_id",
  "correction_sha256",
  "project_key",
  "dossier_summary_id",
  "dossier_revision",
  "grader_version",
  "assembler_version",
] as const satisfies readonly (keyof BaselineEvidence)[];

// Cross-record invariant: baseline_evidence's oracle pin and the parent's
// candidate_config.grader_attached must agree, regardless of attempt
// status — the pin is written before any spend, so this holds even for a
// still-queued/running attempt, not only a completed one.
function assertBaselineEvidenceOraclePin(
  evidence: BaselineEvidence,
  graderAttached: boolean,
  experimentId: number
): void {
  const present = ORACLE_PIN_FIELDS.filter((field) => evidence[field] !== undefined);
  if (graderAttached) {
    const missing = ORACLE_PIN_FIELDS.filter((field) => evidence[field] === undefined);
    if (missing.length > 0) {
      throw new DataIntegrityError(
        `experiment ${experimentId}: baseline_evidence is missing correction-oracle field(s) ` +
          `(${missing.join(", ")}) required when candidate_config.grader_attached is true`
      );
    }
  } else if (present.length > 0) {
    throw new DataIntegrityError(
      `experiment ${experimentId}: baseline_evidence carries correction-oracle field(s) ` +
        `(${present.join(", ")}) but candidate_config.grader_attached is false`
    );
  }
}

// --- score_evidence (trace_comparisons.score_evidence) ---------------------

const scoreEvidenceSchema: z.ZodType<ScoreEvidence> = z
  .object({
    grader_version: z.string().min(1),
    assembler_version: z.string().min(1),
    correction_sha256: z.string().min(1),
    reply_revision_id: z.number().int(),
    baseline_distance: z.number(),
    candidate_distance: z.number(),
    delta: z.number(),
    grader_attached: z.literal(true),
  })
  .strict();

function parseScoreEvidence(raw: string, experimentId: number): ScoreEvidence {
  return parseJsonColumn(
    scoreEvidenceSchema,
    raw,
    `trace_comparisons for experiment ${experimentId} score_evidence`
  );
}

// Cross-record invariant, scoped to a persisted comparison (score_evidence
// only ever exists once trace_comparisons has a row at all): a graded
// attempt's comparison must carry non-null score_evidence, an ungraded
// attempt's must not, and when present its revision/hash/grader/assembler
// identity must match the same attempt's pinned baseline_evidence exactly
// — plus delta must equal the two distances it was computed from, so a
// stored value can never quietly drift from its own inputs.
function assertScoreEvidenceConsistency(
  scoreEvidence: ScoreEvidence | null,
  graderAttached: boolean,
  baselineEvidence: BaselineEvidence,
  experimentId: number
): void {
  if (graderAttached && scoreEvidence === null) {
    throw new DataIntegrityError(
      `experiment ${experimentId}: comparison has no score_evidence but candidate_config.grader_attached is true`
    );
  }
  if (!graderAttached && scoreEvidence !== null) {
    throw new DataIntegrityError(
      `experiment ${experimentId}: comparison has score_evidence but candidate_config.grader_attached is false`
    );
  }
  if (scoreEvidence === null) return;

  if (scoreEvidence.reply_revision_id !== baselineEvidence.reply_revision_id) {
    throw new DataIntegrityError(
      `experiment ${experimentId}: score_evidence.reply_revision_id does not match baseline_evidence.reply_revision_id`
    );
  }
  if (scoreEvidence.correction_sha256 !== baselineEvidence.correction_sha256) {
    throw new DataIntegrityError(
      `experiment ${experimentId}: score_evidence.correction_sha256 does not match baseline_evidence.correction_sha256`
    );
  }
  if (scoreEvidence.grader_version !== baselineEvidence.grader_version) {
    throw new DataIntegrityError(
      `experiment ${experimentId}: score_evidence.grader_version does not match baseline_evidence.grader_version`
    );
  }
  if (scoreEvidence.assembler_version !== baselineEvidence.assembler_version) {
    throw new DataIntegrityError(
      `experiment ${experimentId}: score_evidence.assembler_version does not match baseline_evidence.assembler_version`
    );
  }
  // Both Python and JS compute this as one IEEE 754 double subtraction, so
  // a genuine value round-trips exactly in the common case — but a tiny
  // epsilon guards against a differing rounding path without weakening the
  // check for any real mismatch, which is always orders of magnitude larger.
  const expectedDelta = scoreEvidence.candidate_distance - scoreEvidence.baseline_distance;
  if (Math.abs(scoreEvidence.delta - expectedDelta) > 1e-9) {
    throw new DataIntegrityError(
      `experiment ${experimentId}: score_evidence.delta does not equal candidate_distance - baseline_distance`
    );
  }
}

// --- trace_diff (Jig TraceDiff, dataclasses.asdict + json.dumps) -------

const toolEventSchema = z.object({
  name: z.string(),
  args: z.unknown(),
  output: z.string().nullable(),
  error: z.string().nullable(),
});

const toolDiffSchema = z.object({
  index: z.number().int(),
  divergence: z.enum(["name", "args", "output", "error", "only_a", "only_b"]),
  a: toolEventSchema.nullable(),
  b: toolEventSchema.nullable(),
  tier: z.enum(["identity", "anchor", "ordinal"]).nullable(),
  index_a: z.number().int().nullable(),
  index_b: z.number().int().nullable(),
});

const traceDiffSchema: z.ZodType<TraceDiff> = z.object({
  trace_a_id: z.string(),
  trace_b_id: z.string(),
  tool_divergence: z.array(toolDiffSchema),
  output_diff: z.tuple([z.string(), z.string()]).nullable(),
  error_category_change: z.tuple([z.string().nullable(), z.string().nullable()]).nullable(),
  score_deltas: z.record(z.string(), z.number()),
  score_details: z.record(z.string(), z.tuple([z.number().nullable(), z.number().nullable()])),
  cost_delta: z.number(),
  latency_ms_delta: z.number(),
  comparison_complete: z.boolean(),
  comparison_incomplete_reason: z.string().nullable(),
  a_output_preview: z.string(),
  b_output_preview: z.string(),
  a_output_hash: z.string().nullable(),
  b_output_hash: z.string().nullable(),
  a_output_byte_length: z.number().int().nullable(),
  b_output_byte_length: z.number().int().nullable(),
  a_output_complete: z.unknown(),
  b_output_complete: z.unknown(),
});

function parseTraceDiff(raw: string, experimentId: number): TraceDiff {
  return parseJsonColumn(traceDiffSchema, raw, `trace_comparisons for experiment ${experimentId} trace_diff`);
}

function assertComparisonIdentities(
  traceDiff: TraceDiff,
  firstClassTraceAId: string,
  firstClassTraceBId: string,
  baselineTraceId: string,
  candidateTraceId: string | null,
  experimentId: number
): void {
  if (
    traceDiff.trace_a_id !== firstClassTraceAId ||
    traceDiff.trace_b_id !== firstClassTraceBId ||
    firstClassTraceAId !== baselineTraceId ||
    firstClassTraceBId !== candidateTraceId
  ) {
    throw new DataIntegrityError(
      `experiment ${experimentId}: comparison trace identities disagree with baseline/candidate evidence`
    );
  }
}

// --- domain_diff (Scout's canonical RFC 6901 field diff) ---------------

const domainDiffSideSchema = z.object({
  complete: z.boolean(),
  sha256: z.string().nullable(),
  utf8_byte_length: z.number().int().nullable(),
  incomplete_reason: z.string().nullable(),
  value: z.unknown().optional(),
});

const domainDiffSchema: z.ZodType<DomainDiff> = z.object({
  baseline: domainDiffSideSchema,
  candidate: domainDiffSideSchema,
  grader_not_attached: z.boolean(),
  additions: z.array(z.string()).optional(),
  removals: z.array(z.string()).optional(),
  changes: z.array(z.string()).optional(),
});

function parseDomainDiff(raw: string, experimentId: number): DomainDiff {
  return parseJsonColumn(domainDiffSchema, raw, `trace_comparisons for experiment ${experimentId} domain_diff`);
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
}

function verifyCompleteOutput(
  side: DomainDiff["baseline"],
  traceValue: unknown,
  traceHash: string | null,
  traceLength: number | null,
  label: string,
  experimentId: number
): unknown {
  if (!side.complete) throw new DataIntegrityError(`experiment ${experimentId}: ${label} output is incomplete`);
  if (!("value" in side) || side.sha256 === null || side.utf8_byte_length === null) throw new DataIntegrityError(`experiment ${experimentId}: ${label} output evidence is incomplete`);
  const rendered = canonicalJson(side.value);
  const hash = createHash("sha256").update(rendered, "utf8").digest("hex");
  const length = Buffer.byteLength(rendered, "utf8");
  if (hash !== side.sha256 || length !== side.utf8_byte_length || traceHash !== side.sha256 || traceLength !== side.utf8_byte_length || canonicalJson(traceValue) !== rendered) {
    throw new DataIntegrityError(`experiment ${experimentId}: ${label} output identity mismatch`);
  }
  return side.value;
}

interface ReplyRevisionSqlRow {
  id: number;
  reply_text: string;
  owner_evaluation_id: number;
  project_key: string | null;
  dossier_summary_id: string | null;
  dossier_revision: string | null;
}

function buildReplyEvidence(
  db: ReturnType<typeof getDb>,
  experimentId: number,
  phase: FeedbackPhase,
  status: ExperimentStatus,
  evaluationId: number | null,
  config: CandidateConfig,
  baselineEvidence: BaselineEvidence,
  comparison: ExperimentComparison | null
): ReplyEvidence {
  if (phase !== "reply_draft") return { available: false, reason: "not_reply_draft" };
  if (!config.grader_attached) return { available: false, reason: "grader_not_attached" };
  if (status !== "complete") return { available: false, reason: "attempt_not_complete" };
  if (comparison === null || comparison.score_evidence === null) return { available: false, reason: "comparison_unavailable" };
  if (baselineEvidence.reply_revision_id === undefined || baselineEvidence.correction_sha256 === undefined || evaluationId === null) {
    throw new DataIntegrityError(`experiment ${experimentId}: missing reply oracle identity`);
  }
  // Older read-only fixtures/databases can contain the replay tables while
  // predating revision capture. They cannot supply viewer evidence, but the
  // existing attempt contract remains readable.
  const hasRevisionTable = db.prepare(
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reply_draft_revisions'"
  ).get();
  if (!hasRevisionTable) return { available: false, reason: "comparison_unavailable" };
  const revisions = db.prepare(
    `SELECT rdr.id, rdr.reply_text, dc.evaluation_id AS owner_evaluation_id,
            dc.project_key, dc.dossier_summary_id, dc.dossier_revision
       FROM reply_draft_revisions rdr
       JOIN draft_comments dc ON dc.id=rdr.draft_comment_id
      WHERE rdr.id=?`
  ).all(baselineEvidence.reply_revision_id) as ReplyRevisionSqlRow[];
  if (revisions.length !== 1) throw new DataIntegrityError(`experiment ${experimentId}: pinned reply revision is not unique`);
  const revision = revisions[0];
  if (
    revision.owner_evaluation_id !== evaluationId ||
    revision.project_key !== baselineEvidence.project_key ||
    revision.dossier_summary_id !== baselineEvidence.dossier_summary_id ||
    revision.dossier_revision !== baselineEvidence.dossier_revision ||
    sha256Utf8(revision.reply_text) !== baselineEvidence.correction_sha256
  ) throw new DataIntegrityError(`experiment ${experimentId}: pinned reply revision identity mismatch`);
  const { trace_diff: trace, domain_diff: domain, score_evidence: score } = comparison;
  let baselineOutput: unknown;
  let candidateOutput: unknown;
  try {
    baselineOutput = verifyCompleteOutput(domain.baseline, trace.a_output_complete, trace.a_output_hash, trace.a_output_byte_length, "baseline", experimentId);
    candidateOutput = verifyCompleteOutput(domain.candidate, trace.b_output_complete, trace.b_output_hash, trace.b_output_byte_length, "candidate", experimentId);
  } catch (error) {
    if (error instanceof DataIntegrityError && (!domain.baseline.complete || !domain.candidate.complete)) return { available: false, reason: "output_incomplete" };
    throw error;
  }
  return {
    available: true,
    correction: revision.reply_text,
    baseline_output: baselineOutput,
    candidate_output: candidateOutput,
    baseline_distance: score.baseline_distance,
    candidate_distance: score.candidate_distance,
    delta: score.delta,
  };
}

// --- List ----------------------------------------------------------------

interface ExperimentListSqlRow {
  id: number;
  experiment_run_id: number;
  attempt_number: number;
  supersedes_experiment_id: number | null;
  status: string;
  candidate_trace_id: string | null;
  candidate_llm_call_count: number | null;
  candidate_cost: number | null;
  created_at: string;
  completed_at: string | null;
  run_name: string;
  run_status: string;
  candidate_config: string;
  baseline_phase_run_id: number;
  baseline_trace_id: string;
  phase: string;
  baseline_model: string;
  comparison_trace_a_id: string | null;
  comparison_trace_b_id: string | null;
  trace_diff: string | null;
}

export function listExperiments(filters: ExperimentListFilters): ExperimentListResponse {
  const limit = Math.min(filters.limit ?? DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT);
  const db = getDb();

  const conditions: string[] = [];
  const params: (string | number)[] = [];

  if (filters.status !== undefined) {
    conditions.push("e.status = ?");
    params.push(filters.status);
  }
  if (filters.phase !== undefined) {
    conditions.push("pr.phase = ?");
    params.push(filters.phase);
  }
  if (filters.cursor !== undefined) {
    conditions.push("(e.created_at < ? OR (e.created_at = ? AND e.id < ?))");
    params.push(filters.cursor.created_at, filters.cursor.created_at, filters.cursor.id);
  }
  const where = conditions.length > 0 ? `WHERE ${conditions.join(" AND ")}` : "";

  const rows = db
    .prepare(
      `SELECT
         e.id AS id, e.experiment_run_id AS experiment_run_id,
         e.attempt_number AS attempt_number,
         e.supersedes_experiment_id AS supersedes_experiment_id,
         e.status AS status,
         e.candidate_trace_id AS candidate_trace_id,
         e.candidate_llm_call_count AS candidate_llm_call_count,
         e.candidate_cost AS candidate_cost,
         e.created_at AS created_at, e.completed_at AS completed_at,
         er.name AS run_name, er.status AS run_status,
         er.candidate_config AS candidate_config,
         pr.id AS baseline_phase_run_id, pr.trace_id AS baseline_trace_id,
         pr.phase AS phase, pr.model AS baseline_model,
         tc.trace_a_id AS comparison_trace_a_id, tc.trace_b_id AS comparison_trace_b_id,
         tc.trace_diff AS trace_diff
       FROM evaluation_experiments e
       JOIN experiment_runs er ON er.id = e.experiment_run_id
       JOIN evaluation_phase_runs pr ON pr.id = e.phase_run_id
       LEFT JOIN trace_comparisons tc ON tc.experiment_id = e.id
       ${where}
       ORDER BY e.created_at DESC, e.id DESC
       LIMIT ?`
    )
    .all(...params, limit + 1) as ExperimentListSqlRow[];

  const hasMore = rows.length > limit;
  const page = rows.slice(0, limit);

  const data: ExperimentListRow[] = page.map((row) => {
    const candidateConfig = parseCandidateConfig(row.candidate_config, row.experiment_run_id);
    assertCandidateConfigPhaseMatches(candidateConfig, row.phase as FeedbackPhase, row.id);
    let comparisonComplete: boolean | null = null;
    if (
      row.trace_diff !== null &&
      row.comparison_trace_a_id !== null &&
      row.comparison_trace_b_id !== null
    ) {
      const traceDiff = parseTraceDiff(row.trace_diff, row.id);
      assertComparisonIdentities(
        traceDiff,
        row.comparison_trace_a_id,
        row.comparison_trace_b_id,
        row.baseline_trace_id,
        row.candidate_trace_id,
        row.id
      );
      comparisonComplete = traceDiff.comparison_complete;
    } else if (
      row.trace_diff !== null ||
      row.comparison_trace_a_id !== null ||
      row.comparison_trace_b_id !== null
    ) {
      throw new DataIntegrityError(`experiment ${row.id}: comparison identity columns are incomplete`);
    }
    return {
      id: row.id,
      experiment_run_id: row.experiment_run_id,
      name: row.run_name,
      status: row.status as ExperimentStatus,
      run_status: row.run_status as ExperimentListRow["run_status"],
      phase: row.phase as FeedbackPhase,
      attempt_number: row.attempt_number,
      supersedes_experiment_id: row.supersedes_experiment_id,
      grader_attached: candidateConfig.grader_attached,
      baseline_phase_run_id: row.baseline_phase_run_id,
      candidate_trace_id: row.candidate_trace_id,
      baseline_model: row.baseline_model,
      candidate_model:
        candidateConfig.version === 2
          ? candidateConfig.model
          : candidateConfig.model_override ?? row.baseline_model,
      created_at: row.created_at,
      completed_at: row.completed_at,
      candidate_llm_call_count: row.candidate_llm_call_count,
      candidate_cost: row.candidate_cost,
      comparison_complete: comparisonComplete,
    };
  });

  const last = page[page.length - 1];
  const nextCursor: string | null =
    hasMore && last
      ? encodeExperimentListCursor({
          created_at: last.created_at,
          id: last.id,
          status: filters.status ?? null,
          phase: filters.phase ?? null,
        } satisfies ExperimentListCursor)
      : null;

  return { data, has_more: hasMore, next_cursor: nextCursor };
}

export { cursorMatchesFilters };

// --- Parent-run list/detail -----------------------------------------------

interface ParentRunSqlRow {
  id: number;
  name: string;
  status: ExperimentRunStatus;
  candidate_config: string;
  created_at: string;
  completed_at: string | null;
}

interface ParentAttemptSqlRow extends ExperimentListSqlRow {
  comparison_id: number | null;
  baseline_evidence: string;
  score_evidence: string | null;
}

interface BatchedTraceEvidence {
  trace_id: string;
  root_count: number;
  root_duration_ms: number | null;
  llm_count: number;
  priced_llm_count: number;
  llm_cost: number;
}

function loadRunAttempts(runIds: number[]): Map<number, RunAttemptEvidence[]> {
  const result = new Map<number, RunAttemptEvidence[]>();
  if (runIds.length === 0) return result;
  const placeholders = runIds.map(() => "?").join(",");
  const rows = getDb().prepare(
    `SELECT e.id, e.experiment_run_id, e.phase_run_id AS baseline_phase_run_id,
            e.attempt_number, e.supersedes_experiment_id, e.status,
            e.baseline_evidence, e.candidate_trace_id, e.candidate_llm_call_count,
            e.candidate_cost, e.created_at, e.completed_at,
            er.name AS run_name, er.status AS run_status, er.candidate_config,
            pr.trace_id AS baseline_trace_id, pr.phase, pr.model AS baseline_model,
            tc.id AS comparison_id, tc.trace_a_id AS comparison_trace_a_id,
            tc.trace_b_id AS comparison_trace_b_id,
            tc.trace_diff, tc.score_evidence
       FROM evaluation_experiments e
       JOIN experiment_runs er ON er.id=e.experiment_run_id
       JOIN evaluation_phase_runs pr ON pr.id=e.phase_run_id
       LEFT JOIN trace_comparisons tc ON tc.experiment_id=e.id
      WHERE e.experiment_run_id IN (${placeholders})
      ORDER BY e.experiment_run_id, e.phase_run_id, e.attempt_number, e.id`
  ).all(...runIds) as ParentAttemptSqlRow[];
  const traceIds = [...new Set(rows.flatMap((row) => [row.baseline_trace_id, row.candidate_trace_id].filter((id): id is string => id !== null)))];
  const traceEvidence = new Map<string, BatchedTraceEvidence>();
  if (traceIds.length > 0) {
    const tracePlaceholders = traceIds.map(() => "?").join(",");
    const evidenceRows = getTracesDb().prepare(
      `SELECT trace_id,
              SUM(CASE WHEN parent_id IS NULL AND kind='agent_run' THEN 1 ELSE 0 END) AS root_count,
              MAX(CASE WHEN parent_id IS NULL AND kind='agent_run' THEN duration_ms END) AS root_duration_ms,
              SUM(CASE WHEN kind='llm_call' THEN 1 ELSE 0 END) AS llm_count,
              SUM(CASE WHEN kind='llm_call' AND usage_cost IS NOT NULL THEN 1 ELSE 0 END) AS priced_llm_count,
              COALESCE(SUM(CASE WHEN kind='llm_call' THEN usage_cost END), 0) AS llm_cost
         FROM spans WHERE trace_id IN (${tracePlaceholders}) GROUP BY trace_id`
    ).all(...traceIds) as BatchedTraceEvidence[];
    for (const evidence of evidenceRows) traceEvidence.set(evidence.trace_id, evidence);
  }
  for (const row of rows) {
    const config = parseCandidateConfig(row.candidate_config, row.experiment_run_id);
    assertCandidateConfigPhaseMatches(config, row.phase as FeedbackPhase, row.id);
    const baselineEvidence = parseBaselineEvidence(row.baseline_evidence, row.id);
    if ((config.version === 2) !== (baselineEvidence.version === 2)) {
      throw new DataIntegrityError(`experiment ${row.id}: incompatible evidence versions`);
    }
    const traceDiff = row.trace_diff === null ? null : parseTraceDiff(row.trace_diff, row.id);
    if (traceDiff) {
      if (row.comparison_trace_a_id === null || row.comparison_trace_b_id === null) throw new DataIntegrityError(`experiment ${row.id}: incomplete comparison identity`);
      assertComparisonIdentities(traceDiff, row.comparison_trace_a_id, row.comparison_trace_b_id, row.baseline_trace_id, row.candidate_trace_id, row.id);
    }
    const scoreEvidence = row.score_evidence === null ? null : parseScoreEvidence(row.score_evidence, row.id);
    // A failed/queued attempt may legitimately have no comparison. Apply
    // comparison invariants only once that record exists, matching the
    // single-attempt detail path below.
    if (row.comparison_id !== null) {
      assertScoreEvidenceConsistency(scoreEvidence, config.grader_attached, baselineEvidence, row.id);
    }
    const projected: ExperimentListRow = {
      id: row.id, experiment_run_id: row.experiment_run_id, name: row.run_name,
      status: row.status as ExperimentStatus, run_status: row.run_status as ExperimentRunStatus,
      phase: row.phase as FeedbackPhase, attempt_number: row.attempt_number,
      supersedes_experiment_id: row.supersedes_experiment_id,
      grader_attached: config.grader_attached, baseline_phase_run_id: row.baseline_phase_run_id,
      candidate_trace_id: row.candidate_trace_id, baseline_model: row.baseline_model,
      candidate_model: config.version === 2 ? config.model : (baselineEvidence as BatchCaseEvidenceV1).candidate_model,
      created_at: row.created_at, completed_at: row.completed_at,
      candidate_llm_call_count: row.candidate_llm_call_count, candidate_cost: row.candidate_cost,
      comparison_complete: traceDiff?.comparison_complete ?? null,
    };
    const baselineTrace = traceEvidence.get(row.baseline_trace_id);
    const candidateTrace = row.candidate_trace_id === null ? undefined : traceEvidence.get(row.candidate_trace_id);
    const costsAvailable = baselineTrace?.root_count === 1 && candidateTrace?.root_count === 1 &&
      baselineTrace.llm_count > 0 && candidateTrace.llm_count > 0 &&
      baselineTrace.priced_llm_count === baselineTrace.llm_count && candidateTrace.priced_llm_count === candidateTrace.llm_count;
    const latencyAvailable = baselineTrace?.root_count === 1 && candidateTrace?.root_count === 1 &&
      baselineTrace.root_duration_ms !== null && candidateTrace.root_duration_ms !== null;
    const evidence: RunAttemptEvidence = {
      row: projected, experiment_run_id: row.experiment_run_id,
      phase_run_id: row.baseline_phase_run_id, attempt_number: row.attempt_number,
      supersedes_experiment_id: row.supersedes_experiment_id, score_evidence: scoreEvidence,
      trace_diff: traceDiff, cost_delta_available: traceDiff !== null && costsAvailable,
      latency_delta_available: traceDiff !== null && latencyAvailable,
      baseline_cost: costsAvailable ? baselineTrace.llm_cost : undefined,
      candidate_verified_cost: costsAvailable ? candidateTrace.llm_cost : undefined,
      baseline_latency_ms: latencyAvailable ? baselineTrace.root_duration_ms! : undefined,
      candidate_latency_ms: latencyAvailable ? candidateTrace.root_duration_ms! : undefined,
    };
    const group = result.get(row.experiment_run_id) ?? [];
    group.push(evidence);
    result.set(row.experiment_run_id, group);
  }
  return result;
}

function aggregateParent(row: ParentRunSqlRow, attempts: RunAttemptEvidence[]) {
  const config = parseCandidateConfig(row.candidate_config, row.id);
  try {
    return aggregateExperimentRun({ ...row, candidate_config: config, attempts });
  } catch (error) {
    throw new DataIntegrityError(error instanceof Error ? error.message : `experiment run ${row.id}: invalid lineage`);
  }
}

export function listExperimentRuns(filters: {
  status?: ExperimentRunStatus; phase?: FeedbackPhase; limit?: number; cursor?: ExperimentRunCursor;
}): ExperimentRunListResponse {
  const limit = Math.min(filters.limit ?? DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT);
  const conditions: string[] = [];
  const params: Array<string | number> = [];
  if (filters.status) { conditions.push("er.status = ?"); params.push(filters.status); }
  if (filters.phase) { conditions.push("json_extract(er.candidate_config, '$.phase') = ?"); params.push(filters.phase); }
  if (filters.cursor) {
    conditions.push("(er.created_at < ? OR (er.created_at = ? AND er.id < ?))");
    params.push(filters.cursor.created_at, filters.cursor.created_at, filters.cursor.id);
  }
  const where = conditions.length ? `WHERE ${conditions.join(" AND ")}` : "";
  const rows = getDb().prepare(
    `SELECT er.id, er.name, er.status, er.candidate_config, er.created_at, er.completed_at
       FROM experiment_runs er ${where}
      ORDER BY er.created_at DESC, er.id DESC LIMIT ?`
  ).all(...params, limit + 1) as ParentRunSqlRow[];
  const hasMore = rows.length > limit;
  const page = rows.slice(0, limit);
  const attempts = loadRunAttempts(page.map((row) => row.id));
  const data = page.map((row) => aggregateParent(row, attempts.get(row.id) ?? []));
  const last = page.at(-1);
  return {
    data,
    has_more: hasMore,
    next_cursor: hasMore && last ? encodeExperimentRunCursor({
      version: 1, created_at: last.created_at, id: last.id,
      status: filters.status ?? null, phase: filters.phase ?? null,
    }) : null,
  };
}

export function getExperimentRunDetail(runId: number): ExperimentRunDetailResponse | null {
  const row = getDb().prepare(
    `SELECT id, name, status, candidate_config, created_at, completed_at FROM experiment_runs WHERE id=?`
  ).get(runId) as ParentRunSqlRow | undefined;
  if (!row) return null;
  const config = parseCandidateConfig(row.candidate_config, row.id);
  const attempts = loadRunAttempts([runId]).get(runId) ?? [];
  const summary = aggregateParent(row, attempts);
  const byCase = new Map<number, RunAttemptEvidence[]>();
  for (const attempt of attempts) {
    const group = byCase.get(attempt.phase_run_id) ?? [];
    group.push(attempt); byCase.set(attempt.phase_run_id, group);
  }
  const cases = [...byCase.entries()].sort(([a], [b]) => a - b).map(([phaseRunId, group]) => {
    const ordered = [...group].sort((a, b) => b.attempt_number - a.attempt_number);
    return { phase_run_id: phaseRunId, current: ordered[0].row, history: ordered.slice(1).map((item) => item.row) };
  });
  return {
    run: summary,
    configuration: {
      version: config.version, phase: config.phase, grader_attached: config.grader_attached,
      identity: config.version === 2 ? config.system_prompt_sha256 : config.variant_name,
      plan_sha256: config.version === 4 ? config.plan_sha256 : null,
    },
    cases,
    skipped_pairs: config.version === 4 ? config.skipped_pairs : [],
  };
}

// --- Detail ----------------------------------------------------------------

interface TraceEvidenceAvailability {
  root: TraceSpan;
  costAvailable: boolean;
  latencyAvailable: boolean;
}

function getAgentRunEvidence(traceId: string): TraceEvidenceAvailability | null {
  const spans = getTraceSpans(traceId);
  const root = spans.find((s) => s.parent_id === null);
  if (!root || root.kind !== "agent_run" || root.trace_id !== traceId) return null;
  const llmCalls = spans.filter((span) => span.kind === "llm_call");
  return {
    root,
    costAvailable:
      llmCalls.length > 0 && llmCalls.every((span) => span.usage_cost !== null),
    latencyAvailable: root.duration_ms !== null,
  };
}

// Mirrors evaluation_experiments.py's resolve_baseline extraction of the
// recorded literal system_prompt from the AGENT_RUN root's metadata.config
// snapshot — the value captured at AgentConfig construction, before Jig's
// runtime-appended submit_output instruction.
function extractBaselineSystemPrompt(root: TraceSpan, traceId: string): string {
  const metadata = root.metadata;
  if (metadata === null || typeof metadata !== "object") {
    throw new DataIntegrityError(`trace ${traceId}: no recorded metadata`);
  }
  const config = (metadata as Record<string, unknown>).config;
  if (config === null || typeof config !== "object") {
    throw new DataIntegrityError(`trace ${traceId}: no recorded config snapshot`);
  }
  const systemPrompt = (config as Record<string, unknown>).system_prompt;
  if (typeof systemPrompt !== "string") {
    throw new DataIntegrityError(`trace ${traceId}: no recorded literal system_prompt`);
  }
  return systemPrompt;
}

interface ExperimentDetailSqlRow {
  id: number;
  experiment_run_id: number;
  attempt_number: number;
  supersedes_experiment_id: number | null;
  status: string;
  baseline_evidence: string;
  error_detail: string | null;
  created_at: string;
  completed_at: string | null;
  candidate_trace_id: string | null;
  candidate_llm_call_count: number | null;
  candidate_cost: number | null;
  run_name: string;
  run_status: string;
  candidate_config: string;
  run_created_at: string;
  run_completed_at: string | null;
  phase_run_id: number;
  phase: string;
  baseline_trace_id: string;
  baseline_model: string;
  evaluation_id: number | null;
  snapshot_phase_id: number;
}

interface SnapshotPolicySqlRow {
  snapshot_id: number;
  policy_version: string;
  lookback_days: number;
  max_grades: number;
}

interface ComparisonSqlRow {
  id: number;
  trace_a_id: string;
  trace_b_id: string;
  jig_revision: string;
  trace_diff: string;
  domain_diff: string;
  score_evidence: string | null;
  created_at: string;
}

export function getExperimentDetail(experimentId: number): ExperimentDetailResponse | null {
  const db = getDb();

  const expRow = db
    .prepare(
      `SELECT
         e.id AS id, e.experiment_run_id AS experiment_run_id,
         e.attempt_number AS attempt_number,
         e.supersedes_experiment_id AS supersedes_experiment_id,
         e.status AS status, e.baseline_evidence AS baseline_evidence,
         e.error_detail AS error_detail,
         e.created_at AS created_at, e.completed_at AS completed_at,
         e.candidate_trace_id AS candidate_trace_id,
         e.candidate_llm_call_count AS candidate_llm_call_count,
         e.candidate_cost AS candidate_cost,
         er.name AS run_name, er.status AS run_status,
         er.candidate_config AS candidate_config,
         er.created_at AS run_created_at, er.completed_at AS run_completed_at,
         pr.id AS phase_run_id, pr.phase AS phase, pr.trace_id AS baseline_trace_id,
         pr.model AS baseline_model, pr.evaluation_id AS evaluation_id,
         pr.snapshot_phase_id AS snapshot_phase_id
       FROM evaluation_experiments e
       JOIN experiment_runs er ON er.id = e.experiment_run_id
       JOIN evaluation_phase_runs pr ON pr.id = e.phase_run_id
       WHERE e.id = ?`
    )
    .get(experimentId) as ExperimentDetailSqlRow | undefined;
  if (!expRow) return null;

  const snapshotRow = db
    .prepare(
      `SELECT fsp.snapshot_id AS snapshot_id, fs.policy_version AS policy_version,
              fs.lookback_days AS lookback_days, fs.max_grades AS max_grades
       FROM feedback_snapshot_phases fsp
       JOIN feedback_snapshots fs ON fs.id = fsp.snapshot_id
       WHERE fsp.id = ?`
    )
    .get(expRow.snapshot_phase_id) as SnapshotPolicySqlRow | undefined;
  if (!snapshotRow) {
    throw new DataIntegrityError(
      `experiment ${experimentId}: no feedback_snapshot_phases row for snapshot_phase_id=${expRow.snapshot_phase_id}`
    );
  }

  const candidateConfig = parseCandidateConfig(expRow.candidate_config, expRow.experiment_run_id);
  assertCandidateConfigPhaseMatches(candidateConfig, expRow.phase as FeedbackPhase, experimentId);
  const baselineEvidence = parseBaselineEvidence(expRow.baseline_evidence, experimentId);
  if (
    (candidateConfig.version === 2 && baselineEvidence.version !== 2) ||
    (candidateConfig.version === 4 && baselineEvidence.version !== 1)
  ) {
    throw new DataIntegrityError(
      `experiment ${experimentId}: candidate_config and baseline_evidence versions are incompatible`
    );
  }
  assertBaselineEvidenceOraclePin(baselineEvidence, candidateConfig.grader_attached, experimentId);

  const baselineTraceEvidence = getAgentRunEvidence(expRow.baseline_trace_id);
  if (!baselineTraceEvidence) {
    throw new DataIntegrityError(
      `experiment ${experimentId}: baseline trace ${expRow.baseline_trace_id} does not verify as an AGENT_RUN root`
    );
  }
  const baselineRoot = baselineTraceEvidence.root;
  const baselineSystemPrompt = extractBaselineSystemPrompt(baselineRoot, expRow.baseline_trace_id);
  const baselineSystemPromptSha256 = sha256Utf8(baselineSystemPrompt);

  let comparison: ExperimentComparison | null = null;
  const comparisonRow = db
    .prepare(
      `SELECT id, trace_a_id, trace_b_id, jig_revision, trace_diff, domain_diff,
              score_evidence, created_at
       FROM trace_comparisons WHERE experiment_id = ?`
    )
    .get(experimentId) as ComparisonSqlRow | undefined;
  if (comparisonRow) {
    const traceDiff = parseTraceDiff(comparisonRow.trace_diff, experimentId);
    assertComparisonIdentities(
      traceDiff,
      comparisonRow.trace_a_id,
      comparisonRow.trace_b_id,
      expRow.baseline_trace_id,
      expRow.candidate_trace_id,
      experimentId
    );
    const candidateTraceEvidence = getAgentRunEvidence(comparisonRow.trace_b_id);
    if (!candidateTraceEvidence) {
      throw new DataIntegrityError(
        `experiment ${experimentId}: candidate trace ${comparisonRow.trace_b_id} does not verify as an AGENT_RUN root`
      );
    }
    const scoreEvidence =
      comparisonRow.score_evidence === null
        ? null
        : parseScoreEvidence(comparisonRow.score_evidence, experimentId);
    assertScoreEvidenceConsistency(
      scoreEvidence, candidateConfig.grader_attached, baselineEvidence, experimentId
    );
    comparison = {
      id: comparisonRow.id,
      trace_a_id: comparisonRow.trace_a_id,
      trace_b_id: comparisonRow.trace_b_id,
      jig_revision: comparisonRow.jig_revision,
      created_at: comparisonRow.created_at,
      cost_delta_available:
        baselineTraceEvidence.costAvailable && candidateTraceEvidence.costAvailable,
      latency_delta_available:
        baselineTraceEvidence.latencyAvailable && candidateTraceEvidence.latencyAvailable,
      trace_diff: traceDiff,
      domain_diff: parseDomainDiff(comparisonRow.domain_diff, experimentId),
      score_evidence: scoreEvidence,
    };
  }

  const candidate: ExperimentCandidateDetail = {
    trace_id: expRow.candidate_trace_id,
    trace_url: expRow.candidate_trace_id ? `/traces/${expRow.candidate_trace_id}` : null,
    model:
      candidateConfig.version === 2
        ? candidateConfig.model
        : (baselineEvidence as BatchCaseEvidenceV1).candidate_model,
    system_prompt:
      candidateConfig.version === 2
        ? candidateConfig.system_prompt
        : candidateConfig.system_prompt_override ?? baselineSystemPrompt,
    system_prompt_sha256:
      candidateConfig.version === 2
        ? candidateConfig.system_prompt_sha256
        : (baselineEvidence as BatchCaseEvidenceV1).candidate_prompt_sha256,
    llm_call_count: expRow.candidate_llm_call_count,
    cost: expRow.candidate_cost,
  };

  return {
    id: expRow.id,
    experiment_run: {
      id: expRow.experiment_run_id,
      name: expRow.run_name,
      status: expRow.run_status as ExperimentDetailResponse["experiment_run"]["status"],
      candidate_config: candidateConfig,
      created_at: expRow.run_created_at,
      completed_at: expRow.run_completed_at,
    },
    attempt_number: expRow.attempt_number,
    supersedes_experiment_id: expRow.supersedes_experiment_id,
    status: expRow.status as ExperimentStatus,
    error_detail: expRow.error_detail,
    created_at: expRow.created_at,
    completed_at: expRow.completed_at,
    baseline: {
      phase_run_id: expRow.phase_run_id,
      phase: expRow.phase as FeedbackPhase,
      trace_id: expRow.baseline_trace_id,
      model: expRow.baseline_model,
      system_prompt: baselineSystemPrompt,
      system_prompt_sha256: baselineSystemPromptSha256,
      phase_run_url: `/feedback/phase-runs/${expRow.phase_run_id}`,
      trace_url: `/traces/${expRow.baseline_trace_id}`,
    },
    baseline_evidence: baselineEvidence,
    evaluation_id: expRow.evaluation_id,
    snapshot: {
      snapshot_phase_id: expRow.snapshot_phase_id,
      snapshot_id: snapshotRow.snapshot_id,
      phase: expRow.phase as FeedbackPhase,
      policy_version: snapshotRow.policy_version,
      lookback_days: snapshotRow.lookback_days,
      max_grades: snapshotRow.max_grades,
      snapshot_url: `/feedback?snapshotId=${snapshotRow.snapshot_id}`,
    },
    candidate,
    comparison,
    reply_evidence: buildReplyEvidence(
      db,
      experimentId,
      expRow.phase as FeedbackPhase,
      expRow.status as ExperimentStatus,
      expRow.evaluation_id,
      candidateConfig,
      baselineEvidence,
      comparison
    ),
  };
}
