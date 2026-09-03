"use client";

import Link from "next/link";
import { useGradeDetail } from "@/hooks/use-grade-detail";
import { GradeOverrideControl } from "@/components/molecules/GradeOverrideControl";
import { DetailField } from "@/components/atoms/DetailField";
import { parseUtc } from "@/lib/transforms";

function fmt(iso: string | null): string {
  if (!iso) return "—";
  const d = parseUtc(iso);
  return Number.isNaN(d.valueOf()) ? iso : d.toLocaleString();
}

export function GradeDetail({ gradeId }: { gradeId: number }) {
  const {
    detail,
    loading,
    error,
    notFound,
    loadMoreRevisions,
    loadMoreSnapshotUseHistory,
    loadMorePhaseRuns,
    refetch,
  } = useGradeDetail(gradeId);

  if (loading && !detail) return <p className="text-sm text-gray-600 dark:text-gray-500">Loading...</p>;
  if (notFound) return <p className="text-sm text-gray-600 dark:text-gray-500">Grade #{gradeId} not found.</p>;
  if (error) return <p className="text-sm text-red-600 dark:text-red-400">Failed to load grade: {error}</p>;
  if (!detail) return null;

  const { grade, identity, segment, evidence, eligibility, snapshot_use, override } = detail;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Grade #{grade.id}</h1>
        <p className="mt-1 text-xs text-gray-600 dark:text-gray-500">
          Post #{identity.post_id}
          {identity.evaluation_id !== null && <> · Evaluation #{identity.evaluation_id}</>}
          {identity.scan_id !== null && (
            <>
              {" · "}
              <Link href={`/scans/${identity.scan_id}`} className="text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300">
                Scan #{identity.scan_id}
              </Link>
            </>
          )}
        </p>
      </div>

      <section className="rounded-lg border border-gray-200 bg-gray-50 p-4 dark:border-gray-800 dark:bg-gray-900">
        <h2 className="mb-2 text-sm font-medium uppercase text-gray-600 dark:text-gray-500">Canonical envelope</h2>
        <dl className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
          <DetailField label="Relevance judgment" value={grade.relevance_judgment} />
          <DetailField label="Action judgment" value={grade.action_judgment ?? "—"} />
          <DetailField
            label="Schema / regrade"
            value={
              <>
                v{grade.schema_version}
                {grade.needs_regrade === 1 && <span className="ml-1 text-red-600 dark:text-red-400">needs regrade</span>}
              </>
            }
          />
          <DetailField label="Dimensions" value={grade.dimensions?.join(", ") || "—"} />
          <DetailField
            label="Project / Platform"
            value={`${segment.project_key ?? "—"} / ${segment.platform ?? "—"}`}
          />
          <DetailField
            label="Posture / Terminal status"
            value={`${segment.posture ?? "—"} / ${segment.terminal_status ?? "—"}`}
          />
          <DetailField label="Graded at" value={fmt(grade.graded_at)} />
          <DetailField label="Source" value={grade.source} />
        </dl>
        {grade.failure_note && (
          <details className="mt-3">
            <summary className="cursor-pointer text-xs text-gray-600 dark:text-gray-400">Failure note</summary>
            <p className="mt-1 whitespace-pre-wrap text-xs text-gray-700 dark:text-gray-300">{grade.failure_note}</p>
          </details>
        )}
      </section>

      <section className="rounded-lg border border-gray-200 bg-gray-50 p-4 dark:border-gray-800 dark:bg-gray-900">
        <h2 className="mb-2 text-sm font-medium uppercase text-gray-600 dark:text-gray-500">Evidence</h2>
        <details className="mb-2">
          <summary className="cursor-pointer text-xs text-gray-600 dark:text-gray-400">
            Post #{evidence.post.id} ({evidence.post.platform})
          </summary>
          <p className="mt-1 whitespace-pre-wrap text-xs text-gray-700 dark:text-gray-300">
            {evidence.post.content ?? <span className="italic text-gray-500 dark:text-gray-400">no content</span>}
          </p>
        </details>
        {evidence.evaluation === null ? (
          <p className="text-xs italic text-gray-600 dark:text-gray-500">No linked evaluation.</p>
        ) : (
          <details className="mb-2">
            <summary className="cursor-pointer text-xs text-gray-600 dark:text-gray-400">
              Evaluation #{evidence.evaluation.id} — relevant: {String(evidence.evaluation.relevant)}
            </summary>
            <p className="mt-1 whitespace-pre-wrap text-xs text-gray-700 dark:text-gray-300">
              {evidence.evaluation.reason ?? <span className="italic text-gray-500 dark:text-gray-400">no reason recorded</span>}
            </p>
          </details>
        )}
        {evidence.draft === null ? (
          <p className="text-xs italic text-gray-600 dark:text-gray-500">No linked draft.</p>
        ) : (
          <details className="mb-2">
            <summary className="cursor-pointer text-xs text-gray-600 dark:text-gray-400">Draft #{evidence.draft.id}</summary>
            <p className="mt-1 whitespace-pre-wrap text-xs text-gray-700 dark:text-gray-300">
              {evidence.draft.comment_text ?? <span className="italic text-gray-500 dark:text-gray-400">no text</span>}
            </p>
          </details>
        )}
        {evidence.critique === null ? (
          <p className="text-xs italic text-gray-600 dark:text-gray-500">No linked critique.</p>
        ) : (
          <details>
            <summary className="cursor-pointer text-xs text-gray-600 dark:text-gray-400">
              Critique — {evidence.critique.verdict}
            </summary>
            <p className="mt-1 whitespace-pre-wrap text-xs text-gray-700 dark:text-gray-300">
              {evidence.critique.feedback ?? <span className="italic text-gray-500 dark:text-gray-400">no feedback text</span>}
            </p>
          </details>
        )}
      </section>

      <section className="rounded-lg border border-gray-200 bg-gray-50 p-4 dark:border-gray-800 dark:bg-gray-900">
        <h2 className="mb-2 text-sm font-medium uppercase text-gray-600 dark:text-gray-500">
          Present-time eligibility &amp; override
        </h2>
        <p className="mb-2 text-xs">
          <span
            className={`rounded-full border px-2 py-0.5 text-[10px] ${
              eligibility.reason === "eligible"
                ? "border-green-300 bg-green-100 text-green-700 dark:border-green-500/30 dark:bg-green-500/20 dark:text-green-400"
                : "border-amber-300 bg-amber-100 text-amber-700 dark:border-amber-500/30 dark:bg-amber-500/20 dark:text-amber-400"
            }`}
          >
            {eligibility.reason ?? "unknown"}
          </span>
          {eligibility.policy_version && (
            <span className="ml-2 text-gray-600 dark:text-gray-500">
              {eligibility.policy_version} · lookback {eligibility.resolved_lookback_days}d · cap{" "}
              {eligibility.resolved_max_grades}
            </span>
          )}
        </p>
        <GradeOverrideControl evaluationId={identity.evaluation_id} override={override} onChanged={refetch} />
      </section>

      <section className="rounded-lg border border-gray-200 bg-gray-50 p-4 dark:border-gray-800 dark:bg-gray-900">
        <h2 className="mb-2 text-sm font-medium uppercase text-gray-600 dark:text-gray-500">Model use &amp; trace</h2>
        <dl className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
          <DetailField label="Snapshot use count" value={snapshot_use.count} />
          <DetailField
            label="First / Last used"
            value={`${fmt(snapshot_use.first_at)} / ${fmt(snapshot_use.last_at)}`}
          />
          <DetailField
            label="Actively used (latest scan)"
            value={String(snapshot_use.active_use)}
            valueClassName={snapshot_use.active_use ? "text-green-600 dark:text-green-400" : "text-gray-800 dark:text-gray-200"}
          />
          <DetailField
            label="Trace available"
            value={
              <>
                {String(detail.trace_available)}
                {detail.trace_unavailable_reason && (
                  <span className="ml-1 text-gray-600 dark:text-gray-500">({detail.trace_unavailable_reason})</span>
                )}
              </>
            }
          />
        </dl>

        {detail.snapshot_use_history.data.length > 0 && (
          <details className="mt-3">
            <summary className="cursor-pointer text-xs text-gray-600 dark:text-gray-400">Snapshot use history</summary>
            <table className="mt-2 w-full text-xs">
              <thead>
                <tr className="text-left text-gray-600 dark:text-gray-500">
                  <th className="pb-1 font-normal">Snapshot</th>
                  <th className="pb-1 font-normal">Phase</th>
                  <th className="pb-1 font-normal">Role</th>
                  <th className="pb-1 font-normal">Reason</th>
                  <th className="pb-1 font-normal">Mode</th>
                  <th className="pb-1 font-normal">When</th>
                </tr>
              </thead>
              <tbody className="text-gray-700 dark:text-gray-300">
                {detail.snapshot_use_history.data.map((entry) => (
                  <tr key={entry.id} className="border-t border-gray-200 dark:border-gray-800">
                    <td className="py-1">
                      <Link
                        href={`/feedback?snapshotId=${entry.snapshot_id}`}
                        className="text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300"
                      >
                        #{entry.snapshot_id}
                      </Link>
                    </td>
                    <td className="py-1">{entry.phase}</td>
                    <td className="py-1">{entry.role}</td>
                    <td className="py-1">{entry.reason ?? "—"}</td>
                    <td className="py-1">{entry.mode}</td>
                    <td className="py-1">{fmt(entry.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {detail.snapshot_use_history.has_more && (
              <button
                type="button"
                onClick={loadMoreSnapshotUseHistory}
                className="mt-2 rounded-md border border-gray-300 bg-white px-2 py-1 text-[11px] text-gray-700 hover:border-gray-400 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:border-gray-600"
              >
                Load more
              </button>
            )}
          </details>
        )}

        {detail.phase_runs.data.length > 0 && (
          <details className="mt-3" open>
            <summary className="cursor-pointer text-xs text-gray-600 dark:text-gray-400">
              Phase runs ({detail.phase_runs.data.length}
              {detail.phase_runs.has_more ? "+" : ""})
            </summary>
            <table className="mt-2 w-full text-xs">
              <thead>
                <tr className="text-left text-gray-600 dark:text-gray-500">
                  <th className="pb-1 font-normal">Phase run</th>
                  <th className="pb-1 font-normal">Phase</th>
                  <th className="pb-1 font-normal">Trace</th>
                  <th className="pb-1 font-normal">When</th>
                </tr>
              </thead>
              <tbody className="text-gray-700 dark:text-gray-300">
                {detail.phase_runs.data.map((run) => (
                  <tr key={run.id} className="border-t border-gray-200 dark:border-gray-800">
                    <td className="py-1">
                      <Link
                        href={`/feedback/phase-runs/${run.id}`}
                        className="text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300"
                      >
                        #{run.id}
                      </Link>
                    </td>
                    <td className="py-1">{run.phase}</td>
                    <td className="py-1">
                      <Link
                        href={`/traces/${run.trace_id}`}
                        className="font-mono text-[11px] text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300"
                      >
                        {run.trace_id.slice(0, 8)}…
                      </Link>
                    </td>
                    <td className="py-1">{fmt(run.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {detail.phase_runs.has_more && (
              <button
                type="button"
                onClick={loadMorePhaseRuns}
                className="mt-2 rounded-md border border-gray-300 bg-white px-2 py-1 text-[11px] text-gray-700 hover:border-gray-400 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:border-gray-600"
              >
                Load more
              </button>
            )}
          </details>
        )}
      </section>

      <section className="rounded-lg border border-gray-200 bg-gray-50 p-4 dark:border-gray-800 dark:bg-gray-900">
        <h2 className="mb-2 text-sm font-medium uppercase text-gray-600 dark:text-gray-500">Revision history</h2>
        <ul className="space-y-2">
          {detail.revision_history.data.map((rev) => (
            <li key={rev.id} className="rounded-md border border-gray-200 bg-white p-2 text-xs dark:border-gray-800 dark:bg-gray-950">
              <div className="mb-1 flex items-center gap-2">
                <span className="font-medium text-gray-800 dark:text-gray-200">rev {rev.revision}</span>
                <span className="text-gray-600 dark:text-gray-500">schema v{rev.schema_version}</span>
                <span className="rounded-full border border-gray-300 bg-gray-50 px-2 py-0.5 text-[10px] uppercase text-gray-600 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-400">
                  {rev.source}
                </span>
                <span className="text-gray-600 dark:text-gray-500">{fmt(rev.recorded_at)}</span>
              </div>
              <details>
                <summary className="cursor-pointer text-gray-600 dark:text-gray-400">Payload</summary>
                <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-words text-[11px] text-gray-600 dark:text-gray-400">
                  {rev.payload}
                </pre>
              </details>
            </li>
          ))}
        </ul>
        {detail.revision_history.has_more && (
          <button
            type="button"
            onClick={loadMoreRevisions}
            className="mt-2 rounded-md border border-gray-300 bg-white px-2 py-1 text-[11px] text-gray-700 hover:border-gray-400 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:border-gray-600"
          >
            Load more
          </button>
        )}
      </section>
    </div>
  );
}
