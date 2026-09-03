import { getDb } from "./db";
import {
  type ScanDetail,
  type ScanDetailWithCounts,
  type ScanFetchFailure,
  type ScanStats,
  type ScanWithCounts,
  type MatchedRoute,
  type PromptBundle,
  type PostWithEvaluation,
  type DraftWithContext,
  type DraftWithGrade,
  type ReviewEvaluation,
  type Grade,
  type GradingProgress,
  type GateBlock,
  type Paginated,
  type PostFilters,
  type DraftFilters,
  type NegativeGradingFilters,
  type ParentLookupStatus,
  type SourceParent,
} from "@/types/schema";
import { getGradeRevisionMetaBatch } from "@/lib/feedback-queries";

const DEFAULT_PAGE_SIZE = 50;

// Helpers

function parseRelevantTo(raw: string | null): string[] {
  if (!raw) return [];
  return parseStringList(raw);
}

function parseStringList(raw: string | null): string[] {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed)
      ? parsed.filter((item): item is string => typeof item === "string")
      : [];
  } catch {
    return raw
      .split(/\r?\n/)
      .map((entry) => entry.trim())
      .filter(Boolean);
  }
}

const MATCH_TYPES: readonly MatchedRoute["match_type"][] = [
  "substring",
  "phrase",
  "exact",
  "regex",
] as const;

function normalizeMatchType(value: string | null): MatchedRoute["match_type"] {
  if (value && MATCH_TYPES.includes(value as MatchedRoute["match_type"])) {
    return value as MatchedRoute["match_type"];
  }
  return "substring";
}

function toPromptBundle(row: {
  resolved_evaluate_prompt: string | null;
  resolved_respond_prompt: string | null;
  resolved_critique_prompt: string | null;
}): PromptBundle | null {
  if (
    row.resolved_evaluate_prompt === null &&
    row.resolved_respond_prompt === null &&
    row.resolved_critique_prompt === null
  ) {
    return null;
  }
  return {
    evaluate: row.resolved_evaluate_prompt,
    respond: row.resolved_respond_prompt,
    critique: row.resolved_critique_prompt,
  };
}

function toMatchedRoute(row: {
  matched_route_id: number | null;
  matched_project_key: string | null;
  matched_keyword: string | null;
  matched_match_type: string | null;
  matched_intent: string | null;
  matched_positive_context: string | null;
  matched_negative_context: string | null;
  matched_evaluate_prompt: string | null;
  matched_respond_prompt: string | null;
  matched_critique_prompt: string | null;
  resolved_evaluate_prompt: string | null;
  resolved_respond_prompt: string | null;
  resolved_critique_prompt: string | null;
}): MatchedRoute | null {
  if (
    row.matched_route_id === null ||
    row.matched_project_key === null ||
    row.matched_keyword === null
  ) {
    return null;
  }
  return {
    id: row.matched_route_id,
    project_key: row.matched_project_key,
    keyword: row.matched_keyword,
    match_type: normalizeMatchType(row.matched_match_type),
    intent: row.matched_intent,
    positive_context: parseStringList(row.matched_positive_context),
    negative_context: parseStringList(row.matched_negative_context),
    evaluate_prompt: row.matched_evaluate_prompt,
    respond_prompt: row.matched_respond_prompt,
    critique_prompt: row.matched_critique_prompt,
    resolved_prompt_bundle: toPromptBundle(row),
  };
}

function toBool(val: number | null): boolean {
  return val === 1;
}

function toSourceParent(row: {
  parent_lookup_status: string | null;
  parent_id: string | null;
  parent_author_id: string | null;
  parent_author_name: string | null;
  parent_text: string | null;
  parent_url: string | null;
}): { parent_lookup_status: ParentLookupStatus; parent: SourceParent | null } {
  const raw = row.parent_lookup_status;
  const status: ParentLookupStatus =
    raw === "resolved" || raw === "failed" || raw === "not_applicable"
      ? raw
      : "not_applicable";
  if (
    status === "resolved" &&
    row.parent_id &&
    row.parent_author_id &&
    row.parent_text !== null
  ) {
    return {
      parent_lookup_status: status,
      parent: {
        id: row.parent_id,
        author: { id: row.parent_author_id, name: row.parent_author_name ?? "" },
        text: row.parent_text,
        url: row.parent_url ?? "",
      },
    };
  }
  return { parent_lookup_status: status, parent: null };
}

function isHistoricalTrueEmptyCompletedScan(row: ScanWithCounts): boolean {
  return (
    row.completed_at !== null &&
    (row.messages_scanned ?? 0) === 0 &&
    (row.relevant_found ?? 0) === 0 &&
    (row.post_count ?? 0) === 0 &&
    (row.eval_count ?? 0) === 0 &&
    (row.draft_count ?? 0) === 0 &&
    (row.critique_count ?? 0) === 0
  );
}

// Queries

export function getStats(): ScanStats {
  const db = getDb();
  const row = db
    .prepare(
      `SELECT
        (SELECT COUNT(*) FROM scans) AS total_scans,
        (SELECT COUNT(*) FROM posts) AS total_posts,
        (SELECT COUNT(*) FROM evaluations WHERE relevant = 1) AS total_relevant,
        (SELECT COUNT(*) FROM draft_comments) AS total_drafts,
        (SELECT COUNT(*) FROM critiques WHERE verdict = 'approve') AS total_approved,
        (SELECT COUNT(*) FROM critiques WHERE verdict = 'reject') AS total_rejected`
    )
    .get() as ScanStats;

  return row;
}

