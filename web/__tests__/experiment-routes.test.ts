import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-experiment-routes-"));
const dbPath = path.join(tmpDir, "scout.db");
const tracesDbPath = path.join(tmpDir, "scout_traces.db");
process.env.SCOUT_DB_PATH = dbPath;
process.env.TRACE_DB_PATH = tracesDbPath;

function stamp(n: number): string {
  return `2026-01-01T00:00:${String(n).padStart(2, "0")}.000000+00:00`;
}

function candidateConfigJson(): string {
  return JSON.stringify({
    version: 2,
    phase: "relevance",
    model: "candidate-model",
    system_prompt: "candidate prompt",
    system_prompt_sha256: "hash",
    grader_attached: false,
  });
}

function baselineEvidenceJson(): string {
  return JSON.stringify({
    version: 2,
    recorded_input_sha256: "input-hash",
    baseline_prompt_reused: false,
  });
}

function traceDiffJson(traceAId: string, traceBId: string): string {
  return JSON.stringify({
    trace_a_id: traceAId,
    trace_b_id: traceBId,
    tool_divergence: [],
    output_diff: null,
    error_category_change: null,
    score_deltas: {},
    score_details: {},
    cost_delta: 0,
    latency_ms_delta: 0,
    comparison_complete: true,
    comparison_incomplete_reason: null,
    a_output_preview: "",
    b_output_preview: "",
    a_output_hash: null,
    b_output_hash: null,
    a_output_byte_length: null,
    b_output_byte_length: null,
    a_output_complete: null,
    b_output_complete: null,
  });
}

