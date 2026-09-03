"use client";

import { useStats } from "@/hooks/use-stats";
import { StatsGrid } from "@/components/organisms/StatsGrid";

export default function DashboardPage() {
  const { stats, loading } = useStats();

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Dashboard</h1>
      {loading ? (
        <p className="text-sm text-gray-600 dark:text-gray-500">Loading…</p>
      ) : stats ? (
        <StatsGrid stats={stats} />
      ) : (
        <p className="text-sm text-gray-600 dark:text-gray-500">No scan data available.</p>
      )}
    </div>
  );
}
