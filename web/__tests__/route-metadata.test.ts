import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-route-meta-"));
const dbPath = path.join(tmpDir, "scout.db");
process.env.SCOUT_DB_PATH = dbPath;

beforeAll(() => {
  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE scans (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      started_at TEXT NOT NULL,
      completed_at TEXT,
      messages_scanned INTEGER DEFAULT 0,
      relevant_found INTEGER DEFAULT 0
    );
    CREATE TABLE posts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      platform TEXT NOT NULL,
      platform_msg_id TEXT NOT NULL,
      channel_name TEXT,
      channel_id TEXT,
      author_name TEXT,
      author_id TEXT,
      content TEXT,
      url TEXT,
      created_at TEXT,
      scan_id INTEGER,
      parent_lookup_status TEXT NOT NULL DEFAULT 'not_applicable',
      parent_id TEXT,
      parent_author_id TEXT,
      parent_author_name TEXT,
      parent_text TEXT,
      parent_url TEXT,
      UNIQUE(platform, platform_msg_id)
    );
    CREATE TABLE evaluations (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      post_id INTEGER NOT NULL,
      relevant INTEGER NOT NULL,
      score REAL NOT NULL,
      reason TEXT,
      relevant_to TEXT,
      keyword_route_id INTEGER,
      surface_status TEXT NOT NULL DEFAULT 'legacy_unknown',
      scan_id INTEGER
    );
    CREATE TABLE draft_comments (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      post_id INTEGER NOT NULL,
      evaluation_id INTEGER NOT NULL,
      project_key TEXT,
      comment_text TEXT,
      created_at TEXT,
      scan_id INTEGER,
      posture TEXT,
      structured_output TEXT,
      dossier_summary_id TEXT,
      dossier_revision TEXT
    );
    CREATE TABLE critiques (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      draft_id INTEGER NOT NULL,
      verdict TEXT NOT NULL,
      feedback TEXT,
      created_at TEXT,
      scan_id INTEGER,
      evaluation_id INTEGER
    );
    CREATE TABLE project_keywords (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      project_key TEXT NOT NULL,
      keyword TEXT NOT NULL,
      evaluate_prompt TEXT,
      respond_prompt TEXT,
      critique_prompt TEXT,
      match_type TEXT NOT NULL DEFAULT 'substring',
      intent TEXT,
      positive_context TEXT,
      negative_context TEXT,
      notes TEXT,
      active INTEGER NOT NULL DEFAULT 1,
      priority INTEGER NOT NULL DEFAULT 100,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE prompt_templates (
      name TEXT PRIMARY KEY,
      body TEXT NOT NULL,
      kind TEXT NOT NULL,
      active INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
  `);

  const now = "2026-06-08T00:00:00+00:00";
  db.prepare(
    `INSERT INTO scans (id, started_at, completed_at, messages_scanned, relevant_found)
     VALUES (?, ?, ?, ?, ?)`
  ).run(1, now, now, 1, 1);
  db.prepare(
    `INSERT INTO project_keywords
      (id, project_key, keyword, evaluate_prompt, respond_prompt, critique_prompt,
       match_type, intent, positive_context, negative_context, notes, active, priority,
       created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).run(
    1,
    "gw",
    "alpha",
    "eval_route",
    "respond_route",
    "critique_route",
    "substring",
    "help folks",
    JSON.stringify(["trusted context"]),
    JSON.stringify(["ignore"]),
    "internal note",
    1,
    100,
    now,
    now
  );
  db.prepare(
    `INSERT INTO prompt_templates
      (name, body, kind, active, created_at, updated_at)
     VALUES (?, ?, ?, 1, ?, ?)`
  ).run("eval_route", "evaluate body", "evaluate", now, now);
  db.prepare(
    `INSERT INTO prompt_templates
      (name, body, kind, active, created_at, updated_at)
     VALUES (?, ?, ?, 1, ?, ?)`
  ).run("respond_route", "respond body", "respond", now, now);
  db.prepare(
    `INSERT INTO prompt_templates
      (name, body, kind, active, created_at, updated_at)
     VALUES (?, ?, ?, 1, ?, ?)`
  ).run("critique_route", "critique body", "critique", now, now);
  db.prepare(
    `INSERT INTO posts
      (id, platform, platform_msg_id, channel_name, channel_id, author_name,
       author_id, content, url, created_at, scan_id)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).run(
    1,
    "discord",
    "msg-1",
    "general",
    "ch-1",
    "alice",
    "u-1",
    "alpha launch update",
    "https://example.com/1",
    now,
    1
  );
  db.prepare(
    `INSERT INTO evaluations
      (id, post_id, relevant, score, reason, relevant_to, keyword_route_id, scan_id)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
  ).run(
    1,
    1,
    1,
    0.93,
    "relevant",
    JSON.stringify(["gw"]),
    1,
    1
  );
  db.prepare(
    `INSERT INTO draft_comments
      (id, post_id, evaluation_id, project_key, comment_text, created_at, scan_id)
     VALUES (?, ?, ?, ?, ?, ?, ?)`
  ).run(1, 1, 1, "gw", "Nice update!", now, 1);
  db.close();
});

afterAll(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
  delete process.env.SCOUT_DB_PATH;
});

function makeNextRequest(url: string): { nextUrl: URL } {
  return { nextUrl: new URL(url) };
}

describe("route metadata responses", () => {
  it("includes matched route metadata on post rows", async () => {
    const { GET } = await import("@/app/api/posts/route");
    const resp = await GET(makeNextRequest("http://localhost/api/posts") as never);
    expect(resp.status).toBe(200);
    const body = (await resp.json()) as {
      data: Array<{
        keyword_route_id: number | null;
        matched_route: { project_key: string; resolved_prompt_bundle: { evaluate: string | null } | null } | null;
      }>;
    };
    expect(body.data[0].keyword_route_id).toBe(1);
    expect(body.data[0].matched_route?.project_key).toBe("gw");
    expect(body.data[0].matched_route?.resolved_prompt_bundle?.evaluate).toBe(
      "evaluate body"
    );
  });

  it("includes matched route metadata on evaluation rows", async () => {
    const { GET } = await import("@/app/api/evaluations/route");
    const resp = await GET(
      makeNextRequest("http://localhost/api/evaluations?scan_id=1") as never
    );
    expect(resp.status).toBe(200);
    const body = (await resp.json()) as Array<{
      keyword_route_id: number | null;
      matched_route: { keyword: string } | null;
    }>;
    expect(body[0].keyword_route_id).toBe(1);
    expect(body[0].matched_route?.keyword).toBe("alpha");
  });

  it("includes matched route metadata on draft rows", async () => {
    const { GET } = await import("@/app/api/drafts/route");
    const resp = await GET(
      makeNextRequest("http://localhost/api/drafts?scan_id=1") as never
    );
    expect(resp.status).toBe(200);
    const body = (await resp.json()) as {
      data: Array<{
        keyword_route_id: number | null;
        matched_route: { intent: string | null } | null;
      }>;
    };
    expect(body.data[0].keyword_route_id).toBe(1);
    expect(body.data[0].matched_route?.intent).toBe("help folks");
  });
});
