"use client";

import type { DecisionMetric, DeltaStatus } from "@/lib/experiment-presentation";

const STATUS_STYLES: Record<DeltaStatus, string> = {
  better: "border-green-300 bg-green-100 text-green-700 dark:border-green-500/30 dark:bg-green-500/20 dark:text-green-400",
  worse: "border-red-300 bg-red-100 text-red-700 dark:border-red-500/30 dark:bg-red-500/20 dark:text-red-400",
  same: "border-gray-300 bg-white text-gray-700 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300",
  unavailable: "border-gray-300 bg-white text-gray-500 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-400",
};

const STATUS_LABELS: Record<DeltaStatus, string> = {
  better: "Better",
  worse: "Worse",
  same: "No change",
  unavailable: "Unavailable",
};

// One decision-metric card for the attempt-detail verdict header. Always
// pairs the formatted delta with its directionality (lower-is-better) so
// the sign of a number is never left for the reader to infer.
export function ExperimentMetricExplainer({ metric }: { metric: DecisionMetric }) {
  return (
    <div className={`rounded-lg border p-4 ${STATUS_STYLES[metric.status]}`}>
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium uppercase tracking-wide">{metric.label}</span>
        <span
          className={`rounded-full border px-2 py-0.5 text-[10px] font-medium ${STATUS_STYLES[metric.status]} ${
            metric.status === "unavailable" ? "italic" : ""
          }`}
        >
          {STATUS_LABELS[metric.status]}
        </span>
      </div>
      <p className={`mt-2 text-xl font-semibold ${metric.status === "unavailable" ? "italic" : ""}`}>
        {metric.text}
      </p>
      <p className="mt-1 text-[11px] opacity-80">{metric.helpText}</p>
    </div>
  );
}