export function getScans(): ScanWithCounts[] {
  const db = getDb();
  const rows = db
    .prepare(
      `WITH
        post_counts AS (
          SELECT scan_id, COUNT(*) AS post_count
          FROM posts
          GROUP BY scan_id
        ),
        eval_counts AS (
          SELECT scan_id, COUNT(*) AS eval_count
          FROM evaluations
          GROUP BY scan_id
        ),
        draft_counts AS (
          SELECT scan_id, COUNT(*) AS draft_count
          FROM draft_comments
          GROUP BY scan_id
        ),
        critique_counts AS (
          SELECT scan_id, COUNT(*) AS critique_count
          FROM critiques
          GROUP BY scan_id
        )
      SELECT
        s.*,
        COALESCE(pc.post_count, 0) AS post_count,
        COALESCE(ec.eval_count, 0) AS eval_count,
        COALESCE(dc.draft_count, 0) AS draft_count,
        COALESCE(cc.critique_count, 0) AS critique_count
      FROM scans s
      LEFT JOIN post_counts pc ON pc.scan_id = s.id
      LEFT JOIN eval_counts ec ON ec.scan_id = s.id
      LEFT JOIN draft_counts dc ON dc.scan_id = s.id
      LEFT JOIN critique_counts cc ON cc.scan_id = s.id
      ORDER BY s.id DESC`
    )
    .all() as ScanWithCounts[];

  return rows.filter((row) => !isHistoricalTrueEmptyCompletedScan(row));
}

export function getScanById(id: number): ScanDetailWithCounts | null {
  const db = getDb();
  // Cast to ScanDetail (no failures yet) — failures are appended below.
  const row = db
    .prepare(
      `SELECT
        s.*,
        (SELECT COUNT(*) FROM posts WHERE scan_id = s.id) AS post_count,
        (SELECT COUNT(*) FROM evaluations WHERE scan_id = s.id) AS eval_count,
        (SELECT COUNT(*) FROM draft_comments WHERE scan_id = s.id) AS draft_count,
        (SELECT COUNT(*) FROM critiques WHERE scan_id = s.id AND verdict = 'approve') AS approved_count,
        (SELECT COUNT(*) FROM critiques WHERE scan_id = s.id AND verdict = 'reject') AS rejected_count,
        (SELECT COUNT(*) FROM critiques WHERE scan_id = s.id AND verdict = 'revise') AS revised_count,
        (SELECT COUNT(*) FROM critiques WHERE scan_id = s.id) AS critique_count,
        (SELECT COUNT(*) FROM gate_blocks WHERE scan_id = s.id) AS gate_blocked_count
      FROM scans s
      WHERE s.id = ?`
    )
    .get(id) as (ScanDetail & { critique_count: number }) | undefined;

  if (!row || isHistoricalTrueEmptyCompletedScan(row)) {
    return null;
  }

  const failureRows = db
    .prepare(
      `SELECT id, scan_id, platform, context, kind, message,
              http_status, retry_after, retryable, created_at
       FROM scan_fetch_failures
       WHERE scan_id = ?
       ORDER BY id ASC`
    )
    .all(id) as Array<Omit<ScanFetchFailure, "retryable"> & { retryable: number }>;

  const failures: ScanFetchFailure[] = failureRows.map((f) => ({
    ...f,
    retryable: f.retryable === 1,
  }));

  const result: ScanDetailWithCounts = { ...row, failures };
  return result;
}

