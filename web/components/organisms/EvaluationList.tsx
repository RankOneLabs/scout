"use client";

import type { Grade } from "@/types/schema";
import { EvaluationCard } from "@/components/organisms/EvaluationCard";
import { RelevanceActionBadge } from "@/components/molecules/RelevanceActionBadge";
import {
  selectHoldProvenance,
  selectRelevanceActionBadge,
  selectReleaseOrigin,
} from "@/lib/review-selectors";
import {
  selectSurfaceStatusCounts,
  type ReviewEvaluationWithProvenance,
  type SurfaceStatusCounts,
} from "@/lib/transforms";

export function EvaluationList({ evaluations, onGradeUpdate }: {
  evaluations: ReviewEvaluationWithProvenance[];
  onGradeUpdate?: (evaluationId: number, grade: Grade) => void;
}) {
  if (!evaluations.length) return <p className="py-8 text-center text-sm text-gray-600 dark:text-gray-500">No evaluations found.</p>;
  return <div className="space-y-3">
    <SurfaceStatusSummary counts={selectSurfaceStatusCounts(evaluations)} />
    {evaluations.map((evaluation) => <div key={evaluation.id} className="space-y-1">
      <RelevanceProvenanceStrip evaluation={evaluation} />
      <EvaluationCard evaluation={evaluation} onGradeUpdate={onGradeUpdate} />
    </div>)}
  </div>;
}

/** Every status this population reached, with nothing folded into anything.
 *
 * `held` is its own entry beside `surfaced` and `drafting_failed`, and
 * carries the one sentence that stops it being read as either. */
function SurfaceStatusSummary({ counts }: { counts: SurfaceStatusCounts }) {
  const entries = Object.entries(counts.by_status).sort(([a], [b]) => a.localeCompare(b));
  return <section className="rounded-lg border border-gray-200 bg-white p-3 text-xs dark:border-gray-800 dark:bg-gray-950">
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
      <span className="font-medium text-gray-700 dark:text-gray-300">{counts.total} evaluations</span>
      {entries.map(([status, count]) => <span key={status} className="text-gray-600 dark:text-gray-400">
        {status.replace(/_/g, "-")}: <span className="font-mono">{count}</span>
      </span>)}
      <span className="text-gray-600 dark:text-gray-400">
        postable: <span className="font-mono">{counts.actionable}</span>
      </span>
    </div>
    {counts.held > 0 && <p className="mt-2 text-gray-500">
      {counts.held} held back for blind grading: decided and recorded, then stopped before
      drafting. Neither surfaced nor drafting-failed, and not actionable for posting.
    </p>}
  </section>;
}

/** The decided facts, above the card that renders what actually happened. */
function RelevanceProvenanceStrip({ evaluation }: { evaluation: ReviewEvaluationWithProvenance }) {
  const provenance = evaluation.relevance_provenance;
  const badge = selectRelevanceActionBadge(provenance);
  const hold = selectHoldProvenance(provenance);
  const origin = selectReleaseOrigin(provenance);
  if (badge === null && hold.kind === "not_held" && origin === null) return null;
  return <div className="flex flex-wrap items-center gap-2 px-1 text-[11px] text-gray-600 dark:text-gray-400">
    <RelevanceActionBadge badge={badge} />
    {hold.kind === "awaiting_release" && <span>held {hold.status}, awaiting release</span>}
    {hold.kind === "release_failed" && <span>hold release failed ({hold.attempts} attempts)</span>}
    {hold.kind === "released" && <span>
      hold released as {hold.action} on{" "}
      {hold.authority === "label" ? `blind label ${hold.label ?? "unrecorded"}` : "the recorded action"}
    </span>}
    {origin !== null && <span title={origin.title}>
      {origin.label} from evaluation {origin.source_evaluation_id}
    </span>}
  </div>;
}
