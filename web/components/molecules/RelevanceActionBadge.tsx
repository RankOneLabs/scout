import type { RelevanceActionBadgeViewModel } from "@/lib/review-selectors";

const COLORS = {
  positive:
    "border-green-300 bg-green-50 text-green-800 dark:border-green-700 dark:bg-green-950 dark:text-green-300",
  neutral:
    "border-sky-300 bg-sky-50 text-sky-800 dark:border-sky-700 dark:bg-sky-950 dark:text-sky-300",
  negative:
    "border-gray-300 bg-gray-50 text-gray-700 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300",
};

/** The source classifier's recorded action.
 *
 * `review` is rendered here and nowhere else: it is metadata about what the
 * classifier asked for, not a claim that anything was surfaced or queued.
 * Renders nothing when no classifier was recorded — a historical row, or a
 * released target whose authority is its hold — because unknown reads as
 * unknown rather than as a decision nobody made.
 */
export function RelevanceActionBadge({
  badge,
}: {
  badge: RelevanceActionBadgeViewModel | null;
}) {
  if (!badge) return null;
  return (
    <span
      title={badge.title}
      className={`rounded border px-1.5 py-0.5 text-[11px] font-medium uppercase tracking-wide ${COLORS[badge.tone]}`}
    >
      {badge.label}
      <span className="sr-only">. {badge.title}</span>
    </span>
  );
}
