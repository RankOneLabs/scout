import { getDb } from "@/lib/db";
import {
  HUMAN_GRADE_SCHEMA_VERSION,
  computeEligibilityWindow,
  getContractValidLinkedGradeIds,
} from "@/lib/feedback-grade-queries";
import type {
  CorpusCard,
  CoverageBucket,
  EligibilityReasonCount,
  EligibilitySummary,
  FailureDimensionBucket,
  FeedbackCorpusCards,
  FeedbackSegments,
  FeedbackSummaryResponse,
  LatestSnapshotSummary,
  RelevanceMetrics,
  ResponseAcceptance,
  SegmentEntry,
  SegmentSummary,
} from "@/types/feedback-summary";
import type { EligibilityReason } from "@/types/feedback-grades";

const MAX_SEGMENT_ENTRIES = 50;

const ELIGIBILITY_REASONS_TRACKED: EligibilityReason[] = [
  "eligible",
  "eligible_cap",
  "schema_version",
  "needs_regrade",
  "missing_evaluation_linkage",
  "mismatched_evaluation_identity",
  "shared_contract_invalid",
  "manual_exclude",
];

function validGradeWhere(): string {
  return "g.id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))";
}

function draftQualityWhere(): string {
  return `${validGradeWhere()}
    AND g.relevance_judgment = 'correct' AND e.relevant = 1 AND dc.id IS NOT NULL`;
}

function validGradeParameter(validGradeIds: number[]): string {
  // One JSON parameter avoids SQLite's host-parameter ceiling even when a
  // busy 90-day analytics window contains thousands of contract-valid rows.
  return JSON.stringify(validGradeIds);
}

function getCorpusCards(): FeedbackCorpusCards {
  const db = getDb();
  const row = db
    .prepare(
      `SELECT
         COUNT(*) AS total,
         SUM(CASE WHEN schema_version = ${HUMAN_GRADE_SCHEMA_VERSION} THEN 1 ELSE 0 END) AS current,
         SUM(CASE WHEN schema_version < ${HUMAN_GRADE_SCHEMA_VERSION} THEN 1 ELSE 0 END) AS legacy,
         SUM(CASE WHEN needs_regrade = 1 THEN 1 ELSE 0 END) AS needs_regrade
       FROM grades`
    )
    .get() as { total: number; current: number | null; legacy: number | null; needs_regrade: number | null };

  const denominator = row.total;
  const card = (count: number | null): CorpusCard => ({ count: count ?? 0, denominator });
  return {
    total: card(row.total),
    current: card(row.current),
    legacy: card(row.legacy),
    needs_regrade: card(row.needs_regrade),
  };
}

function getCoverage(from: string, to: string): CoverageBucket[] {
  const db = getDb();
  const rows = db
    .prepare(
      `SELECT e.scan_id AS scan_id, substr(s.started_at, 1, 10) AS day,
              SUM(CASE WHEN g.id IS NOT NULL AND g.schema_version = ${HUMAN_GRADE_SCHEMA_VERSION} THEN 1 ELSE 0 END) AS linked,
              COUNT(*) AS total
       FROM evaluations e
       JOIN scans s ON s.id = e.scan_id
       LEFT JOIN grades g ON g.evaluation_id = e.id
       WHERE e.scan_id IS NOT NULL AND s.started_at >= ? AND s.started_at <= ?
       GROUP BY e.scan_id, day
       ORDER BY day ASC, e.scan_id ASC`
    )
    .all(from, to) as CoverageBucket[];
  return rows;
}

function getRelevanceMetrics(validGradeIds: number[]): RelevanceMetrics {
  const db = getDb();
  const rows = db
    .prepare(
      `SELECT g.relevance_judgment AS relevance_judgment,
              SUM(CASE WHEN e.relevant = 1 THEN 1 ELSE 0 END) AS relevant_count,
              COUNT(*) AS count
       FROM grades g
       JOIN evaluations e ON e.id = g.evaluation_id
       LEFT JOIN draft_comments dc ON dc.evaluation_id = e.id
       WHERE ${validGradeWhere()}
       GROUP BY g.relevance_judgment`
    )
    .all(validGradeParameter(validGradeIds)) as Array<{
    relevance_judgment: string;
    relevant_count: number;
    count: number;
  }>;

  let correct = 0;
  let correctRelevant = 0;
  let falsePositive = 0;
  let falseNegative = 0;
  let reviewedModelNegative = 0;
  for (const row of rows) {
    reviewedModelNegative += row.count - row.relevant_count;
    if (row.relevance_judgment === "correct") {
      correct = row.count;
      correctRelevant = row.relevant_count;
    } else if (row.relevance_judgment === "false_positive") {
      falsePositive = row.count;
    } else if (row.relevance_judgment === "false_negative") {
      falseNegative = row.count;
    }
  }

  return {
    correct,
    false_positive: falsePositive,
    false_negative: falseNegative,
    reviewed_model_negative: reviewedModelNegative,
    correct_relevant: correctRelevant,
    precision_denominator: correctRelevant + falsePositive,
    recall_denominator: correctRelevant + falseNegative,
  };
}

