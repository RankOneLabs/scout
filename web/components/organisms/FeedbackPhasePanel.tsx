"use client";

import Link from "next/link";
import type {
  FeedbackGradeUse,
  FeedbackPhase,
  FeedbackPhaseRunConsumer,
  FeedbackPhaseSummary,
  FeedbackSnapshotMode,
  FeedbackUseFilter,
} from "@/types/feedback";
import { formatTimestamp } from "@/lib/transforms";

const PHASE_TITLES: Record<FeedbackPhase, string> = {
  relevance: "Relevance",
  reply_draft: "Reply Draft Quality",
  critic: "Critic",
};

const USE_FILTERS: FeedbackUseFilter[] = ["all", "included", "excluded"];

interface PhaseItemsState {
  data: FeedbackGradeUse[];
  has_more: boolean;
  use_filter: FeedbackUseFilter;
}

interface PhaseConsumersState {
  data: FeedbackPhaseRunConsumer[];
  has_more: boolean;
}

interface FeedbackPhasePanelProps {
  summary: FeedbackPhaseSummary;
  mode: FeedbackSnapshotMode;
  itemsState: PhaseItemsState;
  consumersState: PhaseConsumersState;
  onUseFilterChange: (use: FeedbackUseFilter) => void;
  onLoadMore: () => void;
  onLoadMoreConsumers: () => void;
}

function GradeUseRow({ item }: { item: FeedbackGradeUse }) {
  const isIncluded = item.primary_use === "included";
  return (
    <li className="rounded border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-950 p-2 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-gray-700 dark:text-gray-300">
          grade #{item.grade_id} · post #{item.post_id}
        </span>
        <span
          className={`rounded border px-1.5 py-0.5 font-medium ${
            isIncluded
              ? "border-green-300 bg-green-100 text-green-700 dark:border-green-800 dark:bg-green-900/50 dark:text-green-400"
              : "border-red-300 bg-red-100 text-red-700 dark:border-red-800 dark:bg-red-900/50 dark:text-red-400"
          }`}
        >
          {item.primary_use}
        </span>
        {item.selected_note && (
          <span className="rounded border border-purple-300 bg-purple-100 px-1.5 py-0.5 font-medium text-purple-700 dark:border-purple-800 dark:bg-purple-900/50 dark:text-purple-300">
            note #{item.example_rank}
          </span>
        )}
        <span className="text-gray-600 dark:text-gray-500">{item.roles.join(", ")}</span>
        <span className="ml-auto text-gray-600 dark:text-gray-500">{formatTimestamp(item.graded_at)}</span>
      </div>
      {item.exclusion_reasons.length > 0 && (
        <p className="mt-1 text-red-700 dark:text-red-300">
          {item.exclusion_reasons.join(", ")}
        </p>
      )}
      <p className="mt-1 text-gray-600 dark:text-gray-500">
        Selection: {item.selection_reasons.join(", ")}
      </p>
      <details className="mt-1">
        <summary className="cursor-pointer text-gray-600 hover:text-gray-900 dark:text-gray-500 dark:hover:text-gray-300">
          Pinned grade revision #{item.pinned_grade_revision_id}
        </summary>
        <div className="mt-1 space-y-1">
          <p className="text-gray-600 dark:text-gray-500">
            recorded {formatTimestamp(item.pinned_grade_revision_recorded_at)}
          </p>
          <pre className="whitespace-pre-wrap break-words rounded bg-gray-100 dark:bg-black/40 p-2 text-gray-600 dark:text-gray-400">
            {item.pinned_grade_revision_payload}
          </pre>
        </div>
      </details>
    </li>
  );
}

