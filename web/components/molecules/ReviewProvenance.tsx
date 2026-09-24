"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import type { EvaluationReviewProvenance } from "@/lib/review-queue-queries";
import type { RelevanceProvenance } from "@/types/schema";
import {
  selectHoldProvenance,
  selectRelevanceActionBadge,
  selectReleaseOrigin,
} from "@/lib/review-selectors";
import { RelevanceActionBadge } from "@/components/molecules/RelevanceActionBadge";
import { formatTimestamp } from "@/lib/transforms";

const EMPTY: EvaluationReviewProvenance = { actions: [], relevance: null };

/** What decided this evaluation, and what has been recorded about reviewing it.
 *
 * Renders three facts apart from one another: the source classifier's
 * recorded action, the holdout lifecycle state and release authority, and
 * the grading-assistance review trail. None of them is the evaluation's
 * surface_status, which the surrounding view renders. */
export function ReviewProvenance({ evaluationId }: { evaluationId: number }) {
  const [provenance, setProvenance] = useState<EvaluationReviewProvenance>(EMPTY);
  const [error, setError] = useState<string | null>(null);
  const [loadedEvaluationId, setLoadedEvaluationId] = useState<number | null>(null);
  useEffect(() => {
    let active = true;
    fetch(`/api/grading/review-actions?evaluationId=${evaluationId}`, { cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) throw new Error("Review provenance unavailable");
        return response.json() as Promise<EvaluationReviewProvenance>;
      }).then((data) => { if (active) { setLoadedEvaluationId(evaluationId); setError(null); setProvenance(data); } })
      .catch((reason: unknown) => { if (active) { setLoadedEvaluationId(evaluationId); setError(reason instanceof Error ? reason.message : "Cannot load provenance"); setProvenance(EMPTY); } });
    return () => { active = false; };
  }, [evaluationId]);
  const loaded = loadedEvaluationId === evaluationId;
  const visibleError = loaded ? error : null;
  const visible = loaded ? provenance : EMPTY;
  if (visibleError) return <p className="text-xs text-gray-500">{visibleError}</p>;
  const relevance = visible.relevance;
  const hasRelevance = relevance !== null
    && (relevance.decision !== null || relevance.holdout !== null || relevance.released_from !== null);
  if (!hasRelevance && visible.actions.length === 0) return null;
  return <div className="space-y-3">
    {hasRelevance && relevance !== null && <RelevanceProvenanceSection provenance={relevance} />}
    {visible.actions.length > 0 && <section className="space-y-2 border-t border-gray-300 pt-3 dark:border-gray-700">
      <h2 className="text-sm font-medium">Grading-assistance provenance</h2>
      <ul className="space-y-2 text-xs">{visible.actions.map((action) => <li key={action.action_id}>
        <Link className="text-blue-600 dark:text-blue-400" href={`/feedback/review-queues/${action.queue_digest}?evaluationId=${evaluationId}`}>
          Queue {action.queue_digest.slice(0, 12)} · {action.action.kind} · revision {action.grade_revision_id ?? "none"}
        </Link>
        <p className="text-gray-500">Action {action.action_id} · {action.timing.elapsed_ms === null ? "duration unavailable" : `${action.timing.elapsed_ms} ms`}</p>
      </li>)}</ul>
    </section>}
  </div>;
}

function RelevanceProvenanceSection({ provenance }: { provenance: RelevanceProvenance }) {
  const hold = selectHoldProvenance(provenance);
  const origin = selectReleaseOrigin(provenance);
  return <section className="space-y-2 border-t border-gray-300 pt-3 dark:border-gray-700">
    <h2 className="text-sm font-medium">Relevance provenance</h2>
    <dl className="space-y-2 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <dt className="text-gray-500">Source decision</dt>
        <dd><RelevanceActionBadge badge={selectRelevanceActionBadge(provenance)} /></dd>
      </div>
      {hold.kind !== "not_held" && <div className="flex flex-wrap items-center gap-2">
        <dt className="text-gray-500">Hold</dt>
        <dd className="text-gray-700 dark:text-gray-300">
          {hold.kind === "awaiting_release" && `${hold.status} since ${formatTimestamp(hold.held_at)}`}
          {hold.kind === "release_failed" && `release failed after ${hold.attempts} attempt${hold.attempts === 1 ? "" : "s"}: ${hold.last_error ?? "no detail recorded"}`}
          {hold.kind === "released" && `released as ${hold.action} on ${hold.authority === "label" ? `blind label ${hold.label ?? "unrecorded"}` : "the recorded classifier action"}${hold.target_evaluation_id === null ? " (no draft)" : ` → evaluation ${hold.target_evaluation_id}`}`}
        </dd>
      </div>}
      {origin !== null && <div className="flex flex-wrap items-center gap-2">
        <dt className="text-gray-500">Release authority</dt>
        <dd className="text-gray-700 dark:text-gray-300" title={origin.title}>
          {origin.label} · from evaluation {origin.source_evaluation_id}
        </dd>
      </div>}
    </dl>
    <p className="text-xs text-gray-500">
      The classifier&rsquo;s action and the release authority are what was decided.
      What actually happened is this evaluation&rsquo;s status.
    </p>
  </section>;
}
