import { createHash } from "node:crypto";
import Database from "better-sqlite3";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getReviewQueue, listReviewQueues } from "@/lib/review-queue-queries";
import type { ReviewQueue } from "@/types/review-queues";

let db: Database.Database;
vi.mock("@/lib/db", () => ({ getDb: () => db }));
beforeEach(() => {
  db = new Database(":memory:");
  db.exec(`
    CREATE TABLE analysis_artifacts(digest TEXT PRIMARY KEY, content BLOB, recorded_at TEXT);
    CREATE TABLE analysis_lineage(digest TEXT PRIMARY KEY);
    CREATE TABLE review_dispositions(sequence INTEGER PRIMARY KEY, queue_digest TEXT, disposition_json TEXT);
    CREATE TABLE grades(id INTEGER PRIMARY KEY, evaluation_id INTEGER, schema_version INTEGER, needs_regrade INTEGER, relevance_judgment TEXT, dimensions TEXT,
      post_id INTEGER, scan_id INTEGER, source TEXT, graded_at TEXT, rejection_reason TEXT,
      comment_quality INTEGER, comment_issue TEXT, action_judgment TEXT, failure_note TEXT,
      factual_offending_claim TEXT, factual_disposition TEXT, factual_contradicting_evidence TEXT,
      context_missing_input TEXT, posture_should_have_been TEXT, implication_implied_claim TEXT,
      implication_missing_support TEXT, reply_revision_id INTEGER);
    CREATE TABLE grade_revisions(id INTEGER PRIMARY KEY, grade_id INTEGER, evaluation_id INTEGER, revision INTEGER, payload TEXT);
    CREATE TABLE reply_draft_revisions(id INTEGER PRIMARY KEY, reply_text TEXT);
  `);
});
afterEach(() => db.close());

function retain(value: unknown): string {
  const content = Buffer.from(JSON.stringify(value));
  const digest = createHash("sha256").update(content).digest("hex");
  db.prepare("INSERT OR IGNORE INTO analysis_artifacts VALUES (?, ?, ?)").run(digest, content, "2026-09-05T00:00:00Z");
  return digest;
}

function seedQueue(version: "1" | "2" = "2") {
  const post = { id: 456, content: "Synthetic post", url: null, author_name: "Synthetic author", channel_name: "Test", parent_text: "Recorded parent", parent_author_name: "Parent" };
  const context = {
    summary: { project_key: "synthetic", last_reviewed: "2026-01-01", reviewer: "Synthetic", facts: [], resources: [], prohibitions: [], references: [] },
    metadata: { project_key: "synthetic", summary_id: "summary", revision: "a".repeat(40), path: "synthetic.yaml" }, known_gaps: [],
  };
  const evaluation = { id: 123, post_id: 456, relevant: 0, scan_id: 4, reason: "Recorded rejection", project_key: "synthetic", posture: "abstain", dossier_revision: "a".repeat(40), dossier_summary_id: "summary" };
  const postDigest = retain(post);
  const contextDigest = retain(context);
  const inputDigest = retain({ format: "scout.rejected-input/v2", evaluation, post_digest: postDigest, context_digest: contextDigest, has_grade: false });
  const populationDigest = version === "2" ? retain({ format: "scout.rejected-population/v2", items: [inputDigest], grouping_posts: [], project_key: "synthetic" })
    : retain({ format: "scout.rejected-population/v1", project_key: "synthetic", items: [{ evaluation, post, context, has_grade: false }] });
  const queue: ReviewQueue = { format: "scout.review-queue/v1", project_key: "synthetic", population_digest: populationDigest,
    items: [{ duplicate_key: "a".repeat(64), sources: [{ evaluation_id: 123, ranked_position: null, random_position: 1 }] }],
    ranked: null, random: { kind: "seeded_random", population_evaluation_ids: [123], selected_evaluation_ids: [123], scores: [], explanation_method: null } };
  const digest = retain(queue);
  const lineageDigest = retain({ kind: "scout.grading.assistance", inputs: ["a".repeat(64), populationDigest],
    outputs: ["b".repeat(64), "c".repeat(64), digest, "d".repeat(64)], process: { id: "scout.grading.assistance", version } });
  db.prepare("INSERT INTO analysis_lineage VALUES (?)").run(lineageDigest);
  return { digest, postDigest, contextDigest };
}

