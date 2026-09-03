"use client";

import { SCORE_COLORS } from "@/lib/design-tokens";
import { classifyScore } from "@/lib/transforms";

type ScoreBarProps = { score: number } | { value: number; max: number; label: string };

export function ScoreBar(props: ScoreBarProps) {
  if ("score" in props) {
    const tier = classifyScore(props.score);
    return <div className="flex items-center gap-2"><div className="h-1.5 w-16 rounded-full bg-gray-200 dark:bg-gray-800"><div className={`h-full rounded-full ${SCORE_COLORS[tier]}`} style={{ width: `${Math.round(props.score * 100)}%` }} /></div><span className="text-xs text-gray-600 dark:text-gray-400">{Math.round(props.score * 100)}%</span></div>;
  }
  const safeMax = Number.isFinite(props.max) && props.max > 0 ? props.max : 1;
  const width = Math.max(0, Math.min(100, (Number.isFinite(props.value) ? props.value : 0) / safeMax * 100));
  return <div className="space-y-1"><div className="flex justify-between text-xs"><span>{props.label}</span><span>{props.value}</span></div><div className="h-2 overflow-hidden rounded bg-gray-200 dark:bg-gray-800" role="img" aria-label={`${props.label}: ${props.value}`}><div className="h-full rounded bg-blue-500" style={{ width: `${width}%` }} /></div></div>;
}
