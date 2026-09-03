import type {
  CandidateConfig,
  ExperimentListRow,
  ExperimentRunStatus,
  ExperimentRunSummary,
  ExperimentStatus,
  ScoreEvidence,
  TraceDiff,
} from "@/types/feedback-experiments";

export interface RunAttemptEvidence {
  row: ExperimentListRow;
  experiment_run_id: number;
  phase_run_id: number;
  attempt_number: number;
  supersedes_experiment_id: number | null;
  score_evidence: ScoreEvidence | null;
  trace_diff: TraceDiff | null;
  cost_delta_available: boolean;
  latency_delta_available: boolean;
  baseline_cost?: number;
  candidate_verified_cost?: number;
  baseline_latency_ms?: number;
  candidate_latency_ms?: number;
}

export interface AggregateExperimentRunInput {
  id: number;
  name: string;
  status: ExperimentRunStatus;
  candidate_config: CandidateConfig;
  created_at: string;
  completed_at: string | null;
  attempts: RunAttemptEvidence[];
}

function fail(message: string): never {
  throw new Error(`experiment run integrity: ${message}`);
}

function metric(pairs: Array<[number, number]>) {
  if (pairs.length === 0) {
    return { available: false, case_count: 0, baseline_mean: null, candidate_mean: null, mean_delta: null };
  }
  const baseline = pairs.reduce((sum, pair) => sum + pair[0], 0) / pairs.length;
  const candidate = pairs.reduce((sum, pair) => sum + pair[1], 0) / pairs.length;
  return {
    available: true,
    case_count: pairs.length,
    baseline_mean: baseline,
    candidate_mean: candidate,
    mean_delta: candidate - baseline,
  };
}

export function aggregateExperimentRun(input: AggregateExperimentRunInput): ExperimentRunSummary {
  const { candidate_config: config } = input;
  const byPhaseRun = new Map<number, RunAttemptEvidence[]>();
  const ids = new Set<number>();
  for (const attempt of input.attempts) {
    if (attempt.experiment_run_id !== input.id) fail("attempt belongs to another parent");
    if (ids.has(attempt.row.id)) fail("duplicate attempt id");
    ids.add(attempt.row.id);
    if (attempt.row.experiment_run_id !== input.id || attempt.row.baseline_phase_run_id !== attempt.phase_run_id) {
      fail("attempt projection identity mismatch");
    }
    const group = byPhaseRun.get(attempt.phase_run_id) ?? [];
    group.push(attempt);
    byPhaseRun.set(attempt.phase_run_id, group);
  }

  const latest: RunAttemptEvidence[] = [];
  for (const attempts of byPhaseRun.values()) {
    attempts.sort((a, b) => a.attempt_number - b.attempt_number);
    const seenNumbers = new Set<number>();
    for (let index = 0; index < attempts.length; index += 1) {
      const attempt = attempts[index];
      if (seenNumbers.has(attempt.attempt_number)) fail("duplicate attempt number");
      seenNumbers.add(attempt.attempt_number);
      if (index === 0) {
        if (attempt.attempt_number !== 1 || attempt.supersedes_experiment_id !== null) fail("invalid lineage root");
      } else if (
        attempt.attempt_number !== attempts[index - 1].attempt_number + 1 ||
        attempt.supersedes_experiment_id !== attempts[index - 1].row.id
      ) {
        fail("broken supersedes lineage");
      }
    }
    latest.push(attempts[attempts.length - 1]);
  }

  let skipped = 0;
  let planned = byPhaseRun.size;
  if (config.version === 4) {
    const plannedIds = new Set(config.phase_run_ids);
    if (plannedIds.size !== config.phase_run_ids.length) fail("duplicate planned phase run");
    const skippedIds = new Set<number>();
    for (const pair of config.skipped_pairs) {
      if (skippedIds.has(pair.phase_run_id)) fail("duplicate skipped pair");
      if (!plannedIds.has(pair.phase_run_id)) fail("skipped pair outside plan");
      if (byPhaseRun.has(pair.phase_run_id)) fail("attempted and skipped populations overlap");
      skippedIds.add(pair.phase_run_id);
    }
    for (const phaseRunId of byPhaseRun.keys()) if (!plannedIds.has(phaseRunId)) fail("attempt outside plan");
    skipped = skippedIds.size;
    planned = config.phase_run_ids.length;
    if (planned !== byPhaseRun.size + skipped) fail("plan population is incomplete");
  }

  const statusCounts: Record<ExperimentStatus, number> = { queued: 0, running: 0, complete: 0, failed: 0 };
  for (const attempt of latest) statusCounts[attempt.row.status] += 1;

  const correctionPairs: Array<[number, number]> = [];
  const costPairs: Array<[number, number]> = [];
  const latencyPairs: Array<[number, number]> = [];
  for (const attempt of latest) {
    if (attempt.row.status !== "complete") continue;
    if (attempt.score_evidence) correctionPairs.push([attempt.score_evidence.baseline_distance, attempt.score_evidence.candidate_distance]);
    if (attempt.cost_delta_available && attempt.baseline_cost !== undefined && attempt.candidate_verified_cost !== undefined) {
      costPairs.push([attempt.baseline_cost, attempt.candidate_verified_cost]);
    }
    if (attempt.latency_delta_available && attempt.baseline_latency_ms !== undefined && attempt.candidate_latency_ms !== undefined) {
      latencyPairs.push([attempt.baseline_latency_ms, attempt.candidate_latency_ms]);
    }
  }
  const correction = metric(correctionPairs);
  const cost = metric(costPairs);
  const latency = metric(latencyPairs);
  let verdict: ExperimentRunSummary["verdict"];
  if (statusCounts.running + statusCounts.queued > 0) verdict = "pending";
  else if (statusCounts.failed > 0 && statusCounts.complete === 0) verdict = "failed";
  else if (!correction.available) verdict = "not_graded";
  else if (correction.mean_delta! < 0) verdict = "candidate_recommended";
  else if (correction.mean_delta! > 0) verdict = "candidate_not_recommended";
  else verdict = "no_measurable_difference";

  const costs = input.attempts.map((attempt) => attempt.row.candidate_cost);
  return {
    id: input.id,
    name: input.name,
    status: input.status,
    phase: config.phase,
    grader_attached: config.grader_attached,
    created_at: input.created_at,
    completed_at: input.completed_at,
    planned_case_count: planned,
    attempted_case_count: byPhaseRun.size,
    skipped_case_count: skipped,
    current_case_count: latest.length,
    retry_count: input.attempts.length - latest.length,
    status_counts: statusCounts,
    total_llm_call_count: input.attempts.reduce((sum, attempt) => sum + (attempt.row.candidate_llm_call_count ?? 0), 0),
    total_cost: costs.some((cost) => cost === null) ? null : costs.reduce<number>((sum, cost) => sum + (cost ?? 0), 0),
    verdict,
    correction_distance: correction,
    cost,
    latency,
  };
}
