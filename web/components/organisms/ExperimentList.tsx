"use client";

import { useState } from "react";
import Link from "next/link";
import { useExperimentList } from "@/hooks/use-experiment-list";
import { EXPERIMENT_PHASES, EXPERIMENT_RUN_STATUSES } from "@/types/feedback-experiments";
import type { ExperimentRunSummary } from "@/types/feedback-experiments";

function RunCard({ run }: { run: ExperimentRunSummary }) {
  return <article className="rounded-lg border border-gray-200 p-4 dark:border-gray-800">
    <div className="flex flex-wrap items-start justify-between gap-2">
      <div><Link href={`/feedback/experiment-runs/${run.id}`} className="font-semibold text-blue-600 dark:text-blue-400">{run.name}</Link><p className="text-xs text-gray-500">{run.phase} · {run.verdict.replaceAll("_", " ")}</p></div>
      <span className="rounded-full border px-2 py-0.5 text-xs">{run.status}</span>
    </div>
    <dl className="mt-3 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
      <div><dt className="text-xs text-gray-500">Cases</dt><dd>{run.current_case_count}/{run.planned_case_count}{run.skipped_case_count ? ` · ${run.skipped_case_count} skipped` : ""}</dd></div>
      <div><dt className="text-xs text-gray-500">Retries</dt><dd>{run.retry_count}</dd></div>
      <div><dt className="text-xs text-gray-500">Spend</dt><dd>{run.total_cost === null ? "Unavailable" : `$${run.total_cost.toFixed(4)}`}</dd></div>
      <div><dt className="text-xs text-gray-500">Correction Δ</dt><dd>{run.correction_distance.mean_delta === null ? "Unavailable" : run.correction_distance.mean_delta.toFixed(3)}</dd></div>
    </dl>
  </article>;
}

export function ExperimentList() {
  const [status, setStatus] = useState("");
  const [phase, setPhase] = useState("");
  const { data, hasMore, loading, error, loadMore } = useExperimentList({ status, phase });
  return <div className="space-y-4">
    <header><h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Experiments</h1><p className="mt-1 text-xs text-gray-600 dark:text-gray-500">Read-only parent runs with retry-aware outcomes and spend. Open a case for complete forensic evidence.</p></header>
    <div className="flex gap-3">
      <label className="text-xs text-gray-500">Status<select value={status} onChange={(event) => setStatus(event.target.value)} className="ml-2 rounded border bg-transparent px-2 py-1"><option value="">Any</option>{EXPERIMENT_RUN_STATUSES.map((value) => <option key={value}>{value}</option>)}</select></label>
      <label className="text-xs text-gray-500">Phase<select value={phase} onChange={(event) => setPhase(event.target.value)} className="ml-2 rounded border bg-transparent px-2 py-1"><option value="">Any</option>{EXPERIMENT_PHASES.map((value) => <option key={value}>{value}</option>)}</select></label>
    </div>
    {error && <p className="text-sm text-red-600">Failed to load experiments: {error}</p>}
    <div className="space-y-3">{data.map((run) => <RunCard key={run.id} run={run} />)}{loading && <p className="text-sm text-gray-500">Loading…</p>}{!loading && data.length === 0 && <p className="text-sm text-gray-500">No experiment runs recorded yet.</p>}</div>
    {hasMore && <button type="button" onClick={loadMore} className="w-full rounded border py-2 text-sm">Load more</button>}
  </div>;
}
