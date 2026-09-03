import type {
  PostWithEvaluation,
  DraftWithContext,
  TraceSpan,
} from "@/types/schema";

export type ScoreTier = "high" | "medium" | "low";

export function groupBy<T, K>(
  items: T[],
  keyFn: (item: T) => K
): Map<K, T[]> {
  return items.reduce((acc, item) => {
    const key = keyFn(item);
    const group = acc.get(key) ?? [];
    group.push(item);
    acc.set(key, group);
    return acc;
  }, new Map<K, T[]>());
}

export function groupByPlatform(
  posts: PostWithEvaluation[]
): Map<string, PostWithEvaluation[]> {
  return groupBy(posts, (p) => p.platform);
}

export function groupByVerdict(
  drafts: DraftWithContext[]
): Map<string, DraftWithContext[]> {
  return groupBy(drafts, (d) => d.verdict ?? "pending");
}

export function groupByProject(
  drafts: DraftWithContext[]
): Map<string, DraftWithContext[]> {
  return groupBy(drafts, (d) => d.project_key ?? "unknown");
}

/** Ensure an ISO timestamp is parsed as UTC (Python stores without trailing Z). */
export function parseUtc(iso: string): Date {
  return iso.endsWith("Z") || /[+-]\d{2}:\d{2}$/.test(iso)
    ? new Date(iso)
    : new Date(iso + "Z");
}

export function formatTimestamp(iso: string | null): string {
  if (!iso) return "—";
  const date = parseUtc(iso);
  return date.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatDuration(
  start: string,
  end: string | null
): string {
  if (!end) return "In progress";
  const ms = parseUtc(end).getTime() - parseUtc(start).getTime();
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  return `${minutes}m ${remainingSeconds}s`;
}

/** Calendar-day phrasing matching the CLI's _format_due() output style. */
export function formatRelativeDays(
  targetIso: string,
  nowIso: string,
): string {
  const MS_PER_DAY = 24 * 60 * 60 * 1000;
  const target = parseUtc(targetIso);
  const now = parseUtc(nowIso);
  const targetDay = Date.UTC(
    target.getUTCFullYear(),
    target.getUTCMonth(),
    target.getUTCDate(),
  );
  const nowDay = Date.UTC(
    now.getUTCFullYear(),
    now.getUTCMonth(),
    now.getUTCDate(),
  );
  const diff = Math.round((targetDay - nowDay) / MS_PER_DAY);
  if (diff === 0) return "due today";
  const magnitude = Math.abs(diff);
  const unit = magnitude === 1 ? "day" : "days";
  if (diff > 0) return `${magnitude} ${unit} from now`;
  return `${magnitude} ${unit} overdue`;
}

export function formatScore(score: number | null): string {
  if (score === null) return "—";
  return (score * 100).toFixed(0) + "%";
}

export function formatPercentage(value: number): string {
  return value.toFixed(1) + "%";
}

export function truncateContent(content: string, maxLen: number): string {
  if (content.length <= maxLen) return content;
  return content.slice(0, maxLen) + "…";
}

export function classifyScore(score: number): ScoreTier {
  if (score >= 0.7) return "high";
  if (score >= 0.3) return "medium";
  return "low";
}

// --- Trace helpers ---

export type SpanStatus = "success" | "error" | "skipped";

export function getSpanStatus(span: TraceSpan): SpanStatus {
  if (span.error !== null) return "error";
  if (span.ended_at === null) return "skipped";
  return "success";
}

export function formatDurationMs(ms: number | null): string {
  if (ms === null) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

export interface SpanTreeNode {
  span: TraceSpan;
  children: SpanTreeNode[];
}

export function buildSpanTree(spans: TraceSpan[]): SpanTreeNode[] {
  const childrenMap = new Map<string | null, TraceSpan[]>();

  for (const span of spans) {
    const parentId = span.parent_id;
    const group = childrenMap.get(parentId) ?? [];
    group.push(span);
    childrenMap.set(parentId, group);
  }

  function buildNodes(parentId: string | null): SpanTreeNode[] {
    const children = childrenMap.get(parentId) ?? [];
    return children.map((span) => ({
      span,
      children: buildNodes(span.id),
    }));
  }

  return buildNodes(null);
}