function getResponseAcceptance(validGradeIds: number[]): ResponseAcceptance {
  const db = getDb();
  const row = db
    .prepare(
      `SELECT
         SUM(CASE WHEN g.action_judgment = 'accept' THEN 1 ELSE 0 END) AS accept,
         SUM(CASE WHEN g.action_judgment = 'fail' THEN 1 ELSE 0 END) AS fail,
         SUM(CASE WHEN g.action_judgment IS NULL THEN 1 ELSE 0 END) AS not_applicable
       FROM grades g
       JOIN evaluations e ON e.id = g.evaluation_id
       JOIN draft_comments dc ON dc.evaluation_id = e.id
       WHERE ${draftQualityWhere()}`
    )
    .get(validGradeParameter(validGradeIds)) as {
    accept: number | null;
    fail: number | null;
    not_applicable: number | null;
  };

  const accept = row.accept ?? 0;
  const fail = row.fail ?? 0;
  return { accept, fail, denominator: accept + fail, not_applicable: row.not_applicable ?? 0 };
}

function getFailureDimensionTrends(validGradeIds: number[]): FailureDimensionBucket[] {
  const db = getDb();
  const denomRows = db
    .prepare(
      `SELECT substr(g.graded_at, 1, 10) AS day, COUNT(*) AS denom
       FROM grades g
       JOIN evaluations e ON e.id = g.evaluation_id
       JOIN draft_comments dc ON dc.evaluation_id = e.id
       WHERE ${draftQualityWhere()} AND g.action_judgment = 'fail'
       GROUP BY day`
    )
    .all(validGradeParameter(validGradeIds)) as Array<{ day: string; denom: number }>;
  const denomByDay = new Map(denomRows.map((r) => [r.day, r.denom]));

  const dimRows = db
    .prepare(
      `SELECT substr(g.graded_at, 1, 10) AS day, je.value AS dimension, COUNT(*) AS count
       FROM grades g
       JOIN evaluations e ON e.id = g.evaluation_id
       JOIN draft_comments dc ON dc.evaluation_id = e.id,
            json_each(g.dimensions) je
       WHERE ${draftQualityWhere()} AND g.action_judgment = 'fail'
       GROUP BY day, dimension
       ORDER BY day ASC, dimension ASC`
    )
    .all(validGradeParameter(validGradeIds)) as Array<{
    day: string;
    dimension: string;
    count: number;
  }>;

  return dimRows.map((row) => ({
    day: row.day,
    dimension: row.dimension,
    count: row.count,
    failed_draft_denominator: denomByDay.get(row.day) ?? 0,
  }));
}

interface SegmentRow {
  label: string | null;
  relevance_judgment: string;
  count: number;
}

function buildSegmentSummary(rows: SegmentRow[]): SegmentSummary {
  const byLabel = new Map<string, SegmentEntry>();
  let denominator = 0;
  for (const row of rows) {
    if (row.label === null) continue;
    denominator += row.count;
    const entry = byLabel.get(row.label) ?? {
      label: row.label,
      count: 0,
      correct: 0,
      false_positive: 0,
      false_negative: 0,
    };
    entry.count += row.count;
    if (row.relevance_judgment === "correct") entry.correct += row.count;
    else if (row.relevance_judgment === "false_positive") entry.false_positive += row.count;
    else if (row.relevance_judgment === "false_negative") entry.false_negative += row.count;
    byLabel.set(row.label, entry);
  }

  const sorted = Array.from(byLabel.values()).sort(
    (a, b) => b.count - a.count || a.label.localeCompare(b.label)
  );
  const entries = sorted.slice(0, MAX_SEGMENT_ENTRIES);
  const remainder = sorted.slice(MAX_SEGMENT_ENTRIES);
  const other: SegmentEntry | null =
    remainder.length > 0
      ? remainder.reduce<SegmentEntry>(
          (acc, e) => ({
            label: "other",
            count: acc.count + e.count,
            correct: acc.correct + e.correct,
            false_positive: acc.false_positive + e.false_positive,
            false_negative: acc.false_negative + e.false_negative,
          }),
          { label: "other", count: 0, correct: 0, false_positive: 0, false_negative: 0 }
        )
      : null;

  return { entries, other, denominator };
}

