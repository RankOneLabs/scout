"use client";

import { SCORE_COLORS } from "@/lib/design-tokens";
import { classifyScore } from "@/lib/transforms";
import type { RelevancePresentation, RouteAction } from "@/types/schema";

type ScoreBarProps =
  | { score: number; relevancePresentation?: RelevancePresentation | null }
  | { value: number; max: number; label: string };

export type ScoreBarSelection =
  | { kind: "action"; action: RouteAction | null }
  | { kind: "score"; score: number }
  | { kind: "distance"; value: number; max: number; label: string };

/** The one badge-versus-score selector used by every ScoreBar render site. */
export function selectScoreBar(props: ScoreBarProps): ScoreBarSelection {
  if ("value" in props) return { kind: "distance", ...props };
  if (props.relevancePresentation?.classifier === "jev") {
    return { kind: "action", action: props.relevancePresentation.jev?.action ?? null };
  }
  return { kind: "score", score: props.score };
}

const ACTION_COLORS: Record<RouteAction, string> = {
  respond:
    "border-green-300 bg-green-100 text-green-800 dark:border-green-700 dark:bg-green-950 dark:text-green-300",
  review:
    "border-sky-300 bg-sky-100 text-sky-800 dark:border-sky-700 dark:bg-sky-950 dark:text-sky-300",
  drop:
    "border-red-300 bg-red-100 text-red-800 dark:border-red-700 dark:bg-red-950 dark:text-red-300",
};

export function ScoreBar(props: ScoreBarProps) {
  const selected = selectScoreBar(props);
  if (selected.kind === "action") {
    const label = selected.action ?? "unavailable";
    const colors = selected.action
      ? ACTION_COLORS[selected.action]
      : "border-gray-300 bg-gray-100 text-gray-700 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300";
    return <span aria-label={`Jev action: ${label}`} className={`inline-flex rounded-full border px-2 py-0.5 text-xs font-medium uppercase ${colors}`}>{label}</span>;
  }
  if (selected.kind === "score") {
    const tier = classifyScore(selected.score);
    return <div className="flex items-center gap-2"><div className="h-1.5 w-16 rounded-full bg-gray-200 dark:bg-gray-800"><div className={`h-full rounded-full ${SCORE_COLORS[tier]}`} style={{ width: `${Math.round(selected.score * 100)}%` }} /></div><span className="text-xs text-gray-600 dark:text-gray-400">{Math.round(selected.score * 100)}%</span></div>;
  }
  const safeMax = Number.isFinite(selected.max) && selected.max > 0 ? selected.max : 1;
  const width = Math.max(0, Math.min(100, (Number.isFinite(selected.value) ? selected.value : 0) / safeMax * 100));
  return <div className="space-y-1"><div className="flex justify-between text-xs"><span>{selected.label}</span><span>{selected.value}</span></div><div className="h-2 overflow-hidden rounded bg-gray-200 dark:bg-gray-800" role="img" aria-label={`${selected.label}: ${selected.value}`}><div className="h-full rounded bg-blue-500" style={{ width: `${width}%` }} /></div></div>;
}
