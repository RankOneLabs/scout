import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createHash } from "node:crypto";

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-experiment-queries-"));
const dbPath = path.join(tmpDir, "scout.db");
const tracesDbPath = path.join(tmpDir, "scout_traces.db");
process.env.SCOUT_DB_PATH = dbPath;
process.env.TRACE_DB_PATH = tracesDbPath;

function stamp(n: number): string {
  return `2026-01-01T00:00:${String(n).padStart(2, "0")}.000000+00:00`;
}

const BASELINE_PROMPT = "You are Scout's relevance evaluator. Unicode: café 🧠";
const CANDIDATE_PROMPT = "You are Scout's stricter relevance evaluator.";

function traceDiffJson(overrides: Record<string, unknown> = {}): string {
  return JSON.stringify({
    trace_a_id: "trace-baseline-1",
    trace_b_id: "trace-candidate-1",
    tool_divergence: [],
    output_diff: null,
    error_category_change: null,
    score_deltas: {},
    score_details: {},
    cost_delta: 0.01,
    latency_ms_delta: 120.5,
    comparison_complete: true,
    comparison_incomplete_reason: null,
    a_output_preview: "baseline preview",
    b_output_preview: "candidate preview ✅",
    a_output_hash: "hash-a",
    b_output_hash: "hash-b",
    a_output_byte_length: 42,
    b_output_byte_length: 55,
    a_output_complete: { relevant: true },
    b_output_complete: { relevant: true, relevant_to: ["gateway"] },
    ...overrides,
  });
}

function domainDiffJson(overrides: Record<string, unknown> = {}): string {
  return JSON.stringify({
    baseline: {
      complete: true,
      sha256: "sha-a",
      utf8_byte_length: 42,
      incomplete_reason: null,
      value: { relevant: true },
    },
    candidate: {
      complete: true,
      sha256: "sha-b",
      utf8_byte_length: 55,
      incomplete_reason: null,
      value: { relevant: true, relevant_to: ["gateway"] },
    },
    grader_not_attached: true,
    additions: ["/relevant_to/0"],
    removals: [],
    changes: [],
    ...overrides,
  });
}

function candidateConfigJson(overrides: Record<string, unknown> = {}): string {
  return JSON.stringify({
    version: 2,
    phase: "relevance",
    model: "candidate-model",
    system_prompt: CANDIDATE_PROMPT,
    system_prompt_sha256: createHash("sha256").update(CANDIDATE_PROMPT, "utf8").digest("hex"),
    grader_attached: false,
    ...overrides,
  });
}