function getSegmentColumn(column: string, validGradeIds: number[]): SegmentRow[] {
  const db = getDb();
  return db
    .prepare(
      `SELECT ${column} AS label, g.relevance_judgment AS relevance_judgment, COUNT(*) AS count
       FROM grades g
       JOIN evaluations e ON e.id = g.evaluation_id
       LEFT JOIN draft_comments dc ON dc.evaluation_id = e.id
       LEFT JOIN posts p ON p.id = g.post_id
       WHERE ${validGradeWhere()} AND ${column} IS NOT NULL
       GROUP BY label, g.relevance_judgment`
    )
    .all(validGradeParameter(validGradeIds)) as SegmentRow[];
}

function getSegments(validGradeIds: number[]): FeedbackSegments {
  return {
    project: buildSegmentSummary(getSegmentColumn("e.project_key", validGradeIds)),
    platform: buildSegmentSummary(getSegmentColumn("p.platform", validGradeIds)),
    posture: buildSegmentSummary(getSegmentColumn("e.posture", validGradeIds)),
    terminal_status: buildSegmentSummary(getSegmentColumn("e.surface_status", validGradeIds)),
  };
}

function getEligibilitySummary(asOf: string): EligibilitySummary {
  const window = computeEligibilityWindow(asOf);
  const counts = new Map<EligibilityReason, number>(ELIGIBILITY_REASONS_TRACKED.map((r) => [r, 0]));
  if (window !== null) {
    for (const classification of window.byGradeId.values()) {
      const key: EligibilityReason =
        classification.status === "eligible" ? "eligible" : (classification.reason as EligibilityReason);
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
  }
  const byReason: EligibilityReasonCount[] = ELIGIBILITY_REASONS_TRACKED.map((reason) => ({
    reason,
    count: counts.get(reason) ?? 0,
  }));

  let outsideLookbackCount = 0;
  if (window !== null) {
    const db = getDb();
    const row = db
      .prepare(`SELECT COUNT(*) AS count FROM grades WHERE graded_at < ?`)
      .get(window.boundary) as { count: number };
    outsideLookbackCount = row.count;
  }

  return {
    in_lookback_population: window?.populationCount ?? 0,
    eligible_after_cap: window?.eligibleCount ?? 0,
    outside_lookback_count: outsideLookbackCount,
    by_reason: byReason,
    resolved_lookback_days: window?.policy.lookback_days ?? 0,
    resolved_max_grades: window?.policy.max_grades ?? 0,
  };
}

interface LatestSnapshotRow {
  id: number;
  scan_id: number;
  policy_version: string;
  mode: string;
  created_at: string;
  population_count: number;
  eligible_count: number;
  excluded_count: number;
}

function getLatestSnapshotSummary(): LatestSnapshotSummary | null {
  const db = getDb();
  const row = db
    .prepare(
      `SELECT id, scan_id, policy_version, mode, created_at,
              population_count, eligible_count, excluded_count
       FROM feedback_snapshots
       ORDER BY created_at DESC, id DESC
       LIMIT 1`
    )
    .get() as LatestSnapshotRow | undefined;
  if (row === undefined) return null;

  let usedGradeCount = 0;
  if (row.mode === "active") {
    const usedRow = db
      .prepare(
        `SELECT COUNT(DISTINCT fsi.grade_id) AS used_count
         FROM feedback_snapshot_items fsi
         JOIN feedback_snapshot_phases fsp ON fsp.id = fsi.snapshot_phase_id
         WHERE fsp.snapshot_id = ? AND fsi.role IN ('aggregate', 'example')`
      )
      .get(row.id) as { used_count: number };
    usedGradeCount = usedRow.used_count;
  }

  return {
    snapshot_id: row.id,
    scan_id: row.scan_id,
    policy_version: row.policy_version,
    mode: row.mode as LatestSnapshotSummary["mode"],
    created_at: row.created_at,
    population_count: row.population_count,
    eligible_count: row.eligible_count,
    excluded_count: row.excluded_count,
    used_grade_count: usedGradeCount,
  };
}

export function getFeedbackSummary(asOf: string, from: string, to: string): FeedbackSummaryResponse {
  const db = getDb();
  const validGradeIds = getContractValidLinkedGradeIds(from, to);
  const policyRow = db
    .prepare(
      `SELECT policy_version, lookback_days, max_grades
       FROM feedback_snapshots ORDER BY created_at DESC, id DESC LIMIT 1`
    )
    .get() as { policy_version: string; lookback_days: number; max_grades: number } | undefined;

  return {
    as_of: asOf,
    from,
    to,
    timezone: "UTC",
    policy: policyRow ?? null,
    corpus: getCorpusCards(),
    coverage: getCoverage(from, to),
    relevance: getRelevanceMetrics(validGradeIds),
    response_acceptance: getResponseAcceptance(validGradeIds),
    failure_dimension_trends: getFailureDimensionTrends(validGradeIds),
    segments: getSegments(validGradeIds),
    eligibility: getEligibilitySummary(asOf),
    latest_snapshot: getLatestSnapshotSummary(),
  };
}
