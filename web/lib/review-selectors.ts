import type { Grade, ShadowRelevanceRunRow } from "@/types/schema";
import type { QueueReviewItem, ReviewCosts, ReviewDisposition, ReviewStatus, ReviewScorePresentation } from "@/types/review-queues";

export function selectReviewScore(score: QueueReviewItem["score"]): ReviewScorePresentation | null {
  if (score === null) return null;
  const isSimilarity = "similarity" in score;
  const summary = isSimilarity
    ? `Similarity to confirmed positives: ${score.similarity.toFixed(3)} (cosine similarity, not a relevance probability or human label)`
    : `Selector probability: ${score.probability.toFixed(3)} (ranking evidence, not a human label)`;
  const terms = score.explanation.map((term) => `${term.term} (${term.contribution.toFixed(3)})`).join(", ");
  return { summary, explanation: `${isSimilarity ? "TF-IDF × positive centroid" : "TF-IDF × coefficient"}: ${terms || "no shared terms"}` };
}

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

export interface ShadowRelevanceBadgeViewModel {
  label: string;
  tone: "positive" | "negative" | "warning" | "error";
  title: string;
  details: Record<string, unknown>;
}

export function selectShadowRelevanceBadge(
  run: ShadowRelevanceRunRow | null | undefined,
): ShadowRelevanceBadgeViewModel | null {
  if (!run) return null;
  const account = run.account_label === null
    ? "account classification unavailable"
    : `account: ${run.account_label}${run.account_confidence === null ? "" : ` (${run.account_confidence.toFixed(2)})`}`;
  if (run.status === "error") return {
    label: "shadow error", tone: "error",
    title: `${run.error_detail ?? "Shadow relevance run failed."}; ${account}`,
    details: run.details,
  };
  if (run.eligible === null) return {
    label: "shadow unavailable", tone: "warning",
    title: `${run.reason ?? "No eligibility decision recorded"}; ${account}`,
    details: run.details,
  };
  const decision = run.eligible ? "eligible" : "ineligible";
  const probability = run.p_eligible === null ? "" : ` (${run.p_eligible.toFixed(2)})`;
  return {
    label: run.uncertain ? `shadow ${decision}?` : `shadow ${decision}`,
    tone: run.uncertain ? "warning" : run.eligible ? "positive" : "negative",
    title: `${run.reason ?? "No reason recorded"}${probability}; ${account}`,
    details: run.details,
  };
}
