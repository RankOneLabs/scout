"use client";

import {
  VERDICT_COLORS,
  PLATFORM_COLORS,
  PROJECT_COLORS,
} from "@/lib/design-tokens";
import { classifyScore, type ScoreTier } from "@/lib/transforms";

const SCORE_BADGE_COLORS: Record<ScoreTier, string> = {
  high: "bg-green-100 text-green-700 border-green-300 dark:bg-green-500/20 dark:text-green-400 dark:border-green-500/30",
  medium:
    "bg-amber-100 text-amber-700 border-amber-300 dark:bg-amber-500/20 dark:text-amber-400 dark:border-amber-500/30",
  low: "bg-red-100 text-red-700 border-red-300 dark:bg-red-500/20 dark:text-red-400 dark:border-red-500/30",
};

type BadgeVariant = "verdict" | "platform" | "project" | "score";

interface BadgeProps {
  label: string;
  variant: BadgeVariant;
  score?: number;
}

const COLOR_MAPS: Record<string, Record<string, string>> = {
  verdict: VERDICT_COLORS,
  platform: PLATFORM_COLORS,
  project: PROJECT_COLORS,
};

export function Badge({ label, variant, score }: BadgeProps) {
  const colorClass =
    variant === "score" && score !== undefined
      ? SCORE_BADGE_COLORS[classifyScore(score)]
      : (COLOR_MAPS[variant]?.[label] ??
        "bg-gray-100 text-gray-700 border-gray-300 dark:bg-gray-500/20 dark:text-gray-400 dark:border-gray-500/30");

  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${colorClass}`}
    >
      {label}
    </span>
  );
}
