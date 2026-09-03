import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// Small, dedicated fixture for eligibility-summary + latest-snapshot
// aggregation — the classifier itself (all six exclusion reasons) is
// exhaustively tested against the ported classifier in
// feedback-grade-queries.test.ts; this file only verifies the summary
// layer aggregates that classifier's output correctly.
const AS_OF = "2026-02-01T00:00:00.000Z"; // boundary with lookback_days=1 -> 2026-01-31

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-feedback-eligibility-"));
const dbPath = path.join(tmpDir, "scout.db");
process.env.SCOUT_DB_PATH = dbPath;

beforeAll(() => {
  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE scans (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL);
    CREATE TABLE posts (id INTEGER PRIMARY KEY, platform TEXT NOT NULL, platform_msg_id TEXT NOT NULL);
    CREATE TABLE evaluations (
      id INTEGER PRIMARY KEY, post_id INTEGER, scan_id INTEGER, relevant INTEGER, score REAL,
      project_key TEXT, posture TEXT, surface_status TEXT, dossier_summary_id TEXT, dossier_revision TEXT
    );
    CREATE TABLE draft_comments (
      id INTEGER PRIMARY KEY, post_id INTEGER, evaluation_id INTEGER, project_key TEXT, posture TEXT,
      dossier_summary_id TEXT, dossier_revision TEXT
    );
    CREATE TABLE grades (
      id INTEGER PRIMARY KEY, evaluation_id INTEGER, post_id INTEGER NOT NULL, scan_id INTEGER,
      source TEXT NOT NULL, graded_at TEXT NOT NULL, schema_version INTEGER NOT NULL DEFAULT 1,
      needs_regrade INTEGER NOT NULL DEFAULT 0, relevance_judgment TEXT NOT NULL, action_judgment TEXT,
      dimensions TEXT, failure_note TEXT, factual_offending_claim TEXT, factual_disposition TEXT,
      factual_contradicting_evidence TEXT, context_missing_input TEXT,
      posture_should_have_been TEXT, implication_implied_claim TEXT,
      implication_missing_support TEXT
    );
    CREATE TABLE grade_revisions (id INTEGER PRIMARY KEY, grade_id INTEGER, evaluation_id INTEGER, revision INTEGER, source TEXT, payload TEXT, recorded_at TEXT);
    CREATE TABLE grade_usage_overrides (id INTEGER PRIMARY KEY, grade_id INTEGER, mode TEXT, reason TEXT, updated_at TEXT);
    CREATE TABLE feedback_snapshots (
      id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL UNIQUE, policy_version TEXT, mode TEXT, as_of TEXT,
      lookback_days INTEGER, max_grades INTEGER, created_at TEXT,
      population_count INTEGER, eligible_count INTEGER, excluded_count INTEGER
    );
    CREATE TABLE feedback_snapshot_phases (id INTEGER PRIMARY KEY, snapshot_id INTEGER, phase TEXT);
    CREATE TABLE feedback_snapshot_items (
      id INTEGER PRIMARY KEY, snapshot_phase_id INTEGER, grade_id INTEGER, grade_revision_id INTEGER,
      role TEXT, reason TEXT, selection_reason TEXT, rank INTEGER, created_at TEXT
    );
  `);

  db.prepare(`INSERT INTO scans (id, started_at) VALUES (900, '2026-01-31T00:00:00.000Z')`).run();

  // Policy: lookback_days=1, max_grades=1 -> boundary is 2026-01-31.
  // Two snapshots; the newer (active) one resolves the policy and is the
  // "latest snapshot".
  db.prepare(
    `INSERT INTO feedback_snapshots
       (id, scan_id, policy_version, mode, as_of, lookback_days, max_grades, created_at,
        population_count, eligible_count, excluded_count)
     VALUES (1, 900, 'evaluation-feedback/v1', 'shadow', '2026-01-20T00:00:00.000Z', 1, 1,
             '2026-01-20T00:00:00.000Z', 1, 1, 0)`
  ).run();
  db.prepare(
    `INSERT INTO feedback_snapshots
       (id, scan_id, policy_version, mode, as_of, lookback_days, max_grades, created_at,
        population_count, eligible_count, excluded_count)
     VALUES (2, 901, 'evaluation-feedback/v1', 'active', '2026-02-01T00:00:00.000Z', 1, 1,
             '2026-02-01T00:00:00.000Z', 2, 1, 1)`
  ).run();

  const insertPost = db.prepare(`INSERT INTO posts (id, platform, platform_msg_id) VALUES (?, 'discord', ?)`);
  const insertEval = db.prepare(
    `INSERT INTO evaluations (id, post_id, scan_id, relevant, score) VALUES (?, ?, ?, 1, 0.5)`
  );
  const insertGrade = db.prepare(
    `INSERT INTO grades (id, evaluation_id, post_id, scan_id, source, graded_at, schema_version, needs_regrade, relevance_judgment, action_judgment)
     VALUES (?, ?, ?, ?, 'web', ?, 3, 0, 'correct', 'accept')`
  );
  const insertRevision = db.prepare(
    `INSERT INTO grade_revisions (id, grade_id, evaluation_id, revision, source, payload, recorded_at)
     VALUES (?, ?, ?, 1, 'web', ?, ?)`
  );

  // grade 1: newest valid -> eligible (rank 1, within max_grades=1)
  insertPost.run(1, "m1"); insertEval.run(1, 1, 900); insertGrade.run(1, 1, 1, 900, "2026-01-31T12:00:00.000Z");
  insertRevision.run(1, 1, 1, "{}", "2026-01-31T12:00:00.000Z");
  // grade 2: 2nd-newest valid -> eligible_cap
  insertPost.run(2, "m2"); insertEval.run(2, 2, 900); insertGrade.run(2, 2, 2, 900, "2026-01-31T06:00:00.000Z");
  insertRevision.run(2, 2, 2, "{}", "2026-01-31T06:00:00.000Z");
  // grade 3: outside the 1-day lookback boundary
  insertPost.run(3, "m3"); insertEval.run(3, 3, 900); insertGrade.run(3, 3, 3, 900, "2026-01-30T00:00:00.000Z");
  insertRevision.run(3, 3, 3, "{}", "2026-01-30T00:00:00.000Z");

  // Latest-active-snapshot usage: grade 1 aggregate, grade 2 excluded.
  db.prepare(
    `INSERT INTO feedback_snapshot_phases (id, snapshot_id, phase) VALUES (10, 2, 'relevance')`
  ).run();
  db.prepare(
    `INSERT INTO feedback_snapshot_items (snapshot_phase_id, grade_id, grade_revision_id, role, reason, selection_reason, rank, created_at)
     VALUES (10, 1, 1, 'aggregate', NULL, 'phase_population', NULL, '2026-02-01T00:00:01.000Z')`
  ).run();
  db.prepare(
    `INSERT INTO feedback_snapshot_items (snapshot_phase_id, grade_id, grade_revision_id, role, reason, selection_reason, rank, created_at)
     VALUES (10, 2, 2, 'excluded', 'eligible_cap', 'eligible_cap', NULL, '2026-02-01T00:00:02.000Z')`
  ).run();

  db.close();
});

afterAll(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
  delete process.env.SCOUT_DB_PATH;
});

describe("getFeedbackSummary — eligibility", () => {
  it("aggregates in-lookback population, eligible-after-cap, outside-lookback, and per-reason counts", async () => {
    const { getFeedbackSummary } = await import("@/lib/feedback-summary-queries");
    const summary = getFeedbackSummary(AS_OF, "2026-01-01T00:00:00.000Z", AS_OF);
    expect(summary.policy).toEqual({
      policy_version: "evaluation-feedback/v1",
      lookback_days: 1,
      max_grades: 1,
    });
    expect(summary.eligibility.in_lookback_population).toBe(2); // grades 1, 2
    expect(summary.eligibility.eligible_after_cap).toBe(1); // grade 1 only
    expect(summary.eligibility.outside_lookback_count).toBe(1); // grade 3
    expect(summary.eligibility.resolved_lookback_days).toBe(1);
    expect(summary.eligibility.resolved_max_grades).toBe(1);
    const byReason = new Map(summary.eligibility.by_reason.map((r) => [r.reason, r.count]));
    expect(byReason.get("eligible")).toBe(1);
    expect(byReason.get("eligible_cap")).toBe(1);
    expect(byReason.get("schema_version")).toBe(0);
  });
});

describe("getFeedbackSummary — latest snapshot", () => {
  it("reports the newest snapshot's header plus the used grade count for an active snapshot", async () => {
    const { getFeedbackSummary } = await import("@/lib/feedback-summary-queries");
    const summary = getFeedbackSummary(AS_OF, "2026-01-01T00:00:00.000Z", AS_OF);
    expect(summary.latest_snapshot).toEqual({
      snapshot_id: 2,
      scan_id: 901,
      policy_version: "evaluation-feedback/v1",
      mode: "active",
      created_at: "2026-02-01T00:00:00.000Z",
      population_count: 2,
      eligible_count: 1,
      excluded_count: 1,
      used_grade_count: 1, // only grade 1 has an aggregate/example role
    });
  });
});
