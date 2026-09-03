"use client";

import Link from "next/link";
import type { FeedbackSnapshotHeader } from "@/types/feedback";
import { formatTimestamp } from "@/lib/transforms";

const MODE_STYLES: Record<FeedbackSnapshotHeader["mode"], string> = {
  shadow:
    "bg-yellow-100 text-yellow-700 border-yellow-300 dark:bg-yellow-900/50 dark:text-yellow-400 dark:border-yellow-800",
  active:
    "bg-green-100 text-green-700 border-green-300 dark:bg-green-900/50 dark:text-green-400 dark:border-green-800",
};

export function FeedbackSnapshotHeaderCard({ snapshot }: { snapshot: FeedbackSnapshotHeader }) {
  return (
    <div className="rounded-lg border border-gray-200 bg-gray-50 p-4 dark:border-gray-800 dark:bg-gray-900">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
          Snapshot #{snapshot.snapshot_id}
        </h2>
        <span
          className={`rounded border px-2 py-0.5 text-xs font-medium ${MODE_STYLES[snapshot.mode]}`}
        >
          {snapshot.mode}
        </span>
        <Link
          href={`/scans/${snapshot.scan_id}`}
          className="text-sm text-blue-600 hover:text-blue-500 dark:text-blue-400 dark:hover:text-blue-300"
        >
          Scan #{snapshot.scan_id}
        </Link>
      </div>

      {snapshot.mode === "shadow" && (
        <p className="mt-2 rounded border border-yellow-300 bg-yellow-100 px-3 py-2 text-xs text-yellow-700 dark:border-yellow-900 dark:bg-yellow-950/30 dark:text-yellow-200">
          Shadow snapshot — this evidence was stored but not used. The legacy feedback
          path remained live for this scan.
        </p>
      )}

      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-4">
        <div>
          <dt className="text-xs uppercase text-gray-600 dark:text-gray-500">Policy</dt>
          <dd className="font-mono text-gray-700 dark:text-gray-300">{snapshot.policy_version}</dd>
        </div>
        <div>
          <dt className="text-xs uppercase text-gray-600 dark:text-gray-500">As of</dt>
          <dd className="text-gray-700 dark:text-gray-300">{formatTimestamp(snapshot.as_of)}</dd>
        </div>
        <div>
          <dt className="text-xs uppercase text-gray-600 dark:text-gray-500">Lookback window</dt>
          <dd className="text-gray-700 dark:text-gray-300">
            {formatTimestamp(snapshot.lookback_started_at)} – {formatTimestamp(snapshot.as_of)}
            <span className="ml-1 text-gray-600 dark:text-gray-500">({snapshot.lookback_days}d)</span>
          </dd>
        </div>
        <div>
          <dt className="text-xs uppercase text-gray-600 dark:text-gray-500">Recorded</dt>
          <dd className="text-gray-700 dark:text-gray-300">{formatTimestamp(snapshot.created_at)}</dd>
        </div>
        <div>
          <dt className="text-xs uppercase text-gray-600 dark:text-gray-500">Population</dt>
          <dd className="text-gray-900 dark:text-gray-100">{snapshot.population_count}</dd>
        </div>
        <div>
          <dt className="text-xs uppercase text-gray-600 dark:text-gray-500">Eligible</dt>
          <dd className="text-gray-900 dark:text-gray-100">{snapshot.eligible_count}</dd>
        </div>
        <div>
          <dt className="text-xs uppercase text-gray-600 dark:text-gray-500">Excluded</dt>
          <dd className="text-gray-900 dark:text-gray-100">{snapshot.excluded_count}</dd>
        </div>
        <div>
          <dt className="text-xs uppercase text-gray-600 dark:text-gray-500">Max grades / segment min</dt>
          <dd className="text-gray-700 dark:text-gray-300">
            {snapshot.max_grades} / {snapshot.segment_min_grades}
          </dd>
        </div>
      </dl>
    </div>
  );
}
