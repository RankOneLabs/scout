import type {
  PostWithEvaluation,
  DraftWithContext,
  TraceSpan,
  Scan,
  ScanFetchFailure,
} from "@/types/schema";

// Mirrors scout.config.SCOUT_STALE_WATERMARK_HOURS's default (the env var
// itself is a Python-process setting; the web read model has no access to
// it, so the default is duplicated here for display purposes only — an
// operator running with a non-default SCOUT_STALE_WATERMARK_HOURS should
// treat this badge as advisory and defer to `scout watermark stale-check`).
export const DEFAULT_STALE_WATERMARK_HOURS = 24;

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

// Coverage / watermark presentation selectors
//
// Processing `status` and `coverage_outcome` are independently-persisted
// facts (decision: neither can be inferred from the other) — these
// selectors read both without collapsing one into the other, so a
// non-blocking degraded scan (`status: 'partial'`, watermark still
// advanced) and a fully-processed but coverage-blocked scan
// (`status: 'complete'`, `coverage_outcome: 'blocked'`) render distinctly.

export type CoverageTone = "ok" | "warn" | "danger" | "neutral";

export interface CoverageDescription {
  label: string;
  tone: CoverageTone;
}

/** Describe a scan's coverage/watermark outcome, independent of its
 * processing `status`. Only `role: 'canonical_live'` scans ever advance
 * the watermark — a secondary/rescore scan's `coverage_outcome` is
 * whatever its own read (if any) derived, never a watermark decision. */
export function describeCoverage(
  scan: Pick<Scan, "coverage_outcome" | "watermark_advanced" | "role">
): CoverageDescription {
  if (scan.role !== "canonical_live") {
    return { label: scan.role === "rescore" ? "rescore (never advances)" : "secondary (never advances)", tone: "neutral" };
  }
  if (scan.coverage_outcome === null) {
    return { label: "coverage not yet finalized", tone: "neutral" };
  }
  if (scan.coverage_outcome === "blocked") {
    return { label: "coverage blocked", tone: "danger" };
  }
  if (scan.coverage_outcome === "complete" && scan.watermark_advanced) {
    return { label: "watermark advanced", tone: "ok" };
  }
  // coverage_outcome is 'complete'/'partial' but watermark_advanced is
  // false — a coverage read that was never asked to advance the cursor
  // (e.g. a re-run of finalize_scan_coverage with advance_watermark=False).
  return { label: `coverage ${scan.coverage_outcome} (not advanced)`, tone: "warn" };
}

/** True when a scan's processing degraded (`status: 'partial'`) for a
 * reason that never blocked watermark advancement — permanent
 * non-primary enrichment failure (e.g. parent-lookup) rather than a
 * primary-source coverage blocker. */
export function isNonBlockingDegradation(scan: Pick<Scan, "status" | "watermark_advanced">): boolean {
  return scan.status === "partial" && scan.watermark_advanced;
}

export type FailureBlockingClass = "blocking" | "eligible_uncounted" | "non_blocking";

/** How one fetch failure relates to the watermark decision:
 * - `blocking` — coverage finalization ran and counted it (a
 *   scan_watermark_blockers row exists): the exact reason coverage blocked.
 * - `eligible_uncounted` — classified as blocking-eligible, but no
 *   blocker row exists because finalization never ran or was refused
 *   (failed/interrupted scan, stale fence, …). It is *not* non-blocking;
 *   it simply was never adjudicated.
 * - `non_blocking` — permanent non-primary degradation (e.g.
 *   `operation_phase: 'parent_lookup'`) that never blocks by design. */
export function classifyFailureBlocking(f: ScanFetchFailure): FailureBlockingClass {
  if (f.blocked_watermark) return "blocking";
  if (f.blocks_watermark_advance) return "eligible_uncounted";
  return "non_blocking";
}

/** Split a scan's fetch failures by classifyFailureBlocking. A missing
 * blocker link is never on its own read as "non-blocking" — that would
 * mislabel a failed scan's primary failure as harmless degradation. */
export function partitionFailuresByBlocking(failures: ScanFetchFailure[]): {
  blocking: ScanFetchFailure[];
  eligibleUncounted: ScanFetchFailure[];
  nonBlocking: ScanFetchFailure[];
} {
  return {
    blocking: failures.filter((f) => classifyFailureBlocking(f) === "blocking"),
    eligibleUncounted: failures.filter((f) => classifyFailureBlocking(f) === "eligible_uncounted"),
    nonBlocking: failures.filter((f) => classifyFailureBlocking(f) === "non_blocking"),
  };
}

/** Whether a lease row currently confers ownership: it needs a holder and
 * an unexpired expiry. environment_leases rows are not cleared on expiry —
 * a stale owner_id lingers until takeover or release — so owner_id alone
 * is not evidence the lease is held. */
export function isLeaseHeld(
  lease: { owner_id: string | null; expires_at: string | null },
  nowIso: string
): boolean {
  if (!lease.owner_id || !lease.expires_at) return false;
  return parseUtc(lease.expires_at).getTime() > parseUtc(nowIso).getTime();
}

/** Whether an environment's watermark is stale against a fixed threshold —
 * the display-layer counterpart of `scout watermark stale-check`. A null
 * watermark (never advanced) is always stale. */
export function isWatermarkStale(
  watermarkAt: string | null,
  nowIso: string,
  staleAfterHours: number = DEFAULT_STALE_WATERMARK_HOURS
): boolean {
  if (!watermarkAt) return true;
  const ageMs = parseUtc(nowIso).getTime() - parseUtc(watermarkAt).getTime();
  return ageMs > staleAfterHours * 60 * 60 * 1000;
}
