"use client";

import Link from "next/link";
import type { Scan, ScanStatus } from "@/types/schema";
import { formatTimestamp, formatDuration } from "@/lib/transforms";

interface ScanTableProps {
  scans: Scan[];
}

const STATUS_STYLES: Record<ScanStatus, string> = {
  complete: "bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-400",
  partial: "bg-yellow-100 text-yellow-700 dark:bg-yellow-900/50 dark:text-yellow-400",
  failed: "bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-400",
  interrupted: "bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-400",
};

function StatusBadge({ status }: { status: ScanStatus | null }) {
  if (!status) return null;
  return (
    <span
      className={`rounded px-2 py-0.5 text-xs font-medium ${STATUS_STYLES[status] ?? "bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-400"}`}
    >
      {status}
    </span>
  );
}

function ScanCard({ scan }: { scan: Scan }) {
  return (
    <Link
      href={`/scans/${scan.id}`}
      className="block rounded-lg border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-900 p-4 transition-colors hover:bg-gray-100 dark:hover:bg-gray-800/70"
    >
      <div className="flex items-center justify-between">
        <span className="font-medium text-blue-600 dark:text-blue-400">#{scan.id}</span>
        <span className="text-xs text-gray-600 dark:text-gray-500">
          {formatDuration(scan.started_at, scan.completed_at)}
        </span>
      </div>
      <div className="mt-1 flex items-center gap-2">
        <p className="text-sm text-gray-700 dark:text-gray-300">{formatTimestamp(scan.started_at)}</p>
        <StatusBadge status={scan.status} />
      </div>
      <div className="mt-2 flex gap-4 text-xs text-gray-600 dark:text-gray-400">
        <span>{scan.messages_scanned} messages</span>
        <span>{scan.relevant_found} relevant</span>
        {scan.overflow_count > 0 && (
          <span className="text-yellow-700 dark:text-yellow-500">+{scan.overflow_count} overflow</span>
        )}
      </div>
    </Link>
  );
}

export function ScanTable({ scans }: ScanTableProps) {
  if (scans.length === 0) {
    return (
      <p className="py-8 text-center text-sm text-gray-600 dark:text-gray-500">No scans yet.</p>
    );
  }

  return (
    <>
      {/* Mobile: card layout */}
      <div className="space-y-3 md:hidden">
        {scans.map((scan) => (
          <ScanCard key={scan.id} scan={scan} />
        ))}
      </div>

      {/* Desktop: table layout */}
      <div className="hidden overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-800 md:block">
        <table className="w-full text-left text-sm">
          <thead className="border-b border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-900 text-xs uppercase text-gray-600 dark:text-gray-400">
            <tr>
              <th className="px-4 py-3">ID</th>
              <th className="px-4 py-3">Status</th>
              <th className="px-4 py-3">Started</th>
              <th className="px-4 py-3">Duration</th>
              <th className="px-4 py-3">Messages</th>
              <th className="px-4 py-3">Relevant</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-200 dark:divide-gray-800">
            {scans.map((scan) => (
              <tr
                key={scan.id}
                className="bg-white dark:bg-gray-950 transition-colors hover:bg-gray-100 dark:hover:bg-gray-900"
              >
                <td className="px-4 py-3">
                  <Link
                    href={`/scans/${scan.id}`}
                    className="font-medium text-blue-600 dark:text-blue-400 hover:text-blue-500 dark:hover:text-blue-300"
                  >
                    #{scan.id}
                  </Link>
                </td>
                <td className="px-4 py-3">
                  <StatusBadge status={scan.status} />
                </td>
                <td className="px-4 py-3 text-gray-700 dark:text-gray-300">
                  {formatTimestamp(scan.started_at)}
                </td>
                <td className="px-4 py-3 text-gray-600 dark:text-gray-400">
                  {formatDuration(scan.started_at, scan.completed_at)}
                </td>
                <td className="px-4 py-3 text-gray-700 dark:text-gray-300">
                  {scan.messages_scanned}
                  {scan.overflow_count > 0 && (
                    <span className="ml-1 text-xs text-yellow-700 dark:text-yellow-500">
                      +{scan.overflow_count}
                    </span>
                  )}
                </td>
                <td className="px-4 py-3 text-gray-700 dark:text-gray-300">
                  {scan.relevant_found}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
