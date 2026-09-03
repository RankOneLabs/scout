import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { spawn, type ChildProcess } from "node:child_process";
import net from "node:net";
import path from "node:path";
import fs from "node:fs";
import os from "node:os";
import Database from "better-sqlite3";

// Every test in this file makes a real network round trip to a real
// subprocess — generous relative to the default 5s so a slow `uv run`
// resolve/spawn on a loaded machine doesn't flake the suite.
vi.setConfig({ testTimeout: 15_000 });

// T-010: end-to-end proof of the grade-proxy write boundary — browser
// (a NextRequest-shaped mock) -> Next.js API route -> a REAL Python
// grading_api_sidecar process over a real TCP socket -> a real SQLite
// database. Every other web test file mocks `@/lib/sidecar-bridge`'s
// callSidecar; this file deliberately does not, so `callSidecar`'s real
// `fetch()` calls leave this process and hit real ASGI/Starlette/
// FastAPI/StateManager code, the same way a production deployment would.
//
// The content-lane coverage this file used to carry (intake, candidate
// lifecycle) moved out with the content engine itself — only the
// `/grades/...` surface (and the shared /healthz path) still lives in the
// sidecar this file spawns.

const REPO_ROOT = path.resolve(__dirname, "../..");
const SIDECAR_TOKEN = "e2e-sidecar-token";
const STARTUP_TIMEOUT_MS = 20_000;

function getFreePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const address = srv.address();
      if (address && typeof address === "object") {
        const port = address.port;
        srv.close(() => resolve(port));
      } else {
        srv.close(() => reject(new Error("could not determine a free port")));
      }
    });
  });
}

async function waitForHealthy(baseUrl: string, deadlineMs: number): Promise<void> {
  const start = Date.now();
  let lastError: unknown;
  while (Date.now() - start < deadlineMs) {
    try {
      const resp = await fetch(`${baseUrl}/healthz`);
      if (resp.status === 200) return;
    } catch (err) {
      lastError = err;
    }
    await new Promise((r) => setTimeout(r, 150));
  }
  throw new Error(
    `sidecar never became healthy within ${deadlineMs}ms (last error: ${String(lastError)})`
  );
}

function makeHeaders(
  entries: Record<string, string> = {}
): { get: (name: string) => string | null } {
  const allEntries = { host: "localhost", ...entries };
  const map = new Map(
    Object.entries(allEntries).map(([k, v]) => [k.toLowerCase(), v])
  );
  return { get: (name: string) => map.get(name.toLowerCase()) ?? null };
}

function jsonRequest(
  url: string,
  body: unknown,
  headerEntries: Record<string, string> = {}
) {
  return {
    nextUrl: new URL(url),
    json: async () => body,
    text: async () => JSON.stringify(body),
    headers: makeHeaders(headerEntries),
  };
}