function baselineEvidenceJson(overrides: Record<string, unknown> = {}): string {
  return JSON.stringify({
    version: 2,
    recorded_input_sha256: "input-hash",
    baseline_prompt_reused: false,
    ...overrides,
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

  const insertPhaseRun = db.prepare(
    `INSERT INTO evaluation_phase_runs
       (id, scan_id, post_id, evaluation_id, snapshot_phase_id, phase, trace_id, model, status, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'complete', ?)`
  );
  insertPhaseRun.run(10, 100, 1, 5, 1, "relevance", "trace-baseline-1", "baseline-model", stamp(0));
  insertPhaseRun.run(11, 100, 2, null, 1, "relevance", "trace-baseline-2", "baseline-model", stamp(0));
  // A third phase run whose trace never made it into the traces db —
  // exercises the data-integrity-error path for a corrupt/missing baseline.
  insertPhaseRun.run(12, 100, 3, null, 1, "relevance", "trace-baseline-bad", "baseline-model", stamp(0));
  // A reply_draft phase run for the graded-attempt fixture.
  insertPhaseRun.run(13, 100, 4, 6, 1, "reply_draft", "trace-baseline-3", "baseline-model", stamp(0));

  // Each old one-off evaluation_experiments row becomes its own one-child
  // experiment_runs parent — every id below names an attempt (child) whose
  // experiment_run_id equals its own id, exactly as the v36 migration maps
  // an existing row 1:1 onto a fresh parent.
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
  // Complete experiment with a full comparison.
  insertRun.run(1, "complete-exp", "complete", candidateConfigJson(), stamp(1), stamp(2));
  insertExperiment.run(
    1, 1, 10, "complete", baselineEvidenceJson(), "trace-candidate-1", 3, 0.02, null,
    stamp(1), stamp(2)
  );
  // Failed experiment that still retains its candidate trace (failed after
  // record_candidate_trace but before diff construction) and no comparison.
  insertRun.run(
    2, "failed-exp", "failed", candidateConfigJson({ model: "candidate-model-2" }), stamp(2), stamp(3)
  );
  insertExperiment.run(
    2, 2, 10, "failed", baselineEvidenceJson(),
    "trace-candidate-2", 2, 0.01, "The trace or domain comparison could not be constructed or serialized.",
    stamp(2), stamp(3)
  );
  // Queued experiment: no candidate trace, no comparison.
  insertRun.run(3, "queued-exp", "queued", candidateConfigJson(), stamp(3), null);
  insertExperiment.run(3, 3, 11, "queued", baselineEvidenceJson(), null, null, null, null, stamp(3), null);
  // A second complete experiment tied on created_at with #1 to exercise the
  // id DESC tiebreaker.
  insertRun.run(4, "tied-exp", "complete", candidateConfigJson(), stamp(1), stamp(1));
  insertExperiment.run(
    4, 4, 11, "complete", baselineEvidenceJson(), "trace-candidate-4", 1, 0.005, null,
    stamp(1), stamp(1)
  );
  // Newest experiment, queued, whose baseline trace has no spans at all.
  insertRun.run(5, "bad-baseline-exp", "queued", candidateConfigJson(), stamp(4), null);
  insertExperiment.run(5, 5, 12, "queued", baselineEvidenceJson(), null, null, null, null, stamp(4), null);

  // A graded reply_draft run: attempt #1 (id=6) failed, and its retry
  // (id=7, attempt #2, supersedes_experiment_id=6) succeeded with a
  // grader-scored comparison — exercises run_status/attempt_number/
  // supersedes_experiment_id/grader_attached/score_evidence together.
  const gradedCandidateConfig = candidateConfigJson({ phase: "reply_draft", grader_attached: true });
  const gradedBaselineEvidence = baselineEvidenceJson({
    baseline_model: "baseline-model",
    baseline_prompt_sha256: "baseline-hash",
    reply_revision_id: 42,
    correction_sha256: "correction-hash",
    project_key: "gateway",
    dossier_summary_id: "gateway-dossier",
    dossier_revision: "a".repeat(40),
    grader_version: "normalized_edit_distance/v1",
    assembler_version: "assemble_draft_text/v1",
  });
  insertRun.run(6, "graded-exp", "complete", gradedCandidateConfig, stamp(5), stamp(6));
  insertExperiment.run(
    6, 6, 13, "failed", gradedBaselineEvidence,
    "trace-candidate-6a", 1, 0.01, "The candidate's reply-correction score could not be verified.",
    stamp(5), stamp(5)
  );
  db.prepare(
    `INSERT INTO evaluation_experiments
       (id, experiment_run_id, phase_run_id, attempt_number, supersedes_experiment_id, status,
        baseline_evidence, candidate_trace_id, candidate_llm_call_count, candidate_cost,
        error_detail, created_at, completed_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).run(
    7, 6, 13, 2, 6, "complete", gradedBaselineEvidence,
    "trace-candidate-6", 1, 0.03, null, stamp(6), stamp(6)
  );
  db.prepare(
    `INSERT INTO trace_comparisons
       (id, experiment_id, trace_a_id, trace_b_id, jig_revision, trace_diff, domain_diff, score_evidence, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).run(
    2, 7, "trace-baseline-3", "trace-candidate-6", "jig-rev-abc",
    traceDiffJson({ trace_a_id: "trace-baseline-3", trace_b_id: "trace-candidate-6" }), domainDiffJson(),
    JSON.stringify({
      grader_version: "normalized_edit_distance/v1",
      assembler_version: "assemble_draft_text/v1",
      correction_sha256: "correction-hash",
      reply_revision_id: 42,
      baseline_distance: 0.4,
      candidate_distance: 0.1,
      delta: -0.3,
      grader_attached: true,
    }),
    stamp(6)
  );

  db.prepare(
    `INSERT INTO trace_comparisons
       (id, experiment_id, trace_a_id, trace_b_id, jig_revision, trace_diff, domain_diff, score_evidence, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).run(
    1, 1, "trace-baseline-1", "trace-candidate-1", "jig-rev-abc",
    traceDiffJson(), domainDiffJson(), null, stamp(2)
  );

  db.close();

  const tracesDb = new Database(tracesDbPath);
  tracesDb.exec(`
    CREATE TABLE spans (
      id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, parent_id TEXT, kind TEXT NOT NULL, name TEXT NOT NULL,
      input TEXT, output TEXT, started_at TEXT NOT NULL, ended_at TEXT, duration_ms REAL,
      metadata TEXT, error TEXT, usage_input_tokens INTEGER, usage_output_tokens INTEGER, usage_cost REAL
    );
  `);
  const insertSpan = tracesDb.prepare(
    `INSERT INTO spans (id, trace_id, parent_id, kind, name, input, output, started_at, ended_at, duration_ms, metadata)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  );
  insertSpan.run(
    "span-root-1", "trace-baseline-1", null, "agent_run", "scout_relevance", null, null,
    stamp(0), stamp(0), 100,
    JSON.stringify({
      config: { system_prompt: BASELINE_PROMPT, system_prompt_is_callable: false, model_id: "baseline-model" },
      input: "recorded input text",
    })
  );
  insertSpan.run(
    "span-root-candidate-1", "trace-candidate-1", null, "agent_run", "scout_relevance",
    null, null, stamp(0), stamp(0), 120, JSON.stringify({})
  );
  const insertLlmSpan = tracesDb.prepare(
    `INSERT INTO spans
       (id, trace_id, parent_id, kind, name, started_at, ended_at, duration_ms, usage_cost)
     VALUES (?, ?, ?, 'llm_call', 'model-call', ?, ?, 10, ?)`
  );
  insertLlmSpan.run(
    "span-llm-baseline-1", "trace-baseline-1", "span-root-1", stamp(0), stamp(0), 0.01
  );
  insertLlmSpan.run(
    "span-llm-candidate-1", "trace-candidate-1", "span-root-candidate-1",
    stamp(0), stamp(0), 0.02
  );
  insertSpan.run(
    "span-root-2", "trace-baseline-2", null, "agent_run", "scout_relevance", null, null,
    stamp(0), stamp(0), 100,
    JSON.stringify({
      config: { system_prompt: "Second baseline prompt.", system_prompt_is_callable: false, model_id: "baseline-model" },
      input: "recorded input text 2",
    })
  );
  // trace-baseline-bad deliberately has no spans at all — a corrupt/missing
  // baseline, for the data-integrity error path.
  insertSpan.run(
    "span-root-3", "trace-baseline-3", null, "agent_run", "scout_reply_draft", null, null,
    stamp(0), stamp(0), 100,
    JSON.stringify({
      config: { system_prompt: "Reply-draft baseline prompt.", system_prompt_is_callable: false, model_id: "baseline-model" },
      input: "recorded input text 3",
    })
  );
  insertSpan.run(
    "span-root-candidate-6", "trace-candidate-6", null, "agent_run", "scout_reply_draft",
    null, null, stamp(0), stamp(0), 130, JSON.stringify({})
  );
  insertLlmSpan.run(
    "span-llm-candidate-6", "trace-candidate-6", "span-root-candidate-6", stamp(0), stamp(0), 0.03
  );
  tracesDb.close();
});

afterAll(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
  delete process.env.SCOUT_DB_PATH;
  delete process.env.TRACE_DB_PATH;
});

describe("listExperiments", () => {
  it("orders created_at DESC, id DESC with id as the tiebreaker", async () => {
    const { listExperiments } = await import("@/lib/feedback-experiment-queries");
    const page = listExperiments({});
    // id4 and id1 share the same created_at (stamp(1)); id4 sorts first.
    expect(page.data.map((r) => r.id)).toEqual([7, 6, 5, 3, 2, 4, 1]);
    expect(page.has_more).toBe(false);
    expect(page.next_cursor).toBeNull();
  });

  it("joins each attempt to its parent run's name/status/candidate_config and exposes attempt identity", async () => {
    const { listExperiments } = await import("@/lib/feedback-experiment-queries");
    const page = listExperiments({});
    const retry = page.data.find((r) => r.id === 7)!;
    expect(retry.name).toBe("graded-exp");
    expect(retry.experiment_run_id).toBe(6);
    expect(retry.run_status).toBe("complete");
    expect(retry.attempt_number).toBe(2);
    expect(retry.supersedes_experiment_id).toBe(6);
    expect(retry.grader_attached).toBe(true);
    expect(retry.phase).toBe("reply_draft");

    const firstAttempt = page.data.find((r) => r.id === 6)!;
    expect(firstAttempt.attempt_number).toBe(1);
    expect(firstAttempt.supersedes_experiment_id).toBeNull();
    expect(firstAttempt.status).toBe("failed");
    // The run's own projected status stays 'complete' (the retry succeeded)
    // even though this earlier attempt itself is 'failed'.
    expect(firstAttempt.run_status).toBe("complete");

    const ungraded = page.data.find((r) => r.id === 1)!;
    expect(ungraded.grader_attached).toBe(false);
  });

  it("derives phase/baseline model from the phase run and candidate model from candidate_config", async () => {
    const { listExperiments } = await import("@/lib/feedback-experiment-queries");
    const page = listExperiments({});
    const row = page.data.find((r) => r.id === 1)!;
    expect(row.phase).toBe("relevance");
    expect(row.baseline_model).toBe("baseline-model");
    expect(row.candidate_model).toBe("candidate-model");
  });

  it("exposes comparison_complete as null when no comparison is persisted", async () => {
    const { listExperiments } = await import("@/lib/feedback-experiment-queries");
    const page = listExperiments({});
    expect(page.data.find((r) => r.id === 2)!.comparison_complete).toBeNull();
    expect(page.data.find((r) => r.id === 3)!.comparison_complete).toBeNull();
  });

  it("exposes comparison_complete=true from the persisted trace_diff for a complete comparison", async () => {
    const { listExperiments } = await import("@/lib/feedback-experiment-queries");
    const page = listExperiments({});
    expect(page.data.find((r) => r.id === 1)!.comparison_complete).toBe(true);
  });

  it("retains the candidate trace id for a failed experiment", async () => {
    const { listExperiments } = await import("@/lib/feedback-experiment-queries");
    const page = listExperiments({});
    expect(page.data.find((r) => r.id === 2)!.candidate_trace_id).toBe("trace-candidate-2");
  });

  it("filters by status", async () => {
    const { listExperiments } = await import("@/lib/feedback-experiment-queries");
    const page = listExperiments({ status: "failed" });
    // id6's attempt status is 'failed' even though its run ultimately
    // completed via the id7 retry — status filters the attempt, not the run.
    expect(page.data.map((r) => r.id)).toEqual([6, 2]);
  });

  it("filters by phase", async () => {
    const { listExperiments } = await import("@/lib/feedback-experiment-queries");
    const page = listExperiments({ phase: "relevance" });
    expect(page.data.map((r) => r.id).sort((a, b) => a - b)).toEqual([1, 2, 3, 4, 5]);
    const replyDraft = listExperiments({ phase: "reply_draft" });
    expect(replyDraft.data.map((r) => r.id).sort((a, b) => a - b)).toEqual([6, 7]);
  });

  it("pages with a stable filter-bound cursor", async () => {
    const { listExperiments } = await import("@/lib/feedback-experiment-queries");
    const { decodeExperimentListCursor } = await import("@/lib/feedback-experiment-filters");

    const page1 = listExperiments({ limit: 4 });
    expect(page1.data.map((r) => r.id)).toEqual([7, 6, 5, 3]);
    expect(page1.has_more).toBe(true);
    expect(page1.next_cursor).not.toBeNull();

    const cursor = decodeExperimentListCursor(page1.next_cursor!)!;
    expect(cursor.status).toBeNull();
    expect(cursor.phase).toBeNull();

    const page2 = listExperiments({ limit: 4, cursor });
    expect(page2.data.map((r) => r.id)).toEqual([2, 4, 1]);
    expect(page2.has_more).toBe(false);
    expect(page2.next_cursor).toBeNull();
  });
});

describe("listExperimentRuns", () => {
  it("includes a failed graded attempt that has no comparison", async () => {
    const { listExperimentRuns } = await import("@/lib/feedback-experiment-queries");
    const result = listExperimentRuns({ status: "complete", limit: 100 });
    const run = result.data.find((item) => item.id === 6);

    expect(run).toBeDefined();
    expect(run!.current_case_count).toBe(1);
    expect(run!.retry_count).toBe(1);
    expect(run!.status_counts.complete).toBe(1);
  });
});

describe("getExperimentDetail", () => {
  it("returns null for an unknown id", async () => {
    const { getExperimentDetail } = await import("@/lib/feedback-experiment-queries");
    expect(getExperimentDetail(9999)).toBeNull();
  });

  it("assembles baseline, snapshot, candidate, and comparison for a complete experiment", async () => {
    const { getExperimentDetail } = await import("@/lib/feedback-experiment-queries");
    const detail = getExperimentDetail(1)!;

    expect(detail.status).toBe("complete");
    expect(detail.error_detail).toBeNull();
    expect(detail.experiment_run.id).toBe(1);
    expect(detail.experiment_run.name).toBe("complete-exp");
    expect(detail.experiment_run.status).toBe("complete");
    expect(detail.experiment_run.candidate_config.grader_attached).toBe(false);
    expect(detail.attempt_number).toBe(1);
    expect(detail.supersedes_experiment_id).toBeNull();
    expect(detail.baseline_evidence.baseline_prompt_reused).toBe(false);
    expect(detail.baseline_evidence.reply_revision_id).toBeUndefined();

    expect(detail.baseline.phase_run_id).toBe(10);
    expect(detail.baseline.trace_id).toBe("trace-baseline-1");
    expect(detail.baseline.model).toBe("baseline-model");
    expect(detail.baseline.system_prompt).toBe(BASELINE_PROMPT);
    expect(detail.baseline.system_prompt_sha256).toBe(
      createHash("sha256").update(BASELINE_PROMPT, "utf8").digest("hex")
    );
    expect(detail.baseline.phase_run_url).toBe("/feedback/phase-runs/10");
    expect(detail.baseline.trace_url).toBe("/traces/trace-baseline-1");

    expect(detail.evaluation_id).toBe(5);

    expect(detail.snapshot.snapshot_id).toBe(1);
    expect(detail.snapshot.policy_version).toBe("v1");
    expect(detail.snapshot.lookback_days).toBe(14);
    expect(detail.snapshot.max_grades).toBe(50);
    expect(detail.snapshot.snapshot_url).toBe("/feedback?snapshotId=1");

    expect(detail.candidate.trace_id).toBe("trace-candidate-1");
    expect(detail.candidate.trace_url).toBe("/traces/trace-candidate-1");
    expect(detail.candidate.model).toBe("candidate-model");
    expect(detail.candidate.system_prompt).toBe(CANDIDATE_PROMPT);
    expect(detail.candidate.llm_call_count).toBe(3);
    expect(detail.candidate.cost).toBe(0.02);

    expect(detail.comparison).not.toBeNull();
    expect(detail.comparison!.trace_a_id).toBe("trace-baseline-1");
    expect(detail.comparison!.trace_b_id).toBe("trace-candidate-1");
    expect(detail.comparison!.jig_revision).toBe("jig-rev-abc");
    expect(detail.comparison!.trace_diff.comparison_complete).toBe(true);
    expect(detail.comparison!.cost_delta_available).toBe(true);
    expect(detail.comparison!.latency_delta_available).toBe(true);
    expect(detail.comparison!.trace_diff.b_output_preview).toBe("candidate preview ✅");
    expect(detail.comparison!.domain_diff.additions).toEqual(["/relevant_to/0"]);
  });

  it("assembles a graded retry attempt's baseline evidence, correction oracle, and score evidence", async () => {
    const { getExperimentDetail } = await import("@/lib/feedback-experiment-queries");
    const detail = getExperimentDetail(7)!;

    expect(detail.status).toBe("complete");
    expect(detail.attempt_number).toBe(2);
    expect(detail.supersedes_experiment_id).toBe(6);
    expect(detail.experiment_run.id).toBe(6);
    expect(detail.experiment_run.name).toBe("graded-exp");
    expect(detail.experiment_run.candidate_config.grader_attached).toBe(true);
    expect(detail.experiment_run.candidate_config.phase).toBe("reply_draft");

    expect(detail.baseline_evidence.reply_revision_id).toBe(42);
    expect(detail.baseline_evidence.project_key).toBe("gateway");
    expect(detail.baseline_evidence.dossier_summary_id).toBe("gateway-dossier");
    expect(detail.baseline_evidence.grader_version).toBe("normalized_edit_distance/v1");

    expect(detail.comparison).not.toBeNull();
    expect(detail.comparison!.score_evidence).not.toBeNull();
    expect(detail.comparison!.score_evidence!.baseline_distance).toBe(0.4);
    expect(detail.comparison!.score_evidence!.candidate_distance).toBe(0.1);
    expect(detail.comparison!.score_evidence!.delta).toBe(-0.3);
    expect(detail.comparison!.score_evidence!.reply_revision_id).toBe(42);
  });

  it("leaves score_evidence null for an ungraded (relevance) comparison", async () => {
    const { getExperimentDetail } = await import("@/lib/feedback-experiment-queries");
    const detail = getExperimentDetail(1)!;
    expect(detail.comparison).not.toBeNull();
    expect(detail.comparison!.score_evidence).toBeNull();
  });

  it("preserves unicode content byte-for-byte through the JSON round trip", async () => {
    const { getExperimentDetail } = await import("@/lib/feedback-experiment-queries");
    const detail = getExperimentDetail(1)!;
    expect(detail.baseline.system_prompt).toContain("café");
    expect(detail.baseline.system_prompt).toContain("🧠");
  });

  it("marks deltas unavailable when stored trace evidence is missing without rewriting TraceDiff", async () => {
    const { getExperimentDetail } = await import("@/lib/feedback-experiment-queries");
    const tracesDb = new Database(tracesDbPath);
    tracesDb.prepare("UPDATE spans SET usage_cost = NULL WHERE id = 'span-llm-candidate-1'").run();
    tracesDb.prepare("UPDATE spans SET duration_ms = NULL WHERE id = 'span-root-candidate-1'").run();
    try {
      const detail = getExperimentDetail(1)!;
      expect(detail.comparison!.cost_delta_available).toBe(false);
      expect(detail.comparison!.latency_delta_available).toBe(false);
      expect(detail.comparison!.trace_diff.cost_delta).toBe(0.01);
      expect(detail.comparison!.trace_diff.latency_ms_delta).toBe(120.5);
    } finally {
      tracesDb.prepare("UPDATE spans SET usage_cost = 0.02 WHERE id = 'span-llm-candidate-1'").run();
      tracesDb.prepare("UPDATE spans SET duration_ms = 120 WHERE id = 'span-root-candidate-1'").run();
      tracesDb.close();
    }
  });

  it("returns null candidate trace/url and null comparison for a queued experiment", async () => {
    const { getExperimentDetail } = await import("@/lib/feedback-experiment-queries");
    const detail = getExperimentDetail(3)!;
    expect(detail.status).toBe("queued");
    expect(detail.candidate.trace_id).toBeNull();
    expect(detail.candidate.trace_url).toBeNull();
    expect(detail.comparison).toBeNull();
  });

  it("retains the candidate trace and surfaces error_detail for a failed experiment with no comparison", async () => {
    const { getExperimentDetail } = await import("@/lib/feedback-experiment-queries");
    const detail = getExperimentDetail(2)!;
    expect(detail.status).toBe("failed");
    expect(detail.error_detail).toBe(
      "The trace or domain comparison could not be constructed or serialized."
    );
    expect(detail.candidate.trace_id).toBe("trace-candidate-2");
    expect(detail.candidate.trace_url).toBe("/traces/trace-candidate-2");
    expect(detail.comparison).toBeNull();
  });

  it("throws a DataIntegrityError when the baseline trace has no verifiable AGENT_RUN root", async () => {
    const { getExperimentDetail, DataIntegrityError } = await import(
      "@/lib/feedback-experiment-queries"
    );
    // Experiment 5's baseline is phase_run 12 -> trace-baseline-bad, which has no spans at all.
    let caught: unknown;
    try {
      getExperimentDetail(5);
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(DataIntegrityError);
  });

  it("rejects disagreement between first-class and embedded comparison identities", async () => {
    const { getExperimentDetail, DataIntegrityError } = await import(
      "@/lib/feedback-experiment-queries"
    );
    const db = new Database(dbPath);
    db.prepare("UPDATE trace_comparisons SET trace_a_id = 'wrong-trace' WHERE id = 1").run();
    try {
      expect(() => getExperimentDetail(1)).toThrow(DataIntegrityError);
    } finally {
      db.prepare("UPDATE trace_comparisons SET trace_a_id = 'trace-baseline-1' WHERE id = 1").run();
      db.close();
    }
  });
});

describe("strict decoder and cross-record invariants", () => {
  // Every fixture here reuses phase_run_id 13 (phase='reply_draft', trace
  // trace-baseline-3), already seeded above for the graded-exp fixture, so
  // candidate_config.phase always agrees with it — isolating each test to
  // exactly the one invariant it names.
  const gradedCandidateConfig = candidateConfigJson({ phase: "reply_draft", grader_attached: true });

  function baseGradedEvidence(overrides: Record<string, unknown> = {}): string {
    return baselineEvidenceJson({
      baseline_model: "baseline-model",
      baseline_prompt_sha256: "baseline-hash",
      reply_revision_id: 42,
      correction_sha256: "correction-hash",
      project_key: "gateway",
      dossier_summary_id: "gateway-dossier",
      dossier_revision: "a".repeat(40),
      grader_version: "normalized_edit_distance/v1",
      assembler_version: "assemble_draft_text/v1",
      ...overrides,
    });
  }

  function validScoreEvidence(overrides: Record<string, unknown> = {}): string {
    return JSON.stringify({
      grader_version: "normalized_edit_distance/v1",
      assembler_version: "assemble_draft_text/v1",
      correction_sha256: "correction-hash",
      reply_revision_id: 42,
      baseline_distance: 0.4,
      candidate_distance: 0.1,
      delta: 0.1 - 0.4,
      grader_attached: true,
      ...overrides,
    });
  }

  function addCandidateSpan(tracesDb: InstanceType<typeof Database>, traceId: string): void {
    tracesDb
      .prepare(
        `INSERT INTO spans (id, trace_id, parent_id, kind, name, input, output, started_at, ended_at, duration_ms, metadata)
         VALUES (?, ?, NULL, 'agent_run', 'scout_reply_draft', NULL, NULL, ?, ?, 100, '{}')`
      )
      .run(`span-root-${traceId}`, traceId, stamp(0), stamp(0));
    tracesDb
      .prepare(
        `INSERT INTO spans (id, trace_id, parent_id, kind, name, started_at, ended_at, duration_ms, usage_cost)
         VALUES (?, ?, ?, 'llm_call', 'model-call', ?, ?, 10, 0.01)`
      )
      .run(`span-llm-${traceId}`, traceId, `span-root-${traceId}`, stamp(0), stamp(0));
  }

  beforeAll(() => {
    const db = new Database(dbPath);
    const tracesDb = new Database(tracesDbPath);

    // id=8: grader_attached=true parent, but the attempt's baseline_evidence
    // is base-only (no correction-oracle pin) -> DataIntegrityError before
    // any trace resolution is attempted.
    db.prepare(
      `INSERT INTO experiment_runs (id, name, status, candidate_config, created_at, completed_at)
       VALUES (8, 'base-only-under-graded', 'queued', ?, ?, NULL)`
    ).run(gradedCandidateConfig, stamp(7));
    db.prepare(
      `INSERT INTO evaluation_experiments
         (id, experiment_run_id, phase_run_id, attempt_number, status, baseline_evidence, created_at)
       VALUES (8, 8, 13, 1, 'queued', ?, ?)`
    ).run(baselineEvidenceJson(), stamp(7));

    // id=9: fully-pinned graded attempt whose persisted comparison has a
    // NULL score_evidence -> DataIntegrityError.
    db.prepare(
      `INSERT INTO experiment_runs (id, name, status, candidate_config, created_at, completed_at)
       VALUES (9, 'null-score-evidence', 'complete', ?, ?, ?)`
    ).run(gradedCandidateConfig, stamp(8), stamp(8));
    db.prepare(
      `INSERT INTO evaluation_experiments
         (id, experiment_run_id, phase_run_id, attempt_number, status, baseline_evidence,
          candidate_trace_id, candidate_llm_call_count, candidate_cost, created_at, completed_at)
       VALUES (9, 9, 13, 1, 'complete', ?, 'trace-candidate-9', 1, 0.01, ?, ?)`
    ).run(baseGradedEvidence(), stamp(8), stamp(8));
    addCandidateSpan(tracesDb, "trace-candidate-9");
    db.prepare(
      `INSERT INTO trace_comparisons
         (id, experiment_id, trace_a_id, trace_b_id, jig_revision, trace_diff, domain_diff, score_evidence, created_at)
       VALUES (9, 9, 'trace-baseline-3', 'trace-candidate-9', 'jig-rev-abc', ?, ?, NULL, ?)`
    ).run(
      traceDiffJson({ trace_a_id: "trace-baseline-3", trace_b_id: "trace-candidate-9" }),
      domainDiffJson(),
      stamp(8)
    );

    // ids 10-14: fully-pinned graded, complete comparisons each with exactly
    // one score_evidence field mismatched against baseline_evidence, or a
    // wrong delta.
    const mismatches: Array<[number, Record<string, unknown>]> = [
      [10, { reply_revision_id: 999 }],
      [11, { correction_sha256: "tampered-hash" }],
      [12, { grader_version: "normalized_edit_distance/v2" }],
      [13, { assembler_version: "assemble_draft_text/v2" }],
      [14, { delta: 999 }],
    ];
    for (const [id, scoreOverrides] of mismatches) {
      const traceId = `trace-candidate-${id}`;
      db.prepare(
        `INSERT INTO experiment_runs (id, name, status, candidate_config, created_at, completed_at)
         VALUES (?, ?, 'complete', ?, ?, ?)`
      ).run(id, `mismatch-${id}`, gradedCandidateConfig, stamp(id), stamp(id));
      db.prepare(
        `INSERT INTO evaluation_experiments
           (id, experiment_run_id, phase_run_id, attempt_number, status, baseline_evidence,
            candidate_trace_id, candidate_llm_call_count, candidate_cost, created_at, completed_at)
         VALUES (?, ?, 13, 1, 'complete', ?, ?, 1, 0.01, ?, ?)`
      ).run(id, id, baseGradedEvidence(), traceId, stamp(id), stamp(id));
      addCandidateSpan(tracesDb, traceId);
      db.prepare(
        `INSERT INTO trace_comparisons
           (id, experiment_id, trace_a_id, trace_b_id, jig_revision, trace_diff, domain_diff, score_evidence, created_at)
         VALUES (?, ?, 'trace-baseline-3', ?, 'jig-rev-abc', ?, ?, ?, ?)`
      ).run(
        id,
        id,
        traceId,
        traceDiffJson({ trace_a_id: "trace-baseline-3", trace_b_id: traceId }),
        domainDiffJson(),
        validScoreEvidence(scoreOverrides),
        stamp(id)
      );
    }

    // id=15: candidate_config carries an unexpected extra field.
    db.prepare(
      `INSERT INTO experiment_runs (id, name, status, candidate_config, created_at, completed_at)
       VALUES (15, 'unexpected-candidate-config-field', 'queued', ?, ?, NULL)`
    ).run(
      JSON.stringify({ ...JSON.parse(candidateConfigJson()), unexpected_field: "surprise" }),
      stamp(15)
    );
    db.prepare(
      `INSERT INTO evaluation_experiments
         (id, experiment_run_id, phase_run_id, attempt_number, status, baseline_evidence, created_at)
       VALUES (15, 15, 10, 1, 'queued', ?, ?)`
    ).run(baselineEvidenceJson(), stamp(15));

    // id=16: baseline_evidence carries an unexpected extra field.
    db.prepare(
      `INSERT INTO experiment_runs (id, name, status, candidate_config, created_at, completed_at)
       VALUES (16, 'unexpected-baseline-evidence-field', 'queued', ?, ?, NULL)`
    ).run(candidateConfigJson(), stamp(16));
    db.prepare(
      `INSERT INTO evaluation_experiments
         (id, experiment_run_id, phase_run_id, attempt_number, status, baseline_evidence, created_at)
       VALUES (16, 16, 10, 1, 'queued', ?, ?)`
    ).run(
      JSON.stringify({ ...JSON.parse(baselineEvidenceJson()), unexpected_field: "surprise" }),
      stamp(16)
    );

    db.close();
    tracesDb.close();
  });

  it("rejects base-only baseline_evidence under a grader_attached parent", async () => {
    const { getExperimentDetail, DataIntegrityError } = await import(
      "@/lib/feedback-experiment-queries"
    );
    expect(() => getExperimentDetail(8)).toThrow(DataIntegrityError);
    expect(() => getExperimentDetail(8)).toThrow(/correction-oracle field/);
  });

  it("rejects null score_evidence on a completed graded attempt's comparison", async () => {
    const { getExperimentDetail, DataIntegrityError } = await import(
      "@/lib/feedback-experiment-queries"
    );
    expect(() => getExperimentDetail(9)).toThrow(DataIntegrityError);
    expect(() => getExperimentDetail(9)).toThrow(/score_evidence/);
  });

  it.each([
    [10, "reply_revision_id", /reply_revision_id/],
    [11, "correction_sha256", /correction_sha256/],
    [12, "grader_version", /grader_version/],
    [13, "assembler_version", /assembler_version/],
    [14, "delta", /delta/],
  ] as const)(
    "rejects score_evidence.%s mismatched against baseline_evidence",
    async (id, _field, messagePattern) => {
      const { getExperimentDetail, DataIntegrityError } = await import(
        "@/lib/feedback-experiment-queries"
      );
      expect(() => getExperimentDetail(id)).toThrow(DataIntegrityError);
      expect(() => getExperimentDetail(id)).toThrow(messagePattern);
    }
  );

  it("rejects an unexpected field on candidate_config", async () => {
    const { getExperimentDetail, DataIntegrityError } = await import(
      "@/lib/feedback-experiment-queries"
    );
    expect(() => getExperimentDetail(15)).toThrow(DataIntegrityError);
  });

  it("rejects an unexpected field on baseline_evidence", async () => {
    const { getExperimentDetail, DataIntegrityError } = await import(
      "@/lib/feedback-experiment-queries"
    );
    expect(() => getExperimentDetail(16)).toThrow(DataIntegrityError);
  });
});
