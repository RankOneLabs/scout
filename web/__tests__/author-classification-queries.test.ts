import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// getEvaluationsByScan carries the annotate node's author class onto the
// post so the review card can show it beside the block button. Databases
// older than v42 have no author_classifications table and must still read.

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-author-class-"));
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
    CREATE TABLE project_keywords (
      id INTEGER PRIMARY KEY, project_key TEXT NOT NULL, keyword TEXT NOT NULL,
      match_type TEXT, intent TEXT, positive_context TEXT, negative_context TEXT,
      evaluate_prompt TEXT, respond_prompt TEXT, critique_prompt TEXT
    );
    CREATE TABLE prompt_templates (
      name TEXT PRIMARY KEY, body TEXT NOT NULL, kind TEXT NOT NULL,
      active INTEGER NOT NULL DEFAULT 1
    );
    INSERT INTO posts (id, platform, platform_msg_id, author_name, author_id, content, scan_id)
      VALUES (1, 'bluesky', 'm1', 'AI Daily', 'did:plc:feed', 'post one', 7),
             (2, 'bluesky', 'm2', 'Alice', 'did:plc:alice', 'post two', 7),
             (3, 'bluesky', 'm3', 'Nobody', 'did:plc:unseen', 'post three', 7);
    INSERT INTO evaluations (id, post_id, relevant, score, reason, scan_id, surface_status)
      VALUES (1, 1, 1, 0.9, 'r', 7, 'surfaced'),
             (2, 2, 1, 0.8, 'r', 7, 'surfaced'),
             (3, 3, 1, 0.7, 'r', 7, 'surfaced');
  `);
  db.close();
});

afterAll(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
});

describe("getEvaluationsByScan author classification", () => {
  it("reads a database without the v42 table as unclassified", async () => {
    const { getEvaluationsByScan } = await import("@/lib/queries");
    const evaluations = getEvaluationsByScan(7);
    expect(evaluations).toHaveLength(3);
    expect(evaluations.map((e) => e.post.author_classification)).toEqual([null, null, null]);
  });

  it("attaches the stored class, rule version and matched text to the post", async () => {
    const db = new Database(dbPath);
    db.exec(`
      CREATE TABLE author_classifications (
        id INTEGER PRIMARY KEY, platform TEXT NOT NULL, author_id TEXT NOT NULL,
        author_class TEXT NOT NULL, rule_version INTEGER NOT NULL, matched_text TEXT,
        classified_at TEXT NOT NULL, UNIQUE(platform, author_id)
      );
      INSERT INTO author_classifications (platform, author_id, author_class, rule_version, matched_text, classified_at)
        VALUES ('bluesky', 'did:plc:feed', 'aggregator', 1, 'Daily', '2026-09-11T00:00:00+00:00'),
               ('bluesky', 'did:plc:alice', 'unknown', 1, NULL, '2026-09-11T00:00:00+00:00');
    `);
    db.close();
    const { getEvaluationsByScan } = await import("@/lib/queries");
    const byPost = new Map(getEvaluationsByScan(7).map((e) => [e.post_id, e.post.author_classification]));
    expect(byPost.get(1)).toEqual({
      author_class: "aggregator", rule_version: 1, matched_text: "Daily",
      classified_at: "2026-09-11T00:00:00+00:00",
    });
    expect(byPost.get(2)).toEqual({
      author_class: "unknown", rule_version: 1, matched_text: null,
      classified_at: "2026-09-11T00:00:00+00:00",
    });
    expect(byPost.get(3)).toBeNull();
  });
});
