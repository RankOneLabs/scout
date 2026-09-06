"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import type { ReviewDisposition } from "@/types/review-queues";

export function ReviewProvenance({ evaluationId }: { evaluationId: number }) {
  const [actions, setActions] = useState<ReviewDisposition[]>([]);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    fetch(`/api/grading/review-actions?evaluationId=${evaluationId}`, { cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) throw new Error("Review provenance unavailable");
        return response.json() as Promise<ReviewDisposition[]>;
      }).then((data) => { if (active) setActions(data); })
      .catch((reason: unknown) => { if (active) setError(reason instanceof Error ? reason.message : "Cannot load provenance"); });
    return () => { active = false; };
  }, [evaluationId]);
  if (error) return <p className="text-xs text-gray-500">{error}</p>;
  if (actions.length === 0) return null;
  return <section className="space-y-2 border-t border-gray-300 pt-3 dark:border-gray-700">
    <h2 className="text-sm font-medium">Grading-assistance provenance</h2>
    <ul className="space-y-2 text-xs">{actions.map((action) => <li key={action.action_id}>
      <Link className="text-blue-600 dark:text-blue-400" href={`/feedback/review-queues/${action.queue_digest}?evaluationId=${evaluationId}`}>
        Queue {action.queue_digest.slice(0, 12)} · {action.action.kind} · revision {action.grade_revision_id ?? "none"}
      </Link>
      <p className="text-gray-500">Action {action.action_id} · {action.timing.elapsed_ms === null ? "duration unavailable" : `${action.timing.elapsed_ms} ms`}</p>
    </li>)}</ul>
  </section>;
}
