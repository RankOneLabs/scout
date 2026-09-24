import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// A database from before the holdout schema. Nothing here could have
// recorded a classifier, a hold or a release, so the read model omits the
// field entirely rather than rendering "nothing recorded" over a database
// that had nowhere to record it.
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-relevance-legacy-"));
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
    CREATE TABLE review_dispositions (queue_digest TEXT, evaluation_id INTEGER, sequence INTEGER,
      action_id TEXT, disposition_json TEXT NOT NULL);
    INSERT INTO posts (id, platform, platform_msg_id, content, scan_id) VALUES (1, 'discord', 'm1', 'hello', 3);
    INSERT INTO evaluations (id, post_id, relevant, score, scan_id, surface_status) VALUES
      (21, 1, 1, 0.8, 3, 'surfaced'),
      (22, 1, 0, 0.1, 3, 'not_relevant');
    INSERT INTO draft_comments (id, post_id, evaluation_id, comment_text, scan_id)
      VALUES (1, 1, 21, 'a reply', 3);
  `);
  db.close();
});

afterAll(() => fs.rmSync(tmpDir, { recursive: true, force: true }));

describe("a database predating the holdout schema", () => {
  it("omits relevance provenance from evaluations rather than inventing it", async () => {
    const { getEvaluationsByScan } = await import("@/lib/queries");
    for (const evaluation of getEvaluationsByScan(3)) {
      expect(evaluation).not.toHaveProperty("relevance_provenance");
    }
  });

  it("omits relevance provenance from drafts", async () => {
    const { getDrafts } = await import("@/lib/queries");
    for (const draft of getDrafts().data) {
      expect(draft).not.toHaveProperty("relevance_provenance");
    }
  });

  it("reads one evaluation's provenance as absent rather than failing", async () => {
    const { getRelevanceProvenance } = await import("@/lib/queries");
    expect(getRelevanceProvenance(21)).toBeNull();
  });

  it("keeps existing status counts unchanged", async () => {
    const { getEvaluationsByScan } = await import("@/lib/queries");
    const { selectSurfaceStatusCounts } = await import("@/lib/transforms");
    expect(selectSurfaceStatusCounts(getEvaluationsByScan(3))).toMatchObject({
      total: 2,
      surfaced: 1,
      held: 0,
      actionable: 1,
    });
  });

  it("still returns the grading-assistance trail beside an absent relevance record", async () => {
    const { listEvaluationReviews } = await import("@/lib/review-queue-queries");
    const result = listEvaluationReviews(21);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.value).toEqual({ actions: [], relevance: null });
  });
});
