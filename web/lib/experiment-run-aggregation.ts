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
  repeat_index: number;
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

// Pairs arrive per chain (case, repeat); a case's repeats are averaged first
// so every metric is over cases, matching the Python report's pairing.
function metric(byCase: Map<number, Array<[number, number]>>) {
  const pairs: Array<[number, number]> = [...byCase.values()].map((chains) => [
    chains.reduce((sum, pair) => sum + pair[0], 0) / chains.length,
    chains.reduce((sum, pair) => sum + pair[1], 0) / chains.length,
  ]);
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
  // A retry chain is one (case, repeat); attempt_number is unique per case
  // across its repeats, so a chain's root past repeat 1 need not be #1.
  const byChain = new Map<string, RunAttemptEvidence[]>();
  const caseIds = new Set<number>();
  const numbersByCase = new Map<number, Set<number>>();
  const ids = new Set<number>();
  for (const attempt of input.attempts) {
    if (attempt.experiment_run_id !== input.id) fail("attempt belongs to another parent");
    if (ids.has(attempt.row.id)) fail("duplicate attempt id");
    ids.add(attempt.row.id);
    if (attempt.row.experiment_run_id !== input.id || attempt.row.baseline_phase_run_id !== attempt.phase_run_id) {
      fail("attempt projection identity mismatch");
    }
    if (!Number.isInteger(attempt.repeat_index) || attempt.repeat_index < 1) fail("invalid repeat index");
    const numbers = numbersByCase.get(attempt.phase_run_id) ?? new Set<number>();
    if (numbers.has(attempt.attempt_number)) fail("duplicate attempt number");
    numbers.add(attempt.attempt_number);
    numbersByCase.set(attempt.phase_run_id, numbers);
    const key = `${attempt.phase_run_id}:${attempt.repeat_index}`;
    const group = byChain.get(key) ?? [];
    group.push(attempt);
    byChain.set(key, group);
    caseIds.add(attempt.phase_run_id);
  }

  const latest: RunAttemptEvidence[] = [];
  for (const attempts of byChain.values()) {
    attempts.sort((a, b) => a.attempt_number - b.attempt_number);
    for (let index = 0; index < attempts.length; index += 1) {
      const attempt = attempts[index];
      if (index === 0) {
        if (attempt.supersedes_experiment_id !== null || (attempt.repeat_index === 1 && attempt.attempt_number !== 1)) {
          fail("invalid lineage root");
        }
      } else if (attempt.supersedes_experiment_id !== attempts[index - 1].row.id) {
        fail("broken supersedes lineage");
      }
    }
    latest.push(attempts[attempts.length - 1]);
  }

  let skipped = 0;
  let planned = caseIds.size;
  if (config.version !== 2) {
    const plannedIds = new Set(config.phase_run_ids);
    if (plannedIds.size !== config.phase_run_ids.length) fail("duplicate planned phase run");
    const skippedIds = new Set<number>();
    for (const pair of config.skipped_pairs) {
      if (skippedIds.has(pair.phase_run_id)) fail("duplicate skipped pair");
      if (!plannedIds.has(pair.phase_run_id)) fail("skipped pair outside plan");
      if (caseIds.has(pair.phase_run_id)) fail("attempted and skipped populations overlap");
      skippedIds.add(pair.phase_run_id);
    }
    for (const phaseRunId of caseIds) if (!plannedIds.has(phaseRunId)) fail("attempt outside plan");
    skipped = skippedIds.size;
    planned = config.phase_run_ids.length;
    // Attempts are inserted one case at a time as execution reaches them,
    // so a run still in flight (or killed mid-way, which leaves it in
    // 'running' forever) legitimately covers only part of its plan. Only a
    // run that reached a terminal status must account for every case.
    const inFlight = input.status === "running" || input.status === "queued";
    if (!inFlight && planned !== caseIds.size + skipped) fail("plan population is incomplete");
    // Execution runs a case's repeats back to back, so a run cut short can
    // leave a case with only its earlier repeats; the summary must not
    // then read as a complete run with fewer observations than authorized.
    const repeats = config.repeats ?? 1;
    const repeatIndexesByCase = new Map<number, Set<number>>();
    for (const attempts of byChain.values()) {
      const { phase_run_id: caseId, repeat_index: repeatIndex } = attempts[0];
      if (repeatIndex > repeats) fail("repeat index outside plan");
      const indexes = repeatIndexesByCase.get(caseId) ?? new Set<number>();
      indexes.add(repeatIndex);
      repeatIndexesByCase.set(caseId, indexes);
    }
    // Chains are keyed by (case, repeat), so a case with `repeats` distinct
    // indexes, none above the plan, holds exactly 1..repeats.
    if (!inFlight) {
      for (const indexes of repeatIndexesByCase.values()) if (indexes.size !== repeats) fail("repeat population is incomplete");
    }
  }

  const statusCounts: Record<ExperimentStatus, number> = { queued: 0, running: 0, complete: 0, failed: 0 };
  for (const attempt of latest) statusCounts[attempt.row.status] += 1;

  const correctionPairs = new Map<number, Array<[number, number]>>();
  const relevancePairs = new Map<number, Array<[number, number]>>();
  const costPairs = new Map<number, Array<[number, number]>>();
  const latencyPairs = new Map<number, Array<[number, number]>>();
  const add = (into: Map<number, Array<[number, number]>>, caseId: number, pair: [number, number]) => {
    const group = into.get(caseId) ?? [];
    group.push(pair);
    into.set(caseId, group);
  };
  for (const attempt of latest) {
    if (attempt.row.status !== "complete") continue;
    const caseId = attempt.phase_run_id;
    if (attempt.score_evidence) {
      const score = attempt.score_evidence;
      if ("format" in score) add(relevancePairs, caseId, [Number(score.baseline_correct), Number(score.candidate_correct)]);
      else add(correctionPairs, caseId, [score.baseline_distance, score.candidate_distance]);
    }
    if (attempt.cost_delta_available && attempt.baseline_cost !== undefined && attempt.candidate_verified_cost !== undefined) {
      add(costPairs, caseId, [attempt.baseline_cost, attempt.candidate_verified_cost]);
    }
    if (attempt.latency_delta_available && attempt.baseline_latency_ms !== undefined && attempt.candidate_latency_ms !== undefined) {
      add(latencyPairs, caseId, [attempt.baseline_latency_ms, attempt.candidate_latency_ms]);
    }
  }
  const correction = metric(correctionPairs);
  const relevance = metric(relevancePairs);
  const qualityDelta = config.phase === "relevance" ? (relevance.mean_delta === null ? null : -relevance.mean_delta) : correction.mean_delta;
  const cost = metric(costPairs);
  const latency = metric(latencyPairs);
  let verdict: ExperimentRunSummary["verdict"];
  if (statusCounts.running + statusCounts.queued > 0) verdict = "pending";
  else if (statusCounts.failed > 0 && statusCounts.complete === 0) verdict = "failed";
  else if (qualityDelta === null) verdict = "not_graded";
  else if (qualityDelta < 0) verdict = "candidate_recommended";
  else if (qualityDelta > 0) verdict = "candidate_not_recommended";
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
    attempted_case_count: caseIds.size,
    skipped_case_count: skipped,
    current_case_count: caseIds.size,
    current_chain_count: latest.length,
    repeat_count: config.version === 2 ? 1 : config.repeats ?? 1,
    retry_count: input.attempts.length - latest.length,
    status_counts: statusCounts,
    total_llm_call_count: input.attempts.reduce((sum, attempt) => sum + (attempt.row.candidate_llm_call_count ?? 0), 0),
    total_cost: costs.some((cost) => cost === null) ? null : costs.reduce<number>((sum, cost) => sum + (cost ?? 0), 0),
    verdict,
    correction_distance: correction,
    relevance_accuracy: relevance,
    cost,
    latency,
  };
}
