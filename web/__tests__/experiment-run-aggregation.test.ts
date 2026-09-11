import { describe, expect, it } from "vitest";
import { aggregateExperimentRun, type RunAttemptEvidence } from "@/lib/experiment-run-aggregation";
import type { ExperimentListRow, ScoreEvidence } from "@/types/feedback-experiments";

function attempt(id: number, number: number, supersedes: number | null, delta: number, cost: number, repeat = 1): RunAttemptEvidence {
  const row: ExperimentListRow = {
    id, experiment_run_id: 7, name: "run", status: "complete", run_status: "complete",
    phase: "reply_draft", attempt_number: number, repeat_index: repeat, supersedes_experiment_id: supersedes,
    grader_attached: true, baseline_phase_run_id: 11, candidate_trace_id: `candidate-${id}`,
    baseline_model: "base", candidate_model: "candidate", created_at: `2026-01-0${number}T00:00:00Z`,
    completed_at: `2026-01-0${number}T00:01:00Z`, candidate_llm_call_count: number,
    candidate_cost: cost, comparison_complete: true,
  };
  const score_evidence: ScoreEvidence = { grader_version: "g", assembler_version: "a", correction_sha256: "c", reply_revision_id: 2, baseline_distance: 4, candidate_distance: 4 + delta, delta, grader_attached: true };
  return { row, experiment_run_id: 7, phase_run_id: 11, attempt_number: number, repeat_index: repeat, supersedes_experiment_id: supersedes, score_evidence, trace_diff: null, cost_delta_available: false, latency_delta_available: false };
}

describe("aggregateExperimentRun", () => {
  it("uses the latest attempt for outcomes while charging every retry", () => {
    const result = aggregateExperimentRun({ id: 7, name: "run", status: "complete", created_at: "2026-01-01T00:00:00Z", completed_at: "2026-01-03T00:00:00Z", candidate_config: { version: 2, phase: "reply_draft", model: "candidate", system_prompt: "p", system_prompt_sha256: "h", grader_attached: true }, attempts: [attempt(1, 1, null, 2, 0.1), attempt(2, 2, 1, -1, 0.2)] });
    expect(result.retry_count).toBe(1);
    expect(result.total_cost).toBeCloseTo(0.3);
    expect(result.total_llm_call_count).toBe(3);
    expect(result.correction_distance.mean_delta).toBe(-1);
    expect(result.verdict).toBe("candidate_recommended");
  });

  it("treats each repeat as its own chain and counts the case once", () => {
    const config = { version: 2 as const, phase: "reply_draft" as const, model: "candidate", system_prompt: "p", system_prompt_sha256: "h", grader_attached: true };
    // Repeat 2's root is attempt #2 with no predecessor; its retry is #3.
    const result = aggregateExperimentRun({ id: 7, name: "run", status: "complete", created_at: "2026-01-01T00:00:00Z", completed_at: null, candidate_config: config, attempts: [attempt(1, 1, null, 2, 0.1), attempt(2, 2, null, 4, 0.1, 2), attempt(3, 3, 2, -1, 0.1, 2)] });
    // Every count is in cases; chains and repeats are reported beside them.
    expect(result.attempted_case_count).toBe(1);
    expect(result.current_case_count).toBe(1);
    expect(result.current_chain_count).toBe(2);
    expect(result.repeat_count).toBe(1); // a v2 single-replay config has no repeats field
    expect(result.retry_count).toBe(1);
    expect(result.correction_distance.case_count).toBe(1);
    // Repeats are averaged per case before the metric: candidate 6 and 3 -> 4.5 vs baseline 4.
    expect(result.correction_distance.mean_delta).toBeCloseTo(0.5);
    // A repeat-2 root that claims a predecessor is still broken.
    expect(() => aggregateExperimentRun({ id: 7, name: "run", status: "complete", created_at: "2026-01-01T00:00:00Z", completed_at: null, candidate_config: config, attempts: [attempt(1, 1, null, 2, 0.1), attempt(2, 2, 1, 4, 0.1, 2)] })).toThrow(/lineage root/);
  });

  it("reports planned repeats at case level and rejects a case missing a repeat", () => {
    const config = {
      version: 4 as const, phase: "reply_draft" as const, variant_name: "default", model_override: "candidate",
      system_prompt_override: null, system_prompt_override_sha256: null, repeats: 2, grader_attached: true,
      sweep: null, plan_sha256: "p", phase_run_ids: [11], dropped_duplicate_phase_run_ids: [], skipped_pairs: [],
    };
    const base = { id: 7, name: "run", status: "complete" as const, created_at: "2026-01-01T00:00:00Z", completed_at: null, candidate_config: config };
    const result = aggregateExperimentRun({ ...base, attempts: [attempt(1, 1, null, 2, 0.1), attempt(2, 2, null, 4, 0.1, 2)] });
    // Both counts are in cases; the chains and repeats sit beside them.
    expect(result.planned_case_count).toBe(1);
    expect(result.current_case_count).toBe(1);
    expect(result.current_chain_count).toBe(2);
    expect(result.repeat_count).toBe(2);
    expect(result.correction_distance.case_count).toBe(1);
    // A run cut short after repeat 1 must not read as a complete run.
    expect(() => aggregateExperimentRun({ ...base, attempts: [attempt(1, 1, null, 2, 0.1)] })).toThrow(/repeat population/);
    // Two chains are not enough: the indexes must be exactly 1..repeats.
    expect(() => aggregateExperimentRun({ ...base, attempts: [attempt(1, 1, null, 2, 0.1), attempt(2, 2, null, 4, 0.1, 3)] })).toThrow(/repeat index outside plan/);
  });

  it("rejects broken lineage", () => {
    expect(() => aggregateExperimentRun({ id: 7, name: "run", status: "complete", created_at: "2026-01-01T00:00:00Z", completed_at: null, candidate_config: { version: 2, phase: "reply_draft", model: "candidate", system_prompt: "p", system_prompt_sha256: "h", grader_attached: true }, attempts: [attempt(2, 2, null, -1, 0.2)] })).toThrow(/lineage root/);
  });
});
