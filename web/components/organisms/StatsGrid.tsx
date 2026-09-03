"use client";

import {
  ScanSearch,
  MessageSquare,
  Target,
  FileText,
  CheckCircle2,
  XCircle,
} from "lucide-react";
import { StatCard } from "@/components/molecules/StatCard";
import { formatPercentage } from "@/lib/transforms";
import type { ScanStats } from "@/types/schema";

interface StatsGridProps {
  stats: ScanStats;
}

export function StatsGrid({ stats }: StatsGridProps) {
  const approvalRate =
    stats.total_drafts > 0
      ? (stats.total_approved / stats.total_drafts) * 100
      : 0;
  const rejectionRate =
    stats.total_drafts > 0
      ? (stats.total_rejected / stats.total_drafts) * 100
      : 0;

  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
      <StatCard
        label="Total Scans"
        value={stats.total_scans}
        icon={ScanSearch}
        color="text-blue-400"
      />
      <StatCard
        label="Total Posts"
        value={stats.total_posts}
        icon={MessageSquare}
        color="text-purple-600 dark:text-purple-400"
      />
      <StatCard
        label="Relevant Posts"
        value={stats.total_relevant}
        icon={Target}
        color="text-green-600 dark:text-green-400"
      />
      <StatCard
        label="Draft Comments"
        value={stats.total_drafts}
        icon={FileText}
        color="text-amber-600 dark:text-amber-400"
      />
      <StatCard
        label="Approval Rate"
        value={formatPercentage(approvalRate)}
        icon={CheckCircle2}
        color="text-green-600 dark:text-green-400"
      />
      <StatCard
        label="Rejection Rate"
        value={formatPercentage(rejectionRate)}
        icon={XCircle}
        color="text-red-400"
      />
    </div>
  );
}
