import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-experiment-batch-config-"));
const dbPath = path.join(tmpDir, "scout.db");
process.env.SCOUT_DB_PATH = dbPath;

beforeAll(() => {
  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE evaluation_phase_runs (
      id INTEGER PRIMARY KEY, phase TEXT NOT NULL, trace_id TEXT NOT NULL,
      model TEXT NOT NULL
    );
    CREATE TABLE experiment_runs (
      id INTEGER PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
      candidate_config TEXT NOT NULL
    );
    CREATE TABLE evaluation_experiments (
      id INTEGER PRIMARY KEY, experiment_run_id INTEGER NOT NULL, phase_run_id INTEGER NOT NULL,
      attempt_number INTEGER NOT NULL, supersedes_experiment_id INTEGER, status TEXT NOT NULL,
      candidate_trace_id TEXT, candidate_llm_call_count INTEGER, candidate_cost REAL,
      created_at TEXT NOT NULL, completed_at TEXT
    );
    CREATE TABLE trace_comparisons (
      experiment_id INTEGER, trace_a_id TEXT, trace_b_id TEXT, trace_diff TEXT
    );
  `);
  db.prepare(
    `INSERT INTO evaluation_phase_runs (id, phase, trace_id, model)
     VALUES (4490, 'reply_draft', 'baseline-trace', 'openrouter/openai/gpt-5-mini')`
  ).run();
  db.prepare(
    `INSERT INTO experiment_runs (id, name, status, candidate_config)
     VALUES (1, 'open-model-sweep', 'partial', ?)`
  ).run(
    JSON.stringify({
      version: 4,
      phase: "reply_draft",
      variant_name: "qwen3.5-9b",
      model_override: "openrouter/qwen/qwen3.5-9b",
      system_prompt_override: null,
      system_prompt_override_sha256: null,
      grader_attached: true,
      sweep: { name: "reply-draft-openrouter-open-models", axis: "model", version: 1 },
      plan_sha256: "plan-sha",
      phase_run_ids: [4490],
      dropped_duplicate_phase_run_ids: [],
      skipped_pairs: [],
    })
  );
  db.prepare(
    `INSERT INTO evaluation_experiments
       (id, experiment_run_id, phase_run_id, attempt_number, status, created_at)
     VALUES (1, 1, 4490, 1, 'failed', '2026-08-27T22:41:00+00:00')`
  ).run();
  db.close();
});

afterAll(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
  delete process.env.SCOUT_DB_PATH;
});

describe("listExperiments — batch/sweep candidate config", () => {
  it("decodes the production v4 parent and exposes its model override", async () => {
    const { listExperiments } = await import("@/lib/feedback-experiment-queries");

    const page = listExperiments({});

    expect(page.data).toHaveLength(1);
    expect(page.data[0]?.candidate_model).toBe("openrouter/qwen/qwen3.5-9b");
    expect(page.data[0]?.grader_attached).toBe(true);
  });
});
