import { describe, expect, it } from "vitest";
import { recordedEvaluationSchema, rejectedInputSchema } from "@/types/review-queues";

// The grading-assistance review queue is a **sighted** surface: a reviewer
// working it sees the post, the recorded reason, the relevance judgment and
// the dossier context. Nothing here is a blind projection, and this file
// makes no such claim.
//
// What it does assert is narrower and still worth holding: classifier,
// holdout and release metadata are excluded from the population these views
// read. Grading assistance asks a reviewer to judge a reply, not to
// re-adjudicate what the classifier decided, so showing the recorded action
// beside the reply would put a production decision in front of the person
// being asked to assess one. The schemas name none of those fields, and zod
// strips what a schema does not name, so a producer that later writes
// classifier metadata into the artifact cannot surface it in a queue view
// without someone adding the field here on purpose.
//
// The actual blind projection — what an external reviewer is handed, with
// the post and nothing production decided — is
// `scout.holdouts.export.blind_case` / `render_blind_jsonl`, allowlisted by
// `BLIND_CASE_FIELDS` and proved in tests/test_holdout_lifecycle.py
// (`test_the_blind_projection_carries_no_classifier_metadata` and
// `test_the_blind_projection_drops_what_the_full_export_keeps`). The web UI
// renders no blind surface at all: every view it serves is an operator view.

const EXCLUDED_KEYS = [
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

describe("the recorded population a sighted review queue reads", () => {
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

  it("strips every excluded key a producer might add later", () => {
    const contaminated = {
      ...recorded,
      ...Object.fromEntries(EXCLUDED_KEYS.map((key) => [key, "leaked"])),
    };
    const projected = recordedEvaluationSchema.parse(contaminated);
    for (const key of EXCLUDED_KEYS) {
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

  it("keeps the sighted fields a reviewer is meant to see", () => {
    const projected = recordedEvaluationSchema.parse(recorded);
    expect(projected.reason).toBe("a recorded reason");
    expect(projected.project_key).toBe("agent-ops");
  });
});
