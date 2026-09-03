"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { ClipboardCheck, Database, History, ShieldAlert } from "lucide-react";
import { StatCard } from "@/components/molecules/StatCard";
import { useFeedbackSummary } from "@/hooks/use-feedback-summary";
import type { CorpusCard, SegmentSummary } from "@/types/feedback-summary";

function pct(numerator: number, denominator: number): string {
  if (denominator === 0) return "—";
  return `${((numerator / denominator) * 100).toFixed(1)}%`;
}

function fraction(card: CorpusCard): string {
  return `${card.count} / ${card.denominator}`;
}

function toDateInputValue(iso: string): string {
  return iso.slice(0, 10);
}

function SegmentTable({ title, summary }: { title: string; summary: SegmentSummary }) {
  return (
    <div className="rounded-lg border border-gray-200 bg-gray-50 dark:border-gray-800 dark:bg-gray-900 p-4">
      <h3 className="mb-2 text-sm font-medium text-gray-700 dark:text-gray-300">{title}</h3>
      {summary.entries.length === 0 ? (
        <p className="text-xs italic text-gray-600 dark:text-gray-500">No data in this window.</p>
      ) : (
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-gray-600 dark:text-gray-500">
              <th className="pb-1 font-normal">Label</th>
              <th className="pb-1 font-normal">Count</th>
              <th className="pb-1 font-normal">Correct</th>
              <th className="pb-1 font-normal">FP</th>
              <th className="pb-1 font-normal">FN</th>
            </tr>
          </thead>
          <tbody className="text-gray-700 dark:text-gray-300">
            {summary.entries.map((entry) => (
              <tr key={entry.label} className="border-t border-gray-200 dark:border-gray-800">
                <td className="py-1">{entry.label}</td>
                <td className="py-1">{entry.count}</td>
                <td className="py-1">{entry.correct}</td>
                <td className="py-1">{entry.false_positive}</td>
                <td className="py-1">{entry.false_negative}</td>
              </tr>
            ))}
            {summary.other && (
              <tr className="border-t border-gray-200 italic text-gray-600 dark:border-gray-800 dark:text-gray-500">
                <td className="py-1">other</td>
                <td className="py-1">{summary.other.count}</td>
                <td className="py-1">{summary.other.correct}</td>
                <td className="py-1">{summary.other.false_positive}</td>
                <td className="py-1">{summary.other.false_negative}</td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  );
}

export function FeedbackOverview() {
  const [range, setRange] = useState<{ from?: string; to?: string }>({});
  const { data, loading, error } = useFeedbackSummary(range);

  const fromValue = useMemo(
    () => (range.from ? toDateInputValue(range.from) : data ? toDateInputValue(data.from) : ""),
    [range.from, data]
  );
  const toValue = useMemo(
    () => (range.to ? toDateInputValue(range.to) : data ? toDateInputValue(data.to) : ""),
    [range.to, data]
  );

  if (loading && !data) {
    return <p className="text-sm text-gray-600 dark:text-gray-500">Loading...</p>;
  }
  if (error) {
    return <p className="text-sm text-red-700 dark:text-red-400">Failed to load summary: {error}</p>;
  }
  if (!data) return null;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Feedback Overview</h1>
          <p className="mt-1 text-xs text-gray-600 dark:text-gray-500">
            As of {new Date(data.as_of).toLocaleString()} — window {new Date(data.from).toLocaleDateString()}
            {" – "}
            {new Date(data.to).toLocaleDateString()} ({data.timezone})
          </p>
        </div>
        <div className="flex items-end gap-2">
          <label className="flex flex-col text-xs text-gray-600 dark:text-gray-500">
            From
            <input
              type="date"
              value={fromValue}
              onChange={(e) =>
                setRange((r) => ({
                  ...r,
                  from: e.target.value ? `${e.target.value}T00:00:00.000Z` : undefined,
                }))
              }
              className="mt-1 rounded-md border border-gray-300 bg-white px-2 py-1 text-sm text-gray-700 focus:border-blue-500 focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300"
            />
          </label>
          <label className="flex flex-col text-xs text-gray-600 dark:text-gray-500">
            To
            <input
              type="date"
              value={toValue}
              onChange={(e) =>
                setRange((r) => ({
                  ...r,
                  to: e.target.value ? `${e.target.value}T23:59:59.999Z` : undefined,
                }))
              }
              className="mt-1 rounded-md border border-gray-300 bg-white px-2 py-1 text-sm text-gray-700 focus:border-blue-500 focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300"
            />
          </label>
          {(range.from || range.to) && (
            <button
              type="button"
              onClick={() => setRange({})}
              className="rounded-md border border-gray-300 bg-white px-2 py-1.5 text-xs text-gray-600 hover:border-gray-400 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-400 dark:hover:border-gray-600"
            >
              Reset (90d)
            </button>
          )}
        </div>
      </div>

      <Link
        href="/feedback/grades"
        className="inline-block text-sm text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300"
      >
        Open Grade Explorer →
      </Link>

      <section>
        <h2 className="mb-2 text-sm font-medium uppercase text-gray-600 dark:text-gray-500">Corpus (all-time)</h2>
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <StatCard label="Total grades" value={fraction(data.corpus.total)} icon={Database} />
          <StatCard
            label="Current schema"
            value={fraction(data.corpus.current)}
            icon={ClipboardCheck}
            color="text-green-600 dark:text-green-400"
          />
          <StatCard label="Legacy" value={fraction(data.corpus.legacy)} icon={History} color="text-amber-600 dark:text-amber-400" />
          <StatCard
            label="Needs regrade"
            value={fraction(data.corpus.needs_regrade)}
            icon={ShieldAlert}
            color="text-red-400"
          />
        </div>
        <p className="mt-1 text-[11px] text-gray-500 dark:text-gray-400">
          Categories can overlap (e.g. a legacy grade can also need a regrade) — every card is
          numerator over the full corpus.
        </p>
      </section>

      <section>
        <h2 className="mb-2 text-sm font-medium uppercase text-gray-600 dark:text-gray-500">Relevance (windowed)</h2>
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <StatCard label="Correct" value={data.relevance.correct} icon={ClipboardCheck} color="text-green-600 dark:text-green-400" />
          <StatCard label="False positive" value={data.relevance.false_positive} icon={ShieldAlert} color="text-red-400" />
          <StatCard label="False negative" value={data.relevance.false_negative} icon={ShieldAlert} color="text-red-400" />
          <StatCard
            label="Reviewed P / R"
            value={`${pct(data.relevance.correct_relevant, data.relevance.precision_denominator)} / ${pct(
              data.relevance.reviewed_model_negative === 0 ? 0 : data.relevance.correct_relevant,
              data.relevance.reviewed_model_negative === 0 ? 0 : data.relevance.recall_denominator
            )}`}
            icon={ClipboardCheck}
          />
        </div>
        <p className="mt-1 text-[11px] text-gray-500 dark:text-gray-400">
          Model-negative cases reviewed in this window: {data.relevance.reviewed_model_negative}.
          {data.relevance.reviewed_model_negative === 0
            ? " Recall is unavailable until at least one negative case is graded."
            : " Precision and recall describe reviewed cases, not an unbiased population sample."}
        </p>
      </section>

      <section>
        <h2 className="mb-2 text-sm font-medium uppercase text-gray-600 dark:text-gray-500">Response acceptance (windowed)</h2>
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <StatCard label="Accept" value={data.response_acceptance.accept} icon={ClipboardCheck} color="text-green-600 dark:text-green-400" />
          <StatCard label="Fail" value={data.response_acceptance.fail} icon={ShieldAlert} color="text-red-400" />
          <StatCard
            label="Accept rate"
            value={pct(data.response_acceptance.accept, data.response_acceptance.denominator)}
            icon={ClipboardCheck}
          />
          <StatCard label="Not applicable" value={data.response_acceptance.not_applicable} icon={Database} />
        </div>
      </section>

      <section>
        <h2 className="mb-2 text-sm font-medium uppercase text-gray-600 dark:text-gray-500">Grading coverage (windowed)</h2>
        {data.coverage.length === 0 ? (
          <p className="text-xs italic text-gray-600 dark:text-gray-500">No scans in this window.</p>
        ) : (
          <div className="overflow-x-auto rounded-lg border border-gray-200 bg-gray-50 dark:border-gray-800 dark:bg-gray-900">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-gray-600 dark:text-gray-500">
                  <th className="px-3 py-2 font-normal">Day</th>
                  <th className="px-3 py-2 font-normal">Scan</th>
                  <th className="px-3 py-2 font-normal">Linked / Total</th>
                  <th className="px-3 py-2 font-normal">Coverage</th>
                </tr>
              </thead>
              <tbody className="text-gray-700 dark:text-gray-300">
                {data.coverage.map((row) => (
                  <tr key={`${row.scan_id}-${row.day}`} className="border-t border-gray-200 dark:border-gray-800">
                    <td className="px-3 py-1.5">{row.day}</td>
                    <td className="px-3 py-1.5">
                      <Link href={`/scans/${row.scan_id}`} className="text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300">
                        #{row.scan_id}
                      </Link>
                    </td>
                    <td className="px-3 py-1.5">
                      {row.linked} / {row.total}
                    </td>
                    <td className="px-3 py-1.5">{pct(row.linked, row.total)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section>
        <h2 className="mb-2 text-sm font-medium uppercase text-gray-600 dark:text-gray-500">Failure dimension trends (windowed)</h2>
        {data.failure_dimension_trends.length === 0 ? (
          <p className="text-xs italic text-gray-600 dark:text-gray-500">No failed drafts in this window.</p>
        ) : (
          <div className="overflow-x-auto rounded-lg border border-gray-200 bg-gray-50 dark:border-gray-800 dark:bg-gray-900">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-gray-600 dark:text-gray-500">
                  <th className="px-3 py-2 font-normal">Day</th>
                  <th className="px-3 py-2 font-normal">Dimension</th>
                  <th className="px-3 py-2 font-normal">Count</th>
                  <th className="px-3 py-2 font-normal">Failed-draft denominator</th>
                </tr>
              </thead>
              <tbody className="text-gray-700 dark:text-gray-300">
                {data.failure_dimension_trends.map((row) => (
                  <tr key={`${row.day}-${row.dimension}`} className="border-t border-gray-200 dark:border-gray-800">
                    <td className="px-3 py-1.5">{row.day}</td>
                    <td className="px-3 py-1.5">{row.dimension}</td>
                    <td className="px-3 py-1.5">{row.count}</td>
                    <td className="px-3 py-1.5">{row.failed_draft_denominator}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section>
        <h2 className="mb-2 text-sm font-medium uppercase text-gray-600 dark:text-gray-500">Segments (windowed)</h2>
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <SegmentTable title="Project" summary={data.segments.project} />
          <SegmentTable title="Platform" summary={data.segments.platform} />
          <SegmentTable title="Posture" summary={data.segments.posture} />
          <SegmentTable title="Terminal status" summary={data.segments.terminal_status} />
        </div>
      </section>

      <section>
        <h2 className="mb-2 text-sm font-medium uppercase text-gray-600 dark:text-gray-500">Model-use eligibility (present-time)</h2>
        {data.policy === null ? (
          <p className="text-xs italic text-gray-600 dark:text-gray-500">
            No evaluation-feedback/v1 snapshot has been recorded yet — eligibility is not resolvable.
          </p>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
              <StatCard label="In lookback" value={data.eligibility.in_lookback_population} icon={Database} />
              <StatCard
                label="Eligible after cap"
                value={data.eligibility.eligible_after_cap}
                icon={ClipboardCheck}
                color="text-green-600 dark:text-green-400"
              />
              <StatCard
                label="Outside lookback"
                value={data.eligibility.outside_lookback_count}
                icon={History}
                color="text-amber-600 dark:text-amber-400"
              />
            </div>
            <p className="mt-1 text-[11px] text-gray-500 dark:text-gray-400">
              Resolved lookback {data.eligibility.resolved_lookback_days}d, cap{" "}
              {data.eligibility.resolved_max_grades}.
            </p>
            <div className="mt-2 overflow-x-auto rounded-lg border border-gray-200 bg-gray-50 dark:border-gray-800 dark:bg-gray-900">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-gray-600 dark:text-gray-500">
                    <th className="px-3 py-2 font-normal">Reason</th>
                    <th className="px-3 py-2 font-normal">Count</th>
                  </tr>
                </thead>
                <tbody className="text-gray-700 dark:text-gray-300">
                  {data.eligibility.by_reason.map((row) => (
                    <tr key={row.reason} className="border-t border-gray-200 dark:border-gray-800">
                      <td className="px-3 py-1.5">{row.reason}</td>
                      <td className="px-3 py-1.5">{row.count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </section>

      <section>
        <h2 className="mb-2 text-sm font-medium uppercase text-gray-600 dark:text-gray-500">Latest scan snapshot</h2>
        {data.latest_snapshot === null ? (
          <p className="text-xs italic text-gray-600 dark:text-gray-500">No feedback snapshot recorded yet.</p>
        ) : (
          <div className="rounded-lg border border-gray-200 bg-gray-50 p-4 text-sm text-gray-700 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-300">
            <div className="mb-2 flex items-center gap-2">
              <span
                className={`rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wide ${
                  data.latest_snapshot.mode === "active"
                    ? "border-green-300 bg-green-100 text-green-700 dark:border-green-500/30 dark:bg-green-500/20 dark:text-green-400"
                    : "border-gray-300 bg-white text-gray-600 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-400"
                }`}
              >
                {data.latest_snapshot.mode}
              </span>
              <Link
                href={`/feedback?snapshotId=${data.latest_snapshot.snapshot_id}`}
                className="text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300"
              >
                Snapshot #{data.latest_snapshot.snapshot_id}
              </Link>
              <Link href={`/scans/${data.latest_snapshot.scan_id}`} className="text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300">
                Scan #{data.latest_snapshot.scan_id}
              </Link>
            </div>
            <dl className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
              <div>
                <dt className="text-gray-600 dark:text-gray-500">Created</dt>
                <dd>{new Date(data.latest_snapshot.created_at).toLocaleString()}</dd>
              </div>
              <div>
                <dt className="text-gray-600 dark:text-gray-500">Population / Eligible / Excluded</dt>
                <dd>
                  {data.latest_snapshot.population_count} / {data.latest_snapshot.eligible_count} /{" "}
                  {data.latest_snapshot.excluded_count}
                </dd>
              </div>
              <div>
                <dt className="text-gray-600 dark:text-gray-500">Used by scan (actually injected)</dt>
                <dd>{data.latest_snapshot.used_grade_count}</dd>
              </div>
            </dl>
          </div>
        )}
      </section>
    </div>
  );
}