export function getPosts(filters?: PostFilters): Paginated<PostWithEvaluation> {
  const db = getDb();
  const conditions: string[] = [];
  const params: (string | number)[] = [];

  if (filters?.platform) {
    conditions.push("p.platform = ?");
    params.push(filters.platform);
  }
  if (filters?.relevant !== undefined) {
    conditions.push("e.relevant = ?");
    params.push(filters.relevant ? 1 : 0);
  }
  if (filters?.score_min !== undefined) {
    conditions.push("e.score >= ?");
    params.push(filters.score_min);
  }
  if (filters?.score_max !== undefined) {
    conditions.push("e.score <= ?");
    params.push(filters.score_max);
  }
  if (filters?.scan_id !== undefined) {
    conditions.push("p.scan_id = ?");
    params.push(filters.scan_id);
  }
  if (filters?.before_id !== undefined) {
    conditions.push("p.id < ?");
    params.push(filters.before_id);
  }

  const limit = filters?.limit ?? DEFAULT_PAGE_SIZE;
  const where = conditions.length > 0 ? `WHERE ${conditions.join(" AND ")}` : "";

  interface PostEvalRow {
    id: number;
    platform: string;
    platform_msg_id: string;
    channel_name: string | null;
    channel_id: string | null;
    author_name: string | null;
    author_id: string | null;
    content: string | null;
    url: string | null;
    created_at: string | null;
    scan_id: number | null;
    parent_lookup_status: string | null;
    parent_id: string | null;
    parent_author_id: string | null;
    parent_author_name: string | null;
    parent_text: string | null;
    parent_url: string | null;
    eval_id: number | null;
    relevant: number | null;
    score: number | null;
    reason: string | null;
    relevant_to: string | null;
    keyword_route_id: number | null;
    matched_route_id: number | null;
    matched_project_key: string | null;
    matched_keyword: string | null;
    matched_match_type: string | null;
    matched_intent: string | null;
    matched_positive_context: string | null;
    matched_negative_context: string | null;
    matched_evaluate_prompt: string | null;
    matched_respond_prompt: string | null;
    matched_critique_prompt: string | null;
    resolved_evaluate_prompt: string | null;
    resolved_respond_prompt: string | null;
    resolved_critique_prompt: string | null;
  }

  const rows = db
    .prepare(
      `SELECT
        p.*,
        e.id AS eval_id,
        e.relevant,
        e.score,
        e.reason,
        e.relevant_to,
        e.keyword_route_id,
        pk.id AS matched_route_id,
        pk.project_key AS matched_project_key,
        pk.keyword AS matched_keyword,
        pk.match_type AS matched_match_type,
        pk.intent AS matched_intent,
        pk.positive_context AS matched_positive_context,
        pk.negative_context AS matched_negative_context,
        pk.evaluate_prompt AS matched_evaluate_prompt,
        pk.respond_prompt AS matched_respond_prompt,
        pk.critique_prompt AS matched_critique_prompt,
        pe.body AS resolved_evaluate_prompt,
        pr.body AS resolved_respond_prompt,
        pc.body AS resolved_critique_prompt
      FROM posts p
      LEFT JOIN evaluations e ON e.post_id = p.id
      LEFT JOIN project_keywords pk ON pk.id = e.keyword_route_id
      LEFT JOIN prompt_templates pe ON pe.name = pk.evaluate_prompt AND pe.active = 1
      LEFT JOIN prompt_templates pr ON pr.name = pk.respond_prompt AND pr.active = 1
      LEFT JOIN prompt_templates pc ON pc.name = pk.critique_prompt AND pc.active = 1
      ${where}
      ORDER BY p.id DESC
      LIMIT ?`
    )
    .all(...params, limit + 1) as PostEvalRow[];

  const has_more = rows.length > limit;
  const data = (has_more ? rows.slice(0, limit) : rows).map((row) => {
    const parentCtx = toSourceParent(row);
    return {
      id: row.id,
      platform: row.platform,
      platform_msg_id: row.platform_msg_id,
      channel_name: row.channel_name,
      channel_id: row.channel_id,
      author_name: row.author_name,
      author_id: row.author_id,
      content: row.content,
      url: row.url,
      created_at: row.created_at,
      scan_id: row.scan_id,
      parent_lookup_status: parentCtx.parent_lookup_status,
      parent: parentCtx.parent,
      eval_id: row.eval_id,
      relevant: row.relevant !== null ? toBool(row.relevant as unknown as number) : null,
      score: row.score,
      reason: row.reason,
      relevant_to: parseRelevantTo(row.relevant_to as unknown as string | null),
      keyword_route_id: row.keyword_route_id,
      matched_route: toMatchedRoute(row),
    };
  });

  return { data, has_more };
}

export function getDrafts(filters?: DraftFilters): Paginated<DraftWithContext> {
  const db = getDb();
  const conditions: string[] = [];
  const params: (string | number)[] = [];

  if (filters?.project_key) {
    conditions.push("d.project_key = ?");
    params.push(filters.project_key);
  }
  if (filters?.verdict) {
    conditions.push("c.verdict = ?");
    params.push(filters.verdict);
  }
  if (filters?.scan_id !== undefined) {
    conditions.push("d.scan_id = ?");
    params.push(filters.scan_id);
  }
  if (filters?.before_id !== undefined) {
    conditions.push("d.id < ?");
    params.push(filters.before_id);
  }

  const limit = filters?.limit ?? DEFAULT_PAGE_SIZE;
  const where = conditions.length > 0 ? `WHERE ${conditions.join(" AND ")}` : "";

  interface DraftRow {
    draft_id: number;
    evaluation_id: number;
    project_key: string | null;
    comment_text: string | null;
    draft_created_at: string | null;
    verdict: string | null;
    feedback: string | null;
    post_id: number;
    platform: string;
    author_name: string | null;
    author_id: string | null;
    content: string | null;
    url: string | null;
    score: number | null;
    scan_id: number | null;
    keyword_route_id: number | null;
    parent_lookup_status: string | null;
    parent_id: string | null;
    parent_author_id: string | null;
    parent_author_name: string | null;
    parent_text: string | null;
    parent_url: string | null;
    matched_route_id: number | null;
    matched_project_key: string | null;
    matched_keyword: string | null;
    matched_match_type: string | null;
    matched_intent: string | null;
    matched_positive_context: string | null;
    matched_negative_context: string | null;
    matched_evaluate_prompt: string | null;
    matched_respond_prompt: string | null;
    matched_critique_prompt: string | null;
    resolved_evaluate_prompt: string | null;
    resolved_respond_prompt: string | null;
    resolved_critique_prompt: string | null;
    surface_status: string | null;
    posture: string | null;
    dossier_revision: string | null;
    relevant: number;
  }

  const rows = db
    .prepare(
      `SELECT
        d.id AS draft_id,
        d.evaluation_id,
        d.project_key,
        d.comment_text,
        d.created_at AS draft_created_at,
        c.verdict,
        c.feedback,
        p.id AS post_id,
        p.platform,
        p.author_name,
        p.author_id,
        p.content,
        p.url,
        p.parent_lookup_status,
        p.parent_id,
        p.parent_author_id,
        p.parent_author_name,
        p.parent_text,
        p.parent_url,
        e.score,
        e.surface_status,
        e.relevant,
        d.scan_id,
        d.posture,
        d.dossier_revision,
        e.keyword_route_id,
        pk.id AS matched_route_id,
        pk.project_key AS matched_project_key,
        pk.keyword AS matched_keyword,
        pk.match_type AS matched_match_type,
        pk.intent AS matched_intent,
        pk.positive_context AS matched_positive_context,
        pk.negative_context AS matched_negative_context,
        pk.evaluate_prompt AS matched_evaluate_prompt,
        pk.respond_prompt AS matched_respond_prompt,
        pk.critique_prompt AS matched_critique_prompt,
        pe.body AS resolved_evaluate_prompt,
        pr.body AS resolved_respond_prompt,
        pc.body AS resolved_critique_prompt
      FROM draft_comments d
      JOIN posts p ON p.id = d.post_id
      JOIN evaluations e ON e.id = d.evaluation_id
      LEFT JOIN project_keywords pk ON pk.id = e.keyword_route_id
      LEFT JOIN prompt_templates pe ON pe.name = pk.evaluate_prompt AND pe.active = 1
      LEFT JOIN prompt_templates pr ON pr.name = pk.respond_prompt AND pr.active = 1
      LEFT JOIN prompt_templates pc ON pc.name = pk.critique_prompt AND pc.active = 1
      LEFT JOIN critiques c ON c.draft_id = d.id
      ${where}
      ORDER BY d.id DESC
      LIMIT ?`
    )
    .all(...params, limit + 1) as DraftRow[];

  const has_more = rows.length > limit;
  const data = (has_more ? rows.slice(0, limit) : rows).map((row) => {
    const parentCtx = toSourceParent(row);
    return {
      draft_id: row.draft_id,
      evaluation_id: row.evaluation_id,
      project_key: row.project_key,
      comment_text: row.comment_text,
      draft_created_at: row.draft_created_at,
      verdict: row.verdict,
      feedback: row.feedback,
      post_id: row.post_id,
      platform: row.platform,
      author_name: row.author_name,
      author_id: row.author_id,
      content: row.content,
      url: row.url,
      score: row.score,
      scan_id: row.scan_id,
      keyword_route_id: row.keyword_route_id,
      parent_lookup_status: parentCtx.parent_lookup_status,
      parent: parentCtx.parent,
      matched_route: toMatchedRoute(row),
      surface_status: row.surface_status ?? null,
      posture: row.posture ?? null,
      dossier_revision: row.dossier_revision ?? null,
      relevant: toBool(row.relevant),
    };
  });
  return { data, has_more };
}

