import { describe, expect, it } from "vitest";
import { aggregateExperimentRun, type RunAttemptEvidence } from "@/lib/experiment-run-aggregation";
import type { ExperimentListRow, ScoreEvidence } from "@/types/feedback-experiments";

function attempt(id: number, number: number, supersedes: number | null, delta: number, cost: number): RunAttemptEvidence {
  const row: ExperimentListRow = {
    id, experiment_run_id: 7, name: "run", status: "complete", run_status: "complete",
    phase: "reply_draft", attempt_number: number, supersedes_experiment_id: supersedes,
    grader_attached: true, baseline_phase_run_id: 11, candidate_trace_id: `candidate-${id}`,
    baseline_model: "base", candidate_model: "candidate", created_at: `2026-01-0${number}T00:00:00Z`,
    completed_at: `2026-01-0${number}T00:01:00Z`, candidate_llm_call_count: number,
    candidate_cost: cost, comparison_complete: true,
  };
  const score_evidence: ScoreEvidence = { grader_version: "g", assembler_version: "a", correction_sha256: "c", reply_revision_id: 2, baseline_distance: 4, candidate_distance: 4 + delta, delta, grader_attached: true };
  return { row, experiment_run_id: 7, phase_run_id: 11, attempt_number: number, supersedes_experiment_id: supersedes, score_evidence, trace_diff: null, cost_delta_available: false, latency_delta_available: false };
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

  it("rejects broken lineage", () => {
    expect(() => aggregateExperimentRun({ id: 7, name: "run", status: "complete", created_at: "2026-01-01T00:00:00Z", completed_at: null, candidate_config: { version: 2, phase: "reply_draft", model: "candidate", system_prompt: "p", system_prompt_sha256: "h", grader_attached: true }, attempts: [attempt(2, 2, null, -1, 0.2)] })).toThrow(/lineage root/);
  });
});