export function FeedbackPhasePanel({
  summary,
  mode,
  itemsState,
  consumersState,
  onUseFilterChange,
  onLoadMore,
  onLoadMoreConsumers,
}: FeedbackPhasePanelProps) {
  const { counts } = summary;
  return (
    <div
      id={`phase-${summary.phase}`}
      className="flex flex-col gap-3 rounded-lg border border-gray-200 bg-gray-50 dark:border-gray-800 dark:bg-gray-900 p-4"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-base font-medium text-gray-900 dark:text-gray-100">{PHASE_TITLES[summary.phase]}</h3>
          <p className="text-[10px] text-gray-500 dark:text-gray-400">
            Snapshot phase #{summary.snapshot_phase_id}
          </p>
        </div>
        {summary.truncated && (
          <span className="rounded border border-orange-300 bg-orange-100 px-2 py-0.5 text-xs text-orange-700 dark:border-orange-800 dark:bg-orange-900/50 dark:text-orange-300">
            truncated to budget
          </span>
        )}
      </div>

      <dl className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-3">
        <div>
          <dt className="text-gray-600 dark:text-gray-500">Included</dt>
          <dd className="text-gray-900 dark:text-gray-100">{counts.included_count}</dd>
        </div>
        <div>
          <dt className="text-gray-600 dark:text-gray-500">Excluded</dt>
          <dd className="text-gray-900 dark:text-gray-100">{counts.excluded_count}</dd>
        </div>
        <div>
          <dt className="text-gray-600 dark:text-gray-500">Examples</dt>
          <dd className="text-gray-900 dark:text-gray-100">{counts.example_count}</dd>
        </div>
        <div>
          <dt className="text-gray-600 dark:text-gray-500">Actually used</dt>
          <dd className="text-gray-900 dark:text-gray-100">
            {counts.actually_used_count}
            {mode === "shadow" && <span className="ml-1 text-yellow-700 dark:text-yellow-500">(shadow)</span>}
          </dd>
        </div>
        <div>
          <dt className="text-gray-600 dark:text-gray-500">Tokens</dt>
          <dd className="text-gray-900 dark:text-gray-100">
            {summary.token_estimate} / {summary.token_budget}
          </dd>
        </div>
      </dl>

      <div>
        <div className="mb-1 flex items-center justify-between">
          <h4 className="text-xs font-medium uppercase text-gray-600 dark:text-gray-500">Rendered text</h4>
          <span className="font-mono text-[10px] text-gray-500 dark:text-gray-400">{summary.rendered_sha256}</span>
        </div>
        {summary.rendered_text === "" ? (
          <p className="rounded border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-950 p-3 text-sm italic text-gray-600 dark:text-gray-500">
            No eligible feedback for this phase.
          </p>
        ) : (
          <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words rounded border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-950 p-3 text-sm text-gray-700 dark:text-gray-300">
            {summary.rendered_text}
          </pre>
        )}
      </div>

      <details>
        <summary className="cursor-pointer text-xs text-gray-600 hover:text-gray-900 dark:text-gray-500 dark:hover:text-gray-300">
          Structured summary (JSON)
        </summary>
        <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-gray-100 dark:bg-black/40 p-2 text-xs text-gray-600 dark:text-gray-400">
          {summary.structured_summary}
        </pre>
      </details>

      <div>
        <h4 className="mb-2 text-xs font-medium uppercase text-gray-600 dark:text-gray-500">
          Phase consumers ({consumersState.data.length}{consumersState.has_more ? "+" : ""})
        </h4>
        {consumersState.data.length === 0 ? (
          <p className="text-xs italic text-gray-600 dark:text-gray-500">No stored phase runs consumed this policy.</p>
        ) : (
          <ul className="max-h-72 space-y-1 overflow-auto">
            {consumersState.data.map((consumer) => (
              <li key={consumer.id} className="rounded border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-950 p-2 text-xs">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <Link
                    href={`/feedback/phase-runs/${consumer.id}`}
                    className="text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300"
                  >
                    Phase run #{consumer.id}
                  </Link>
                  <span className="text-gray-600 dark:text-gray-500">{consumer.status}</span>
                  <span className="text-gray-500 dark:text-gray-400">
                    scan #{consumer.scan_id} · post #{consumer.post_id}
                  </span>
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1">
                  <Link
                    href={`/traces/${consumer.trace_id}`}
                    className="font-mono text-[11px] text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300"
                  >
                    trace {consumer.trace_id.slice(0, 8)}…
                  </Link>
                  {consumer.evaluation_id !== null && (
                    <Link
                      href={`/scans/${consumer.scan_id}#evaluation-${consumer.evaluation_id}`}
                      className="text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300"
                    >
                      evaluation #{consumer.evaluation_id}
                    </Link>
                  )}
                  {consumer.grade_id !== null && (
                    <Link
                      href={`/feedback/grades/${consumer.grade_id}`}
                      className="text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300"
                    >
                      grade #{consumer.grade_id}
                    </Link>
                  )}
                  <span className="ml-auto text-gray-500 dark:text-gray-400">
                    {formatTimestamp(consumer.created_at)}
                  </span>
                </div>
              </li>
            ))}
          </ul>
        )}
        {consumersState.has_more && (
          <button
            type="button"
            onClick={onLoadMoreConsumers}
            className="mt-2 w-full rounded border border-gray-200 py-1.5 text-xs text-gray-600 hover:bg-gray-100 hover:text-gray-900 dark:border-gray-800 dark:text-gray-400 dark:hover:bg-gray-800/50 dark:hover:text-gray-200"
          >
            Load more phase consumers
          </button>
        )}
      </div>

      <div>
        <div className="mb-2 flex items-center gap-1">
          {USE_FILTERS.map((use) => (
            <button
              key={use}
              type="button"
              onClick={() => onUseFilterChange(use)}
              className={`rounded px-2 py-1 text-xs font-medium ${
                itemsState.use_filter === use
                  ? "bg-gray-200 text-gray-900 dark:bg-gray-700 dark:text-gray-100"
                  : "bg-gray-100 text-gray-600 hover:text-gray-900 dark:bg-gray-800 dark:text-gray-400 dark:hover:text-gray-200"
              }`}
            >
              {use}
            </button>
          ))}
        </div>
        <ul className="max-h-96 space-y-1 overflow-auto">
          {itemsState.data.map((item) => (
            <GradeUseRow key={`${item.grade_id}-${item.primary_use}`} item={item} />
          ))}
          {itemsState.data.length === 0 && (
            <li className="text-xs text-gray-600 dark:text-gray-500">No grades in this view.</li>
          )}
        </ul>
        {itemsState.has_more && (
          <button
            type="button"
            onClick={onLoadMore}
            className="mt-2 w-full rounded border border-gray-200 py-1.5 text-xs text-gray-600 hover:bg-gray-100 hover:text-gray-900 dark:border-gray-800 dark:text-gray-400 dark:hover:bg-gray-800/50 dark:hover:text-gray-200"
          >
            Load more
          </button>
        )}
      </div>
    </div>
  );
}