beforeAll(() => {
  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE feedback_snapshots (
      id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL UNIQUE, policy_version TEXT NOT NULL,
      mode TEXT NOT NULL DEFAULT 'shadow', as_of TEXT NOT NULL, lookback_days INTEGER NOT NULL,
      max_grades INTEGER NOT NULL, segment_min_grades INTEGER NOT NULL, note_max_chars INTEGER NOT NULL,
      relevance_token_budget INTEGER NOT NULL, reply_draft_token_budget INTEGER NOT NULL,
      critic_token_budget INTEGER NOT NULL, population_count INTEGER NOT NULL,
      eligible_count INTEGER NOT NULL, excluded_count INTEGER NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE feedback_snapshot_phases (
      id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, phase TEXT NOT NULL,
      token_budget INTEGER NOT NULL, token_estimate INTEGER NOT NULL, truncated INTEGER NOT NULL DEFAULT 0,
      structured_summary TEXT NOT NULL, rendered_text TEXT NOT NULL, rendered_sha256 TEXT NOT NULL,
      created_at TEXT NOT NULL, UNIQUE(snapshot_id, phase)
    );
    CREATE TABLE evaluation_phase_runs (
      id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL, post_id INTEGER NOT NULL,
      evaluation_id INTEGER, snapshot_phase_id INTEGER NOT NULL, phase TEXT NOT NULL,
      trace_id TEXT NOT NULL UNIQUE, model TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE experiment_runs (
      id INTEGER PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
      candidate_config TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT
    );
    CREATE TABLE evaluation_experiments (
      id INTEGER PRIMARY KEY, experiment_run_id INTEGER NOT NULL, phase_run_id INTEGER NOT NULL,
      attempt_number INTEGER NOT NULL, supersedes_experiment_id INTEGER, status TEXT NOT NULL,
      baseline_evidence TEXT NOT NULL, candidate_trace_id TEXT UNIQUE,
      candidate_llm_call_count INTEGER, candidate_cost REAL, error_detail TEXT,
      created_at TEXT NOT NULL, completed_at TEXT
    );
    CREATE TABLE trace_comparisons (
      id INTEGER PRIMARY KEY, experiment_id INTEGER NOT NULL UNIQUE,
      trace_a_id TEXT NOT NULL, trace_b_id TEXT NOT NULL, jig_revision TEXT NOT NULL,
      trace_diff TEXT NOT NULL, domain_diff TEXT NOT NULL, score_evidence TEXT, created_at TEXT NOT NULL
    );
  `);

  db.prepare(
    `INSERT INTO feedback_snapshots
       (id, scan_id, policy_version, mode, as_of, lookback_days, max_grades, segment_min_grades,
        note_max_chars, relevance_token_budget, reply_draft_token_budget, critic_token_budget,
        population_count, eligible_count, excluded_count, created_at)
     VALUES (1, 100, 'v1', 'shadow', ?, 14, 50, 5, 280, 2000, 2000, 2000, 30, 20, 10, ?)`
  ).run(stamp(0), stamp(0));
  db.prepare(
    `INSERT INTO feedback_snapshot_phases
       (id, snapshot_id, phase, token_budget, token_estimate, truncated, structured_summary,
        rendered_text, rendered_sha256, created_at)
     VALUES (1, 1, 'relevance', 2000, 1500, 0, '{}', 'rendered', 'sha-rendered', ?)`
  ).run(stamp(0));
  db.prepare(
    `INSERT INTO evaluation_phase_runs
       (id, scan_id, post_id, evaluation_id, snapshot_phase_id, phase, trace_id, model, status, created_at)
     VALUES (10, 100, 1, null, 1, 'relevance', 'trace-baseline-1', 'baseline-model', 'complete', ?)`
  ).run(stamp(0));
  db.prepare(
    `INSERT INTO evaluation_phase_runs
       (id, scan_id, post_id, evaluation_id, snapshot_phase_id, phase, trace_id, model, status, created_at)
     VALUES (11, 100, 2, null, 1, 'relevance', 'trace-baseline-bad', 'baseline-model', 'complete', ?)`
  ).run(stamp(0));

  const insertRun = db.prepare(
    `INSERT INTO experiment_runs (id, name, status, candidate_config, created_at, completed_at)
     VALUES (?, ?, ?, ?, ?, ?)`
  );
  const insertExperiment = db.prepare(
    `INSERT INTO evaluation_experiments
       (id, experiment_run_id, phase_run_id, attempt_number, status, baseline_evidence,
        candidate_trace_id, candidate_llm_call_count, candidate_cost, error_detail,
        created_at, completed_at)
     VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)`
  );
  insertRun.run(1, "exp-one", "complete", candidateConfigJson(), stamp(1), stamp(1));
  insertExperiment.run(1, 1, 10, "complete", baselineEvidenceJson(), "trace-candidate-1", 1, 0.01, null, stamp(1), stamp(1));
  insertRun.run(2, "exp-two", "failed", candidateConfigJson(), stamp(2), stamp(2));
  insertExperiment.run(2, 2, 10, "failed", baselineEvidenceJson(), null, null, null, "boom", stamp(2), stamp(2));
  // A "bad baseline" experiment for the detail route's 500 data-integrity path.
  insertRun.run(3, "exp-bad-baseline", "queued", candidateConfigJson(), stamp(3), null);
  insertExperiment.run(3, 3, 11, "queued", baselineEvidenceJson(), null, null, null, null, stamp(3), null);

  db.prepare(
    `INSERT INTO trace_comparisons
       (id, experiment_id, trace_a_id, trace_b_id, jig_revision, trace_diff, domain_diff, score_evidence, created_at)
     VALUES (1, 1, 'trace-baseline-1', 'trace-candidate-1', 'rev', ?, '{"baseline":{"complete":true,"sha256":null,"utf8_byte_length":null,"incomplete_reason":null},"candidate":{"complete":true,"sha256":null,"utf8_byte_length":null,"incomplete_reason":null},"grader_not_attached":true}', NULL, ?)`
  ).run(traceDiffJson("trace-baseline-1", "trace-candidate-1"), stamp(1));

  db.close();

  const tracesDb = new Database(tracesDbPath);
  tracesDb.exec(`
    CREATE TABLE spans (
      id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, parent_id TEXT, kind TEXT NOT NULL, name TEXT NOT NULL,
      input TEXT, output TEXT, started_at TEXT NOT NULL, ended_at TEXT, duration_ms REAL,
      metadata TEXT, error TEXT, usage_input_tokens INTEGER, usage_output_tokens INTEGER, usage_cost REAL
    );
  `);
  tracesDb
    .prepare(
      `INSERT INTO spans (id, trace_id, parent_id, kind, name, started_at, metadata)
       VALUES (?, ?, NULL, 'agent_run', 'scout_relevance', ?, ?)`
    )
    .run(
      "span-root-1",
      "trace-baseline-1",
      stamp(0),
      JSON.stringify({ config: { system_prompt: "baseline prompt", system_prompt_is_callable: false } })
    );
  tracesDb
    .prepare(
      `INSERT INTO spans
         (id, trace_id, parent_id, kind, name, started_at, ended_at, duration_ms, metadata)
       VALUES (?, ?, NULL, 'agent_run', 'scout_relevance', ?, ?, 120, '{}')`
    )
    .run("span-root-candidate-1", "trace-candidate-1", stamp(0), stamp(0));
  const insertLlmSpan = tracesDb.prepare(
    `INSERT INTO spans
       (id, trace_id, parent_id, kind, name, started_at, ended_at, duration_ms, usage_cost)
     VALUES (?, ?, ?, 'llm_call', 'model-call', ?, ?, 10, ?)`
  );
  insertLlmSpan.run("span-llm-baseline", "trace-baseline-1", "span-root-1", stamp(0), stamp(0), 0.01);
  insertLlmSpan.run(
    "span-llm-candidate", "trace-candidate-1", "span-root-candidate-1", stamp(0), stamp(0), 0.02
  );
  // trace-baseline-bad has no spans at all.
  tracesDb.close();
});

afterAll(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
  delete process.env.SCOUT_DB_PATH;
  delete process.env.TRACE_DB_PATH;
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

describe("GET /api/feedback/experiments", () => {
  async function call(query: string, headers: Record<string, string> = {}) {
    const { GET } = await import("@/app/api/feedback/experiments/route");
    return GET(makeNextRequest(`http://localhost/api/feedback/experiments${query}`, headers) as never);
  }

  it("rejects an untrusted host with 403", async () => {
    expect((await call("", UNTRUSTED)).status).toBe(403);
  });

  it("rejects an unknown status value with 400", async () => {
    expect((await call("?status=bogus")).status).toBe(400);
  });

  it("rejects a repeated phase value with 400", async () => {
    expect((await call("?phase=relevance&phase=critic")).status).toBe(400);
  });

  it("rejects an invalid cursor with 400", async () => {
    expect((await call("?cursor=not-valid-base64!!")).status).toBe(400);
  });

  it("rejects a cursor whose bound filters differ from the request with 400", async () => {
    const { encodeExperimentListCursor } = await import("@/lib/feedback-experiment-filters");
    const cursor = encodeExperimentListCursor({
      created_at: stamp(1),
      id: 1,
      status: "complete",
      phase: null,
    });
    const resp = await call(`?cursor=${cursor}`); // no status filter in this request
    expect(resp.status).toBe(400);
  });

  it("accepts a cursor whose bound filters match the request", async () => {
    const { encodeExperimentListCursor } = await import("@/lib/feedback-experiment-filters");
    const cursor = encodeExperimentListCursor({
      created_at: stamp(2),
      id: 2,
      status: null,
      phase: null,
    });
    const resp = await call(`?cursor=${cursor}`);
    expect(resp.status).toBe(200);
  });

  it("returns a 200 page with the expected shape", async () => {
    const resp = await call("");
    expect(resp.status).toBe(200);
    const body = await resp.json();
    expect(body.data.map((r: { id: number }) => r.id)).toEqual([3, 2, 1]);
    expect(body.has_more).toBe(false);
    expect(body.next_cursor).toBeNull();
  });

  it("filters by status", async () => {
    const resp = await call("?status=failed");
    const body = await resp.json();
    expect(body.data.map((r: { id: number }) => r.id)).toEqual([2]);
  });
});

