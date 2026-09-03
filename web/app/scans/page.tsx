"use client";

import { useScans } from "@/hooks/use-scans";
import { ScanTable } from "@/components/organisms/ScanTable";

export default function ScansPage() {
  const { scans, loading, error } = useScans();

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-gray-600 dark:text-gray-500">Loading...</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="space-y-3">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Scans</h1>
        <div className="rounded-lg border border-red-300 bg-red-100 p-4 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
          Failed to load scans: {error}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Scans</h1>
      <ScanTable scans={scans} />
    </div>
  );
}