export function getProjectKeys(): string[] {
  const db = getDb();
  const rows = db
    .prepare(
      `SELECT DISTINCT project_key FROM draft_comments
       WHERE project_key IS NOT NULL
       ORDER BY project_key`
    )
    .all() as Array<{ project_key: string }>;

  return rows.map((row) => row.project_key);
}

// Grade queries

export function getPostScanId(postId: number): number | null {
  const db = getDb();
  const row = db
    .prepare("SELECT scan_id FROM posts WHERE id = ?")
    .get(postId) as { scan_id: number | null } | undefined;
  return row?.scan_id ?? null;
}

export interface GradeableEvaluation {
  id: number;
  post_id: number;
  scan_id: number | null;
  posture: string | null;
  relevant: number;
}

export function getEvaluationById(evaluationId: number): GradeableEvaluation | null {
  const db = getDb();
  const row = db
    .prepare("SELECT id, post_id, scan_id, posture, relevant FROM evaluations WHERE id = ?")
    .get(evaluationId) as GradeableEvaluation | undefined;
  return row ?? null;
}

export function getEvaluationForScanPost(
  postId: number,
  scanId: number
): GradeableEvaluation | null {
  const db = getDb();
  const row = db
    .prepare(
      "SELECT id, post_id, scan_id, posture, relevant FROM evaluations "
      + "WHERE post_id = ? AND scan_id = ? ORDER BY id DESC LIMIT 1"
    )
    .get(postId, scanId) as GradeableEvaluation | undefined;
  return row ?? null;
}

function getEvaluationIdForPostInScan(
  postId: number,
  scanId: number | null
): number | null {
  const db = getDb();
  const row = db
    .prepare(
      scanId === null
        ? `SELECT id FROM evaluations WHERE post_id = ?
           ORDER BY id DESC LIMIT 1`
        : `SELECT id FROM evaluations WHERE post_id = ? AND scan_id = ?
           ORDER BY id DESC LIMIT 1`
    )
    .get(...(scanId === null ? [postId] : [postId, scanId])) as
    | { id: number }
    | undefined;
  return row?.id ?? null;
}

function parseGradeRow(row: Record<string, unknown> | undefined): Grade | null {
  if (!row) return null;
  return {
    ...row,
    dimensions: row.dimensions
      ? (() => { try { return JSON.parse(row.dimensions as string); } catch { return null; } })()
      : null,
  } as Grade;
}

export function getGradeByPostId(postId: number): Grade | null {
  const db = getDb();
  const row = db
    .prepare("SELECT * FROM grades WHERE post_id = ? ORDER BY graded_at DESC, id DESC LIMIT 1")
    .get(postId) as Record<string, unknown> | undefined;
  return parseGradeRow(row);
}

export function getGradeByScanPost(postId: number, scanId: number): Grade | null {
  const db = getDb();
  const evaluationId = getEvaluationIdForPostInScan(postId, scanId);
  if (evaluationId === null) return null;
  const row = db
    .prepare("SELECT * FROM grades WHERE evaluation_id = ?")
    .get(evaluationId) as Record<string, unknown> | undefined;
  return parseGradeRow(row);
}

export function getGradingProgress(scanId: number): GradingProgress {
  const db = getDb();
  const row = db
    .prepare(
      `SELECT
        (SELECT COUNT(*)
         FROM evaluations e
         INNER JOIN grades g ON g.evaluation_id = e.id
         WHERE e.scan_id = ?
           AND g.schema_version = 2 AND g.needs_regrade = 0) AS graded,
        (SELECT COUNT(*) FROM evaluations WHERE scan_id = ?) AS total`
    )
    .get(scanId, scanId) as GradingProgress;
  return row;
}

