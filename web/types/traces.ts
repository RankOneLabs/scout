// Trace Detail's second backlink relationship set — zero or more replay
// comparisons that used this trace as either side. Independent of the
// existing single phase-run backlink (types/feedback-grades.ts's
// PhaseRunDetail): a baseline trace may be replayed into many experiments,
// so this is a list, never a single nullable link.

// Re-exported rather than redeclared so the status allowlist has one
// authoritative source: types/feedback-experiments.ts.
import type { ExperimentStatus } from "@/types/feedback-experiments";
export type { ExperimentStatus };

export interface TraceComparisonBacklink {
  experiment_id: number;
  comparison_id: number;
  // Which side of the persisted trace_diff this trace was: 'baseline' when
  // it matches trace_diff.trace_a_id, 'candidate' when it matches
  // trace_diff.trace_b_id — the durable stored keys, never inferred from
  // timestamps, prompts, models, or post identity.
  role: "baseline" | "candidate";
  experiment_name: string;
  experiment_status: ExperimentStatus;
  experiment_url: string;
}
