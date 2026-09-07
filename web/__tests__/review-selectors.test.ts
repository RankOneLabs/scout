import { describe, expect, it } from "vitest";
import { selectReviewCosts, selectReviewStatus, selectReviewScore } from "@/lib/review-selectors";
import type { ReviewDisposition } from "@/types/review-queues";

const observation: ReviewDisposition = {
  format: "scout.review-disposition/v1", action_id: "synthetic", queue_digest: "a".repeat(64),
  evaluation_id: 101, grade_revision_id: null, action: { kind: "skip", reason: "Need context" },
  recorded_at: "2026-09-05T00:00:00Z", timing: { elapsed_ms: 1000, method: "active-visible-idle60/v1" },
  pricing: { usd_per_hour: 36, basis: "Synthetic" },
};
describe("queue review projections", () => {
  it("labels cosine evidence as positive similarity, never a classifier probability", () => {
    expect(selectReviewScore({ evaluation_id: 1, similarity: 0.75, explanation: [{ term: "agent", contribution: 0.75 }] })).toEqual({
      summary: "Similarity to confirmed positives: 0.750 (cosine similarity, not a relevance probability or human label)",
      explanation: "TF-IDF × positive centroid: agent (0.750)",
    });
  });
  it("keeps legacy classifier labeling", () => {
    expect(selectReviewScore({ evaluation_id: 1, probability: 0.75, explanation: [] })?.summary).toContain("Selector probability: 0.750");
  });
  it.each([
    { evaluation_id: 1, similarity: 0, explanation: [] },
    { evaluation_id: 1, probability: 0.5, explanation: [] },
  ])("shows a fallback for empty term explanations: %j", (score) => {
    expect(selectReviewScore(score)?.explanation).toMatch(/: no shared terms$/);
  });
  it("does not manufacture score evidence for random-only items", () => {
    expect(selectReviewScore(null)).toBeNull();
  });
  it("deduplicates action IDs, counts skip time, and does not price unavailable time", () => {
    const unavailable = { ...observation, action_id: "missing", timing: { elapsed_ms: null, method: null } };
    expect(selectReviewCosts([observation, observation, unavailable])).toEqual({
      action_ids: ["synthetic", "missing"], measured_action_count: 1, unavailable_action_count: 1,
      elapsed_ms: 1000, priced_elapsed_ms: 1000, estimated_usd: 0.01, purpose: "corpus_building",
    });
  });
  it("never treats reconciliation as measured labor", () => {
    expect(selectReviewCosts([{ ...observation, action: { kind: "reconcile" }, timing: { elapsed_ms: null, method: null }, pricing: null }]).action_ids).toEqual([]);
  });
  it("skip does not masquerade as a grade", () => {
    expect(selectReviewStatus({ grade: null, revisionId: null, disposition: observation })).toBe("skipped");
  });
});
