import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-scans-"));
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
      UNIQUE(platform, platform_msg_id)
    );
    CREATE TABLE evaluations (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      post_id INTEGER NOT NULL,
      relevant INTEGER NOT NULL,
      score REAL NOT NULL,
      reason TEXT,
      relevant_to TEXT,
      scan_id INTEGER,
      posture TEXT
    );
    CREATE TABLE draft_comments (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      post_id INTEGER NOT NULL,
      evaluation_id INTEGER NOT NULL,
      project_key TEXT,
      comment_text TEXT,
      created_at TEXT,
      scan_id INTEGER
    );
    CREATE TABLE critiques (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      draft_id INTEGER NOT NULL,
      verdict TEXT NOT NULL,
      feedback TEXT,
      created_at TEXT,
      scan_id INTEGER
    );
    CREATE TABLE grades (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      evaluation_id INTEGER,
      post_id INTEGER NOT NULL,
      scan_id INTEGER,
      source TEXT NOT NULL,
      graded_at TEXT NOT NULL,
      schema_version INTEGER NOT NULL DEFAULT 1,
      needs_regrade INTEGER NOT NULL DEFAULT 0,
      relevance_judgment TEXT NOT NULL,
      action_judgment TEXT,
      dimensions TEXT,
      failure_note TEXT,
      factual_offending_claim TEXT,
      factual_disposition TEXT,
      factual_contradicting_evidence TEXT,
      context_missing_input TEXT,
      posture_should_have_been TEXT,
      implication_implied_claim TEXT,
      implication_missing_support TEXT
    );
    CREATE UNIQUE INDEX grades_evaluation_id_unique
      ON grades(evaluation_id) WHERE evaluation_id IS NOT NULL;
    CREATE TABLE gate_blocks (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      scan_id INTEGER
    );
    CREATE TABLE scan_fetch_failures (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      scan_id INTEGER NOT NULL,
      platform TEXT NOT NULL,
      context TEXT,
      kind TEXT NOT NULL,
      message TEXT,
      http_status INTEGER,
      retry_after TEXT,
      retryable INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL
    );
    CREATE TABLE feedback_snapshots (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      scan_id INTEGER NOT NULL UNIQUE,
      policy_version TEXT NOT NULL,
      mode TEXT NOT NULL,
      as_of TEXT NOT NULL,
      lookback_days INTEGER NOT NULL,
      max_grades INTEGER NOT NULL,
      segment_min_grades INTEGER NOT NULL,
      note_max_chars INTEGER NOT NULL,
      population_count INTEGER NOT NULL,
      eligible_count INTEGER NOT NULL,
      excluded_count INTEGER NOT NULL,
      created_at TEXT NOT NULL
    );
  `);

  const now = "2026-05-15T00:00:00Z";
  const insertScan = db.prepare(
    `INSERT INTO scans (id, started_at, completed_at, messages_scanned, relevant_found)
     VALUES (?, ?, ?, ?, ?)`
  );

  insertScan.run(1, now, now, 0, 0);
  insertScan.run(2, now, now, 0, 0);
  insertScan.run(3, now, null, 0, 0);
  insertScan.run(4, now, now, 1, 0);

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
    "user-1",
    "hello",
    "https://example.com/1",
    now,
    2
  );

  // Evaluation linking post 1 to scan 2 (required for evaluation-scoped grading)
  db.prepare(
    `INSERT INTO evaluations (post_id, relevant, score, scan_id) VALUES (?, ?, ?, ?)`
  ).run(1, 1, 0.9, 2);
  db.prepare(
    `INSERT INTO evaluations (post_id, relevant, score, scan_id) VALUES (?, ?, ?, ?)`
  ).run(1, 1, 0.5, 4);

  // Scan 4 has a feedback snapshot; scan 2 (queried below) does not.
  db.prepare(
    `INSERT INTO feedback_snapshots
       (scan_id, policy_version, mode, as_of, lookback_days, max_grades,
        segment_min_grades, note_max_chars, population_count, eligible_count,
        excluded_count, created_at)
     VALUES (4, 'evaluation-feedback/v1', 'shadow', ?, 90, 200, 5, 240, 0, 0, 0, ?)`
  ).run(now, now);

  db.close();
});

afterAll(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
  delete process.env.SCOUT_DB_PATH;
});

function makeHeaders(
  entries: Record<string, string> = {}
): { get: (name: string) => string | null } {
  const allEntries = {
    host: "localhost",
    ...entries,
  };
  const map = new Map(
    Object.entries(allEntries).map(([k, v]) => [k.toLowerCase(), v])
  );
  return { get: (name: string) => map.get(name.toLowerCase()) ?? null };
}

function makeNextRequest(url: string): {
  nextUrl: URL;
  headers: { get: (name: string) => string | null };
} {
  return { nextUrl: new URL(url), headers: makeHeaders() };
}

describe("scans query layer", () => {
  it("hides historical true-empty completed scans from the list", async () => {
    const { getScans } = await import("@/lib/queries");

    expect(getScans().map((scan) => scan.id)).toEqual([4, 3, 2]);
  });

  it("returns 404 for a hidden true-empty completed scan id", async () => {
    const { GET } = await import("@/app/api/scans/route");

    const resp = await GET(makeNextRequest("http://localhost/api/scans?id=1") as never);
    expect(resp.status).toBe(404);
  });

  it("includes the scan's feedback snapshot summary when one is recorded", async () => {
    const { GET } = await import("@/app/api/scans/route");
    const resp = await GET(makeNextRequest("http://localhost/api/scans?id=4") as never);
    expect(resp.status).toBe(200);
    const body = await resp.json();
    expect(body.feedback).toMatchObject({ mode: "shadow", policy_version: "evaluation-feedback/v1" });
  });

  it("returns feedback: null for a scan with no recorded snapshot", async () => {
    const { GET } = await import("@/app/api/scans/route");
    const resp = await GET(makeNextRequest("http://localhost/api/scans?id=2") as never);
    expect(resp.status).toBe(200);
    const body = await resp.json();
    expect(body.feedback).toBeNull();
  });
});

describe("grade write context guard", () => {
  function makeGradeRequest(headers: Record<string, string> = {}, body?: unknown) {
    return {
      nextUrl: new URL("http://localhost/api/scans/2/posts/1/grade"),
      headers: makeHeaders(headers),
      json: async () => body ?? {},
      text: async () => (body !== undefined ? JSON.stringify(body) : ""),
    };
  }

  it("rejects POST grade from an untrusted host", async () => {
    const { POST } = await import("@/app/api/scans/[id]/posts/[postId]/grade/route");
    const resp = await POST(
      makeGradeRequest({ host: "attacker.example.com" }) as never,
      { params: Promise.resolve({ id: "2", postId: "1" }) }
    );
    expect(resp.status).toBe(403);
  });

  it("rejects POST grade when x-forwarded-for is set", async () => {
    const { POST } = await import("@/app/api/scans/[id]/posts/[postId]/grade/route");
    const resp = await POST(
      makeGradeRequest({ "x-forwarded-for": "1.2.3.4" }) as never,
      { params: Promise.resolve({ id: "2", postId: "1" }) }
    );
    expect(resp.status).toBe(403);
  });

  it("rejects POST grade when origin is external", async () => {
    const { POST } = await import("@/app/api/scans/[id]/posts/[postId]/grade/route");
    const resp = await POST(
      makeGradeRequest({ "origin": "https://evil.example.com" }) as never,
      { params: Promise.resolve({ id: "2", postId: "1" }) }
    );
    expect(resp.status).toBe(403);
  });

  it("returns 400 for non-numeric scan id", async () => {
    const { POST } = await import("@/app/api/scans/[id]/posts/[postId]/grade/route");
    const resp = await POST(
      makeGradeRequest() as never,
      { params: Promise.resolve({ id: "abc", postId: "1" }) }
    );
    expect(resp.status).toBe(400);
  });

  it("returns 400 for path-injection post id", async () => {
    const { POST } = await import("@/app/api/scans/[id]/posts/[postId]/grade/route");
    const injectedId = "1%2Fgrade%3Fx%3D";
    const resp = await POST(
      makeGradeRequest() as never,
      { params: Promise.resolve({ id: "2", postId: injectedId }) }
    );
    expect(resp.status).toBe(400);
  });

  it("returns 404 when post has no evaluation in the requested scan", async () => {
    const { POST } = await import("@/app/api/scans/[id]/posts/[postId]/grade/route");
    const resp = await POST(
      makeGradeRequest({}, { relevance_judgment: "correct", action_judgment: "accept" }) as never,
      { params: Promise.resolve({ id: "3", postId: "1" }) }
    );
    expect(resp.status).toBe(404);
  });
});

// Grade validation, persistence, and legacy-adoption behavior now live in
// the sidecar (see tests/test_grading_api_sidecar.py::TestGrade) and this
// route's own forwarding contract (see __tests__/grade-routes.test.ts) —
// this file retains only the guard checks above, which run entirely
// before the route ever calls the sidecar.