describe("GET /api/feedback/experiments/[experimentId]", () => {
  async function call(experimentId: string, headers: Record<string, string> = {}) {
    const { GET } = await import("@/app/api/feedback/experiments/[experimentId]/route");
    return GET(
      makeNextRequest(`http://localhost/api/feedback/experiments/${experimentId}`, headers) as never,
      { params: Promise.resolve({ experimentId }) }
    );
  }

  it("rejects an untrusted host with 403", async () => {
    expect((await call("1", UNTRUSTED)).status).toBe(403);
  });

  it("rejects a non-numeric id with 400", async () => {
    expect((await call("abc")).status).toBe(400);
  });

  it("rejects a negative id with 400", async () => {
    expect((await call("-1")).status).toBe(400);
  });

  it("404s for an experiment id that does not exist", async () => {
    expect((await call("9999")).status).toBe(404);
  });

  it("returns 200 with the full detail shape for a known experiment", async () => {
    const resp = await call("1");
    expect(resp.status).toBe(200);
    const body = await resp.json();
    expect(body.id).toBe(1);
    expect(body.status).toBe("complete");
    expect(body.baseline.trace_id).toBe("trace-baseline-1");
    expect(body.comparison).not.toBeNull();
  });

  it("500s with an internal data-integrity error for a corrupt baseline trace", async () => {
    const resp = await call("3");
    expect(resp.status).toBe(500);
    const body = await resp.json();
    expect(body.errors).toEqual(["internal data-integrity error"]);
  });
});

describe("GET /api/traces/[id]/comparisons", () => {
  async function call(traceId: string, headers: Record<string, string> = {}) {
    const { GET } = await import("@/app/api/traces/[id]/comparisons/route");
    return GET(makeNextRequest(`http://localhost/api/traces/${traceId}/comparisons`, headers) as never, {
      params: Promise.resolve({ id: traceId }),
    });
  }

  it("rejects an untrusted host with 403", async () => {
    expect((await call("trace-baseline-1", UNTRUSTED)).status).toBe(403);
  });

  it("returns an empty array (200) for a trace with no comparisons", async () => {
    const resp = await call("trace-with-no-comparisons");
    expect(resp.status).toBe(200);
    expect(await resp.json()).toEqual([]);
  });

  it("returns the comparison backlink for a trace used as a baseline", async () => {
    const resp = await call("trace-baseline-1");
    expect(resp.status).toBe(200);
    const body = await resp.json();
    expect(body).toEqual([
      {
        experiment_id: 1,
        comparison_id: 1,
        role: "baseline",
        experiment_name: "exp-one",
        experiment_status: "complete",
        experiment_url: "/feedback/experiments/1",
      },
    ]);
  });
});
