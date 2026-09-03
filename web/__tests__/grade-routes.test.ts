import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// Both grade write routes are now thin authenticated adapters: they resolve
// the exact evaluation through a read-only query (404 when it can't be
// resolved) and forward the request to the sidecar's POST /grades/{id}
// endpoint, relaying its status and body unchanged. All grade persistence,
// validation, and legacy adoption now live in the sidecar/StateManager —
// these tests cover only the adapter seam: auth, evaluation resolution, and
// faithful forwarding of the sidecar's response (including failures).

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-grade-routes-"));
const dbPath = path.join(tmpDir, "scout.db");
process.env.SCOUT_DB_PATH = dbPath;
process.env.SCOUT_SIDECAR_URL = "http://127.0.0.1:8799";

const fetchMock = vi.fn();
vi.stubGlobal("fetch", fetchMock);

beforeAll(() => {
  const db = new Database(dbPath);
  db.exec(`
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
  `);

  const now = "2026-05-15T00:00:00Z";
  const insertPost = db.prepare(
    `INSERT INTO posts
      (id, platform, platform_msg_id, channel_name, channel_id, author_name,
       author_id, content, url, created_at, scan_id)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  );
  insertPost.run(1, "discord", "msg-1", "general", "ch-1", "alice", "user-1", "hello", null, now, 1);
  insertPost.run(2, "discord", "msg-2", "general", "ch-1", "bob", "user-2", "hi", null, now, 1);
  insertPost.run(3, "discord", "msg-3", "general", "ch-1", "carol", "user-3", "relevant", null, now, 1);

  const insertEvaluation = db.prepare(
    `INSERT INTO evaluations (id, post_id, relevant, score, scan_id, posture) VALUES (?, ?, ?, ?, ?, ?)`
  );
  insertEvaluation.run(1, 1, 1, 0.9, 1, "ask");
  insertEvaluation.run(2, 2, 1, 0.9, 1, "ask");
  insertEvaluation.run(3, 3, 0, 0.1, 1, null);

  db.close();
});

afterAll(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
  delete process.env.SCOUT_DB_PATH;
  delete process.env.SCOUT_SIDECAR_URL;
  vi.unstubAllGlobals();
});

beforeEach(() => {
  fetchMock.mockReset();
});

function makeHeaders(trustedHost = true): { get: (name: string) => string | null } {
  const map = new Map(
    Object.entries({
      host: trustedHost ? "localhost" : "attacker.example.com",
    })
  );
  return { get: (name: string) => map.get(name.toLowerCase()) ?? null };
}

function makePostRequest(url: string, body: unknown, trustedHost = true) {
  return {
    nextUrl: new URL(url),
    headers: makeHeaders(trustedHost),
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

function sidecarSuccess(body: unknown) {
  return { status: 200, text: async () => JSON.stringify(body) };
}

describe("POST /api/grades/[evaluationId]/promote", () => {
  it("forwards a valid false-negative grade to the draft promotion endpoint", async () => {
    fetchMock.mockResolvedValueOnce(
      sidecarSuccess({
        id: 3,
        evaluation_id: 3,
        relevance_judgment: "false_negative",
        action_judgment: "fail",
        dimensions: ["usefulness"],
        failure_note: "Scout should have surfaced this",
        promotion: { target_evaluation_id: 4, surface_status: "surfaced" },
      })
    );
    const payload = {
      relevance_judgment: "false_negative",
      action_judgment: "fail",
      dimensions: ["usefulness"],
      failure_note: "Scout should have surfaced this",
    };
    const { POST } = await import("@/app/api/grades/[evaluationId]/promote/route");
    const response = await POST(
      makePostRequest("http://localhost/api/grades/3/promote", payload) as never,
      { params: Promise.resolve({ evaluationId: "3" }) }
    );

    expect(response.status).toBe(200);
    expect((await response.json()).promotion.target_evaluation_id).toBe(4);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8799/grades/3/promote",
      expect.objectContaining({ method: "POST", body: JSON.stringify(payload) })
    );
  });

  it("forwards edited_text unfiltered and relays the sidecar's reply_revision_id", async () => {
    fetchMock.mockResolvedValueOnce(
      sidecarSuccess({
        id: 3,
        evaluation_id: 3,
        relevance_judgment: "false_negative",
        action_judgment: "fail",
        dimensions: ["usefulness"],
        failure_note: "Scout should have surfaced this",
        reply_revision_id: 7,
        promotion: { target_evaluation_id: 4, surface_status: "surfaced" },
      })
    );
    const payload = {
      relevance_judgment: "false_negative",
      action_judgment: "fail",
      dimensions: ["usefulness"],
      failure_note: "Scout should have surfaced this",
      edited_text: "a corrected reply for the promoted case",
    };
    const { POST } = await import("@/app/api/grades/[evaluationId]/promote/route");
    const response = await POST(
      makePostRequest("http://localhost/api/grades/3/promote", payload) as never,
      { params: Promise.resolve({ evaluationId: "3" }) }
    );
    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body.reply_revision_id).toBe(7);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8799/grades/3/promote",
      expect.objectContaining({ method: "POST", body: JSON.stringify(payload) })
    );
  });

  it("rejects positive predictions and invalid grade shapes locally", async () => {
    const { POST } = await import("@/app/api/grades/[evaluationId]/promote/route");
    const positive = await POST(
      makePostRequest("http://localhost/api/grades/1/promote", {
        relevance_judgment: "false_negative",
        action_judgment: "fail",
        dimensions: ["usefulness"],
        failure_note: "wrong source",
      }) as never,
      { params: Promise.resolve({ evaluationId: "1" }) }
    );
    expect(positive.status).toBe(409);

    const invalid = await POST(
      makePostRequest("http://localhost/api/grades/3/promote", {
        relevance_judgment: "false_negative",
        action_judgment: "accept",
      }) as never,
      { params: Promise.resolve({ evaluationId: "3" }) }
    );
    expect(invalid.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("POST /api/grades/[evaluationId]", () => {
  it("requires a trusted write context and never reaches the sidecar without it", async () => {
    const { POST } = await import("@/app/api/grades/[evaluationId]/route");
    const resp = await POST(
      makePostRequest(
        "http://localhost/api/grades/1",
        { relevance_judgment: "correct", action_judgment: "accept" },
        false
      ) as never,
      { params: Promise.resolve({ evaluationId: "1" }) }
    );
    expect(resp.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("returns 404 when the evaluation does not exist, without calling the sidecar", async () => {
    const { POST } = await import("@/app/api/grades/[evaluationId]/route");
    const resp = await POST(
      makePostRequest("http://localhost/api/grades/999", {
        relevance_judgment: "correct",
        action_judgment: "accept",
      }) as never,
      { params: Promise.resolve({ evaluationId: "999" }) }
    );
    expect(resp.status).toBe(404);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("resolves the exact evaluation and forwards the request to the sidecar", async () => {
    fetchMock.mockResolvedValueOnce(
      sidecarSuccess({
        id: 1,
        evaluation_id: 1,
        post_id: 1,
        scan_id: 1,
        source: "web",
        graded_at: "2026-05-15T00:00:00.000Z",
        schema_version: 2,
        needs_regrade: 0,
        relevance_judgment: "correct",
        action_judgment: "accept",
        dimensions: null,
        failure_note: null,
        factual_offending_claim: null,
        factual_disposition: null,
        factual_contradicting_evidence: null,
        context_missing_input: null,
        posture_should_have_been: null,
        implication_implied_claim: null,
        implication_missing_support: null,
      })
    );
    const { POST } = await import("@/app/api/grades/[evaluationId]/route");
    const resp = await POST(
      makePostRequest("http://localhost/api/grades/1", {
        relevance_judgment: "correct",
        action_judgment: "accept",
      }) as never,
      { params: Promise.resolve({ evaluationId: "1" }) }
    );
    expect(resp.status).toBe(200);
    const body = await resp.json();
    expect(body.evaluation_id).toBe(1);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8799/grades/1",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ relevance_judgment: "correct", action_judgment: "accept" }),
      })
    );
  });

  it("rejects an envelope-invalid grade locally without ever calling the sidecar", async () => {
    const { POST } = await import("@/app/api/grades/[evaluationId]/route");
    const resp = await POST(
      makePostRequest("http://localhost/api/grades/1", {
        relevance_judgment: "correct",
      }) as never,
      { params: Promise.resolve({ evaluationId: "1" }) }
    );
    expect(resp.status).toBe(400);
    const body = await resp.json();
    expect(Array.isArray(body.errors)).toBe(true);
    expect(body.errors.length).toBeGreaterThan(0);
    expect(fetchMock).not.toHaveBeenCalled();

    const db = new Database(dbPath, { readonly: true });
    const count = db.prepare("SELECT COUNT(*) AS count FROM grades WHERE evaluation_id = 1").get() as { count: number };
    db.close();
    expect(count.count).toBe(0);
  });

  it("forwards a sidecar validation failure (400) unchanged and writes nothing locally", async () => {
    // The envelope is locally valid (relevance_judgment + action_judgment
    // shaped correctly) — this exercises a rejection that only the
    // sidecar/StateManager can know about (e.g. a stale schema_version),
    // not something the shared grading_schema.json envelope catches.
    fetchMock.mockResolvedValueOnce({
      status: 400,
      text: async () => JSON.stringify({ errors: ["save_grade requires schema_version=2, got 1"] }),
    });
    const { POST } = await import("@/app/api/grades/[evaluationId]/route");
    const resp = await POST(
      makePostRequest("http://localhost/api/grades/1", {
        relevance_judgment: "correct",
        action_judgment: "accept",
      }) as never,
      { params: Promise.resolve({ evaluationId: "1" }) }
    );
    expect(resp.status).toBe(400);
    const body = await resp.json();
    expect(body.errors).toEqual(["save_grade requires schema_version=2, got 1"]);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    const db = new Database(dbPath, { readonly: true });
    const count = db.prepare("SELECT COUNT(*) AS count FROM grades WHERE evaluation_id = 1").get() as { count: number };
    db.close();
    expect(count.count).toBe(0);
  });

  it("forwards edited_text unfiltered and relays the sidecar's reply_revision_id", async () => {
    // This route forwards `parsed.body` as-is (never a typed pick of known
    // fields), so a v3 edit-bearing grade must reach the sidecar with
    // edited_text intact and the sidecar's server-assigned
    // reply_revision_id must come back unchanged in the response body.
    fetchMock.mockResolvedValueOnce(
      sidecarSuccess({
        id: 1,
        evaluation_id: 1,
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["tone"],
        failure_note: "too casual",
        reply_revision_id: 42,
      })
    );
    const payload = {
      relevance_judgment: "correct",
      action_judgment: "fail",
      dimensions: ["tone"],
      failure_note: "too casual",
      edited_text: "a corrected reply",
    };
    const { POST } = await import("@/app/api/grades/[evaluationId]/route");
    const resp = await POST(
      makePostRequest("http://localhost/api/grades/1", payload) as never,
      { params: Promise.resolve({ evaluationId: "1" }) }
    );
    expect(resp.status).toBe(200);
    const body = await resp.json();
    expect(body.reply_revision_id).toBe(42);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8799/grades/1",
      expect.objectContaining({ method: "POST", body: JSON.stringify(payload) })
    );
  });

  it("surfaces a sidecar network failure as a gateway timeout", async () => {
    fetchMock.mockRejectedValueOnce(new DOMException("signal timed out", "TimeoutError"));
    const { POST } = await import("@/app/api/grades/[evaluationId]/route");
    const resp = await POST(
      makePostRequest("http://localhost/api/grades/1", {
        relevance_judgment: "correct",
        action_judgment: "accept",
      }) as never,
      { params: Promise.resolve({ evaluationId: "1" }) }
    );
    expect(resp.status).toBe(504);
    const body = await resp.json();
    expect(body.code).toBe("SIDECAR_TIMEOUT");
  });
});

describe("POST /api/scans/[id]/posts/[postId]/grade", () => {
  it("returns 404 when the post has no evaluation in the requested scan, without calling the sidecar", async () => {
    const { POST } = await import("@/app/api/scans/[id]/posts/[postId]/grade/route");
    const resp = await POST(
      makePostRequest("http://localhost/api/scans/999/posts/2/grade", {
        relevance_judgment: "correct",
        action_judgment: "accept",
      }) as never,
      { params: Promise.resolve({ id: "999", postId: "2" }) }
    );
    expect(resp.status).toBe(404);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("resolves the evaluation for the exact scan/post pair and calls the sidecar at /grades/{evaluationId}", async () => {
    fetchMock.mockResolvedValueOnce(
      sidecarSuccess({
        id: 2,
        evaluation_id: 2,
        post_id: 2,
        scan_id: 1,
        source: "web",
        graded_at: "2026-05-15T00:00:00.000Z",
        schema_version: 2,
        needs_regrade: 0,
        relevance_judgment: "correct",
        action_judgment: "accept",
        dimensions: null,
        failure_note: null,
        factual_offending_claim: null,
        factual_disposition: null,
        factual_contradicting_evidence: null,
        context_missing_input: null,
        posture_should_have_been: null,
        implication_implied_claim: null,
        implication_missing_support: null,
      })
    );
    const { POST } = await import("@/app/api/scans/[id]/posts/[postId]/grade/route");
    const resp = await POST(
      makePostRequest("http://localhost/api/scans/1/posts/2/grade", {
        relevance_judgment: "correct",
        action_judgment: "accept",
      }) as never,
      { params: Promise.resolve({ id: "1", postId: "2" }) }
    );
    expect(resp.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8799/grades/2",
      expect.objectContaining({ method: "POST" })
    );
  });

  it("forwards edited_text unfiltered and relays the sidecar's reply_revision_id", async () => {
    fetchMock.mockResolvedValueOnce(
      sidecarSuccess({
        id: 2,
        evaluation_id: 2,
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["tone"],
        failure_note: "too casual",
        reply_revision_id: 43,
      })
    );
    const payload = {
      relevance_judgment: "correct",
      action_judgment: "fail",
      dimensions: ["tone"],
      failure_note: "too casual",
      edited_text: "a corrected reply",
    };
    const { POST } = await import("@/app/api/scans/[id]/posts/[postId]/grade/route");
    const resp = await POST(
      makePostRequest("http://localhost/api/scans/1/posts/2/grade", payload) as never,
      { params: Promise.resolve({ id: "1", postId: "2" }) }
    );
    expect(resp.status).toBe(200);
    const body = await resp.json();
    expect(body.reply_revision_id).toBe(43);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8799/grades/2",
      expect.objectContaining({ method: "POST", body: JSON.stringify(payload) })
    );
  });

  it("rejects a posture correction equal to the stored evaluation posture locally, without calling the sidecar", async () => {
    // Evaluation 2's stored posture is "ask" (see fixture setup above), so
    // posture_should_have_been: "ask" is a contextual posture-inequality
    // violation the shared envelope schema catches before the sidecar is
    // ever reached.
    const { POST } = await import("@/app/api/scans/[id]/posts/[postId]/grade/route");
    const resp = await POST(
      makePostRequest("http://localhost/api/scans/1/posts/2/grade", {
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["posture"],
        failure_note: "wrong posture",
        posture_should_have_been: "ask",
      }) as never,
      { params: Promise.resolve({ id: "1", postId: "2" }) }
    );
    expect(resp.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();

    const db = new Database(dbPath, { readonly: true });
    const count = db.prepare("SELECT COUNT(*) AS count FROM grades WHERE evaluation_id = 2").get() as { count: number };
    db.close();
    expect(count.count).toBe(0);
  });

  it("forwards a sidecar validation failure (400) unchanged and writes nothing locally", async () => {
    // The envelope is locally valid (posture correction differs from the
    // stored "ask" posture) — this exercises a rejection that only the
    // sidecar/StateManager can know about.
    fetchMock.mockResolvedValueOnce({
      status: 400,
      text: async () => JSON.stringify({ errors: ["save_grade requires schema_version=2, got 1"] }),
    });
    const { POST } = await import("@/app/api/scans/[id]/posts/[postId]/grade/route");
    const resp = await POST(
      makePostRequest("http://localhost/api/scans/1/posts/2/grade", {
        relevance_judgment: "correct",
        action_judgment: "fail",
        dimensions: ["posture"],
        failure_note: "wrong posture",
        posture_should_have_been: "answer",
      }) as never,
      { params: Promise.resolve({ id: "1", postId: "2" }) }
    );
    expect(resp.status).toBe(400);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    const db = new Database(dbPath, { readonly: true });
    const count = db.prepare("SELECT COUNT(*) AS count FROM grades WHERE evaluation_id = 2").get() as { count: number };
    db.close();
    expect(count.count).toBe(0);
  });
});
