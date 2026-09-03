import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-phase-run-routes-"));
const dbPath = path.join(tmpDir, "scout.db");
process.env.SCOUT_DB_PATH = dbPath;

beforeAll(() => {
  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE feedback_snapshot_phases (
      id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, phase TEXT NOT NULL
    );
    CREATE TABLE grades (
      id INTEGER PRIMARY KEY, evaluation_id INTEGER UNIQUE
    );
    CREATE TABLE evaluation_phase_runs (
      id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL, post_id INTEGER NOT NULL,
      evaluation_id INTEGER, snapshot_phase_id INTEGER NOT NULL, phase TEXT NOT NULL,
      trace_id TEXT NOT NULL UNIQUE, model TEXT NOT NULL, status TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    INSERT INTO feedback_snapshot_phases VALUES (10, 77, 'relevance');
    INSERT INTO grades VALUES (101, 1);
  `);
  db.prepare(
    `INSERT INTO evaluation_phase_runs
       (id, scan_id, post_id, evaluation_id, snapshot_phase_id, phase, trace_id, model, status, created_at)
     VALUES (1, 500, 1, 1, 10, 'relevance', 'trace-abc-123', 'model-a', 'complete', '2026-01-01T00:00:00.000Z')`
  ).run();
  db.close();
});

afterAll(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
  delete process.env.SCOUT_DB_PATH;
});

function makeHeaders(entries: Record<string, string> = {}): { get: (name: string) => string | null } {
  const allEntries = { host: "localhost", ...entries };
  const map = new Map(Object.entries(allEntries).map(([k, v]) => [k.toLowerCase(), v]));
  return { get: (name: string) => map.get(name.toLowerCase()) ?? null };
}

function makeNextRequest(
  url: string,
  headerEntries: Record<string, string> = {}
): { nextUrl: URL; headers: { get: (name: string) => string | null } } {
  return { nextUrl: new URL(url), headers: makeHeaders(headerEntries) };
}

const UNTRUSTED = { host: "attacker.example.com" };

describe("GET /api/feedback/phase-runs/[phaseRunId]", () => {
  async function call(phaseRunId: string, headers: Record<string, string> = {}) {
    const { GET } = await import("@/app/api/feedback/phase-runs/[phaseRunId]/route");
    return GET(
      makeNextRequest(`http://localhost/api/feedback/phase-runs/${phaseRunId}`, headers) as never,
      { params: Promise.resolve({ phaseRunId }) }
    );
  }

  it("rejects an untrusted host with 403", async () => {
    const resp = await call("1", UNTRUSTED);
    expect(resp.status).toBe(403);
  });

  it("rejects a non-numeric id with 400", async () => {
    expect((await call("abc")).status).toBe(400);
  });

  it("rejects a negative or zero id with 400", async () => {
    expect((await call("0")).status).toBe(400);
    expect((await call("-1")).status).toBe(400);
  });

  it("404s for a phase run id that does not exist", async () => {
    expect((await call("9999")).status).toBe(404);
  });

  it("returns the full stored-key identity for a known phase run", async () => {
    const resp = await call("1");
    expect(resp.status).toBe(200);
    const body = await resp.json();
    expect(body).toEqual({
      id: 1,
      scan_id: 500,
      post_id: 1,
      evaluation_id: 1,
      grade_id: 101,
      snapshot_id: 77,
      snapshot_phase_id: 10,
      phase: "relevance",
      trace_id: "trace-abc-123",
      model: "model-a",
      status: "complete",
      created_at: "2026-01-01T00:00:00.000Z",
    });
  });
});

describe("GET /api/traces/[id]/phase-run", () => {
  async function call(traceId: string, headers: Record<string, string> = {}) {
    const { GET } = await import("@/app/api/traces/[id]/phase-run/route");
    return GET(makeNextRequest(`http://localhost/api/traces/${traceId}/phase-run`, headers) as never, {
      params: Promise.resolve({ id: traceId }),
    });
  }

  it("rejects an untrusted host with 403", async () => {
    expect((await call("trace-abc-123", UNTRUSTED)).status).toBe(403);
  });

  it("404s when no phase run is linked to this trace", async () => {
    expect((await call("no-such-trace")).status).toBe(404);
  });

  it("resolves the backlink by trace_id and matches the phase-run detail exactly (bidirectional navigation)", async () => {
    const { GET: getPhaseRun } = await import("@/app/api/feedback/phase-runs/[phaseRunId]/route");
    const byPhaseRunId = await getPhaseRun(
      makeNextRequest("http://localhost/api/feedback/phase-runs/1") as never,
      { params: Promise.resolve({ phaseRunId: "1" }) }
    );
    const byTraceId = await call("trace-abc-123");
    expect(byTraceId.status).toBe(200);
    expect(await byTraceId.json()).toEqual(await byPhaseRunId.json());
  });
});
