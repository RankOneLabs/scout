"use client";

interface StatusDotProps {
  platform: string;
}

const DOT_COLORS: Record<string, string> = {
  farcaster: "bg-purple-500 dark:bg-purple-400",
  discord: "bg-indigo-500 dark:bg-indigo-400",
  bluesky: "bg-sky-500 dark:bg-sky-400",
};

export function StatusDot({ platform }: StatusDotProps) {
  const color = DOT_COLORS[platform] ?? "bg-gray-500 dark:bg-gray-400";
  return <span className={`inline-block h-2 w-2 rounded-full ${color}`} />;
}
