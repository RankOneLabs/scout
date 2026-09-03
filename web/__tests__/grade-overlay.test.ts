import { describe, expect, it } from "vitest";
import { overlayGradesByEvaluation } from "@/lib/grade-overlay";
import type { DraftWithGrade, Grade } from "@/types/schema";

function makeGrade(evaluationId: number, judgment: Grade["relevance_judgment"]): Grade {
  return {
    id: evaluationId,
    evaluation_id: evaluationId,
    post_id: 10,
    scan_id: 7,
    source: "web",
    graded_at: "2026-05-15T00:00:00+00:00",
    schema_version: 2,
    needs_regrade: 0,
    relevance_judgment: judgment,
    action_judgment: null,
    dimensions: null,
    failure_note: null,
    factual_offending_claim: null,
    factual_disposition: null,
    factual_contradicting_evidence: null,
    context_missing_input: null,
    posture_should_have_been: null,
    implication_implied_claim: null,
    implication_missing_support: null,
    reply_revision_id: null,
  };
}

function makeDraft(
  draftId: number,
  postId: number,
  evaluationId: number,
  grade: Grade | null
): DraftWithGrade {
  return {
    draft_id: draftId,
    evaluation_id: evaluationId,
    project_key: "gateway",
    comment_text: `draft ${draftId}`,
    draft_created_at: "2026-05-15T00:00:00+00:00",
    verdict: null,
    feedback: null,
    post_id: postId,
    platform: "discord",
    author_name: "alice",
    author_id: "user-alice",
    content: "post",
    url: null,
    score: 0.9,
    scan_id: 7,
    keyword_route_id: null,
    matched_route: null,
    parent_lookup_status: "not_applicable",
    parent: null,
    relevant: true,
    grade,
  };
}

describe("overlayGradesByEvaluation", () => {
  it("overlays local grades onto the latest fetched draft list", () => {
    const localGrade = makeGrade(100, "correct");
    const freshDrafts = [
      makeDraft(1, 10, 100, null),
      makeDraft(2, 11, 101, makeGrade(101, "false_positive")),
    ];

    const result = overlayGradesByEvaluation(
      freshDrafts,
      new Map([[100, localGrade]])
    );

    expect(result).toHaveLength(2);
    expect(result[0].grade).toBe(localGrade);
    expect(result[1]).toBe(freshDrafts[1]);
    expect(result[1].grade?.relevance_judgment).toBe("false_positive");
  });

  it("keys by evaluation_id, not post_id, so grading one evaluation of a rescanned post leaves the other untouched", () => {
    // Both drafts share post_id 10 (the same post surfaced across two
    // scans) but carry distinct evaluation_ids. Only the draft whose
    // evaluation_id matches the overlay entry should repaint.
    const targetedGrade = makeGrade(200, "correct");
    const drafts = [
      makeDraft(1, 10, 200, null),
      makeDraft(2, 10, 201, null),
    ];

    const result = overlayGradesByEvaluation(
      drafts,
      new Map([[200, targetedGrade]])
    );

    expect(result[0].grade).toBe(targetedGrade);
    expect(result[1].grade).toBeNull();
    expect(result[1]).toBe(drafts[1]);
  });
});