function seedGrade() {
  // Mirrors the Python revision payload, independently of the projection query.
  const payload = {
    id: 1, evaluation_id: 123, schema_version: 3, needs_regrade: 0,
    relevance_judgment: "correct", dimensions: ["usefulness", "factual"],
    post_id: 456, scan_id: null, source: "web", graded_at: "2026-09-05T00:00:00Z",
    rejection_reason: null, comment_quality: null, comment_issue: null,
    action_judgment: "fail", failure_note: "Synthetic note", factual_offending_claim: null,
    factual_disposition: null, factual_contradicting_evidence: null, context_missing_input: null,
    posture_should_have_been: null, implication_implied_claim: null,
    implication_missing_support: null, reply_revision_id: 2, edited_text: "Pinned correction",
  };
  db.prepare(`INSERT INTO grades (id, evaluation_id, schema_version, needs_regrade,
    relevance_judgment, dimensions, post_id, source, graded_at, action_judgment, failure_note, reply_revision_id)
    VALUES (1, 123, 3, 0, 'correct', ?, 456, 'web', ?, 'fail', 'Synthetic note', 2)`)
    .run(JSON.stringify(payload.dimensions), payload.graded_at);
  db.prepare("INSERT INTO reply_draft_revisions VALUES (2, ?)").run(payload.edited_text);
  db.prepare("INSERT INTO grade_revisions VALUES (9, 1, 123, 1, ?)").run(JSON.stringify(payload));
}

describe("retained queue projection", () => {
  it.each(["1", "2"] as const)("reads frozen post/context and exact evaluation for producer %s", (version) => {
    const { digest } = seedQueue(version);
    const result = getReviewQueue(digest);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.value.items[0].recorded.post?.content).toBe("Synthetic post");
    expect(result.value.items[0].source.evaluation_id).toBe(123);
    expect(result.value.items[0].status).toBe("pending");
  });
  it("lists queues without loading the population or dossier blobs", () => {
    const { contextDigest } = seedQueue();
    db.prepare("DELETE FROM analysis_artifacts WHERE digest = ?").run(contextDigest);
    const result = listReviewQueues("synthetic");
    expect(result.ok && result.value[0].source_count).toBe(1);
    expect(listReviewQueues("other")).toEqual({ ok: true, value: [] });
  });
  it("fails detail on a corrupt context instead of silently showing live context", () => {
    const { digest, contextDigest } = seedQueue();
    db.prepare("UPDATE analysis_artifacts SET content = ? WHERE digest = ?").run(Buffer.from("{}"), contextDigest);
    expect(getReviewQueue(digest).ok).toBe(false);
  });
  it("detects an external grade by the current immutable revision", () => {
    const { digest } = seedQueue();
    seedGrade();
    const result = getReviewQueue(digest);
    expect(result.ok && result.value.items[0].status).toBe("graded_elsewhere");
    expect(result.ok && result.value.items[0].current_revision_id).toBe(9);
  });
  it.each([
    "DELETE FROM grade_revisions",
    "UPDATE grades SET failure_note = 'Drifted'",
    "UPDATE reply_draft_revisions SET reply_text = 'Drifted correction'",
    "UPDATE grade_revisions SET payload = 'invalid json'",
  ])("surfaces revision damage as needs_regrade: %s", (damage) => {
    const { digest } = seedQueue();
    seedGrade();
    db.exec(damage);
    const result = getReviewQueue(digest);
    expect(result.ok && result.value.items[0].status).toBe("needs_regrade");
    const list = listReviewQueues("synthetic");
    expect(list.ok && list.value[0].graded_elsewhere).toBe(0);
  });
});
