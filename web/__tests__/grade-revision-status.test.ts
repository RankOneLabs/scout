import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// Covers the Draft/Evaluation card "grade-save status" decision: last-saved
// time and revision count, sourced from grade_revisions via
// getGradeRevisionMetaBatch, attached onto Grade objects returned by
// getDraftsWithGrades and getEvaluationsByScan.

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-revision-status-"));
const dbPath = path.join(tmpDir, "scout.db");
process.env.SCOUT_DB_PATH = dbPath;

beforeAll(() => {
  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE scans (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      started_at TEXT NOT NULL
    );
    CREATE TABLE posts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      platform TEXT NOT NULL,
      platform_msg_id TEXT NOT NULL,
      channel_name TEXT, channel_id TEXT, author_name TEXT, author_id TEXT,
      content TEXT, url TEXT, created_at TEXT, scan_id INTEGER,
      parent_lookup_status TEXT NOT NULL DEFAULT 'not_applicable',
      parent_id TEXT, parent_author_id TEXT, parent_author_name TEXT,
      parent_text TEXT, parent_url TEXT,
      UNIQUE(platform, platform_msg_id)
    );
    CREATE TABLE evaluations (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      post_id INTEGER NOT NULL, relevant INTEGER NOT NULL, score REAL NOT NULL,
      reason TEXT, relevant_to TEXT, keyword_route_id INTEGER, scan_id INTEGER,
      project_key TEXT, posture TEXT, surface_status TEXT NOT NULL DEFAULT 'surfaced',
      failure_reason TEXT, dossier_summary_id TEXT, dossier_revision TEXT
    );
    CREATE TABLE draft_comments (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      post_id INTEGER NOT NULL, evaluation_id INTEGER NOT NULL, project_key TEXT,
      comment_text TEXT, created_at TEXT, scan_id INTEGER, posture TEXT,
      structured_output TEXT, dossier_summary_id TEXT, dossier_revision TEXT
    );
    CREATE TABLE critiques (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      draft_id INTEGER, evaluation_id INTEGER, verdict TEXT NOT NULL,
      feedback TEXT, created_at TEXT, scan_id INTEGER
    );
    CREATE TABLE gate_blocks (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      reason_code TEXT, offending_text TEXT, segment_index INTEGER, project_key TEXT,
      dossier_summary_id TEXT, dossier_revision TEXT, scan_id INTEGER, post_id INTEGER,
      evaluation_id INTEGER, context TEXT, created_at TEXT
    );
    CREATE TABLE grades (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      evaluation_id INTEGER, post_id INTEGER NOT NULL, scan_id INTEGER,
      source TEXT NOT NULL, graded_at TEXT NOT NULL, schema_version INTEGER NOT NULL DEFAULT 1,
      needs_regrade INTEGER NOT NULL DEFAULT 0, relevance_judgment TEXT NOT NULL,
      action_judgment TEXT, dimensions TEXT, failure_note TEXT,
      factual_offending_claim TEXT, factual_disposition TEXT,
      factual_contradicting_evidence TEXT, context_missing_input TEXT,
      posture_should_have_been TEXT, implication_implied_claim TEXT,
      implication_missing_support TEXT
    );
    CREATE TABLE grade_revisions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      grade_id INTEGER NOT NULL, evaluation_id INTEGER, revision INTEGER NOT NULL,
      source TEXT NOT NULL, payload TEXT NOT NULL, recorded_at TEXT NOT NULL
    );
    CREATE TABLE project_keywords (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      project_key TEXT NOT NULL, keyword TEXT NOT NULL, evaluate_prompt TEXT,
      respond_prompt TEXT, critique_prompt TEXT, match_type TEXT NOT NULL DEFAULT 'substring',
      intent TEXT, positive_context TEXT, negative_context TEXT
    );
    CREATE TABLE prompt_templates (
      name TEXT PRIMARY KEY, body TEXT NOT NULL, kind TEXT NOT NULL,
      active INTEGER NOT NULL DEFAULT 1
    );
  `);

  db.prepare(`INSERT INTO scans (id, started_at) VALUES (1, '2026-05-01T00:00:00Z')`).run();
  db.prepare(
    `INSERT INTO posts (id, platform, platform_msg_id, scan_id) VALUES (1, 'discord', 'm1', 1)`
  ).run();
  db.prepare(
    `INSERT INTO evaluations (id, post_id, relevant, score, scan_id, surface_status)
     VALUES (1, 1, 1, 0.9, 1, 'surfaced')`
  ).run();
  db.prepare(
    `INSERT INTO draft_comments (id, post_id, evaluation_id, comment_text, scan_id)
     VALUES (1, 1, 1, 'hello', 1)`
  ).run();
  db.prepare(
    `INSERT INTO grades
       (id, evaluation_id, post_id, scan_id, source, graded_at, relevance_judgment)
     VALUES (1, 1, 1, 1, 'web', '2026-05-01T00:00:00.000Z', 'correct')`
  ).run();
  db.prepare(
    `INSERT INTO grade_revisions (grade_id, revision, source, payload, recorded_at)
     VALUES (1, 1, 'web', '{}', '2026-05-01T00:00:00.000000')`
  ).run();
  db.prepare(
    `INSERT INTO grade_revisions (grade_id, revision, source, payload, recorded_at)
     VALUES (1, 2, 'web', '{}', '2026-05-02T00:00:00.000000')`
  ).run();

  db.close();
});

afterAll(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
  delete process.env.SCOUT_DB_PATH;
});

describe("grade-save status on Draft/Evaluation reads", () => {
  it("attaches revision_count and latest_recorded_at to getDraftsWithGrades' grade", async () => {
    const { getDraftsWithGrades } = await import("@/lib/queries");
    const { data } = getDraftsWithGrades({ scan_id: 1 });
    expect(data).toHaveLength(1);
    expect(data[0].grade?.revision_count).toBe(2);
    expect(data[0].grade?.latest_recorded_at).toBe("2026-05-02T00:00:00.000000");
  });

  it("attaches revision_count and latest_recorded_at to getEvaluationsByScan's grade", async () => {
    const { getEvaluationsByScan } = await import("@/lib/queries");
    const evaluations = getEvaluationsByScan(1);
    expect(evaluations).toHaveLength(1);
    expect(evaluations[0].grade?.revision_count).toBe(2);
    expect(evaluations[0].grade?.latest_recorded_at).toBe("2026-05-02T00:00:00.000000");
  });
});
