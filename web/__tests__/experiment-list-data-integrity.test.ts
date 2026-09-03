import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// Regression coverage for a review finding: the list route must translate a
// DataIntegrityError from a corrupt persisted candidate_config the same way
// the detail route does, rather than leaking an unhandled exception with an
// inconsistent error contract.

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-experiment-list-integrity-"));
const dbPath = path.join(tmpDir, "scout.db");
process.env.SCOUT_DB_PATH = dbPath;

beforeAll(() => {
  const db = new Database(dbPath);
  db.exec(`
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
    `INSERT INTO evaluation_phase_runs
       (id, scan_id, post_id, evaluation_id, snapshot_phase_id, phase, trace_id, model, status, created_at)
     VALUES (10, 100, 1, null, 1, 'relevance', 'trace-baseline-1', 'baseline-model', 'complete', '2026-01-01T00:00:00.000000+00:00')`
  ).run();
  // candidate_config is missing every required field — fails Zod validation.
  db.prepare(
    `INSERT INTO experiment_runs
       (id, name, status, candidate_config, created_at)
     VALUES (1, 'corrupt-exp', 'queued', '{}', '2026-01-01T00:00:01.000000+00:00')`
  ).run();
  db.prepare(
    `INSERT INTO evaluation_experiments
       (id, experiment_run_id, phase_run_id, attempt_number, status, baseline_evidence, created_at)
     VALUES (1, 1, 10, 1, 'queued', '{}', '2026-01-01T00:00:01.000000+00:00')`
  ).run();
  db.close();
});

afterAll(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
  delete process.env.SCOUT_DB_PATH;
});

describe("listExperiments — corrupt candidate_config", () => {
  it("throws DataIntegrityError rather than returning a coerced row", async () => {
    const { listExperiments, DataIntegrityError } = await import("@/lib/feedback-experiment-queries");
    expect(() => listExperiments({})).toThrow(DataIntegrityError);
  });
});

describe("GET /api/feedback/experiments — corrupt candidate_config", () => {
  function makeHeaders(): { get: (name: string) => string | null } {
    const map = new Map([["host", "localhost"]]);
    return { get: (name: string) => map.get(name.toLowerCase()) ?? null };
  }

  it("500s with the same internal data-integrity error contract as the detail route", async () => {
    const { GET } = await import("@/app/api/feedback/experiments/route");
    const resp = await GET({
      nextUrl: new URL("http://localhost/api/feedback/experiments"),
      headers: makeHeaders(),
    } as never);
    expect(resp.status).toBe(500);
    expect(await resp.json()).toEqual({ errors: ["internal data-integrity error"] });
  });
});