export function getGradeByEvaluationId(evaluationId: number): Grade | null {
  const db = getDb();
  return parseGradeRow(db.prepare("SELECT * FROM grades WHERE evaluation_id = ?").get(evaluationId) as Record<string, unknown> | undefined);
}

export function getDraftsWithGrades(filters?: DraftFilters): Paginated<DraftWithGrade> {
  const db = getDb();
  const conditions: string[] = [];
  const params: (string | number)[] = [];

  if (filters?.project_key) {
    conditions.push("d.project_key = ?");
    params.push(filters.project_key);
  }
  if (filters?.verdict) {
    conditions.push("c.verdict = ?");
    params.push(filters.verdict);
  }
  if (filters?.scan_id !== undefined) {
    conditions.push("d.scan_id = ?");
    params.push(filters.scan_id);
  }
  if (filters?.before_id !== undefined) {
    conditions.push("d.id < ?");
    params.push(filters.before_id);
  }

  const limit = filters?.limit ?? DEFAULT_PAGE_SIZE;
  const where = conditions.length > 0 ? `WHERE ${conditions.join(" AND ")}` : "";

  interface DraftGradeRow {
    draft_id: number;
    evaluation_id: number;
    project_key: string | null;
    comment_text: string | null;
    draft_created_at: string | null;
    verdict: string | null;
    feedback: string | null;
    post_id: number;
    platform: string;
    author_name: string | null;
    author_id: string | null;
    content: string | null;
    url: string | null;
    score: number | null;
    scan_id: number | null;
    keyword_route_id: number | null;
    parent_lookup_status: string | null;
    parent_id: string | null;
    parent_author_id: string | null;
    parent_author_name: string | null;
    parent_text: string | null;
    parent_url: string | null;
    grade_id: number | null;
    grade_evaluation_id: number | null;
    grade_post_id: number | null;
    grade_scan_id: number | null;
    grade_source: string | null;
    grade_graded_at: string | null;
    grade_schema_version: number | null;
    grade_needs_regrade: number | null;
    grade_relevance_judgment: string | null;
    grade_action_judgment: string | null;
    grade_dimensions: string | null;
    grade_failure_note: string | null;
    grade_factual_offending_claim: string | null;
    grade_factual_disposition: string | null;
    grade_factual_contradicting_evidence: string | null;
    grade_context_missing_input: string | null;
    grade_posture_should_have_been: string | null;
    grade_implication_implied_claim: string | null;
    grade_implication_missing_support: string | null;
    matched_route_id: number | null;
    matched_project_key: string | null;
    matched_keyword: string | null;
    matched_match_type: string | null;
    matched_intent: string | null;
    matched_positive_context: string | null;
    matched_negative_context: string | null;
    matched_evaluate_prompt: string | null;
    matched_respond_prompt: string | null;
    matched_critique_prompt: string | null;
    resolved_evaluate_prompt: string | null;
    resolved_respond_prompt: string | null;
    resolved_critique_prompt: string | null;
    surface_status: string | null;
    posture: string | null;
    dossier_revision: string | null;
    relevant: number;
  }

  const rows = db
    .prepare(
      `SELECT
        d.id AS draft_id,
        d.evaluation_id,
        d.project_key,
        d.comment_text,
        d.created_at AS draft_created_at,
        c.verdict,
        c.feedback,
        p.id AS post_id,
        p.platform,
        p.author_name,
        p.author_id,
        p.content,
        p.url,
        p.parent_lookup_status,
        p.parent_id,
        p.parent_author_id,
        p.parent_author_name,
        p.parent_text,
        p.parent_url,
        e.score,
        e.surface_status,
        e.relevant,
        d.scan_id,
        d.posture,
        d.dossier_revision,
        e.keyword_route_id,
        pk.id AS matched_route_id,
        pk.project_key AS matched_project_key,
        pk.keyword AS matched_keyword,
        pk.match_type AS matched_match_type,
        pk.intent AS matched_intent,
        pk.positive_context AS matched_positive_context,
        pk.negative_context AS matched_negative_context,
        pk.evaluate_prompt AS matched_evaluate_prompt,
        pk.respond_prompt AS matched_respond_prompt,
        pk.critique_prompt AS matched_critique_prompt,
        pe.body AS resolved_evaluate_prompt,
        pr.body AS resolved_respond_prompt,
        pc.body AS resolved_critique_prompt,
        g.id AS grade_id,
        g.evaluation_id AS grade_evaluation_id,
        g.post_id AS grade_post_id,
        g.scan_id AS grade_scan_id,
        g.source AS grade_source,
        g.graded_at AS grade_graded_at,
        g.schema_version AS grade_schema_version,
        g.needs_regrade AS grade_needs_regrade,
        g.relevance_judgment AS grade_relevance_judgment,
        g.action_judgment AS grade_action_judgment,
        g.dimensions AS grade_dimensions,
        g.failure_note AS grade_failure_note,
        g.factual_offending_claim AS grade_factual_offending_claim,
        g.factual_disposition AS grade_factual_disposition,
        g.factual_contradicting_evidence AS grade_factual_contradicting_evidence,
        g.context_missing_input AS grade_context_missing_input,
        g.posture_should_have_been AS grade_posture_should_have_been,
        g.implication_implied_claim AS grade_implication_implied_claim,
        g.implication_missing_support AS grade_implication_missing_support
      FROM draft_comments d
      JOIN posts p ON p.id = d.post_id
      JOIN evaluations e ON e.id = d.evaluation_id
      LEFT JOIN project_keywords pk ON pk.id = e.keyword_route_id
      LEFT JOIN prompt_templates pe ON pe.name = pk.evaluate_prompt AND pe.active = 1
      LEFT JOIN prompt_templates pr ON pr.name = pk.respond_prompt AND pr.active = 1
      LEFT JOIN prompt_templates pc ON pc.name = pk.critique_prompt AND pc.active = 1
      LEFT JOIN critiques c ON c.draft_id = d.id
      LEFT JOIN grades g ON g.evaluation_id = e.id
      ${where}
      ORDER BY d.id DESC
      LIMIT ?`
    )
    .all(...params, limit + 1) as DraftGradeRow[];

  const has_more = rows.length > limit;
  const sliced = has_more ? rows.slice(0, limit) : rows;

  const revisionMeta = getGradeRevisionMetaBatch(
    sliced.filter((row) => row.grade_id !== null).map((row) => row.grade_id!)
  );

  const data = sliced.map((row) => {
    const parentCtx = toSourceParent(row);
    return {
      draft_id: row.draft_id,
      evaluation_id: row.evaluation_id,
      project_key: row.project_key,
      comment_text: row.comment_text,
      draft_created_at: row.draft_created_at,
      verdict: row.verdict,
      feedback: row.feedback,
      post_id: row.post_id,
      platform: row.platform,
      author_name: row.author_name,
      author_id: row.author_id,
      content: row.content,
      url: row.url,
      score: row.score,
      scan_id: row.scan_id,
      keyword_route_id: row.keyword_route_id,
      parent_lookup_status: parentCtx.parent_lookup_status,
      parent: parentCtx.parent,
      matched_route: toMatchedRoute(row),
      surface_status: row.surface_status ?? null,
      posture: row.posture ?? null,
      dossier_revision: row.dossier_revision ?? null,
      relevant: toBool(row.relevant),
      grade: row.grade_id
        ? {
            id: row.grade_id,
            evaluation_id: row.grade_evaluation_id,
            post_id: row.grade_post_id!,
            scan_id: row.grade_scan_id,
            source: row.grade_source!,
            graded_at: row.grade_graded_at!,
            schema_version: row.grade_schema_version ?? 1,
            needs_regrade: row.grade_needs_regrade ?? 0,
            relevance_judgment: row.grade_relevance_judgment!,
            action_judgment: row.grade_action_judgment ?? null,
            dimensions: row.grade_dimensions
              ? (() => { try { return JSON.parse(row.grade_dimensions); } catch { return null; } })()
              : null,
            failure_note: row.grade_failure_note ?? null,
            factual_offending_claim: row.grade_factual_offending_claim ?? null,
            factual_disposition: row.grade_factual_disposition ?? null,
            factual_contradicting_evidence: row.grade_factual_contradicting_evidence ?? null,
            context_missing_input: row.grade_context_missing_input ?? null,
            posture_should_have_been: row.grade_posture_should_have_been ?? null,
            implication_implied_claim: row.grade_implication_implied_claim ?? null,
            implication_missing_support: row.grade_implication_missing_support ?? null,
            ...revisionMeta.get(row.grade_id),
          } as Grade
        : null,
    };
  });

  return { data, has_more };
}

