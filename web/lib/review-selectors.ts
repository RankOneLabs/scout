import type { Grade, ShadowRelevanceRunRow } from "@/types/schema";
import type {
  HoldoutLabel,
  RelevanceAction,
  RelevanceProvenance,
  ReleaseAuthority,
} from "@/lib/transforms";
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

// Relevance provenance selectors
//
// Three facts, three selectors, deliberately never merged into one summary
// string: the classifier's recorded action, the hold's release authority,
// and — read from the target's side — which hold a released evaluation came
// out of. What actually happened is the evaluation's own `surface_status`,
// which none of these reads. See docs/relevance-holdouts.md.

export interface RelevanceActionBadgeViewModel {
  label: string;
  tone: "positive" | "neutral" | "negative";
  title: string;
}

const ACTION_TONE: Record<RelevanceAction, RelevanceActionBadgeViewModel["tone"]> = {
  respond: "positive",
  review: "neutral",
  drop: "negative",
};

const ACTION_MEANING: Record<RelevanceAction, string> = {
  respond: "The classifier read this as answerable from the post.",
  review:
    "The classifier asked for a human look. Review is classifier metadata — " +
    "it is not a promise that anything was surfaced.",
  drop: "The classifier read this as not worth a reply.",
};

/** The source classifier's recorded action, as a badge.
 *
 * Null when nothing recorded a classifier for this evaluation: a historical
 * row, or a released target whose authority is its hold rather than a
 * classifier run. A null is unknown and reads as unknown — never guessed. */
export function selectRelevanceActionBadge(
  provenance: RelevanceProvenance | null | undefined
): RelevanceActionBadgeViewModel | null {
  const decision = provenance?.decision;
  if (!decision) return null;
  const catalogue =
    decision.catalogue_version === null
      ? ""
      : `; catalogue ${decision.catalogue_id ?? "unnamed"} @ ${decision.catalogue_version.slice(0, 12)}`;
  return {
    label: `${decision.classifier} · ${decision.action}`,
    tone: ACTION_TONE[decision.action],
    title: `${ACTION_MEANING[decision.action]} Recorded by ${decision.classifier} (${decision.model})${catalogue}.`,
  };
}

/** Where one evaluation sits in the holdout lifecycle, from its own side. */
export type HoldProvenanceView =
  | { kind: "not_held" }
  | { kind: "awaiting_release"; status: "pending" | "claimed"; held_at: string; attempts: number }
  | { kind: "release_failed"; held_at: string; attempts: number; last_error: string | null }
  | {
      kind: "released";
      authority: ReleaseAuthority;
      action: RelevanceAction;
      label: HoldoutLabel | null;
      label_source: string | null;
      released_at: string | null;
      target_evaluation_id: number | null;
    };

/** Read the hold this evaluation is the *source* of.
 *
 * A released hold reports the authority and the action it released as. What
 * the released target then did is the target's `surface_status`, read
 * separately — a release that drafted may still have been rejected by the
 * critic or blocked by a gate. */
export function selectHoldProvenance(
  provenance: RelevanceProvenance | null | undefined
): HoldProvenanceView {
  const holdout = provenance?.holdout;
  if (!holdout) return { kind: "not_held" };
  if (holdout.status === "failed") {
    return {
      kind: "release_failed",
      held_at: holdout.held_at,
      attempts: holdout.attempts,
      last_error: holdout.last_error,
    };
  }
  if (holdout.status !== "released") {
    return {
      kind: "awaiting_release",
      status: holdout.status,
      held_at: holdout.held_at,
      attempts: holdout.attempts,
    };
  }
  if (holdout.release_authority === null || holdout.release_action === null) {
    // A released row is constrained to carry both. Reading a partial one as
    // a completed release would invent an authority nobody recorded.
    return {
      kind: "release_failed",
      held_at: holdout.held_at,
      attempts: holdout.attempts,
      last_error: "released without a recorded authority",
    };
  }
  return {
    kind: "released",
    authority: holdout.release_authority,
    action: holdout.release_action,
    label: holdout.label,
    label_source: holdout.label_source,
    released_at: holdout.released_at,
    target_evaluation_id: holdout.target_evaluation_id,
  };
}

export interface ReleaseOriginViewModel {
  label: string;
  title: string;
  source_evaluation_id: number;
}

/** Read the hold this evaluation is the released *target* of.
 *
 * Null for an ordinary evaluation. Present, this says what authority put the
 * evaluation here — a blind label, or the action the classifier recorded —
 * without saying anything about what happened to it afterwards. */
export function selectReleaseOrigin(
  provenance: RelevanceProvenance | null | undefined
): ReleaseOriginViewModel | null {
  const origin = provenance?.released_from;
  if (!origin) return null;
  const authority =
    origin.release_authority === "label"
      ? `blind label ${origin.label ?? "unrecorded"}`
      : "the recorded classifier action";
  return {
    label: `released · ${origin.release_action}`,
    title:
      `Released from holdout ${origin.holdout_id} (evaluation ${origin.source_evaluation_id}) ` +
      `on ${authority}${origin.label_source === null ? "" : ` (${origin.label_source})`}. ` +
      "The release authority, not the outcome.",
    source_evaluation_id: origin.source_evaluation_id,
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
