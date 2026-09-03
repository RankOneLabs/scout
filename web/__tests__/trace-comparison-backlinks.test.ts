import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-trace-comparison-backlinks-"));
const dbPath = path.join(tmpDir, "scout.db");
process.env.SCOUT_DB_PATH = dbPath;

function stamp(n: number): string {
  return `2026-01-01T00:00:${String(n).padStart(2, "0")}.000000+00:00`;
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

  const insertRun = db.prepare(
    `INSERT INTO experiment_runs (id, name, status, candidate_config, created_at)
     VALUES (?, ?, ?, '{}', ?)`
  );
  const insertExperiment = db.prepare(
    `INSERT INTO evaluation_experiments
       (id, experiment_run_id, phase_run_id, attempt_number, status, baseline_evidence,
        candidate_trace_id, created_at)
     VALUES (?, ?, ?, 1, ?, '{}', ?, ?)`
  );
  // trace-shared is the baseline of experiment 1 and (later) the candidate
  // of experiment 2 — one trace can participate on either side across
  // distinct experiments.
  insertRun.run(1, "exp-one", "complete", stamp(1));
  insertExperiment.run(1, 1, 10, "complete", "trace-candidate-1", stamp(1));
  insertRun.run(2, "exp-two", "complete", stamp(2));
  insertExperiment.run(2, 2, 11, "complete", "trace-shared", stamp(2));
  insertRun.run(3, "exp-three", "failed", stamp(3));
  insertExperiment.run(3, 3, 12, "failed", "trace-candidate-3", stamp(3));

  const insertComparison = db.prepare(
    `INSERT INTO trace_comparisons
       (id, experiment_id, trace_a_id, trace_b_id, jig_revision, trace_diff, domain_diff, score_evidence, created_at)
     VALUES (?, ?, ?, ?, 'rev', ?, '{}', NULL, ?)`
  );
  insertComparison.run(
    1, 1, "trace-shared", "trace-candidate-1",
    traceDiffJson("trace-shared", "trace-candidate-1"), stamp(1)
  );
  insertComparison.run(
    2, 2, "trace-baseline-2", "trace-shared",
    traceDiffJson("trace-baseline-2", "trace-shared"), stamp(2)
  );
  // A third comparison unrelated to trace-shared entirely.
  insertComparison.run(
    3, 3, "trace-baseline-3", "trace-candidate-3",
    traceDiffJson("trace-baseline-3", "trace-candidate-3"), stamp(3)
  );

  db.close();
});

afterAll(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
  delete process.env.SCOUT_DB_PATH;
});

describe("getTraceComparisonBacklinks", () => {
  it("finds a trace used as baseline in one comparison and candidate in another, ordered created_at DESC, id DESC", async () => {
    const { getTraceComparisonBacklinks } = await import("@/lib/trace-queries");
    const backlinks = getTraceComparisonBacklinks("trace-shared");
    expect(backlinks).toEqual([
      {
        experiment_id: 2,
        comparison_id: 2,
        role: "candidate",
        experiment_name: "exp-two",
        experiment_status: "complete",
        experiment_url: "/feedback/experiments/2",
      },
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

  it("returns an empty array for a trace with no comparisons", async () => {
    const { getTraceComparisonBacklinks } = await import("@/lib/trace-queries");
    expect(getTraceComparisonBacklinks("trace-with-no-comparisons")).toEqual([]);
  });

  it("does not surface an unrelated trace's comparison", async () => {
    const { getTraceComparisonBacklinks } = await import("@/lib/trace-queries");
    const backlinks = getTraceComparisonBacklinks("trace-shared");
    expect(backlinks.some((b) => b.experiment_id === 3)).toBe(false);
  });
});
