"use client";

import Link from "next/link";
import type { TraceListItem } from "@/types/schema";
import { formatTimestamp, formatDurationMs } from "@/lib/transforms";
import { SPAN_STATUS_COLORS } from "@/lib/design-tokens";

interface TraceListProps {
  traces: TraceListItem[];
}

function TraceCard({ trace }: { trace: TraceListItem }) {
  const status = trace.has_error ? "error" : "success";
  const statusColor = SPAN_STATUS_COLORS[status];

  return (
    <Link
      href={`/traces/${trace.trace_id}`}
      className="block rounded-lg border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-900 p-4 transition-colors hover:bg-gray-100 dark:hover:bg-gray-800/70"
    >
      <div className="flex items-center justify-between">
        <span className="font-medium text-blue-600 dark:text-blue-400">{trace.name}</span>
        <span className={`rounded border px-1.5 py-0.5 text-xs ${statusColor}`}>
          {status}
        </span>
      </div>
      <p className="mt-1 text-sm text-gray-700 dark:text-gray-300">
        {formatTimestamp(trace.started_at)}
      </p>
      <div className="mt-2 flex gap-4 text-xs text-gray-600 dark:text-gray-400">
        <span>{formatDurationMs(trace.duration_ms)}</span>
        <span>{trace.step_count} steps</span>
      </div>
    </Link>
  );
}

export function TraceList({ traces }: TraceListProps) {
  if (traces.length === 0) {
    return (
      <p className="py-8 text-center text-sm text-gray-600 dark:text-gray-500">
        No traces yet.
      </p>
    );
  }

  return (
    <>
      {/* Mobile: card layout */}
      <div className="space-y-3 md:hidden">
        {traces.map((trace) => (
          <TraceCard key={trace.trace_id} trace={trace} />
        ))}
      </div>

      {/* Desktop: table layout */}
      <div className="hidden overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-800 md:block">
        <table className="w-full text-left text-sm">
          <thead className="border-b border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-900 text-xs uppercase text-gray-600 dark:text-gray-400">
            <tr>
              <th className="px-4 py-3">Pipeline</th>
              <th className="px-4 py-3">Started</th>
              <th className="px-4 py-3">Duration</th>
              <th className="px-4 py-3">Steps</th>
              <th className="px-4 py-3">Status</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-200 dark:divide-gray-800">
            {traces.map((trace) => {
              const status = trace.has_error ? "error" : "success";
              const statusColor = SPAN_STATUS_COLORS[status];

              return (
                <tr
                  key={trace.trace_id}
                  className="bg-white dark:bg-gray-950 transition-colors hover:bg-gray-100 dark:hover:bg-gray-900"
                >
                  <td className="px-4 py-3">
                    <Link
                      href={`/traces/${trace.trace_id}`}
                      className="font-medium text-blue-600 dark:text-blue-400 hover:text-blue-500 dark:hover:text-blue-300"
                    >
                      {trace.name}
                    </Link>
                  </td>
                  <td className="px-4 py-3 text-gray-700 dark:text-gray-300">
                    {formatTimestamp(trace.started_at)}
                  </td>
                  <td className="px-4 py-3 text-gray-600 dark:text-gray-400">
                    {formatDurationMs(trace.duration_ms)}
                  </td>
                  <td className="px-4 py-3 text-gray-700 dark:text-gray-300">
                    {trace.step_count}
                  </td>
                  <td className="px-4 py-3">
                    <span
                      className={`rounded border px-1.5 py-0.5 text-xs ${statusColor}`}
                    >
                      {status}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}
