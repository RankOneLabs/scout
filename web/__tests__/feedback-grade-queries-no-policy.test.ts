import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// A database that has never recorded a feedback_snapshots row (no scan has
// run evaluation-feedback/v1 selection yet) — every eligibility-dependent
// field must degrade honestly to null/zero rather than guessing a policy.
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-no-policy-"));
const dbPath = path.join(tmpDir, "scout.db");
process.env.SCOUT_DB_PATH = dbPath;

beforeAll(() => {
  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE posts (
      id INTEGER PRIMARY KEY, platform TEXT NOT NULL, platform_msg_id TEXT NOT NULL,
      author_name TEXT, author_id TEXT, content TEXT, url TEXT, created_at TEXT
    );
    CREATE TABLE evaluations (
      id INTEGER PRIMARY KEY, post_id INTEGER, scan_id INTEGER, relevant INTEGER, score REAL, reason TEXT,
      project_key TEXT, posture TEXT, surface_status TEXT, failure_reason TEXT,
      dossier_summary_id TEXT, dossier_revision TEXT
    );
    CREATE TABLE draft_comments (id INTEGER PRIMARY KEY, post_id INTEGER, evaluation_id INTEGER, project_key TEXT, comment_text TEXT, created_at TEXT, posture TEXT, dossier_summary_id TEXT, dossier_revision TEXT);
    CREATE TABLE critiques (id INTEGER PRIMARY KEY, draft_id INTEGER, evaluation_id INTEGER, verdict TEXT, feedback TEXT, created_at TEXT);
    CREATE TABLE grade_usage_overrides (id INTEGER PRIMARY KEY, grade_id INTEGER, mode TEXT, reason TEXT, updated_at TEXT);
    CREATE TABLE grades (
      id INTEGER PRIMARY KEY, evaluation_id INTEGER, post_id INTEGER NOT NULL, scan_id INTEGER,
      source TEXT NOT NULL, graded_at TEXT NOT NULL, schema_version INTEGER NOT NULL DEFAULT 1,
      needs_regrade INTEGER NOT NULL DEFAULT 0, relevance_judgment TEXT NOT NULL, action_judgment TEXT,
      dimensions TEXT, failure_note TEXT, factual_offending_claim TEXT, factual_disposition TEXT,
      factual_contradicting_evidence TEXT, context_missing_input TEXT,
      posture_should_have_been TEXT, implication_implied_claim TEXT,
      implication_missing_support TEXT
    );
    CREATE TABLE grade_revisions (
      id INTEGER PRIMARY KEY, grade_id INTEGER NOT NULL, evaluation_id INTEGER, revision INTEGER NOT NULL,
      schema_version INTEGER NOT NULL DEFAULT 2,
      source TEXT NOT NULL, payload TEXT NOT NULL, recorded_at TEXT NOT NULL
    );
    CREATE TABLE feedback_snapshots (
      id INTEGER PRIMARY KEY, scan_id INTEGER, policy_version TEXT, mode TEXT, as_of TEXT,
      lookback_days INTEGER, max_grades INTEGER, created_at TEXT
    );
    CREATE TABLE feedback_snapshot_phases (id INTEGER PRIMARY KEY, snapshot_id INTEGER, phase TEXT);
    CREATE TABLE feedback_snapshot_items (
      id INTEGER PRIMARY KEY, snapshot_phase_id INTEGER, grade_id INTEGER, grade_revision_id INTEGER,
      role TEXT, reason TEXT, created_at TEXT
    );
    CREATE TABLE evaluation_phase_runs (
      id INTEGER PRIMARY KEY, scan_id INTEGER, post_id INTEGER, evaluation_id INTEGER,
      snapshot_phase_id INTEGER, phase TEXT, trace_id TEXT UNIQUE, model TEXT, status TEXT,
      created_at TEXT
    );
  `);
  db.prepare(`INSERT INTO posts (id, platform, platform_msg_id) VALUES (1, 'discord', 'm1')`).run();
  db.prepare(
    `INSERT INTO grades (id, evaluation_id, post_id, source, graded_at, schema_version, needs_regrade, relevance_judgment)
     VALUES (1, NULL, 1, 'web', '2026-01-01T00:00:00.000Z', 2, 0, 'correct')`
  ).run();
  db.prepare(
    `INSERT INTO grade_revisions (id, grade_id, revision, source, payload, recorded_at)
     VALUES (1, 1, 1, 'web', '{"graded_at":"2026-01-01T00:00:00.000Z"}', '2026-01-01T00:00:00.000Z')`
  ).run();
  db.close();
});

afterAll(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
  delete process.env.SCOUT_DB_PATH;
});

describe("no evaluation-feedback/v1 policy resolved yet", () => {
  it("resolveFeedbackPolicy and computeEligibilityWindow return null", async () => {
    const { resolveFeedbackPolicy, computeEligibilityWindow } = await import(
      "@/lib/feedback-grade-queries"
    );
    expect(resolveFeedbackPolicy()).toBeNull();
    expect(computeEligibilityWindow("2026-02-01T00:00:00.000Z")).toBeNull();
  });

  it("grade list rows report a null eligibility_reason rather than guessing", async () => {
    const { listGrades } = await import("@/lib/feedback-grade-queries");
    const page = listGrades({}, "2026-02-01T00:00:00.000Z");
    expect(page.data).toHaveLength(1);
    expect(page.data[0].eligibility_reason).toBeNull();
  });

  it("grade detail reports null eligibility policy fields", async () => {
    const { getGradeDetail } = await import("@/lib/feedback-grade-queries");
    const detail = getGradeDetail(1, "2026-02-01T00:00:00.000Z", {})!;
    expect(detail.eligibility).toEqual({
      reason: null,
      policy_version: null,
      resolved_lookback_days: null,
      resolved_max_grades: null,
    });
  });
});
