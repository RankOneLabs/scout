import { describe, expect, it } from "vitest";
import { recordedEvaluationSchema, rejectedInputSchema } from "@/types/review-queues";

// The review-queue views render the recorded population an artifact carries,
// read through these schemas. What the schemas do not name, they strip — so
// the strip is the blind projection, and this file holds it. A producer that
// later writes classifier metadata into the artifact cannot make it appear
// in a queue view without someone adding the field here on purpose.

const PRIVATE_KEYS = [
  "classifier",
  "action",
  "recorded_action",
  "relevance_decision",
  "catalogue_id",
  "catalogue_version",
  "router_version",
  "answers",
  "holdout_id",
  "release_action",
  "release_authority",
  "label",
  "production_decision",
  "production_score",
  "surface_status",
] as const;

const recorded = {
  id: 14,
  post_id: 4,
  scan_id: 7,
  project_key: "agent-ops",
  reason: "a recorded reason",
  relevant: 0,
  posture: null,
  dossier_revision: null,
  dossier_summary_id: null,
};

describe("the blind projection a review queue renders", () => {
  it("names no classifier, holdout or release field", () => {
    expect(Object.keys(recordedEvaluationSchema.parse(recorded))).toEqual([
      "id",
      "post_id",
      "scan_id",
      "project_key",
      "reason",
      "relevant",
      "posture",
      "dossier_revision",
      "dossier_summary_id",
    ]);
  });

  it("strips every private key a producer might add later", () => {
    const contaminated = {
      ...recorded,
      ...Object.fromEntries(PRIVATE_KEYS.map((key) => [key, "leaked"])),
    };
    const projected = recordedEvaluationSchema.parse(contaminated);
    for (const key of PRIVATE_KEYS) {
      expect(projected).not.toHaveProperty(key);
    }
  });

  it("strips them from the population entry the queue reads, too", () => {
    const projected = rejectedInputSchema.parse({
      evaluation: { ...recorded, classifier: "jev", action: "review" },
      post: null,
      context: null,
      has_grade: false,
    });
    expect(projected.evaluation).not.toHaveProperty("classifier");
    expect(projected.evaluation).not.toHaveProperty("action");
  });
});
