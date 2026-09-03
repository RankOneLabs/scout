import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// tests/fixtures/feedback_eligibility_cases.json is the single
// language-neutral source of first-exclusion-reason precedence and
// cap-behavior cases, also loaded by
// tests/test_evaluation_feedback.py::TestEligibilityFixtureConformance. A
// change to precedence must update both runtimes' verdicts identically or
// one of the two suites fails.
const FIXTURE_PATH = path.resolve(
  __dirname,
  "../../tests/fixtures/feedback_eligibility_cases.json"
);

interface FixtureRow {
  grade_id: number;
  post_id: number;
  scan_id: number | null;
  graded_at: string;
  schema_version: number;
  needs_regrade: boolean;
  relevance_judgment: string;
  action_judgment: string | null;
  dimensions: unknown;
  failure_note: string | null;
  factual_disposition: string | null;
  factual_offending_claim: string | null;
  factual_contradicting_evidence: string | null;
  context_missing_input: string | null;
  posture_should_have_been: string | null;
  implication_implied_claim: string | null;
  implication_missing_support: string | null;
  evaluation_id: number | null;
  evaluation_post_id: number | null;
  evaluation_scan_id: number | null;
  evaluation_project_key: string | null;
  evaluation_dossier_summary_id: string | null;
  evaluation_dossier_revision: string | null;
  evaluation_posture: string | null;
  draft_comment_id: number | null;
  draft_project_key: string | null;
  draft_dossier_summary_id: string | null;
  draft_dossier_revision: string | null;
  draft_posture: string | null;
  override_mode: string;
  override_reason: string | null;
}

interface PrecedenceCase {
  name: string;
  description: string;
  row: FixtureRow;
  expected: { status: "eligible" | "excluded"; reason: string | null };
}

interface CapCase {
  name: string;
  description: string;
  max_grades: number;
  lookback_days: number;
  as_of: string;
  rows: FixtureRow[];
  expected: Array<{ grade_id: number; status: "eligible" | "excluded"; reason: string | null }>;
}

interface Fixture {
  precedence_window: { as_of: string; lookback_days: number; max_grades: number };
  precedence_cases: PrecedenceCase[];
  cap_cases: CapCase[];
}

const fixture = JSON.parse(fs.readFileSync(FIXTURE_PATH, "utf8")) as Fixture;

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-feedback-eligibility-"));
const dbPath = path.join(tmpDir, "scout.db");
process.env.SCOUT_DB_PATH = dbPath;

function createSchema(db: Database.Database): void {
  db.exec(`
    CREATE TABLE grades (
      id INTEGER PRIMARY KEY, evaluation_id INTEGER, post_id INTEGER NOT NULL, scan_id INTEGER,
      source TEXT NOT NULL DEFAULT 'cli', graded_at TEXT NOT NULL,
      schema_version INTEGER NOT NULL DEFAULT 1, needs_regrade INTEGER NOT NULL DEFAULT 0,
      relevance_judgment TEXT NOT NULL, action_judgment TEXT, dimensions TEXT, failure_note TEXT,
      factual_offending_claim TEXT, factual_disposition TEXT, factual_contradicting_evidence TEXT,
      context_missing_input TEXT, posture_should_have_been TEXT,
      implication_implied_claim TEXT, implication_missing_support TEXT
    );
    CREATE TABLE evaluations (
      id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL, scan_id INTEGER,
      relevant INTEGER NOT NULL DEFAULT 1, score REAL NOT NULL DEFAULT 1.0, reason TEXT,
      project_key TEXT, posture TEXT, surface_status TEXT, failure_reason TEXT,
      dossier_summary_id TEXT, dossier_revision TEXT
    );
    CREATE TABLE draft_comments (
      id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL, evaluation_id INTEGER NOT NULL,
      project_key TEXT, comment_text TEXT, created_at TEXT, posture TEXT,
      dossier_summary_id TEXT, dossier_revision TEXT
    );
    CREATE TABLE grade_usage_overrides (
      id INTEGER PRIMARY KEY, grade_id INTEGER NOT NULL UNIQUE, mode TEXT NOT NULL,
      reason TEXT, updated_at TEXT NOT NULL
    );
    CREATE TABLE feedback_snapshots (
      id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL UNIQUE, policy_version TEXT NOT NULL,
      mode TEXT NOT NULL, as_of TEXT NOT NULL, lookback_days INTEGER NOT NULL, max_grades INTEGER NOT NULL,
      segment_min_grades INTEGER NOT NULL, note_max_chars INTEGER NOT NULL,
      relevance_token_budget INTEGER NOT NULL DEFAULT 800, reply_draft_token_budget INTEGER NOT NULL DEFAULT 800,
      critic_token_budget INTEGER NOT NULL DEFAULT 1000,
      population_count INTEGER NOT NULL, eligible_count INTEGER NOT NULL, excluded_count INTEGER NOT NULL,
      created_at TEXT NOT NULL
    );
  `);
}

