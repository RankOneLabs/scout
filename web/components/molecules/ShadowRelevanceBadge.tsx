import type { ShadowRelevanceRunRow } from "@/types/schema";
import { selectShadowRelevanceBadge } from "@/lib/review-selectors";

const COLORS = {
  positive: "border-green-300 bg-green-50 text-green-800 dark:border-green-700 dark:bg-green-950 dark:text-green-300",
  negative: "border-gray-300 bg-gray-50 text-gray-700 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300",
  warning: "border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-300",
  error: "border-red-300 bg-red-50 text-red-800 dark:border-red-700 dark:bg-red-950 dark:text-red-300",
};

export function ShadowRelevanceBadge({ run }: { run: ShadowRelevanceRunRow | null | undefined }) {
  const badge = selectShadowRelevanceBadge(run);
  if (!badge) return null;
  const detail = Object.keys(badge.details).length === 0
    ? badge.title : `${badge.title}; bands: ${JSON.stringify(badge.details)}`;
  return <span title={detail} className={`rounded border px-1.5 py-0.5 text-[11px] font-medium uppercase tracking-wide ${COLORS[badge.tone]}`}>
    {badge.label}<span className="sr-only">. {detail}</span>
  </span>;
}