describe("real sidecar end-to-end (T-010)", () => {
  let proc: ChildProcess;
  let baseUrl: string;
  let tmpDir: string;
  let dbPath: string;
  let db: Database.Database;

  beforeAll(async () => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-e2e-sidecar-"));
    dbPath = path.join(tmpDir, "scout.db");
    const port = await getFreePort();
    baseUrl = `http://127.0.0.1:${port}`;

    proc = spawn(
      "uv",
      ["run", "python", "-c", "from scout.cli.grading_api import main; main()"],
      {
        cwd: REPO_ROOT,
        env: {
          ...process.env,
          SCOUT_SIDECAR_HOST: "127.0.0.1",
          SCOUT_SIDECAR_PORT: String(port),
          SCOUT_SIDECAR_TOKEN: SIDECAR_TOKEN,
          DB_PATH: dbPath,
          TRACE_DB_PATH: path.join(tmpDir, "scout_traces.db"),
          FEEDBACK_DB_PATH: path.join(tmpDir, "scout_feedback.db"),
        },
        stdio: ["ignore", "pipe", "pipe"],
      }
    );

    let stderr = "";
    proc.stderr?.on("data", (chunk) => {
      stderr += String(chunk);
    });
    proc.on("exit", (code) => {
      if (code !== null && code !== 0) {
        console.error(`sidecar subprocess exited with code ${code}:\n${stderr}`);
      }
    });

    await waitForHealthy(baseUrl, STARTUP_TIMEOUT_MS);

    // sidecar-bridge.ts reads these from process.env on every call, so
    // setting them here (in this Node process, not the child's) is what
    // points callSidecar at the live server.
    process.env.SCOUT_SIDECAR_URL = baseUrl;
    process.env.SCOUT_SIDECAR_TOKEN = SIDECAR_TOKEN;
    // The grades/scans proxy routes pre-check evaluation existence via a
    // real DB read (web/lib/queries.ts) before ever calling the sidecar —
    // point that read path at the same file the live sidecar just
    // bootstrapped the schema into.
    process.env.SCOUT_DB_PATH = dbPath;

    db = new Database(dbPath);
  }, STARTUP_TIMEOUT_MS + 5000);

  afterAll(() => {
    db?.close();
    delete process.env.SCOUT_SIDECAR_URL;
    delete process.env.SCOUT_SIDECAR_TOKEN;
    delete process.env.SCOUT_DB_PATH;
    proc?.kill("SIGTERM");
    // Guard against beforeAll failing before tmpDir is usable (e.g. `uv`
    // missing, port allocation failure) — an unguarded rmSync here would
    // throw and bury the real beforeAll error behind a cleanup error.
    if (tmpDir) {
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  });

  describe("grade proxy surface — real evaluation/grade persistence", () => {
    function seedEvaluation(platformId: string): { scanId: number; postId: number; evalId: number } {
      const now = new Date().toISOString();
      const scanId = db
        .prepare(
          `INSERT INTO scans (started_at, completed_at, messages_scanned, relevant_found)
           VALUES (?, ?, 0, 0)`
        )
        .run(now, now).lastInsertRowid as number;
      const postId = db
        .prepare(
          `INSERT INTO posts
             (platform, platform_msg_id, channel_name, channel_id, author_name,
              author_id, content, url, created_at, scan_id)
           VALUES ('discord', ?, 'general', 'ch-1', 'alice', 'user-1', 'hello', NULL, ?, ?)`
        )
        .run(platformId, now, scanId).lastInsertRowid as number;
      const evalId = db
        .prepare(
          `INSERT INTO evaluations (post_id, relevant, score, scan_id, posture, surface_status)
           VALUES (?, 1, 0.9, ?, 'ask', 'not_relevant')`
        )
        .run(postId, scanId).lastInsertRowid as number;
      return { scanId, postId, evalId };
    }

    it("saves a grade for real through /api/grades/[evaluationId]", async () => {
      const { evalId } = seedEvaluation("grade-e2e-1");
      const { POST } = await import("@/app/api/grades/[evaluationId]/route");
      const resp = await POST(
        jsonRequest(`http://localhost/api/grades/${evalId}`, {
          relevance_judgment: "correct",
          action_judgment: "accept",
        }) as never,
        { params: Promise.resolve({ evaluationId: String(evalId) }) }
      );
      expect(resp.status).toBe(200);
      const row = db
        .prepare("SELECT source, graded_at FROM grades WHERE evaluation_id = ?")
        .get(evalId) as { source: string; graded_at: string };
      expect(row.source).toBe("web");
      expect(row.graded_at).toBeTruthy();
    });

    it("saves a grade for real through /api/scans/[id]/posts/[postId]/grade", async () => {
      const { scanId, postId } = seedEvaluation("grade-e2e-2");
      const { POST } = await import(
        "@/app/api/scans/[id]/posts/[postId]/grade/route"
      );
      const resp = await POST(
        jsonRequest(`http://localhost/api/scans/${scanId}/posts/${postId}/grade`, {
          relevance_judgment: "correct",
          action_judgment: "accept",
        }) as never,
        { params: Promise.resolve({ id: String(scanId), postId: String(postId) }) }
      );
      expect(resp.status).toBe(200);
      const row = db
        .prepare("SELECT source FROM grades WHERE post_id = ? AND scan_id = ?")
        .get(postId, scanId) as { source: string };
      expect(row.source).toBe("web");
    });

    it("carries edited_text through the real sidecar into a durable reply_draft_revisions row and returns reply_revision_id", async () => {
      const { scanId, postId, evalId } = seedEvaluation("grade-e2e-edit-1");
      const now = new Date().toISOString();
      const draftId = db
        .prepare(
          `INSERT INTO draft_comments
             (post_id, evaluation_id, project_key, comment_text, created_at, scan_id)
           VALUES (?, ?, 'gateway', 'original reply', ?, ?)`
        )
        .run(postId, evalId, now, scanId).lastInsertRowid as number;

      const { POST } = await import("@/app/api/grades/[evaluationId]/route");
      const resp = await POST(
        jsonRequest(`http://localhost/api/grades/${evalId}`, {
          relevance_judgment: "correct",
          action_judgment: "fail",
          dimensions: ["tone"],
          failure_note: "too casual",
          edited_text: "a corrected, more formal reply",
        }) as never,
        { params: Promise.resolve({ evaluationId: String(evalId) }) }
      );
      expect(resp.status).toBe(200);
      const body = await resp.json();
      expect(body.reply_revision_id).not.toBeNull();

      const rev = db
        .prepare("SELECT reply_text, draft_comment_id FROM reply_draft_revisions WHERE id = ?")
        .get(body.reply_revision_id) as { reply_text: string; draft_comment_id: number };
      expect(rev.reply_text).toBe("a corrected, more formal reply");
      expect(rev.draft_comment_id).toBe(draftId);
    });

    it("rejects edited_text with no resolvable draft through the real sidecar, writing nothing", async () => {
      const { evalId } = seedEvaluation("grade-e2e-edit-2");
      const { POST } = await import("@/app/api/grades/[evaluationId]/route");
      const resp = await POST(
        jsonRequest(`http://localhost/api/grades/${evalId}`, {
          relevance_judgment: "correct",
          action_judgment: "fail",
          dimensions: ["tone"],
          failure_note: "too casual",
          edited_text: "nothing to attach this to",
        }) as never,
        { params: Promise.resolve({ evaluationId: String(evalId) }) }
      );
      expect(resp.status).toBe(400);
      const body = await resp.json();
      expect(Array.isArray(body.errors)).toBe(true);

      const row = db
        .prepare("SELECT COUNT(*) AS count FROM grades WHERE evaluation_id = ?")
        .get(evalId) as { count: number };
      expect(row.count).toBe(0);

      const revisionRow = db
        .prepare(
          `SELECT COUNT(*) AS count
           FROM reply_draft_revisions AS revisions
           JOIN draft_comments AS drafts ON drafts.id = revisions.draft_comment_id
           WHERE drafts.evaluation_id = ?`
        )
        .get(evalId) as { count: number };
      expect(revisionRow.count).toBe(0);
    });
  });

  describe("healthz stays responsive while a real write is blocked mid-request", () => {
    it("GET /healthz completes quickly while another connection holds the write lock", async () => {
      // A separate connection to the same file holds a real BEGIN
      // IMMEDIATE write lock open (not a sleep). Python's sqlite3 module
      // defaults every connect() to a 5s busy-retry timeout, so the live
      // sidecar's own write blocks (genuinely in flight, retrying) for up
      // to 5s waiting on that lock — inside FastAPI's threadpool, since
      // grade_endpoint is a `def` handler. This test proves /healthz
      // (async, no StateManager) answers immediately regardless.
      const now = new Date().toISOString();
      const scanId = db
        .prepare(
          `INSERT INTO scans (started_at, completed_at, messages_scanned, relevant_found)
           VALUES (?, ?, 0, 0)`
        )
        .run(now, now).lastInsertRowid as number;
      const postId = db
        .prepare(
          `INSERT INTO posts
             (platform, platform_msg_id, channel_name, channel_id, author_name,
              author_id, content, url, created_at, scan_id)
           VALUES ('discord', 'healthz-lock', 'general', 'ch-1', 'alice', 'user-1', 'hello', NULL, ?, ?)`
        )
        .run(now, scanId).lastInsertRowid as number;
      const evalId = db
        .prepare(
          `INSERT INTO evaluations (post_id, relevant, score, scan_id, posture, surface_status)
           VALUES (?, 1, 0.9, ?, 'ask', 'not_relevant')`
        )
        .run(postId, scanId).lastInsertRowid as number;

      const blocker = new Database(dbPath);
      blocker.exec("BEGIN IMMEDIATE");

      const writeAttempt = fetch(`${baseUrl}/grades/${evalId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Scout-Sidecar-Token": SIDECAR_TOKEN },
        body: JSON.stringify({
          relevance_judgment: "correct",
          action_judgment: "accept",
        }),
      });

      try {
        const start = Date.now();
        const healthResp = await fetch(`${baseUrl}/healthz`);
        const elapsedMs = Date.now() - start;

        expect(healthResp.status).toBe(200);
        expect(elapsedMs).toBeLessThan(2000);
      } finally {
        // Release the lock so the blocked write can proceed, then drain
        // it before closing the blocker connection.
        blocker.exec("COMMIT");
        blocker.close();
        await writeAttempt;
      }
    });
  });
});
