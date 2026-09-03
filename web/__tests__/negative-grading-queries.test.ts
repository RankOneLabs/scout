import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-negative-grading-"));
const dbPath = path.join(tmpDir, "scout.db");
process.env.SCOUT_DB_PATH = dbPath;

beforeAll(() => {
  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE posts (
      id INTEGER PRIMARY KEY, platform TEXT NOT NULL, platform_msg_id TEXT NOT NULL,
      channel_name TEXT, channel_id TEXT, author_name TEXT, author_id TEXT,
      content TEXT, url TEXT, created_at TEXT, scan_id INTEGER,
      parent_lookup_status TEXT NOT NULL DEFAULT 'not_applicable',
      parent_id TEXT, parent_author_id TEXT, parent_author_name TEXT,
      parent_text TEXT, parent_url TEXT
    );
    CREATE TABLE evaluations (
      id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL, relevant INTEGER NOT NULL,
      score REAL NOT NULL, reason TEXT, relevant_to TEXT, keyword_route_id INTEGER,
      scan_id INTEGER, surface_status TEXT, posture TEXT, project_key TEXT,
      failure_reason TEXT, dossier_revision TEXT, dossier_summary_id TEXT
    );
    CREATE TABLE draft_comments (
      id INTEGER PRIMARY KEY, post_id INTEGER, evaluation_id INTEGER, project_key TEXT,
      comment_text TEXT, created_at TEXT, scan_id INTEGER, posture TEXT,
      structured_output TEXT, dossier_revision TEXT, dossier_summary_id TEXT
    );
    CREATE TABLE critiques (
      id INTEGER PRIMARY KEY, draft_id INTEGER, evaluation_id INTEGER, verdict TEXT,
      feedback TEXT, created_at TEXT, scan_id INTEGER
    );
    CREATE TABLE gate_blocks (
      id INTEGER PRIMARY KEY, reason_code TEXT, offending_text TEXT, segment_index INTEGER,
      project_key TEXT, dossier_summary_id TEXT, dossier_revision TEXT, scan_id INTEGER,
      post_id INTEGER, evaluation_id INTEGER, context TEXT, created_at TEXT
    );
    CREATE TABLE grades (
      id INTEGER PRIMARY KEY, evaluation_id INTEGER, post_id INTEGER NOT NULL, scan_id INTEGER,
      source TEXT NOT NULL, graded_at TEXT NOT NULL, schema_version INTEGER NOT NULL,
      needs_regrade INTEGER NOT NULL, relevance_judgment TEXT NOT NULL, action_judgment TEXT,
      dimensions TEXT, failure_note TEXT, factual_offending_claim TEXT,
      factual_disposition TEXT, factual_contradicting_evidence TEXT,
      context_missing_input TEXT, posture_should_have_been TEXT,
      implication_implied_claim TEXT, implication_missing_support TEXT
    );
    CREATE TABLE grade_revisions (
      id INTEGER PRIMARY KEY, grade_id INTEGER NOT NULL, evaluation_id INTEGER,
      revision INTEGER NOT NULL, source TEXT NOT NULL, payload TEXT NOT NULL,
      recorded_at TEXT NOT NULL
    );
    CREATE TABLE human_positive_promotions (
      source_evaluation_id INTEGER PRIMARY KEY, source_grade_id INTEGER NOT NULL,
      scan_id INTEGER, target_evaluation_id INTEGER, status TEXT NOT NULL,
      error_detail TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      completed_at TEXT
    );
    CREATE TABLE project_keywords (
      id INTEGER PRIMARY KEY, project_key TEXT NOT NULL, keyword TEXT NOT NULL,
      match_type TEXT, intent TEXT, positive_context TEXT, negative_context TEXT,
      evaluate_prompt TEXT, respond_prompt TEXT, critique_prompt TEXT
    );
    CREATE TABLE prompt_templates (
      name TEXT PRIMARY KEY, body TEXT NOT NULL, kind TEXT NOT NULL,
      active INTEGER NOT NULL DEFAULT 1
    );
  `);

  const insertPost = db.prepare(
    `INSERT INTO posts
       (id, platform, platform_msg_id, author_name, content, created_at, scan_id)
     VALUES (?, 'bluesky', ?, ?, ?, '2026-07-22T00:00:00.000Z', ?)`
  );
  const insertEvaluation = db.prepare(
    `INSERT INTO evaluations
       (id, post_id, relevant, score, reason, scan_id, surface_status, project_key)
     VALUES (?, ?, ?, ?, ?, ?, 'not_relevant', 'agent-ops')`
  );

  [1, 2, 3, 4, 5, 6, 7, 8].forEach((id) => {
    insertPost.run(id, `message-${id}`, `author-${id}`, `post ${id}`, 100 + id);
  });
  insertEvaluation.run(1, 1, 0, 0.1, "skip one", 101); // queued
  insertEvaluation.run(2, 2, 1, 0.9, "surface", 102); // positive: excluded
  insertEvaluation.run(3, 3, 0, 0.2, "skip three", 103); // already current: excluded
  insertEvaluation.run(4, 4, 0, 0.3, "skip four", 104); // needs regrade: queued
  insertEvaluation.run(5, 5, 0, 0.4, "skip five", 105); // queued, newest
  insertEvaluation.run(6, 6, 0, 0.5, "skip six", 106); // failed promotion: queued
  insertEvaluation.run(7, 7, 0, 0.6, "skip seven", 107); // completed promotion: excluded
  insertEvaluation.run(8, 8, 0, 0.7, "critic rejected", 108); // not a relevance negative
  db.prepare("UPDATE evaluations SET surface_status = 'critic_rejected' WHERE id = 8").run();

  const insertGrade = db.prepare(
    `INSERT INTO grades
       (id, evaluation_id, post_id, scan_id, source, graded_at, schema_version,
        needs_regrade, relevance_judgment, action_judgment)
     VALUES (?, ?, ?, ?, 'web', '2026-07-22T01:00:00.000Z', 2, ?, 'correct', 'accept')`
  );
  insertGrade.run(30, 3, 3, 103, 0);
  insertGrade.run(40, 4, 4, 104, 1);
  db.prepare(
    `INSERT INTO grades
       (id, evaluation_id, post_id, scan_id, source, graded_at, schema_version,
        needs_regrade, relevance_judgment, action_judgment, dimensions, failure_note)
     VALUES (?, ?, ?, ?, 'web', '2026-07-22T01:00:00.000Z', 2, 0,
             'false_negative', 'fail', '["usefulness"]', 'should respond')`
  ).run(60, 6, 6, 106);
  db.prepare(
    `INSERT INTO grades
       (id, evaluation_id, post_id, scan_id, source, graded_at, schema_version,
        needs_regrade, relevance_judgment, action_judgment, dimensions, failure_note)
     VALUES (?, ?, ?, ?, 'web', '2026-07-22T01:00:00.000Z', 2, 0,
             'false_negative', 'fail', '["usefulness"]', 'should respond')`
  ).run(70, 7, 7, 107);
  db.prepare(
    `INSERT INTO human_positive_promotions
       (source_evaluation_id, source_grade_id, status, created_at, updated_at, completed_at,
        scan_id, target_evaluation_id)
     VALUES (6, 60, 'failed', '2026-07-22T01:00:00Z', '2026-07-22T01:01:00Z', NULL,
             206, NULL),
            (7, 70, 'completed', '2026-07-22T01:00:00Z', '2026-07-22T01:01:00Z',
             '2026-07-22T01:01:00Z', 207, 700)`
  ).run();
  db.prepare(
    `INSERT INTO grade_revisions
       (id, grade_id, evaluation_id, revision, source, payload, recorded_at)
     VALUES (1, 40, 4, 1, 'web', '{}', '2026-07-22T01:00:00.000Z')`
  ).run();
  db.close();
});

afterAll(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
  delete process.env.SCOUT_DB_PATH;
});

describe("getNegativeGradingCases", () => {
  it("returns recent model-negative evaluations without a current valid grade", async () => {
    const { getNegativeGradingCases } = await import("@/lib/queries");
    const page = getNegativeGradingCases({ limit: 2 });

    expect(page.has_more).toBe(true);
    expect(page.data.map((evaluation) => evaluation.id)).toEqual([6, 5]);
    expect(page.data.every((evaluation) => evaluation.relevant === false)).toBe(true);
    expect(page.data[0].grade?.relevance_judgment).toBe("false_negative");
    expect(page.data[1].grade).toBeNull();
  });

  it("paginates by evaluation id without returning reviewed or positive cases", async () => {
    const { getNegativeGradingCases } = await import("@/lib/queries");
    const page = getNegativeGradingCases({ before_id: 4, limit: 2 });

    expect(page.has_more).toBe(false);
    expect(page.data.map((evaluation) => evaluation.id)).toEqual([1]);
  });
});

describe("GET /api/grading/negative-cases", () => {
  it("returns a paginated queue and validates its cursor", async () => {
    const { GET } = await import("@/app/api/grading/negative-cases/route");
    const response = await GET({
      nextUrl: new URL("http://localhost/api/grading/negative-cases?limit=2"),
    } as never);
    expect(response.status).toBe(200);
    const body = (await response.json()) as { data: Array<{ id: number }>; has_more: boolean };
    expect(body.data.map((evaluation) => evaluation.id)).toEqual([6, 5]);
    expect(body.has_more).toBe(true);

    const invalid = await GET({
      nextUrl: new URL("http://localhost/api/grading/negative-cases?before_id=0"),
    } as never);
    expect(invalid.status).toBe(400);
  });
});
