import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// Fixed instant so eligibility classification (lookback boundary, cap) is
// deterministic across runs. lookback_days=90 -> boundary 2025-11-03.
const AS_OF = "2026-02-01T00:00:00.000Z";

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-feedback-grades-"));
const dbPath = path.join(tmpDir, "scout.db");
process.env.SCOUT_DB_PATH = dbPath;

function revisionPayload(gradeId: number, gradedAt: string): string {
  return JSON.stringify({ id: gradeId, graded_at: gradedAt });
}

beforeAll(() => {
  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE scans (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL);
    CREATE TABLE posts (
      id INTEGER PRIMARY KEY, platform TEXT NOT NULL, platform_msg_id TEXT NOT NULL,
      author_name TEXT, author_id TEXT, content TEXT, url TEXT, created_at TEXT, scan_id INTEGER
    );
    CREATE TABLE evaluations (
      id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL, scan_id INTEGER,
      relevant INTEGER NOT NULL, score REAL NOT NULL, reason TEXT,
      project_key TEXT, posture TEXT, surface_status TEXT, failure_reason TEXT,
      dossier_summary_id TEXT, dossier_revision TEXT
    );
    CREATE TABLE draft_comments (
      id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL, evaluation_id INTEGER NOT NULL,
      project_key TEXT, comment_text TEXT, created_at TEXT, posture TEXT,
      dossier_summary_id TEXT, dossier_revision TEXT
    );
    CREATE TABLE critiques (
      id INTEGER PRIMARY KEY, draft_id INTEGER, evaluation_id INTEGER,
      verdict TEXT NOT NULL, feedback TEXT, created_at TEXT, scan_id INTEGER
    );
    CREATE TABLE grades (
      id INTEGER PRIMARY KEY, evaluation_id INTEGER, post_id INTEGER NOT NULL, scan_id INTEGER,
      source TEXT NOT NULL, graded_at TEXT NOT NULL, schema_version INTEGER NOT NULL DEFAULT 1,
      needs_regrade INTEGER NOT NULL DEFAULT 0,
      relevance_judgment TEXT NOT NULL, action_judgment TEXT, dimensions TEXT, failure_note TEXT,
      factual_offending_claim TEXT, factual_disposition TEXT, factual_contradicting_evidence TEXT,
      context_missing_input TEXT, posture_should_have_been TEXT,
      implication_implied_claim TEXT, implication_missing_support TEXT
    );
    CREATE TABLE grade_revisions (
      id INTEGER PRIMARY KEY, grade_id INTEGER NOT NULL, evaluation_id INTEGER, revision INTEGER NOT NULL,
      schema_version INTEGER NOT NULL DEFAULT 3,
      source TEXT NOT NULL, payload TEXT NOT NULL, recorded_at TEXT NOT NULL
    );
    CREATE TABLE grade_usage_overrides (
      id INTEGER PRIMARY KEY, grade_id INTEGER NOT NULL UNIQUE, mode TEXT NOT NULL,
      reason TEXT, updated_at TEXT NOT NULL
    );
    CREATE TABLE feedback_snapshots (
      id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL UNIQUE, policy_version TEXT NOT NULL,
      mode TEXT NOT NULL, as_of TEXT NOT NULL, lookback_days INTEGER NOT NULL, max_grades INTEGER NOT NULL,
      segment_min_grades INTEGER NOT NULL, note_max_chars INTEGER NOT NULL,
      relevance_token_budget INTEGER NOT NULL DEFAULT 800, reply_draft_token_budget INTEGER NOT NULL DEFAULT 800,
      critic_token_budget INTEGER NOT NULL DEFAULT 1000,
      population_count INTEGER NOT NULL, eligible_count INTEGER NOT NULL, excluded_count INTEGER NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE TABLE feedback_snapshot_phases (
      id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, phase TEXT NOT NULL,
      token_budget INTEGER NOT NULL, token_estimate INTEGER NOT NULL, truncated INTEGER NOT NULL,
      structured_summary TEXT NOT NULL, rendered_text TEXT NOT NULL, rendered_sha256 TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE TABLE feedback_snapshot_items (
      id INTEGER PRIMARY KEY, snapshot_phase_id INTEGER NOT NULL, grade_id INTEGER NOT NULL,
      grade_revision_id INTEGER NOT NULL, role TEXT NOT NULL, reason TEXT,
      selection_reason TEXT NOT NULL, rank INTEGER, created_at TEXT NOT NULL
    );
    CREATE TABLE evaluation_phase_runs (
      id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL, post_id INTEGER NOT NULL,
      evaluation_id INTEGER, snapshot_phase_id INTEGER NOT NULL, phase TEXT NOT NULL,
      trace_id TEXT NOT NULL UNIQUE, model TEXT NOT NULL, status TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
  `);

  db.prepare(`INSERT INTO scans (id, started_at) VALUES (?, ?)`).run(500, "2026-01-01T00:00:00.000Z");
  db.prepare(`INSERT INTO scans (id, started_at) VALUES (?, ?)`).run(501, "2026-01-02T00:00:00.000Z");

  const insertPost = db.prepare(
    `INSERT INTO posts (id, platform, platform_msg_id, scan_id) VALUES (?, ?, ?, ?)`
  );
  const platforms: Record<number, string> = {
    1: "discord", 2: "farcaster", 3: "discord", 4: "bluesky", 5: "discord",
    6: "discord", 7: "discord", 8: "discord", 9: "discord", 11: "farcaster",
  };
  for (const [id, platform] of Object.entries(platforms)) {
    insertPost.run(Number(id), platform, `msg-${id}`, 500);
  }

  const insertEval = db.prepare(
    `INSERT INTO evaluations
       (id, post_id, scan_id, relevant, score, project_key, posture, surface_status,
        dossier_summary_id, dossier_revision)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  );
  insertEval.run(10, 1, 500, 1, 0.9, "acme", "engage", "surfaced", "d1", "r1");
  insertEval.run(11, 2, 500, 1, 0.8, "acme", "answer", "surfaced", null, null);
  insertEval.run(12, 3, 501, 0, 0.3, "beta", "abstain", "not_relevant", null, null);
  insertEval.run(13, 5, 500, 1, 0.7, "acme", "engage", "surfaced", null, null);
  insertEval.run(14, 6, 500, 1, 0.7, "acme", "engage", "surfaced", null, null);
  insertEval.run(15, 7, 501, 1, 0.7, "acme", "engage", "surfaced", null, null);
  insertEval.run(16, 8, 500, 1, 0.7, "acme", "engage", "surfaced", null, null);
  // eval17's scan (500) intentionally disagrees with grade9's own scan_id (501).
  insertEval.run(17, 9, 500, 1, 0.7, "acme", "engage", "surfaced", null, null);
  insertEval.run(18, 11, 500, 1, 0.7, "acme", "engage", "surfaced", "d1", "r1");

  // draft_comments for eval10 (matches contract) and eval18 (mismatched project_key).
  db.prepare(
    `INSERT INTO draft_comments
       (id, post_id, evaluation_id, project_key, comment_text, created_at, posture,
        dossier_summary_id, dossier_revision)
     VALUES (8001, 1, 10, 'acme', 'Great point!', '2026-01-10T00:00:01.000Z', 'engage', 'd1', 'r1')`
  ).run();
  db.prepare(
    `INSERT INTO draft_comments
       (id, post_id, evaluation_id, project_key, comment_text, created_at, posture,
        dossier_summary_id, dossier_revision)
     VALUES (8002, 11, 18, 'mismatched-project', 'text', '2026-01-02T00:00:01.000Z', 'engage', 'd1', 'r1')`
  ).run();

  db.prepare(
    `INSERT INTO critiques (id, draft_id, evaluation_id, verdict, feedback, created_at, scan_id)
     VALUES (5001, NULL, 10, 'approve', 'Nice work', '2026-01-10T00:00:05.000Z', 500)`
  ).run();

  const insertGrade = db.prepare(
    `INSERT INTO grades
       (id, evaluation_id, post_id, scan_id, source, graded_at, schema_version, needs_regrade,
        relevance_judgment, action_judgment, dimensions)
     VALUES (?, ?, ?, ?, 'web', ?, ?, ?, ?, ?, ?)`
  );
  // id, evaluation_id, post_id, scan_id, graded_at, schema_version, needs_regrade, relevance_judgment, action_judgment, dimensions
  insertGrade.run(1, 10, 1, 500, "2026-01-10T00:00:00.000Z", 3, 0, "correct", "accept", null);
  insertGrade.run(2, 11, 2, 500, "2026-01-09T00:00:00.000Z", 3, 0, "correct", "fail", JSON.stringify(["tone", "usefulness"]));
  insertGrade.run(3, 16, 8, 500, "2026-01-08T00:00:00.000Z", 3, 0, "correct", null, null);
  insertGrade.run(4, 12, 3, 501, "2026-01-07T00:00:00.000Z", 1, 0, "correct", null, null);
  insertGrade.run(5, 13, 5, 500, "2025-01-01T00:00:00.000Z", 3, 0, "correct", null, null);
  insertGrade.run(6, 14, 6, 500, "2026-01-06T00:00:00.000Z", 3, 1, "correct", null, null);
  insertGrade.run(7, 15, 7, 501, "2026-01-05T00:00:00.000Z", 3, 0, "correct", null, null);
  insertGrade.run(8, null, 4, null, "2026-01-04T00:00:00.000Z", 3, 0, "false_negative", null, null);
  insertGrade.run(9, 17, 9, 501, "2026-01-03T00:00:00.000Z", 3, 0, "correct", null, null);
  insertGrade.run(10, 18, 11, 500, "2026-01-02T00:00:00.000Z", 3, 0, "correct", null, null);
  db.prepare(`UPDATE grades SET failure_note = 'tone failure' WHERE id = 2`).run();
  db.prepare(`UPDATE grades SET action_judgment = 'accept' WHERE id IN (3, 7, 9, 10)`).run();

  db.prepare(
    `INSERT INTO grade_usage_overrides (id, grade_id, mode, reason, updated_at)
     VALUES (1, 7, 'exclude', 'test exclude reason', '2026-01-05T01:00:00.000Z')`
  ).run();

  const insertRevision = db.prepare(
    `INSERT INTO grade_revisions (id, grade_id, evaluation_id, revision, source, payload, recorded_at)
     VALUES (?, ?, ?, ?, 'web', ?, ?)`
  );
  insertRevision.run(1001, 1, 10, 1, revisionPayload(1, "2026-01-08T00:00:00.000Z"), "2026-01-08T00:00:00.000Z");
  insertRevision.run(1002, 1, 10, 2, revisionPayload(1, "2026-01-09T00:00:00.000Z"), "2026-01-09T00:00:00.000Z");
  insertRevision.run(1003, 1, 10, 3, revisionPayload(1, "2026-01-10T00:00:00.000Z"), "2026-01-10T00:00:00.000Z");
  insertRevision.run(1004, 2, 11, 1, revisionPayload(2, "2026-01-09T00:00:00.000Z"), "2026-01-09T00:00:00.000Z");
  insertRevision.run(1005, 3, 16, 1, revisionPayload(3, "2026-01-08T00:00:00.000Z"), "2026-01-08T00:00:00.000Z");
  insertRevision.run(1006, 4, 12, 1, revisionPayload(4, "2026-01-07T00:00:00.000Z"), "2026-01-07T00:00:00.000Z");
  insertRevision.run(1007, 5, 13, 1, revisionPayload(5, "2025-01-01T00:00:00.000Z"), "2025-01-01T00:00:00.000Z");
  insertRevision.run(1008, 6, 14, 1, revisionPayload(6, "2026-01-06T00:00:00.000Z"), "2026-01-06T00:00:00.000Z");
  insertRevision.run(1009, 7, 15, 1, revisionPayload(7, "2026-01-05T00:00:00.000Z"), "2026-01-05T00:00:00.000Z");
  insertRevision.run(1010, 8, null, 1, revisionPayload(8, "2026-01-04T00:00:00.000Z"), "2026-01-04T00:00:00.000Z");
  // grade9 and grade10 share an identical last_edit_time to test keyset
  // pagination stability under equal timestamps.
  insertRevision.run(1011, 9, 17, 1, revisionPayload(9, "2026-01-03T00:00:00.000Z"), "2026-01-03T00:00:00.000Z");
  insertRevision.run(1012, 10, 18, 1, revisionPayload(10, "2026-01-02T00:00:00.000Z"), "2026-01-03T00:00:00.000Z");

  // Snapshot 900 (shadow, scan 500, older) and 901 (active, scan 501,
  // newest — resolves the policy and is the "latest snapshot").
  db.prepare(
    `INSERT INTO feedback_snapshots
       (id, scan_id, policy_version, mode, as_of, lookback_days, max_grades, segment_min_grades,
        note_max_chars, population_count, eligible_count, excluded_count, created_at)
     VALUES (900, 500, 'evaluation-feedback/v1', 'shadow', '2026-01-09T00:00:00.000Z', 90, 2, 1, 240,
             3, 2, 1, '2026-01-09T00:00:00.000Z')`
  ).run();
  db.prepare(
    `INSERT INTO feedback_snapshots
       (id, scan_id, policy_version, mode, as_of, lookback_days, max_grades, segment_min_grades,
        note_max_chars, population_count, eligible_count, excluded_count, created_at)
     VALUES (901, 501, 'evaluation-feedback/v1', 'active', '2026-01-11T00:00:00.000Z', 90, 2, 1, 240,
             3, 2, 1, '2026-01-11T00:00:00.000Z')`
  ).run();

  const insertPhase = db.prepare(
    `INSERT INTO feedback_snapshot_phases
       (id, snapshot_id, phase, token_budget, token_estimate, truncated,
        structured_summary, rendered_text, rendered_sha256, created_at)
     VALUES (?, ?, ?, 800, 10, 0, '{}', '', 'sha', ?)`
  );
  insertPhase.run(9000, 900, "relevance", "2026-01-09T00:00:01.000Z");
  insertPhase.run(9010, 901, "relevance", "2026-01-11T00:00:01.000Z");
  insertPhase.run(9011, 901, "reply_draft", "2026-01-11T00:00:01.000Z");
  insertPhase.run(9012, 901, "critic", "2026-01-11T00:00:01.000Z");

  const insertItem = db.prepare(
    `INSERT INTO feedback_snapshot_items
       (snapshot_phase_id, grade_id, grade_revision_id, role, reason, selection_reason, rank, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
  );
  insertItem.run(9000, 1, 1003, "aggregate", null, "phase_population", null, "2026-01-09T00:00:02.000Z");
  insertItem.run(9010, 1, 1003, "aggregate", null, "phase_population", null, "2026-01-11T00:00:02.000Z");
  insertItem.run(9011, 1, 1003, "aggregate", null, "phase_population", null, "2026-01-11T00:00:03.000Z");
  insertItem.run(9012, 1, 1003, "example", null, "selected_recent_note", 1, "2026-01-11T00:00:04.000Z");
  insertItem.run(9010, 2, 1004, "excluded", "eligible_cap", "eligible_cap", null, "2026-01-11T00:00:05.000Z");

  db.close();
});

afterAll(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
  delete process.env.SCOUT_DB_PATH;
});

describe("resolveFeedbackPolicy / computeEligibilityWindow", () => {
  it("resolves policy from the latest snapshot by created_at", async () => {
    const { resolveFeedbackPolicy } = await import("@/lib/feedback-grade-queries");
    expect(resolveFeedbackPolicy()).toEqual({
      policy_version: "evaluation-feedback/v1",
      lookback_days: 90,
      max_grades: 2,
    });
  });

  it("classifies the newest max_grades valid rows as eligible and the rest eligible_cap", async () => {
    const { computeEligibilityWindow } = await import("@/lib/feedback-grade-queries");
    const window = computeEligibilityWindow(AS_OF)!;
    expect(window.byGradeId.get(1)).toEqual({ status: "eligible", reason: null });
    expect(window.byGradeId.get(2)).toEqual({ status: "eligible", reason: null });
    expect(window.byGradeId.get(3)).toEqual({ status: "excluded", reason: "eligible_cap" });
  });

  it("classifies each of the six global exclusion reasons correctly", async () => {
    const { computeEligibilityWindow } = await import("@/lib/feedback-grade-queries");
    const window = computeEligibilityWindow(AS_OF)!;
    expect(window.byGradeId.get(4)).toEqual({ status: "excluded", reason: "schema_version" });
    expect(window.byGradeId.get(6)).toEqual({ status: "excluded", reason: "needs_regrade" });
    expect(window.byGradeId.get(8)).toEqual({
      status: "excluded",
      reason: "missing_evaluation_linkage",
    });
    expect(window.byGradeId.get(9)).toEqual({
      status: "excluded",
      reason: "mismatched_evaluation_identity",
    });
    expect(window.byGradeId.get(10)).toEqual({
      status: "excluded",
      reason: "shared_contract_invalid",
    });
    expect(window.byGradeId.get(7)).toEqual({ status: "excluded", reason: "manual_exclude" });
  });

  it("excludes out-of-window grades from the population entirely", async () => {
    const { computeEligibilityWindow } = await import("@/lib/feedback-grade-queries");
    const window = computeEligibilityWindow(AS_OF)!;
    expect(window.byGradeId.has(5)).toBe(false);
    expect(window.populationCount).toBe(9); // every grade except grade 5
  });

  it("resolveEligibilityReason reports outside_lookback for a grade older than the boundary", async () => {
    const { computeEligibilityWindow, resolveEligibilityReason } = await import(
      "@/lib/feedback-grade-queries"
    );
    const window = computeEligibilityWindow(AS_OF);
    expect(resolveEligibilityReason(window, 5, "2025-01-01T00:00:00.000Z")).toBe(
      "outside_lookback"
    );
  });

});

describe("listGrades", () => {
  it("returns every grade including the unlinked one, addressed only by grade_id", async () => {
    const { listGrades } = await import("@/lib/feedback-grade-queries");
    const page = listGrades({ limit: 100 }, AS_OF);
    expect(page.total_matching).toBe(10);
    const unlinked = page.data.find((r) => r.grade_id === 8)!;
    expect(unlinked.evaluation_id).toBeNull();
    expect(unlinked.eligibility_reason).toBe("missing_evaluation_linkage");
    expect(unlinked.post_id).toBe(4);
  });

  it("orders by last_edit_time desc then grade_id desc, paginating without skip/duplicate", async () => {
    const { listGrades } = await import("@/lib/feedback-grade-queries");
    const seen: number[] = [];
    let cursor: string | undefined;
    for (let i = 0; i < 20; i++) {
      const { decodeGradeListCursor } = await import("@/lib/feedback-grade-filters");
      const page = listGrades(
        { limit: 3, cursor: cursor ? decodeGradeListCursor(cursor)! : undefined },
        AS_OF
      );
      seen.push(...page.data.map((r) => r.grade_id));
      if (!page.has_more) break;
      cursor = page.next_cursor!;
    }
    expect(seen).toEqual([1, 2, 3, 4, 6, 7, 8, 10, 9, 5]);
  });

  it("keeps a stable order across a keyset cursor boundary when two rows share last_edit_time", async () => {
    // grade 9 and grade 10 share last_edit_time = 2026-01-03T00:00:00.000Z.
    // A page boundary that lands exactly between them (grade 10 last on
    // page 1) must not skip or duplicate grade 9 on page 2.
    const { listGrades } = await import("@/lib/feedback-grade-queries");
    const { decodeGradeListCursor } = await import("@/lib/feedback-grade-filters");
    const page1 = listGrades({ limit: 8 }, AS_OF);
    expect(page1.data.map((r) => r.grade_id)).toEqual([1, 2, 3, 4, 6, 7, 8, 10]);
    expect(page1.has_more).toBe(true);

    const page2 = listGrades(
      { limit: 8, cursor: decodeGradeListCursor(page1.next_cursor!)! },
      AS_OF
    );
    expect(page2.data.map((r) => r.grade_id)).toEqual([9, 5]);
    expect(page2.has_more).toBe(false);
  });

  it("filters by schemaVersion, distinguishing the legacy grade", async () => {
    const { listGrades } = await import("@/lib/feedback-grade-queries");
    const page = listGrades({ schemaVersion: 1 }, AS_OF);
    expect(page.data.map((r) => r.grade_id)).toEqual([4]);
  });

  it("filters by needsRegrade", async () => {
    const { listGrades } = await import("@/lib/feedback-grade-queries");
    const page = listGrades({ needsRegrade: true }, AS_OF);
    expect(page.data.map((r) => r.grade_id)).toEqual([6]);
  });

  it("filters by relevanceJudgment", async () => {
    const { listGrades } = await import("@/lib/feedback-grade-queries");
    const page = listGrades({ relevanceJudgment: "false_negative" }, AS_OF);
    expect(page.data.map((r) => r.grade_id)).toEqual([8]);
  });

  it("filters by failureDimension against the JSON dimensions array", async () => {
    const { listGrades } = await import("@/lib/feedback-grade-queries");
    const page = listGrades({ failureDimension: "tone" }, AS_OF);
    expect(page.data.map((r) => r.grade_id)).toEqual([2]);
  });

  it("filters by scanId using the grade's own scan_id", async () => {
    const { listGrades } = await import("@/lib/feedback-grade-queries");
    const page = listGrades({ scanId: 501 }, AS_OF);
    expect(page.data.map((r) => r.grade_id).sort((a, b) => a - b)).toEqual([4, 7, 9]);
  });

  it("filters by evaluationId", async () => {
    const { listGrades } = await import("@/lib/feedback-grade-queries");
    const page = listGrades({ evaluationId: 10 }, AS_OF);
    expect(page.data.map((r) => r.grade_id)).toEqual([1]);
  });

  it("filters by overrideMode", async () => {
    const { listGrades } = await import("@/lib/feedback-grade-queries");
    const page = listGrades({ overrideMode: "exclude" }, AS_OF);
    expect(page.data.map((r) => r.grade_id)).toEqual([7]);
    expect(page.data[0].override_reason).toBe("test exclude reason");
  });

  it("filters by every eligibilityReason value against the ported classifier", async () => {
    const { listGrades } = await import("@/lib/feedback-grade-queries");
    expect(listGrades({ eligibilityReason: "eligible" }, AS_OF).data.map((r) => r.grade_id).sort()).toEqual([1, 2]);
    expect(listGrades({ eligibilityReason: "eligible_cap" }, AS_OF).data.map((r) => r.grade_id)).toEqual([3]);
    expect(listGrades({ eligibilityReason: "outside_lookback" }, AS_OF).data.map((r) => r.grade_id)).toEqual([5]);
    expect(listGrades({ eligibilityReason: "manual_exclude" }, AS_OF).data.map((r) => r.grade_id)).toEqual([7]);
    expect(
      listGrades({ eligibilityReason: "missing_evaluation_linkage" }, AS_OF).data.map((r) => r.grade_id)
    ).toEqual([8]);
    expect(
      listGrades({ eligibilityReason: "mismatched_evaluation_identity" }, AS_OF).data.map((r) => r.grade_id)
    ).toEqual([9]);
    expect(
      listGrades({ eligibilityReason: "shared_contract_invalid" }, AS_OF).data.map((r) => r.grade_id)
    ).toEqual([10]);
  });

  it("distinguishes grade_time (earliest revision envelope) from last_edit_time (newest revision)", async () => {
    const { listGrades } = await import("@/lib/feedback-grade-queries");
    const page = listGrades({ evaluationId: 10 }, AS_OF);
    const row = page.data[0];
    expect(row.grade_time).toBe("2026-01-08T00:00:00.000Z");
    expect(row.last_edit_time).toBe("2026-01-10T00:00:00.000Z");
    expect(row.revision_count).toBe(3);
  });

  it("filters gradedTo/gradedFrom against grade_time, independent of editedTo/editedFrom", async () => {
    const { listGrades } = await import("@/lib/feedback-grade-queries");
    // grade 1's grade_time (01-08) is within this bound even though its
    // last_edit_time (01-10) is not.
    const byGraded = listGrades({ evaluationId: 10, gradedTo: "2026-01-08T23:59:59.999Z" }, AS_OF);
    expect(byGraded.data.map((r) => r.grade_id)).toEqual([1]);
    const byEdited = listGrades({ evaluationId: 10, editedTo: "2026-01-08T23:59:59.999Z" }, AS_OF);
    expect(byEdited.data).toEqual([]);
  });

  it("aggregates snapshot use across snapshots and phases, and flags active use from the latest snapshot", async () => {
    const { listGrades } = await import("@/lib/feedback-grade-queries");
    const page = listGrades({ limit: 100 }, AS_OF);
    const grade1 = page.data.find((r) => r.grade_id === 1)!;
    expect(grade1.snapshot_use_count).toBe(2); // snapshots 900 and 901
    expect(grade1.snapshot_use_first_at).toBe("2026-01-09T00:00:00.000Z");
    expect(grade1.snapshot_use_last_at).toBe("2026-01-11T00:00:00.000Z");
    expect(grade1.snapshot_active_use).toBe(true);

    const grade2 = page.data.find((r) => r.grade_id === 2)!;
    expect(grade2.snapshot_use_count).toBe(1);
    expect(grade2.snapshot_active_use).toBe(false); // excluded role, not aggregate/example

    const grade3 = page.data.find((r) => r.grade_id === 3)!;
    expect(grade3.snapshot_use_count).toBe(0);
    expect(grade3.snapshot_use_first_at).toBeNull();
  });

  it("filters by snapshotUse never/historical/active", async () => {
    const { listGrades } = await import("@/lib/feedback-grade-queries");
    expect(listGrades({ snapshotUse: "active" }, AS_OF).data.map((r) => r.grade_id)).toEqual([1]);
    expect(
      listGrades({ snapshotUse: "historical" }, AS_OF).data.map((r) => r.grade_id).sort()
    ).toEqual([1, 2]);
    expect(
      listGrades({ snapshotUse: "never" }, AS_OF).data.map((r) => r.grade_id).sort((a, b) => a - b)
    ).toEqual([3, 4, 5, 6, 7, 8, 9, 10]);
  });

  it("always reports trace_available false with the pre-cohort-6 reason", async () => {
    const { listGrades } = await import("@/lib/feedback-grade-queries");
    const page = listGrades({ limit: 100 }, AS_OF);
    expect(page.data.every((r) => r.trace_available === false)).toBe(true);
    expect(page.data.every((r) => r.trace_unavailable_reason === "no_linked_phase_runs")).toBe(
      true
    );
    expect(listGrades({ traceAvailability: "available" }, AS_OF).data).toEqual([]);
    expect(listGrades({ traceAvailability: "unavailable" }, AS_OF).total_matching).toBe(10);
  });

  it("rejects filter combinations that match nothing without throwing", async () => {
    const { listGrades } = await import("@/lib/feedback-grade-queries");
    const page = listGrades({ project: "does-not-exist" }, AS_OF);
    expect(page.data).toEqual([]);
    expect(page.total_matching).toBe(0);
  });
});

describe("getGradeDetail", () => {
  it("returns null for a nonexistent grade id", async () => {
    const { getGradeDetail } = await import("@/lib/feedback-grade-queries");
    expect(getGradeDetail(999999, AS_OF, {})).toBeNull();
  });

  it("rehydrates the canonical current envelope with parsed dimensions", async () => {
    const { getGradeDetail } = await import("@/lib/feedback-grade-queries");
    const detail = getGradeDetail(2, AS_OF, {})!;
    expect(detail.grade.id).toBe(2);
    expect(detail.grade.dimensions).toEqual(["tone", "usefulness"]);
    expect(detail.grade.relevance_judgment).toBe("correct");
  });

  it("returns explicit null evidence/segment states for an unlinked grade, addressed only by grade_id", async () => {
    const { getGradeDetail } = await import("@/lib/feedback-grade-queries");
    const detail = getGradeDetail(8, AS_OF, {})!;
    expect(detail.identity.evaluation_id).toBeNull();
    expect(detail.evidence.evaluation).toBeNull();
    expect(detail.evidence.draft).toBeNull();
    expect(detail.evidence.critique).toBeNull();
    expect(detail.segment.project_key).toBeNull();
    expect(detail.segment.posture).toBeNull();
    expect(detail.segment.terminal_status).toBeNull();
    // The post itself is always resolvable — a grade's post_id is never null.
    expect(detail.evidence.post.id).toBe(4);
    expect(detail.evidence.post.platform).toBe("bluesky");
    expect(detail.segment.platform).toBe("bluesky");
    expect(detail.eligibility.reason).toBe("missing_evaluation_linkage");
  });

  it("returns full collapsed evidence (post, draft, critique) and override state when present", async () => {
    const { getGradeDetail } = await import("@/lib/feedback-grade-queries");
    const detail = getGradeDetail(1, AS_OF, {})!;
    expect(detail.evidence.post.id).toBe(1);
    expect(detail.evidence.evaluation?.id).toBe(10);
    expect(detail.evidence.draft?.comment_text).toBe("Great point!");
    expect(detail.evidence.critique?.verdict).toBe("approve");
    expect(detail.override).toBeNull();

    const excluded = getGradeDetail(7, AS_OF, {})!;
    expect(excluded.override).toEqual({
      mode: "exclude",
      reason: "test exclude reason",
      updated_at: "2026-01-05T01:00:00.000Z",
    });
  });

  it("paginates revision history independently, ordered revision desc", async () => {
    const { getGradeDetail } = await import("@/lib/feedback-grade-queries");
    const { decodeRevisionCursor } = await import("@/lib/feedback-grade-filters");
    const page1 = getGradeDetail(1, AS_OF, { revisionLimit: 2 })!;
    expect(page1.revision_history.data.map((r) => r.revision)).toEqual([3, 2]);
    expect(page1.revision_history.data.every((revision) => revision.schema_version === 3)).toBe(
      true
    );
    expect(page1.revision_history.has_more).toBe(true);

    const cursor = decodeRevisionCursor(page1.revision_history.next_cursor!)!;
    const page2 = getGradeDetail(1, AS_OF, { revisionLimit: 2, revisionCursor: cursor })!;
    expect(page2.revision_history.data.map((r) => r.revision)).toEqual([1]);
    expect(page2.revision_history.has_more).toBe(false);
  });

  it("paginates snapshot-use history independently of revision history, ordered created_at desc then id desc", async () => {
    const { getGradeDetail } = await import("@/lib/feedback-grade-queries");
    const { decodeSnapshotUseHistoryCursor } = await import("@/lib/feedback-grade-filters");
    const page1 = getGradeDetail(1, AS_OF, { snapshotUseLimit: 2 })!;
    expect(page1.snapshot_use_history.data).toHaveLength(2);
    expect(page1.snapshot_use_history.data[0].phase).toBe("critic");
    expect(page1.snapshot_use_history.has_more).toBe(true);

    const cursor = decodeSnapshotUseHistoryCursor(page1.snapshot_use_history.next_cursor!)!;
    const page2 = getGradeDetail(1, AS_OF, { snapshotUseLimit: 2, snapshotUseCursor: cursor })!;
    expect(page2.snapshot_use_history.data).toHaveLength(2);
    expect(page2.snapshot_use_history.has_more).toBe(false);
  });

  it("returns an always-empty phase_runs page with trace unavailable pre-cohort-6", async () => {
    const { getGradeDetail } = await import("@/lib/feedback-grade-queries");
    const detail = getGradeDetail(1, AS_OF, {})!;
    expect(detail.phase_runs).toEqual({ data: [], has_more: false, next_cursor: null });
    expect(detail.trace_available).toBe(false);
    expect(detail.trace_unavailable_reason).toBe("no_linked_phase_runs");
  });

  it("reports eligibility with the resolved policy alongside the reason", async () => {
    const { getGradeDetail } = await import("@/lib/feedback-grade-queries");
    const detail = getGradeDetail(3, AS_OF, {})!;
    expect(detail.eligibility).toEqual({
      reason: "eligible_cap",
      policy_version: "evaluation-feedback/v1",
      resolved_lookback_days: 90,
      resolved_max_grades: 2,
    });
  });
});
