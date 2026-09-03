export const PLATFORM_COLORS: Record<string, string> = {
  farcaster:
    "bg-purple-100 text-purple-700 border-purple-300 dark:bg-purple-500/20 dark:text-purple-400 dark:border-purple-500/30",
  discord:
    "bg-indigo-100 text-indigo-700 border-indigo-300 dark:bg-indigo-500/20 dark:text-indigo-400 dark:border-indigo-500/30",
  bluesky:
    "bg-sky-100 text-sky-700 border-sky-300 dark:bg-sky-500/20 dark:text-sky-400 dark:border-sky-500/30",
};

export const VERDICT_COLORS: Record<string, string> = {
  approve:
    "bg-green-100 text-green-700 border-green-300 dark:bg-green-500/20 dark:text-green-400 dark:border-green-500/30",
  revise:
    "bg-amber-100 text-amber-700 border-amber-300 dark:bg-amber-500/20 dark:text-amber-400 dark:border-amber-500/30",
  reject:
    "bg-red-100 text-red-700 border-red-300 dark:bg-red-500/20 dark:text-red-400 dark:border-red-500/30",
  pending:
    "bg-gray-100 text-gray-700 border-gray-300 dark:bg-gray-500/20 dark:text-gray-400 dark:border-gray-500/30",
};

export const SCORE_COLORS: Record<string, string> = {
  high: "bg-green-500",
  medium: "bg-amber-500",
  low: "bg-red-500",
};

export const SPAN_KIND_COLORS: Record<string, string> = {
  pipeline_run:
    "bg-blue-100 text-blue-700 border-blue-300 dark:bg-blue-500/20 dark:text-blue-400 dark:border-blue-500/30",
  pipeline_step:
    "bg-cyan-100 text-cyan-700 border-cyan-300 dark:bg-cyan-500/20 dark:text-cyan-400 dark:border-cyan-500/30",
  llm_call:
    "bg-violet-100 text-violet-700 border-violet-300 dark:bg-violet-500/20 dark:text-violet-400 dark:border-violet-500/30",
  tool_call:
    "bg-amber-100 text-amber-700 border-amber-300 dark:bg-amber-500/20 dark:text-amber-400 dark:border-amber-500/30",
  agent_run:
    "bg-pink-100 text-pink-700 border-pink-300 dark:bg-pink-500/20 dark:text-pink-400 dark:border-pink-500/30",
  memory_query:
    "bg-teal-100 text-teal-700 border-teal-300 dark:bg-teal-500/20 dark:text-teal-400 dark:border-teal-500/30",
  grading:
    "bg-orange-100 text-orange-700 border-orange-300 dark:bg-orange-500/20 dark:text-orange-400 dark:border-orange-500/30",
};

export const SPAN_STATUS_COLORS: Record<string, string> = {
  success:
    "bg-green-100 text-green-700 border-green-300 dark:bg-green-500/20 dark:text-green-400 dark:border-green-500/30",
  error:
    "bg-red-100 text-red-700 border-red-300 dark:bg-red-500/20 dark:text-red-400 dark:border-red-500/30",
  skipped:
    "bg-gray-100 text-gray-700 border-gray-300 dark:bg-gray-500/20 dark:text-gray-400 dark:border-gray-500/30",
};

const PROJECT_COLOR_PALETTE = [
  "bg-cyan-100 text-cyan-700 border-cyan-300 dark:bg-cyan-500/20 dark:text-cyan-400 dark:border-cyan-500/30",
  "bg-pink-100 text-pink-700 border-pink-300 dark:bg-pink-500/20 dark:text-pink-400 dark:border-pink-500/30",
  "bg-teal-100 text-teal-700 border-teal-300 dark:bg-teal-500/20 dark:text-teal-400 dark:border-teal-500/30",
  "bg-orange-100 text-orange-700 border-orange-300 dark:bg-orange-500/20 dark:text-orange-400 dark:border-orange-500/30",
  "bg-violet-100 text-violet-700 border-violet-300 dark:bg-violet-500/20 dark:text-violet-400 dark:border-violet-500/30",
  "bg-lime-100 text-lime-700 border-lime-300 dark:bg-lime-500/20 dark:text-lime-400 dark:border-lime-500/30",
  "bg-rose-100 text-rose-700 border-rose-300 dark:bg-rose-500/20 dark:text-rose-400 dark:border-rose-500/30",
  "bg-sky-100 text-sky-700 border-sky-300 dark:bg-sky-500/20 dark:text-sky-400 dark:border-sky-500/30",
] as const;

function hashIndex(key: string): number {
  let hash = 0;
  for (let i = 0; i < key.length; i++) {
    hash = (hash * 31 + key.charCodeAt(i)) | 0;
  }
  return Math.abs(hash) % PROJECT_COLOR_PALETTE.length;
}

export function getProjectColor(key: string): string {
  return PROJECT_COLOR_PALETTE[hashIndex(key)];
}

export const PROJECT_COLORS: Record<string, string> = new Proxy(
  {},
  { get: (_target, prop: string) => getProjectColor(prop) },
);

export const GRADE_COLORS: Record<string, string> = {
  correct: "border-l-green-600 dark:border-l-green-500",
  false_positive: "border-l-red-600 dark:border-l-red-500",
  false_negative: "border-l-amber-600 dark:border-l-amber-500",
  ungraded: "",
};

export const QUALITY_COLORS: Record<string, string> = {
  "1": "bg-red-100 text-red-700 border-red-300 dark:bg-red-500/20 dark:text-red-400 dark:border-red-500/30",
  "2": "bg-amber-100 text-amber-700 border-amber-300 dark:bg-amber-500/20 dark:text-amber-400 dark:border-amber-500/30",
  "3": "bg-green-100 text-green-700 border-green-300 dark:bg-green-500/20 dark:text-green-400 dark:border-green-500/30",
};

export const VERB_COLORS: Record<string, string> = {
  citation_needed:
    "bg-amber-100 text-amber-700 border-amber-300 dark:bg-amber-500/20 dark:text-amber-400 dark:border-amber-500/30",
  verify:
    "bg-sky-100 text-sky-700 border-sky-300 dark:bg-sky-500/20 dark:text-sky-400 dark:border-sky-500/30",
  soften:
    "bg-indigo-100 text-indigo-700 border-indigo-300 dark:bg-indigo-500/20 dark:text-indigo-400 dark:border-indigo-500/30",
  sharpen:
    "bg-violet-100 text-violet-700 border-violet-300 dark:bg-violet-500/20 dark:text-violet-400 dark:border-violet-500/30",
  expand:
    "bg-teal-100 text-teal-700 border-teal-300 dark:bg-teal-500/20 dark:text-teal-400 dark:border-teal-500/30",
  trim: "bg-rose-100 text-rose-700 border-rose-300 dark:bg-rose-500/20 dark:text-rose-400 dark:border-rose-500/30",
  custom:
    "bg-gray-100 text-gray-700 border-gray-300 dark:bg-gray-500/20 dark:text-gray-400 dark:border-gray-500/30",
};