function insertRow(db: Database.Database, row: FixtureRow): void {
  db.prepare(
    `INSERT INTO grades
       (id, evaluation_id, post_id, scan_id, source, graded_at, schema_version, needs_regrade,
        relevance_judgment, action_judgment, dimensions, failure_note, factual_offending_claim,
        factual_disposition, factual_contradicting_evidence, context_missing_input,
        posture_should_have_been, implication_implied_claim, implication_missing_support)
     VALUES (?, ?, ?, ?, 'cli', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).run(
    row.grade_id,
    row.evaluation_id,
    row.post_id,
    row.scan_id,
    row.graded_at,
    row.schema_version,
    row.needs_regrade ? 1 : 0,
    row.relevance_judgment,
    row.action_judgment,
    row.dimensions === null ? null : JSON.stringify(row.dimensions),
    row.failure_note,
    row.factual_offending_claim,
    row.factual_disposition,
    row.factual_contradicting_evidence,
    row.context_missing_input,
    row.posture_should_have_been,
    row.implication_implied_claim,
    row.implication_missing_support
  );

  if (row.evaluation_id !== null) {
    db.prepare(
      `INSERT OR IGNORE INTO evaluations
         (id, post_id, scan_id, project_key, posture, dossier_summary_id, dossier_revision)
       VALUES (?, ?, ?, ?, ?, ?, ?)`
    ).run(
      row.evaluation_id,
      row.evaluation_post_id,
      row.evaluation_scan_id,
      row.evaluation_project_key,
      row.evaluation_posture,
      row.evaluation_dossier_summary_id,
      row.evaluation_dossier_revision
    );

    if (row.draft_comment_id !== null) {
      db.prepare(
        `INSERT OR IGNORE INTO draft_comments
           (id, post_id, evaluation_id, project_key, posture, dossier_summary_id, dossier_revision)
         VALUES (?, ?, ?, ?, ?, ?, ?)`
      ).run(
        row.draft_comment_id,
        row.post_id,
        row.evaluation_id,
        row.draft_project_key,
        row.draft_posture,
        row.draft_dossier_summary_id,
        row.draft_dossier_revision
      );
    }
  }

  if (row.override_mode === "exclude") {
    db.prepare(
      `INSERT INTO grade_usage_overrides (grade_id, mode, reason, updated_at)
       VALUES (?, 'exclude', ?, ?)`
    ).run(row.grade_id, row.override_reason, row.graded_at);
  }
}

let nextSnapshotId = 1;
let nextScanId = 1;

function insertPolicySnapshot(
  db: Database.Database,
  policy: { lookback_days: number; max_grades: number },
  createdAt: string
): void {
  const id = nextSnapshotId++;
  const scanId = nextScanId++;
  db.prepare(
    `INSERT INTO feedback_snapshots
       (id, scan_id, policy_version, mode, as_of, lookback_days, max_grades, segment_min_grades,
        note_max_chars, population_count, eligible_count, excluded_count, created_at)
     VALUES (?, ?, 'evaluation-feedback/v1', 'shadow', ?, ?, ?, 1, 240, 0, 0, 0, ?)`
  ).run(id, scanId, createdAt, policy.lookback_days, policy.max_grades, createdAt);
}

let writeDb: Database.Database;

beforeAll(() => {
  writeDb = new Database(dbPath);
  createSchema(writeDb);

  // Precedence cases all share one wide-lookback, high-max_grades policy so
  // the cap never interferes with a single-row precedence verdict. This is
  // the only snapshot inserted before the precedence assertions run, so it
  // is guaranteed to be resolveFeedbackPolicy()'s "latest" snapshot for all
  // of them — resolveFeedbackPolicy always reads the single most-recent
  // snapshot row, not a policy scoped to whichever asOf a caller passes, so
  // every other snapshot below is inserted lazily (immediately before the
  // test that depends on it) rather than all upfront.
  insertPolicySnapshot(writeDb, fixture.precedence_window, "2020-01-01T00:00:00.000Z");
  for (const testCase of fixture.precedence_cases) {
    insertRow(writeDb, testCase.row);
  }
});

afterAll(() => {
  writeDb.close();
  delete process.env.SCOUT_DB_PATH;
  fs.rmSync(tmpDir, { recursive: true, force: true });
});

describe("feedback eligibility fixture conformance", () => {
  it.each(fixture.precedence_cases.map((c) => [c.name, c] as const))(
    "%s",
    async (_name, testCase) => {
      const { computeEligibilityWindow } = await import("@/lib/feedback-grade-queries");
      const window = computeEligibilityWindow(fixture.precedence_window.as_of);
      expect(window).not.toBeNull();
      const classification = window!.byGradeId.get(testCase.row.grade_id);
      expect(classification).toBeDefined();
      if (testCase.expected.status === "eligible") {
        expect(classification!.status).toBe("eligible");
        expect(classification!.reason).toBeNull();
      } else {
        expect(classification!.status).toBe("excluded");
        expect(classification!.reason).toBe(testCase.expected.reason);
      }
    }
  );

  // Each cap case's snapshot (and thus "latest resolved policy") and rows
  // are inserted immediately before that case's own assertion, in file
  // order — vitest runs tests within one file sequentially by default, so
  // each case observes exactly its own policy, never a later case's.
  it.each(fixture.cap_cases.map((c) => [c.name, c] as const))(
    "%s",
    async (_name, testCase) => {
      insertPolicySnapshot(
        writeDb,
        { lookback_days: testCase.lookback_days, max_grades: testCase.max_grades },
        testCase.as_of
      );
      for (const row of testCase.rows) {
        insertRow(writeDb, row);
      }

      const { computeEligibilityWindow } = await import("@/lib/feedback-grade-queries");
      const window = computeEligibilityWindow(testCase.as_of);
      expect(window).not.toBeNull();
      for (const expected of testCase.expected) {
        const classification = window!.byGradeId.get(expected.grade_id);
        expect(classification).toBeDefined();
        expect(classification!.status).toBe(expected.status);
        expect(classification!.reason).toBe(expected.reason);
      }
    }
  );
});