interface ReviewEvaluationQuery {
  whereClause: string;
  params: Array<string | number>;
  orderBy: string;
  limit?: number;
}

function getReviewEvaluations({
  whereClause,
  params,
  orderBy,
  limit,
}: ReviewEvaluationQuery): ReviewEvaluation[] {
  const db = getDb();
  const columnCache = new Map<string, Set<string>>();
  const columns = (table: string) => {
    const cached = columnCache.get(table);
    if (cached) return cached;
    const found = new Set((db.prepare(`PRAGMA table_info(${table})`).all() as Array<{ name: string }>).map((row) => row.name));
    columnCache.set(table, found);
    return found;
  };
  const evaluationColumns = columns("evaluations");
  const critiqueColumns = columns("critiques");
  const tableNames = new Set((db.prepare("SELECT name FROM sqlite_master WHERE type = 'table'").all() as Array<{ name: string }>).map((row) => row.name));
  const optional = (table: string, column: string, alias: string, qualifier = table) =>
    columns(table).has(column) ? `${qualifier}.${column} AS ${alias}` : `NULL AS ${alias}`;
  const critiqueJoin = critiqueColumns.has("evaluation_id")
    ? `LEFT JOIN critiques c ON c.id = (
         SELECT candidate.id
         FROM critiques candidate
         WHERE candidate.evaluation_id = e.id
            OR (candidate.evaluation_id IS NULL AND candidate.draft_id = d.id)
         ORDER BY CASE WHEN candidate.evaluation_id = e.id THEN 0 ELSE 1 END, candidate.id DESC
         LIMIT 1
       )`
    : `LEFT JOIN critiques c ON c.id = (
         SELECT legacy.id
         FROM critiques legacy
         WHERE legacy.draft_id = d.id
         ORDER BY legacy.id DESC
         LIMIT 1
       )`;
  interface EvalRow {
    id: number;
    post_id: number;
    relevant: number;
    score: number;
    reason: string | null;
    relevant_to: string | null;
    keyword_route_id: number | null;
    scan_id: number | null;
    surface_status: ReviewEvaluation["surface_status"];
    posture: string | null;
    project_key: string | null;
    failure_reason: string | null;
    dossier_revision: string | null;
    dossier_summary_id: string | null;
    post_platform: string;
    post_platform_msg_id: string;
    post_channel_name: string | null;
    post_channel_id: string | null;
    post_author_name: string | null;
    post_author_id: string | null;
    post_content: string | null;
    post_url: string | null;
    post_created_at: string | null;
    parent_lookup_status: ParentLookupStatus;
    parent_id: string | null;
    parent_author_id: string | null;
    parent_author_name: string | null;
    parent_text: string | null;
    parent_url: string | null;
    draft_id: number | null;
    draft_comment_text: string | null;
    draft_created_at: string | null;
    draft_posture: string | null;
    draft_structured_output: string | null;
    draft_dossier_revision: string | null;
    draft_dossier_summary_id: string | null;
    critique_id: number | null;
    critique_draft_id: number | null;
    critique_evaluation_id: number | null;
    critique_verdict: string | null;
    critique_feedback: string | null;
    critique_created_at: string | null;
    critique_scan_id: number | null;
    matched_route_id: number | null;
    matched_project_key: string | null;
    matched_keyword: string | null;
    matched_match_type: string | null;
    matched_intent: string | null;
    matched_positive_context: string | null;
    matched_negative_context: string | null;
    matched_evaluate_prompt: string | null;
    matched_respond_prompt: string | null;
    matched_critique_prompt: string | null;
    resolved_evaluate_prompt: string | null;
    resolved_respond_prompt: string | null;
    resolved_critique_prompt: string | null;
  }

  const queryParams: Array<string | number> = [...params];
  const limitClause = limit === undefined ? "" : "LIMIT ?";
  if (limit !== undefined) queryParams.push(limit);

  const rows = db
    .prepare(
      `SELECT
        e.*,
        p.platform AS post_platform, p.platform_msg_id AS post_platform_msg_id,
        p.channel_name AS post_channel_name, p.channel_id AS post_channel_id,
        p.author_name AS post_author_name, p.author_id AS post_author_id,
        p.content AS post_content, p.url AS post_url, p.created_at AS post_created_at,
        p.parent_lookup_status, p.parent_id, p.parent_author_id, p.parent_author_name,
        p.parent_text, p.parent_url,
        d.id AS draft_id, d.comment_text AS draft_comment_text,
        d.created_at AS draft_created_at, d.posture AS draft_posture,
        ${optional("draft_comments", "structured_output", "draft_structured_output", "d")},
        d.dossier_revision AS draft_dossier_revision,
        ${optional("draft_comments", "dossier_summary_id", "draft_dossier_summary_id", "d")},
        c.id AS critique_id, c.draft_id AS critique_draft_id,
        ${optional("critiques", "evaluation_id", "critique_evaluation_id", "c")},
        c.verdict AS critique_verdict, c.feedback AS critique_feedback,
        c.created_at AS critique_created_at, c.scan_id AS critique_scan_id,
        pk.id AS matched_route_id,
        pk.project_key AS matched_project_key,
        pk.keyword AS matched_keyword,
        pk.match_type AS matched_match_type,
        pk.intent AS matched_intent,
        pk.positive_context AS matched_positive_context,
        pk.negative_context AS matched_negative_context,
        pk.evaluate_prompt AS matched_evaluate_prompt,
        pk.respond_prompt AS matched_respond_prompt,
        pk.critique_prompt AS matched_critique_prompt,
        pe.body AS resolved_evaluate_prompt,
        pr.body AS resolved_respond_prompt,
        pc.body AS resolved_critique_prompt
      FROM evaluations e
      JOIN posts p ON p.id = e.post_id
      LEFT JOIN draft_comments d ON d.evaluation_id = e.id
      ${critiqueJoin}
      LEFT JOIN project_keywords pk ON pk.id = e.keyword_route_id
      LEFT JOIN prompt_templates pe ON pe.name = pk.evaluate_prompt AND pe.active = 1
      LEFT JOIN prompt_templates pr ON pr.name = pk.respond_prompt AND pr.active = 1
      LEFT JOIN prompt_templates pc ON pc.name = pk.critique_prompt AND pc.active = 1
      WHERE ${whereClause}
      ORDER BY ${orderBy}
      ${limitClause}`
    )
    .all(...queryParams) as EvalRow[];

  if (rows.length === 0) return [];

  const evaluationIds = JSON.stringify(rows.map((row) => row.id));

  const violations = tableNames.has("gate_blocks") ? db.prepare(
    `SELECT id, reason_code, offending_text, segment_index, project_key,
            dossier_summary_id, dossier_revision, scan_id, post_id,
            evaluation_id, context, created_at
       FROM gate_blocks
       WHERE evaluation_id IN (
         SELECT CAST(value AS INTEGER) FROM json_each(?)
       )
       ORDER BY id`
  ).all(evaluationIds) as GateBlock[] : [];
  const violationsByEvaluation = new Map<number, GateBlock[]>();
  for (const violation of violations) {
    if (violation.evaluation_id === null) continue;
    const list = violationsByEvaluation.get(violation.evaluation_id) ?? [];
    list.push(violation);
    violationsByEvaluation.set(violation.evaluation_id, list);
  }
  const gradesByEvaluation = new Map<number, Grade>();
  if (tableNames.has("grades")) {
    const grades = db.prepare(
      `SELECT g.* FROM grades g
       WHERE g.evaluation_id IN (
         SELECT CAST(value AS INTEGER) FROM json_each(?)
       )`
    ).all(evaluationIds) as Array<Record<string, unknown>>;
    const revisionMeta = getGradeRevisionMetaBatch(
      grades.map((grade) => grade.id as number)
    );
    for (const grade of grades) {
      const parsed = parseGradeRow(grade);
      if (parsed !== null && parsed.evaluation_id !== null) {
        gradesByEvaluation.set(parsed.evaluation_id, {
          ...parsed,
          ...revisionMeta.get(grade.id as number),
        });
      }
    }
  }

  return rows.map((row) => {
    const parent = toSourceParent({
      parent_lookup_status: row.parent_lookup_status,
      parent_id: row.parent_id,
      parent_author_id: row.parent_author_id,
      parent_author_name: row.parent_author_name,
      parent_text: row.parent_text,
      parent_url: row.parent_url,
    });
    return {
      id: row.id, post_id: row.post_id, relevant: toBool(row.relevant),
      score: row.score, reason: row.reason, relevant_to: parseRelevantTo(row.relevant_to),
      keyword_route_id: row.keyword_route_id, scan_id: row.scan_id,
      surface_status: (evaluationColumns.has("surface_status") ? row.surface_status : "not_relevant") as ReviewEvaluation["surface_status"], failure_reason: evaluationColumns.has("failure_reason") ? row.failure_reason : null,
      project_key: row.project_key, posture: row.posture,
      dossier_revision: row.dossier_revision, dossier_summary_id: row.dossier_summary_id,
      matched_route: toMatchedRoute(row),
      post: {
        id: row.post_id, platform: row.post_platform, platform_msg_id: row.post_platform_msg_id,
        channel_name: row.post_channel_name, channel_id: row.post_channel_id,
        author_name: row.post_author_name, author_id: row.post_author_id,
        content: row.post_content, url: row.post_url, created_at: row.post_created_at,
        scan_id: row.scan_id, ...parent,
      },
      draft: row.draft_id === null ? null : {
        id: row.draft_id, post_id: row.post_id, evaluation_id: row.id,
        project_key: row.project_key, comment_text: row.draft_comment_text,
        created_at: row.draft_created_at, scan_id: row.scan_id,
        posture: row.draft_posture, structured_output: row.draft_structured_output,
        dossier_revision: row.draft_dossier_revision,
        dossier_summary_id: row.draft_dossier_summary_id,
      },
      critique: row.critique_id === null ? null : {
        id: row.critique_id, draft_id: row.critique_draft_id,
        evaluation_id: row.critique_evaluation_id,
        verdict: row.critique_verdict!, feedback: row.critique_feedback,
        created_at: row.critique_created_at, scan_id: row.critique_scan_id,
      },
      gate_violations: violationsByEvaluation.get(row.id) ?? [],
      grade: gradesByEvaluation.get(row.id) ?? null,
    };
  });
}

