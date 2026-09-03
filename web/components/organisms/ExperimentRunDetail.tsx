"use client";

import Link from "next/link";
import { useExperimentRunDetail } from "@/hooks/use-experiment-run-detail";

function number(value: number | null, digits = 3) { return value === null ? "Unavailable" : value.toFixed(digits); }

export function ExperimentRunDetail({ experimentRunId }: { experimentRunId: number }) {
  const { detail, loading, error } = useExperimentRunDetail(experimentRunId);
  if (loading) return <p className="text-sm text-gray-500">Loading experiment run…</p>;
  if (error || !detail) return <p className="text-sm text-red-600">Failed to load experiment run{error ? `: ${error}` : "."}</p>;
  const { run } = detail;
  return <main className="space-y-5">
    <header>
      <Link href="/feedback/experiments" className="text-sm text-blue-600">← Experiments</Link>
      <h1 className="mt-2 text-2xl font-bold text-gray-900 dark:text-gray-100">{run.name}</h1>
      <p className="text-sm text-gray-600 dark:text-gray-400">{run.verdict.replaceAll("_", " ")} · {run.status}</p>
    </header>
    <section aria-label="Run summary" className="grid gap-3 sm:grid-cols-3">
      <div className="rounded-lg border p-3"><span className="text-xs text-gray-500">Cases</span><p>{run.current_case_count}/{run.planned_case_count} current · {run.retry_count} retries</p></div>
      <div className="rounded-lg border p-3"><span className="text-xs text-gray-500">Total spend</span><p>{run.total_cost === null ? "Unavailable" : `$${run.total_cost.toFixed(4)}`} · {run.total_llm_call_count} calls</p></div>
      <div className="rounded-lg border p-3"><span className="text-xs text-gray-500">Mean correction delta</span><p>{number(run.correction_distance.mean_delta)}</p></div>
    </section>
    <details className="rounded-lg border p-3"><summary className="disclosure-summary font-medium">Configuration identity</summary><dl className="mt-2 text-sm"><dt>Version</dt><dd>{detail.configuration.version}</dd><dt>Identity</dt><dd className="break-all font-mono">{detail.configuration.identity}</dd>{detail.configuration.plan_sha256 && <><dt>Plan SHA-256</dt><dd className="break-all font-mono">{detail.configuration.plan_sha256}</dd></>}</dl></details>
    <section aria-labelledby="run-cases"><h2 id="run-cases" className="text-lg font-semibold">Cases</h2>
      <div className="mt-2 space-y-2">{detail.cases.map((item) => <article key={item.phase_run_id} className="rounded-lg border p-3 text-sm">
        <div className="flex justify-between"><Link href={`/feedback/experiments/${item.current.id}`} className="text-blue-600">Attempt #{item.current.id}</Link><span>{item.current.status}</span></div>
        {item.history.length > 0 && <details className="mt-2"><summary className="disclosure-summary">Attempt history ({item.history.length})</summary><ul>{item.history.map((attempt) => <li key={attempt.id}><Link href={`/feedback/experiments/${attempt.id}`} className="text-blue-600">Attempt {attempt.attempt_number}</Link> — {attempt.status}</li>)}</ul></details>}
      </article>)}</div>
    </section>
  </main>;
}
