import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-shadow-relevance-"));
const dbPath = path.join(tmpDir, "scout.db");
process.env.SCOUT_DB_PATH = dbPath;

beforeAll(() => {
  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE posts (id INTEGER PRIMARY KEY, platform TEXT NOT NULL, platform_msg_id TEXT NOT NULL,
      channel_name TEXT, channel_id TEXT, author_name TEXT, author_id TEXT, content TEXT, url TEXT,
      created_at TEXT, scan_id INTEGER, parent_lookup_status TEXT NOT NULL DEFAULT 'not_applicable',
      parent_id TEXT, parent_author_id TEXT, parent_author_name TEXT, parent_text TEXT, parent_url TEXT);
    CREATE TABLE evaluations (id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL, relevant INTEGER NOT NULL,
      score REAL NOT NULL, reason TEXT, relevant_to TEXT, keyword_route_id INTEGER, scan_id INTEGER,
      surface_status TEXT, posture TEXT, project_key TEXT, failure_reason TEXT,
      dossier_revision TEXT, dossier_summary_id TEXT);
    CREATE TABLE draft_comments (id INTEGER PRIMARY KEY, post_id INTEGER, evaluation_id INTEGER,
      project_key TEXT, comment_text TEXT, created_at TEXT, scan_id INTEGER, posture TEXT,
      structured_output TEXT, dossier_revision TEXT, dossier_summary_id TEXT);
    CREATE TABLE critiques (id INTEGER PRIMARY KEY, draft_id INTEGER, evaluation_id INTEGER,
      verdict TEXT, feedback TEXT, created_at TEXT, scan_id INTEGER);
    CREATE TABLE project_keywords (id INTEGER PRIMARY KEY, project_key TEXT NOT NULL, keyword TEXT NOT NULL,
      match_type TEXT, intent TEXT, positive_context TEXT, negative_context TEXT,
      evaluate_prompt TEXT, respond_prompt TEXT, critique_prompt TEXT);
    CREATE TABLE prompt_templates (name TEXT PRIMARY KEY, body TEXT NOT NULL, kind TEXT NOT NULL, active INTEGER NOT NULL);
    INSERT INTO posts (id, platform, platform_msg_id, content, scan_id) VALUES (1, 'discord', 'm1', 'hello', 7);
    INSERT INTO evaluations (id, post_id, relevant, score, scan_id, surface_status) VALUES (11, 1, 1, .8, 7, 'surfaced');
  `);
  db.close();
});

afterAll(() => fs.rmSync(tmpDir, { recursive: true, force: true }));

describe("getEvaluationsByScan shadow relevance", () => {
  it("omits shadow data when the v47 table is absent", async () => {
    const { getEvaluationsByScan } = await import("@/lib/queries");
    const [evaluation] = getEvaluationsByScan(7);
    expect(evaluation).not.toHaveProperty("shadow_relevance");
  });

  it("returns the latest run by created_at", async () => {
    const db = new Database(dbPath);
    db.exec(`CREATE TABLE shadow_relevance_runs (
      id INTEGER PRIMARY KEY, evaluation_id INTEGER, eligible INTEGER, p_eligible REAL,
      uncertain INTEGER, decision_json TEXT, account_label TEXT, account_confidence REAL,
      status TEXT, error_detail TEXT, created_at TEXT);
      INSERT INTO shadow_relevance_runs VALUES
        (1, 11, 0, .2, 0, '{"reason":"old","details":{"low":2}}', NULL, NULL, 'ok', NULL, '2026-09-16T00:00:00Z'),
        (2, 11, 1, .9, 1, '{"reason":"new","details":{"high":3,"medium":1}}', 'individual', .8, 'ok', NULL, '2026-09-17T00:00:00Z');`);
    db.close();
    const { getEvaluationsByScan } = await import("@/lib/queries");
    expect(getEvaluationsByScan(7)[0].shadow_relevance).toMatchObject({
      id: 2, eligible: true, p_eligible: .9, uncertain: true, reason: "new",
      details: { high: 3, medium: 1 }, account_label: "individual", account_confidence: .8,
    });
  });
});
