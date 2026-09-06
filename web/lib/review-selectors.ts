import type { Grade } from "@/types/schema";
import type { QueueReviewItem, ReviewCosts, ReviewDisposition, ReviewStatus } from "@/types/review-queues";

export function selectReviewStatus(input: {
  grade: Grade | null; revisionId: number | null; disposition: ReviewDisposition | null;
}): ReviewStatus {
  if (input.grade !== null) {
    if (input.grade.needs_regrade || input.grade.schema_version !== 3) return "needs_regrade";
    return input.disposition?.grade_revision_id === input.revisionId && input.revisionId !== null
      ? "reviewed" : "graded_elsewhere";
  }
  return input.disposition?.action.kind === "skip" ? "skipped" : "pending";
}

export function selectReviewCosts(dispositions: ReviewDisposition[]): ReviewCosts {
  const actions = [...new Map(dispositions.map((item) => [item.action_id, item])).values()]
    .filter((item) => item.action.kind !== "reconcile");
  const measured = actions.filter((item) => item.timing.elapsed_ms !== null);
  const priced = measured.filter((item) => item.pricing !== null);
  return {
    action_ids: actions.map((item) => item.action_id),
    measured_action_count: measured.length,
    unavailable_action_count: actions.length - measured.length,
    elapsed_ms: measured.reduce((sum, item) => sum + (item.timing.elapsed_ms ?? 0), 0),
    priced_elapsed_ms: priced.reduce((sum, item) => sum + (item.timing.elapsed_ms ?? 0), 0),
    estimated_usd: priced.length === 0 ? null : priced.reduce(
      (sum, item) => sum + (item.timing.elapsed_ms ?? 0) * (item.pricing?.usd_per_hour ?? 0) / 3_600_000, 0),
    purpose: "corpus_building",
  };
}

export function selectQueueItems(items: QueueReviewItem[], status: ReviewStatus | "all"): QueueReviewItem[] {
  return status === "all" ? items : items.filter((item) => item.status === status);
}

export function selectReviewProgress(items: QueueReviewItem[]) {
  return {
    reviewed: items.filter((item) => item.status === "reviewed").length,
    skipped: items.filter((item) => item.status === "skipped").length,
    graded_elsewhere: items.filter((item) => item.status === "graded_elsewhere").length,
  };
}
