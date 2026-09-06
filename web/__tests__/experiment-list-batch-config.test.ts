import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { NextRequest } from "next/server";
import type { BatchCaseEvidenceV1, ReplayWorkerConfiguration } from "@/types/feedback-experiments";

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-experiment-batch-config-"));
const dbPath = path.join(tmpDir, "scout.db");
process.env.SCOUT_DB_PATH = dbPath;
const tracesDbPath = path.join(tmpDir, "traces.db");
process.env.TRACE_DB_PATH = tracesDbPath;
let worker: ReplayWorkerConfiguration;

function evidence(): BatchCaseEvidenceV1 {
  return {
    version: 1, recorded_input_sha256: "input-hash",
    baseline_model: "baseline-model", baseline_prompt_sha256: "baseline-hash",
    baseline_prompt_reused: false, candidate_model: "candidate-model",
    candidate_prompt_sha256: "candidate-hash", estimated_usd: null,
    reply_revision_id: 42, correction_sha256: "correction-hash", project_key: "synthetic",
    dossier_summary_id: "synthetic-dossier", dossier_revision: "a".repeat(40),
    grader_version: "normalized_edit_distance/v1", assembler_version: worker.assembler_version,
  };
}

function setWorkerConfiguration(value: unknown): void {
  const db = new Database(dbPath);
  db.prepare("UPDATE evaluation_experiments SET baseline_evidence=? WHERE id=1")
    .run(JSON.stringify({ ...evidence(), worker_configuration: value }));
  db.close();
}

beforeAll(() => {
  // Exercise the actual Python producer, not a hand-maintained JSON mirror.
  worker = JSON.parse(execFileSync("uv", ["run", "--no-sync", "python", "-c", `
import dataclasses, json
from scout.replay.experiments import CandidateReplayPlan, replay_worker_configuration
plan = CandidateReplayPlan(
    phase="reply_draft", candidate_model="candidate-model", candidate_system_prompt="synthetic",
    candidate_config_json="{}", baseline_prompt_sha256="baseline-hash",
    candidate_prompt_sha256="candidate-hash", baseline_prompt_reused=False,
    recorded_input_sha256="input-hash", grader_attached=True, is_no_op=False,
)
print(json.dumps(dataclasses.asdict(replay_worker_configuration(plan))))
`], { cwd: path.resolve(__dirname, "../.."), encoding: "utf8", timeout: 20_000 }));
  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE evaluation_phase_runs (
      id INTEGER PRIMARY KEY, phase TEXT NOT NULL, trace_id TEXT NOT NULL,
      model TEXT NOT NULL
    );
    CREATE TABLE experiment_runs (
      id INTEGER PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
      candidate_config TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT '2026-01-01', completed_at TEXT
    );
    CREATE TABLE evaluation_experiments (
      id INTEGER PRIMARY KEY, experiment_run_id INTEGER NOT NULL, phase_run_id INTEGER NOT NULL,
      attempt_number INTEGER NOT NULL, supersedes_experiment_id INTEGER, status TEXT NOT NULL,
      baseline_evidence TEXT, candidate_trace_id TEXT, candidate_llm_call_count INTEGER, candidate_cost REAL,
      created_at TEXT NOT NULL, completed_at TEXT
    );
    CREATE TABLE trace_comparisons (
      id INTEGER PRIMARY KEY, experiment_id INTEGER, trace_a_id TEXT, trace_b_id TEXT,
      trace_diff TEXT, score_evidence TEXT
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
  db.exec(`
    INSERT INTO experiment_runs (id,name,status,candidate_config,created_at)
      SELECT 2,'legacy-run','failed',candidate_config,'2025-01-01' FROM experiment_runs WHERE id=1;
    INSERT INTO evaluation_experiments
      (id,experiment_run_id,phase_run_id,attempt_number,status,created_at)
      VALUES (2,2,4490,1,'failed','2025-01-01');
  `);
  db.close();
  const traces = new Database(tracesDbPath);
  traces.exec(`CREATE TABLE spans (
    trace_id TEXT, parent_id TEXT, kind TEXT, duration_ms REAL, usage_cost REAL
  )`);
  traces.close();
});

beforeEach(() => {
  const db = new Database(dbPath);
  db.prepare("UPDATE evaluation_experiments SET baseline_evidence=?").run(JSON.stringify(evidence()));
  db.close();
  setWorkerConfiguration(worker);
});

afterAll(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
  delete process.env.SCOUT_DB_PATH;
  delete process.env.TRACE_DB_PATH;
});

describe("listExperiments — batch/sweep candidate config", () => {
  it("decodes the production v4 parent and exposes its model override", async () => {
    const { listExperiments } = await import("@/lib/feedback-experiment-queries");

    const page = listExperiments({});

    expect(page.data).toHaveLength(2);
    expect(page.data[0]?.candidate_model).toBe("openrouter/qwen/qwen3.5-9b");
    expect(page.data[0]?.grader_attached).toBe(true);
  });

  it("lists mixed legacy and Python-produced worker evidence through the page's real API", async () => {
    const { GET } = await import("@/app/api/feedback/experiment-runs/route");
    const response = await GET(new NextRequest("http://localhost/api/feedback/experiment-runs", {
      headers: { host: "localhost" },
    }));
    expect(response.status).toBe(200);
    expect((await response.json()).data.map((run: { id: number }) => run.id)).toEqual([1, 2]);
  });

  it.each([1, 2])("reads run %i detail with or without recorded worker configuration", async (id) => {
    const { getExperimentRunDetail } = await import("@/lib/feedback-experiment-queries");
    expect(getExperimentRunDetail(id)?.run.attempted_case_count).toBe(1);
  });

  it("accepts nullable grader identity and captured zero tool/parse limits", async () => {
    setWorkerConfiguration({ ...worker, grader_version: null, max_tool_calls: 0, max_parse_retries: 0 });
    const { listExperimentRuns } = await import("@/lib/feedback-experiment-queries");
    expect(listExperimentRuns({}).data).toHaveLength(2);
  });

  it.each([
    ["null configuration", (): unknown => null],
    ["missing field", (): unknown => ({ ...worker, model: undefined })],
    ["unknown field", (): unknown => ({ ...worker, invented: true })],
    ["string call limit", (): unknown => ({ ...worker, max_llm_calls: "3" })],
    ["fractional call limit", (): unknown => ({ ...worker, max_llm_calls: 1.5 })],
    ["negative tool limit", (): unknown => ({ ...worker, max_tool_calls: -1 })],
    ["invalid phase", (): unknown => ({ ...worker, phase: "unknown" })],
    ["invalid tool", (): unknown => ({ ...worker, tools: [42] })],
    ["string boolean", (): unknown => ({ ...worker, include_memory_in_prompt: "false" })],
  ] as const)("still rejects %s in retained worker evidence", async (_label, invalid) => {
    setWorkerConfiguration(invalid());
    const { listExperimentRuns, DataIntegrityError } = await import("@/lib/feedback-experiment-queries");
    expect(() => listExperimentRuns({})).toThrow(DataIntegrityError);
  });

  it("keeps the API integrity-error boundary for malformed worker evidence", async () => {
    setWorkerConfiguration({ ...worker, model: null });
    const { GET } = await import("@/app/api/feedback/experiment-runs/route");
    const response = await GET(new NextRequest("http://localhost/api/feedback/experiment-runs", {
      headers: { host: "localhost" },
    }));
    expect(response.status).toBe(500);
    expect(await response.json()).toEqual({ errors: ["internal data-integrity error"] });
  });
});
