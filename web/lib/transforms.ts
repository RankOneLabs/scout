import type {
  PostWithEvaluation,
  DraftWithContext,
  DraftWithGrade,
  ReviewEvaluation,
  TraceSpan,
  Scan,
  ScanFetchFailure,
  SurfaceStatus,
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

// Relevance provenance — row mirrors
//
// These mirror the `relevance_decisions` and `relevance_holdouts` tables
// (scout/storage/schema.py, v48) and would conventionally live in
// types/schema.ts beside the other table mirrors. They are here because
// types/schema.ts is outside this change's authorized paths; moving them
// there is a mechanical relocation once that is authorized. One definition
// each, imported by lib/queries.ts, the selectors and the components —
// nothing is duplicated or hand-synced.

/** Mirrors `relevance_decisions.classifier`. */
export type RelevanceClassifier = "llm" | "jev";

/** Mirrors `relevance_decisions.action` and `relevance_holdouts.release_action`. */
export type RelevanceAction = "respond" | "review" | "drop";

/** Mirrors `relevance_holdouts.status`. */
export type HoldoutStatus = "pending" | "claimed" | "released" | "failed";

/** Mirrors `relevance_holdouts.release_authority`: a stored blind label, or
 *  the action the classifier recorded when no label exists for the case. */
export type ReleaseAuthority = "label" | "recorded_action";

/** Mirrors `relevance_holdouts.label`. */
export type HoldoutLabel = "exclusion" | "in_post" | "pointer" | "none";

/** Mirrors one `relevance_decisions` row: what classified this evaluation.
 *  Absent for a historical row — nothing recorded what produced it — and
 *  absent for a released target, whose authority is its hold's release
 *  record rather than a classifier run. */
export interface RelevanceDecisionRow {
  decision_uid: string;
  classifier: RelevanceClassifier;
  model: string;
  catalogue_id: string | null;
  catalogue_version: string | null;
  router_version: string | null;
  action: RelevanceAction;
  reason: string | null;
  selected_for_holdout: boolean;
  created_at: string;
}

/** Mirrors one `relevance_holdouts` row, read from the held evaluation's
 *  side: this evaluation is the immutable source the hold references. */
export interface HoldoutRow {
  id: number;
  status: HoldoutStatus;
  held_at: string;
  released_at: string | null;
  release_authority: ReleaseAuthority | null;
  release_action: RelevanceAction | null;
  label: HoldoutLabel | null;
  label_source: string | null;
  target_evaluation_id: number | null;
  attempts: number;
  last_error: string | null;
}

/** The same row read from the released target's side: this evaluation is
 *  what a release produced, and `source_evaluation_id` is the held decision
 *  it was released from. The target's own `surface_status` is a separate
 *  fact — a release that drafted may still have been rejected or blocked. */
export interface ReleasedFromRow {
  holdout_id: number;
  source_evaluation_id: number;
  release_authority: ReleaseAuthority;
  release_action: RelevanceAction;
  label: HoldoutLabel | null;
  label_source: string | null;
  released_at: string | null;
}

/** Every provenance fact recorded about one evaluation, kept apart.
 *
 * `decision` is what the classifier decided (the source action). `holdout`
 * is the hold this evaluation is the source of. `released_from` is the hold
 * this evaluation is the released target of. None of the three is the
 * evaluation's `surface_status`, which is what actually happened. */
export interface RelevanceProvenance {
  evaluation_id: number;
  decision: RelevanceDecisionRow | null;
  holdout: HoldoutRow | null;
  released_from: ReleasedFromRow | null;
}

/** Carried by a row read from a database that has the holdout schema.
 *
 * Omitted entirely for a database that predates it: an absent field is an
 * honest absence, where a null would read as "nothing was recorded" over a
 * database that could not record anything. */
export interface WithRelevanceProvenance {
  relevance_provenance?: RelevanceProvenance;
}

export type ReviewEvaluationWithProvenance = ReviewEvaluation & WithRelevanceProvenance;
export type DraftWithProvenance = DraftWithContext & WithRelevanceProvenance;
export type DraftWithGradeAndProvenance = DraftWithGrade & WithRelevanceProvenance;

// Surface-status selectors
//
// A hold is a lifecycle state of its own — decided, recorded, held back from
// surfacing for blind grading. It is neither an approved reply nor a
// drafting defect, so every count and every "can an operator act on this"
// question goes through the two predicates below rather than enumerating the
// negative cases and missing one. Mirrors scout.storage.evaluations.
//
// `SurfaceStatus` in types/schema.ts and `SURFACE_STATUSES` in
// lib/filter-schemas.ts are the same vocabulary, `held` included — the type
// and the runtime value. The predicates below narrow against the union, so a
// status the database can hold but the union cannot name would not compile.

export const HELD_SURFACE_STATUS: SurfaceStatus = "held";

/** Whether this evaluation was held back from surfacing for blind grading. */
export function isHeld(surfaceStatus: SurfaceStatus | null): boolean {
  return surfaceStatus === HELD_SURFACE_STATUS;
}

/** Whether an operator can still post a reply for this evaluation.
 *
 * False for 'held'. A hold produced a decision and stopped: it has no draft
 * to post and no defect to fix, so it belongs in neither the draft queue nor
 * the review queue. */
export function isActionableForPosting(
  surfaceStatus: SurfaceStatus | null
): boolean {
  return surfaceStatus === "surfaced";
}

export interface SurfaceStatusCounts {
  by_status: Record<string, number>;
  total: number;
  held: number;
  surfaced: number;
  drafting_failed: number;
  /** How many an operator can act on. Derived from isActionableForPosting so
   *  it cannot drift from the predicate the queues use. */
  actionable: number;
}

/** Count one evaluation population by `surface_status`, folding nothing.
 *
 * The status breakdown a scan's evaluation view reads. 'held' is its own
 * column: it is not added to `surfaced`, and it is not added to
 * `drafting_failed`. */
export function selectSurfaceStatusCounts(
  evaluations: ReadonlyArray<{ surface_status: SurfaceStatus | null }>
): SurfaceStatusCounts {
  const by_status = evaluations.reduce<Record<string, number>>((counts, evaluation) => {
    const status = evaluation.surface_status ?? "unrecorded";
    counts[status] = (counts[status] ?? 0) + 1;
    return counts;
  }, {});
  return {
    by_status,
    total: evaluations.length,
    held: by_status[HELD_SURFACE_STATUS] ?? 0,
    surfaced: by_status["surfaced"] ?? 0,
    drafting_failed: by_status["drafting_failed"] ?? 0,
    actionable: evaluations.filter((evaluation) =>
      isActionableForPosting(evaluation.surface_status)
    ).length,
  };
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
