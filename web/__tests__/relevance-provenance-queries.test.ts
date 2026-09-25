import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// One synthetic database covering every row shape the status views have to
// tell apart: a JEV review that surfaced with a draft, a held positive, a
// held drop, a held review that released and was then rejected by the
// critic, the released target itself, and a legacy row nothing recorded a
// classifier for.
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-relevance-provenance-"));
const dbPath = path.join(tmpDir, "scout.db");
process.env.SCOUT_DB_PATH = dbPath;

const SCAN = 7;

beforeAll(() => {
  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE posts (id INTEGER PRIMARY KEY, platform TEXT NOT NULL, platform_msg_id TEXT NOT NULL,
      channel_name TEXT, channel_id TEXT, author_name TEXT, author_id TEXT, content TEXT, url TEXT,
      created_at TEXT, scan_id INTEGER, parent_lookup_status TEXT NOT NULL DEFAULT 'not_applicable',
      parent_id TEXT, parent_author_id TEXT, parent_author_name TEXT, parent_text TEXT, parent_url TEXT);
    CREATE TABLE evaluations (id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL, relevant INTEGER NOT NULL,
      score REAL NOT NULL, reason TEXT, relevant_to TEXT, keyword_route_id INTEGER, scan_id INTEGER,
      surface_status TEXT, posture TEXT, project_key TEXT, failure_reason TEXT,
      dossier_revision TEXT, dossier_summary_id TEXT);
    CREATE TABLE draft_comments (id INTEGER PRIMARY KEY, post_id INTEGER, evaluation_id INTEGER,
      project_key TEXT, comment_text TEXT, created_at TEXT, scan_id INTEGER, posture TEXT,
      structured_output TEXT, dossier_revision TEXT, dossier_summary_id TEXT);
    CREATE TABLE critiques (id INTEGER PRIMARY KEY, draft_id INTEGER, evaluation_id INTEGER,
      verdict TEXT, feedback TEXT, created_at TEXT, scan_id INTEGER);
    CREATE TABLE project_keywords (id INTEGER PRIMARY KEY, project_key TEXT NOT NULL, keyword TEXT NOT NULL,
      match_type TEXT, intent TEXT, positive_context TEXT, negative_context TEXT,
      evaluate_prompt TEXT, respond_prompt TEXT, critique_prompt TEXT);
    CREATE TABLE prompt_templates (name TEXT PRIMARY KEY, body TEXT NOT NULL, kind TEXT NOT NULL, active INTEGER NOT NULL);
    CREATE TABLE grades (id INTEGER PRIMARY KEY, evaluation_id INTEGER, post_id INTEGER, scan_id INTEGER,
      source TEXT, graded_at TEXT, schema_version INTEGER, needs_regrade INTEGER,
      relevance_judgment TEXT, action_judgment TEXT, dimensions TEXT, failure_note TEXT,
      factual_offending_claim TEXT, factual_disposition TEXT, factual_contradicting_evidence TEXT,
      context_missing_input TEXT, posture_should_have_been TEXT, implication_implied_claim TEXT,
      implication_missing_support TEXT, rejection_reason TEXT, comment_quality TEXT,
      comment_issue TEXT, reply_revision_id INTEGER);
    CREATE TABLE review_dispositions (queue_digest TEXT, evaluation_id INTEGER, sequence INTEGER,
      action_id TEXT, disposition_json TEXT NOT NULL);
    CREATE TABLE relevance_decisions (id INTEGER PRIMARY KEY, decision_uid TEXT NOT NULL,
      evaluation_id INTEGER NOT NULL, selected_for_holdout INTEGER NOT NULL DEFAULT 0,
      phase_run_id INTEGER, classifier TEXT NOT NULL, model TEXT NOT NULL, catalogue_id TEXT,
      catalogue_version TEXT, router_version TEXT, action TEXT NOT NULL, reason TEXT,
      answers_json TEXT, decision_json TEXT, created_at TEXT NOT NULL);
    CREATE TABLE relevance_holdouts (id INTEGER PRIMARY KEY, evaluation_id INTEGER NOT NULL,
      post_id INTEGER NOT NULL, scan_id INTEGER NOT NULL, project_key TEXT, status TEXT NOT NULL,
      held_at TEXT NOT NULL, released_at TEXT, frozen_input_json TEXT NOT NULL, claim_token TEXT,
      claim_fence INTEGER NOT NULL DEFAULT 0, claim_owner TEXT, claim_expires_at TEXT,
      release_authority TEXT, release_action TEXT, label TEXT, label_source TEXT,
      label_provenance_json TEXT, key_provenance_json TEXT, labelled_at TEXT,
      target_evaluation_id INTEGER, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT);

    INSERT INTO posts (id, platform, platform_msg_id, content, author_id, author_name, scan_id) VALUES
      (1, 'farcaster', 'm-review-live', 'a routed post', 'a1', 'Ada', ${SCAN}),
      (2, 'farcaster', 'm-held-respond', 'a held positive', 'a2', 'Bea', ${SCAN}),
      (3, 'farcaster', 'm-held-drop', 'a held drop', 'a3', 'Cy', ${SCAN}),
      (4, 'farcaster', 'm-held-review', 'a held review', 'a4', 'Di', ${SCAN}),
      (5, 'farcaster', 'm-legacy', 'a pre-JEV post', 'a5', 'Eli', ${SCAN});

    INSERT INTO evaluations (id, post_id, relevant, score, scan_id, surface_status, project_key) VALUES
      (11, 1, 1, 1.0, ${SCAN}, 'surfaced', 'agent-ops'),
      (12, 2, 1, 1.0, ${SCAN}, 'held', 'agent-ops'),
      (13, 3, 0, 0.0, ${SCAN}, 'held', 'agent-ops'),
      (14, 4, 1, 1.0, ${SCAN}, 'held', 'agent-ops'),
      (15, 4, 1, 1.0, ${SCAN}, 'critic_rejected', 'agent-ops'),
      (16, 5, 0, 0.2, ${SCAN}, 'not_relevant', 'agent-ops');

    INSERT INTO draft_comments (id, post_id, evaluation_id, project_key, comment_text, scan_id) VALUES
      (1, 1, 11, 'agent-ops', 'a surfaced reply', ${SCAN}),
      (2, 4, 15, 'agent-ops', 'a released reply the critic rejected', ${SCAN});
    INSERT INTO critiques (id, draft_id, evaluation_id, verdict, feedback, scan_id) VALUES
      (1, 2, 15, 'reject', 'off topic', ${SCAN});

    INSERT INTO relevance_decisions (id, decision_uid, evaluation_id, selected_for_holdout,
      classifier, model, catalogue_id, catalogue_version, router_version, action, reason, created_at) VALUES
      (1, 'uid-11', 11, 0, 'jev', 'jev-latest', 'agent-ops-relevance', 'ab12cd34ef56', 'agent_ops_route/v1', 'review', 'needs a thread', '2026-09-20T00:00:00Z'),
      (2, 'uid-12', 12, 1, 'jev', 'jev-latest', 'agent-ops-relevance', 'ab12cd34ef56', 'agent_ops_route/v1', 'respond', 'answerable', '2026-09-20T00:01:00Z'),
      (3, 'uid-13', 13, 1, 'llm', 'test-model', NULL, NULL, NULL, 'drop', 'not relevant', '2026-09-20T00:02:00Z'),
      (4, 'uid-14', 14, 1, 'jev', 'jev-latest', 'agent-ops-relevance', 'ab12cd34ef56', 'agent_ops_route/v1', 'review', 'points somewhere', '2026-09-20T00:03:00Z');

    INSERT INTO relevance_holdouts (id, evaluation_id, post_id, scan_id, project_key, status, held_at,
      released_at, frozen_input_json, release_authority, release_action, label, label_source,
      target_evaluation_id, attempts, last_error) VALUES
      (1, 12, 2, ${SCAN}, 'agent-ops', 'pending', '2026-09-20T00:01:00Z', NULL, '{}', NULL, NULL, NULL, NULL, NULL, 0, NULL),
      (2, 13, 3, ${SCAN}, 'agent-ops', 'failed', '2026-09-20T00:02:00Z', NULL, '{}', NULL, NULL, NULL, NULL, NULL, 2, 'project was deleted'),
      (3, 14, 4, ${SCAN}, 'agent-ops', 'released', '2026-09-20T00:03:00Z', '2026-09-23T00:00:00Z', '{}', 'label', 'review', 'pointer', 'synthetic-sitting#case-3', 15, 1, NULL);
  `);
  db.close();
});

afterAll(() => fs.rmSync(tmpDir, { recursive: true, force: true }));

async function evaluationsByStatus() {
  const { getEvaluationsByScan } = await import("@/lib/queries");
  return new Map(getEvaluationsByScan(SCAN).map((evaluation) => [evaluation.id, evaluation]));
}

describe("relevance provenance in the review read model", () => {
  it("reads a surfaced JEV review's recorded action", async () => {
    const evaluations = await evaluationsByStatus();
    expect(evaluations.get(11)?.relevance_provenance?.decision).toMatchObject({
      classifier: "jev",
      action: "review",
      catalogue_version: "ab12cd34ef56",
      selected_for_holdout: false,
    });
  });

  it("keeps a surfaced review's action apart from its surface status", async () => {
    const evaluations = await evaluationsByStatus();
    const evaluation = evaluations.get(11);
    expect(evaluation?.surface_status).toBe("surfaced");
    expect(evaluation?.relevance_provenance?.decision?.action).toBe("review");
  });

  it("reads a held positive's pending hold", async () => {
    const evaluations = await evaluationsByStatus();
    expect(evaluations.get(12)?.relevance_provenance?.holdout).toMatchObject({
      status: "pending",
      release_authority: null,
      target_evaluation_id: null,
    });
  });

  it("reads a held drop as a hold, not as an irrelevance", async () => {
    const evaluations = await evaluationsByStatus();
    const evaluation = evaluations.get(13);
    expect(evaluation?.surface_status).toBe("held");
    expect(evaluation?.relevance_provenance?.decision?.action).toBe("drop");
  });

  it("reads a failed release attempt with its error", async () => {
    const evaluations = await evaluationsByStatus();
    expect(evaluations.get(13)?.relevance_provenance?.holdout).toMatchObject({
      status: "failed",
      attempts: 2,
      last_error: "project was deleted",
    });
  });

  it("reads a released hold's authority on the source evaluation", async () => {
    const evaluations = await evaluationsByStatus();
    expect(evaluations.get(14)?.relevance_provenance?.holdout).toMatchObject({
      status: "released",
      release_authority: "label",
      release_action: "review",
      label: "pointer",
      target_evaluation_id: 15,
    });
  });

  it("leaves a released source at held, never at its target's status", async () => {
    const evaluations = await evaluationsByStatus();
    expect(evaluations.get(14)?.surface_status).toBe("held");
  });

  it("reads the release authority on the released target", async () => {
    const evaluations = await evaluationsByStatus();
    expect(evaluations.get(15)?.relevance_provenance?.released_from).toMatchObject({
      holdout_id: 3,
      source_evaluation_id: 14,
      release_authority: "label",
      release_action: "review",
      label: "pointer",
    });
  });

  it("records no classifier of its own for a released target", async () => {
    const evaluations = await evaluationsByStatus();
    expect(evaluations.get(15)?.relevance_provenance?.decision).toBeNull();
  });

  it("keeps a suppressed release's authority apart from its outcome", async () => {
    const evaluations = await evaluationsByStatus();
    const target = evaluations.get(15);
    expect(target?.relevance_provenance?.released_from?.release_action).toBe("review");
    expect(target?.surface_status).toBe("critic_rejected");
  });

  it("reports a legacy row's classifier as unknown rather than guessing", async () => {
    const evaluations = await evaluationsByStatus();
    expect(evaluations.get(16)?.relevance_provenance).toEqual({
      evaluation_id: 16,
      decision: null,
      holdout: null,
      released_from: null,
    });
  });
});

describe("held rows in counts, filters and queues", () => {
  it("counts held apart from surfaced and drafting_failed", async () => {
    const { getEvaluationsByScan } = await import("@/lib/queries");
    const { selectSurfaceStatusCounts } = await import("@/lib/transforms");
    const counts = selectSurfaceStatusCounts(getEvaluationsByScan(SCAN));
    expect(counts).toMatchObject({ total: 6, held: 3, surfaced: 1, drafting_failed: 0 });
  });

  it("treats no held row as actionable for posting", async () => {
    const { getEvaluationsByScan } = await import("@/lib/queries");
    const { selectSurfaceStatusCounts } = await import("@/lib/transforms");
    expect(selectSurfaceStatusCounts(getEvaluationsByScan(SCAN)).actionable).toBe(1);
  });

  it("returns held rows in the scan population rather than hiding them", async () => {
    const { getEvaluationsByScan } = await import("@/lib/queries");
    const { isHeld } = await import("@/lib/transforms");
    const held = getEvaluationsByScan(SCAN)
      .filter((row) => isHeld(row.surface_status))
      .map((row) => row.id)
      .sort();
    expect(held).toEqual([12, 13, 14]);
  });

  it("reads held as its own status, never as actionable", async () => {
    const { getEvaluationsByScan } = await import("@/lib/queries");
    const { isActionableForPosting } = await import("@/lib/transforms");
    const actionable = getEvaluationsByScan(SCAN).filter((row) =>
      isActionableForPosting(row.surface_status)
    );
    expect(actionable.map((row) => row.id)).toEqual([11]);
  });

  it("puts no held evaluation in the draft queue", async () => {
    const { getDrafts } = await import("@/lib/queries");
    const drafted = getDrafts().data.map((draft) => draft.evaluation_id);
    expect(drafted).not.toContain(12);
    expect(drafted).not.toContain(13);
    expect(drafted).not.toContain(14);
  });

  it("carries the release authority onto the draft a release produced", async () => {
    const { getDrafts } = await import("@/lib/queries");
    const released = getDrafts().data.find((draft) => draft.evaluation_id === 15);
    expect(released?.relevance_provenance?.released_from).toMatchObject({
      release_authority: "label",
      release_action: "review",
    });
  });

  it("carries the recorded action onto a graded draft", async () => {
    const { getDraftsWithGrades } = await import("@/lib/queries");
    const surfaced = getDraftsWithGrades().data.find((draft) => draft.evaluation_id === 11);
    expect(surfaced?.relevance_provenance?.decision?.action).toBe("review");
  });
});

describe("evaluation review provenance", () => {
  it("returns the relevance provenance beside the grading-assistance trail", async () => {
    const { listEvaluationReviews } = await import("@/lib/review-queue-queries");
    const result = listEvaluationReviews(14);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.value.actions).toEqual([]);
    expect(result.value.relevance?.holdout?.release_action).toBe("review");
  });
});
