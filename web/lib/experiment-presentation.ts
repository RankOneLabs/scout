// Pure presentation helpers for the attempt detail view — no React, no
// fetch. Kept separate from ExperimentDetail so directional formatting,
// the decision-metric rollup, and the failure allowlist can be unit
// tested without rendering.

import type {
  ExperimentComparison,
  ExperimentDetailResponse,
} from "@/types/feedback-experiments";

// --- Directional deltas -----------------------------------------------
//
// Correction distance, cost, and latency are all "lower is better":
// delta = candidate - baseline, so a negative delta means the candidate
// improved. A delta of exactly 0 is a measured "no change" and must
// render distinctly from an unmeasured (null) delta — neither one is
// allowed to collapse into the other.

export type DeltaStatus = "better" | "worse" | "same" | "unavailable";

export interface FormattedDelta {
  status: DeltaStatus;
  text: string;
}

export function directionalDeltaStatus(delta: number | null): DeltaStatus {
  if (delta === null) return "unavailable";
  if (delta < 0) return "better";
  if (delta > 0) return "worse";
  return "same";
}

export function formatDirectionalDelta(
  delta: number | null,
  formatMagnitude: (magnitude: number) => string
): FormattedDelta {
  const status = directionalDeltaStatus(delta);
  if (delta === null) {
    return { status, text: "Unavailable" };
  }
  if (delta === 0) {
    return { status, text: `${formatMagnitude(0)} (no change)` };
  }
  const sign = delta > 0 ? "+" : "-";
  return { status, text: `${sign}${formatMagnitude(Math.abs(delta))}` };
}

// --- Decision metrics ---------------------------------------------------

export type DecisionMetricKey = "correction_distance" | "cost" | "latency";

export interface DecisionMetric {
  key: DecisionMetricKey;
  label: string;
  helpText: string;
  status: DeltaStatus;
  text: string;
}

export function computeDecisionMetrics(comparison: ExperimentComparison | null): DecisionMetric[] {
  const correctionDelta = comparison?.score_evidence?.delta ?? null;
  const costDelta =
    comparison !== null && comparison.cost_delta_available ? comparison.trace_diff.cost_delta : null;
  const latencyDelta =
    comparison !== null && comparison.latency_delta_available
      ? comparison.trace_diff.latency_ms_delta
      : null;

  const correction = formatDirectionalDelta(correctionDelta, (n) => n.toFixed(3));
  const cost = formatDirectionalDelta(costDelta, (n) => `$${n.toFixed(4)}`);
  const latency = formatDirectionalDelta(latencyDelta, (n) => `${Math.round(n)} ms`);

  return [
    {
      key: "correction_distance",
      label: "Correction distance",
      helpText: "Candidate distance minus baseline distance from the pinned correction. Lower is better.",
      status: correction.status,
      text: correction.text,
    },
    {
      key: "cost",
      label: "Cost delta",
      helpText: "Candidate cost minus baseline cost. Lower is better.",
      status: cost.status,
      text: cost.text,
    },
    {
      key: "latency",
      label: "Latency delta",
      helpText: "Candidate latency minus baseline latency. Lower is better.",
      status: latency.status,
      text: latency.text,
    },
  ];
}

// --- Failure allowlist ---------------------------------------------------
//
// Mirrors evaluation_experiments.py's _STAGE_MESSAGES — the closed,
// operator-safe set of messages the backend ever writes to
// evaluation_experiments.error_detail on the fail_experiment() path. The
// column itself only enforces a length cap (see migrations.py), not
// membership in that set, so an older row, a future call site, or
// corrupted data could hold arbitrary text. The browser must never
// interpolate unrecognized persisted error text into rendered output —
// an unknown value renders a fixed, generic message instead.

const KNOWN_FAILURE_MESSAGES: readonly string[] = [
  "Candidate phase execution failed before producing a valid result.",
  "The candidate trace could not be verified as a stored AGENT_RUN root.",
  "The candidate's reply-correction score could not be verified or was not produced.",
  "The trace or domain comparison could not be constructed or serialized.",
];

const UNKNOWN_FAILURE_MESSAGE =
  "This attempt failed for a reason outside the known failure categories.";

export interface MappedFailure {
  known: boolean;
  message: string;
}

export function mapFailureDetail(errorDetail: string | null): MappedFailure {
  if (errorDetail !== null && KNOWN_FAILURE_MESSAGES.includes(errorDetail)) {
    return { known: true, message: errorDetail };
  }
  return { known: false, message: UNKNOWN_FAILURE_MESSAGE };
}

// --- Attempt verdict ---------------------------------------------------

export type AttemptVerdictKind =
  | "pending"
  | "failed"
  | "no_comparison"
  | "not_graded"
  | "candidate_recommended"
  | "candidate_not_recommended"
  | "no_measurable_difference";

export interface AttemptVerdict {
  kind: AttemptVerdictKind;
  label: string;
  description: string;
}

export function computeAttemptVerdict(
  detail: Pick<ExperimentDetailResponse, "status" | "error_detail" | "comparison">
): AttemptVerdict {
  if (detail.status === "queued" || detail.status === "running") {
    return {
      kind: "pending",
      label: detail.status === "queued" ? "Queued" : "Running",
      description: "This attempt has not finished yet.",
    };
  }

  if (detail.status === "failed") {
    const failure = mapFailureDetail(detail.error_detail);
    return { kind: "failed", label: "Failed", description: failure.message };
  }

  // status === "complete"
  if (detail.comparison === null) {
    return {
      kind: "no_comparison",
      label: "No comparison",
      description: "This attempt completed but no comparison was persisted.",
    };
  }

  const scoreEvidence = detail.comparison.score_evidence;
  if (scoreEvidence === null) {
    return {
      kind: "not_graded",
      label: "Not graded",
      description:
        "No reply-correction grader was attached to this comparison — review cost and latency instead.",
    };
  }

  const status = directionalDeltaStatus(scoreEvidence.delta);
  if (status === "better") {
    return {
      kind: "candidate_recommended",
      label: "Candidate recommended",
      description: "The candidate is closer to the pinned correction than the baseline was.",
    };
  }
  if (status === "worse") {
    return {
      kind: "candidate_not_recommended",
      label: "Candidate not recommended",
      description: "The candidate is farther from the pinned correction than the baseline was.",
    };
  }
  return {
    kind: "no_measurable_difference",
    label: "No measurable difference",
    description: "The candidate's distance to the pinned correction matched the baseline exactly.",
  };
}