export function getEvaluationsByScan(scanId: number): ReviewEvaluation[] {
  return getReviewEvaluations({
    whereClause: "e.scan_id = ?",
    params: [scanId],
    orderBy: "e.score DESC",
  });
}

export function getNegativeGradingCases(
  filters: NegativeGradingFilters = {}
): Paginated<ReviewEvaluation> {
  const conditions = [
    "e.relevant = 0",
    "e.surface_status = 'not_relevant'",
    "e.scan_id IS NOT NULL",
    `(NOT EXISTS (
        SELECT 1 FROM grades current_grade
        WHERE current_grade.evaluation_id = e.id
          AND current_grade.schema_version = 2
          AND current_grade.needs_regrade = 0
      ) OR EXISTS (
        SELECT 1
        FROM grades current_grade
        LEFT JOIN human_positive_promotions promotion
          ON promotion.source_evaluation_id = current_grade.evaluation_id
        WHERE current_grade.evaluation_id = e.id
          AND current_grade.schema_version = 2
          AND current_grade.needs_regrade = 0
          AND current_grade.relevance_judgment = 'false_negative'
          AND (promotion.source_evaluation_id IS NULL OR promotion.status != 'completed')
      ))`,
  ];
  const params: Array<string | number> = [];
  if (filters.before_id !== undefined) {
    conditions.push("e.id < ?");
    params.push(filters.before_id);
  }

  const limit = filters.limit ?? DEFAULT_PAGE_SIZE;
  const rows = getReviewEvaluations({
    whereClause: conditions.join(" AND "),
    params,
    orderBy: "e.id DESC",
    limit: limit + 1,
  });
  const has_more = rows.length > limit;
  return {
    data: has_more ? rows.slice(0, limit) : rows,
    has_more,
  };
}

export function getGateBlocksByScan(scanId: number, limit = 100): GateBlock[] {
  const db = getDb();
  const rows = db
    .prepare(
      `SELECT id, reason_code, offending_text, segment_index, project_key,
              dossier_summary_id, dossier_revision, scan_id, post_id,
              evaluation_id, context, created_at
       FROM gate_blocks
       WHERE scan_id = ?
       ORDER BY id DESC
       LIMIT ?`
    )
    .all(scanId, limit) as GateBlock[];
  return rows;
}

export function getGateBlocksByProject(projectKey: string, limit = 100): GateBlock[] {
  const db = getDb();
  const rows = db
    .prepare(
      `SELECT id, reason_code, offending_text, segment_index, project_key,
              dossier_summary_id, dossier_revision, scan_id, post_id,
              evaluation_id, context, created_at
       FROM gate_blocks
       WHERE project_key = ?
       ORDER BY id DESC
       LIMIT ?`
    )
    .all(projectKey, limit) as GateBlock[];
  return rows;
}
