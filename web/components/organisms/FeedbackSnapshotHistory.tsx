"use client";

import Link from "next/link";
import type { SnapshotListItem } from "@/types/feedback";
import { formatTimestamp } from "@/lib/transforms";

interface FeedbackSnapshotHistoryProps {
  snapshots: SnapshotListItem[];
  loading: boolean;
  error: string | null;
  currentSnapshotId: number;
}

export function FeedbackSnapshotHistory({
  snapshots,
  loading,
  error,
  currentSnapshotId,
}: FeedbackSnapshotHistoryProps) {
  return (
    <div className="rounded-lg border border-gray-200 bg-gray-50 p-4 dark:border-gray-800 dark:bg-gray-900">
      <h3 className="mb-3 text-xs font-medium uppercase text-gray-600 dark:text-gray-500">Snapshot history</h3>
      {loading && <p className="text-sm text-gray-600 dark:text-gray-500">Loading...</p>}
      {error && <p className="text-sm text-red-600 dark:text-red-400">Failed to load history: {error}</p>}
      {!loading && !error && snapshots.length === 0 && (
        <p className="text-sm text-gray-600 dark:text-gray-500">No snapshots recorded.</p>
      )}
      <ul className="space-y-1">
        {snapshots.map((s) => {
          const isCurrent = s.snapshot_id === currentSnapshotId;
          return (
            <li key={s.snapshot_id}>
              <Link
                href={`/feedback?snapshotId=${s.snapshot_id}`}
                className={`flex items-center justify-between gap-2 rounded px-2 py-1.5 text-sm ${
                  isCurrent
                    ? "bg-gray-100 text-gray-900 dark:bg-gray-800 dark:text-gray-100"
                    : "text-gray-600 hover:bg-gray-100 hover:text-gray-800 dark:text-gray-400 dark:hover:bg-gray-800/50 dark:hover:text-gray-200"
                }`}
              >
                <span>
                  Scan #{s.scan_id}
                  <span className="ml-2 text-xs text-gray-600 dark:text-gray-500">{s.mode}</span>
                </span>
                <span className="text-xs text-gray-600 dark:text-gray-500">{formatTimestamp(s.created_at)}</span>
              </Link>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
