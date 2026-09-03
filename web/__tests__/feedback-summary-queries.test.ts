import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const AS_OF = "2026-02-01T00:00:00.000Z";
const FROM = "2025-11-03T00:00:00.000Z"; // AS_OF - 90 days
const TO = AS_OF;

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-feedback-summary-"));
const dbPath = path.join(tmpDir, "scout.db");
process.env.SCOUT_DB_PATH = dbPath;

let nextPostId = 1;
let nextEvalId = 1;
let nextGradeId = 1;
let nextRevisionId = 1;

interface GradeSpec {
  platform: string;
  scanId?: number | null;
  gradedAt: string;
  schemaVersion: number;
  needsRegrade: number;
  relevanceJudgment: "correct" | "false_positive" | "false_negative";
  actionJudgment?: string | null;
  relevant: number;
  projectKey?: string | null;
  hasDraft?: boolean;
  dimensions?: string[] | null;
}

function insertGrade(db: Database.Database, spec: GradeSpec): number {
  const postId = nextPostId++;
  const evalId = nextEvalId++;
  const gradeId = nextGradeId++;
  db.prepare(
    `INSERT INTO posts (id, platform, platform_msg_id) VALUES (?, ?, ?)`
  ).run(postId, spec.platform, `msg-${postId}`);
  db.prepare(
    `INSERT INTO evaluations (id, post_id, scan_id, relevant, score, project_key, posture, surface_status)
     VALUES (?, ?, ?, ?, 0.5, ?, 'engage', 'surfaced')`
  ).run(evalId, postId, spec.scanId ?? null, spec.relevant, spec.projectKey ?? null);
  if (spec.hasDraft) {
    db.prepare(
      `INSERT INTO draft_comments (id, post_id, evaluation_id, project_key, posture)
       VALUES (?, ?, ?, ?, 'engage')`
    ).run(evalId, postId, evalId, spec.projectKey ?? null);
  }
  const actionJudgment = Object.prototype.hasOwnProperty.call(spec, "actionJudgment")
    ? (spec.actionJudgment ?? null)
    : spec.relevanceJudgment === "correct"
      ? "accept"
      : "fail";
  const dimensions =
    actionJudgment === "fail"
      ? (spec.dimensions ?? ["tone"])
      : null;
  const failureNote = actionJudgment === "fail" ? "fixture failure evidence" : null;
  const postureShouldHaveBeen = dimensions?.includes("posture") ? "answer" : null;
  db.prepare(
    `INSERT INTO grades
       (id, evaluation_id, post_id, scan_id, source, graded_at, schema_version, needs_regrade,
        relevance_judgment, action_judgment, dimensions, failure_note, posture_should_have_been)
     VALUES (?, ?, ?, ?, 'web', ?, ?, ?, ?, ?, ?, ?, ?)`
  ).run(
    gradeId,
    evalId,
    postId,
    spec.scanId ?? null,
    spec.gradedAt,
    spec.schemaVersion,
    spec.needsRegrade,
    spec.relevanceJudgment,
    actionJudgment,
    dimensions ? JSON.stringify(dimensions) : null,
    failureNote,
    postureShouldHaveBeen
  );
  const revisionId = nextRevisionId++;
  db.prepare(
    `INSERT INTO grade_revisions (id, grade_id, evaluation_id, revision, source, payload, recorded_at)
     VALUES (?, ?, ?, 1, 'web', ?, ?)`
  ).run(revisionId, gradeId, evalId, JSON.stringify({ graded_at: spec.gradedAt }), spec.gradedAt);
  return gradeId;
}

beforeAll(() => {
  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE scans (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL);
    CREATE TABLE posts (id INTEGER PRIMARY KEY, platform TEXT NOT NULL, platform_msg_id TEXT NOT NULL);
    CREATE TABLE evaluations (
      id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL, scan_id INTEGER, relevant INTEGER NOT NULL,
      score REAL NOT NULL, project_key TEXT, posture TEXT, surface_status TEXT,
      dossier_summary_id TEXT, dossier_revision TEXT
    );
    CREATE TABLE draft_comments (
      id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL, evaluation_id INTEGER NOT NULL,
      project_key TEXT, posture TEXT, dossier_summary_id TEXT, dossier_revision TEXT
    );
    CREATE TABLE grades (
      id INTEGER PRIMARY KEY, evaluation_id INTEGER, post_id INTEGER NOT NULL, scan_id INTEGER,
      source TEXT NOT NULL, graded_at TEXT NOT NULL, schema_version INTEGER NOT NULL DEFAULT 1,
      needs_regrade INTEGER NOT NULL DEFAULT 0, relevance_judgment TEXT NOT NULL, action_judgment TEXT,
      dimensions TEXT, failure_note TEXT, factual_offending_claim TEXT, factual_disposition TEXT,
      factual_contradicting_evidence TEXT, context_missing_input TEXT,
      posture_should_have_been TEXT, implication_implied_claim TEXT,
      implication_missing_support TEXT
    );
    CREATE TABLE grade_revisions (
      id INTEGER PRIMARY KEY, grade_id INTEGER NOT NULL, evaluation_id INTEGER, revision INTEGER NOT NULL,
      source TEXT NOT NULL, payload TEXT NOT NULL, recorded_at TEXT NOT NULL
    );
    CREATE TABLE grade_usage_overrides (id INTEGER PRIMARY KEY, grade_id INTEGER, mode TEXT, reason TEXT, updated_at TEXT);
    CREATE TABLE feedback_snapshots (
      id INTEGER PRIMARY KEY, scan_id INTEGER, policy_version TEXT, mode TEXT, as_of TEXT,
      lookback_days INTEGER, max_grades INTEGER, created_at TEXT,
      population_count INTEGER, eligible_count INTEGER, excluded_count INTEGER
    );
    CREATE TABLE feedback_snapshot_phases (id INTEGER PRIMARY KEY, snapshot_id INTEGER, phase TEXT);
    CREATE TABLE feedback_snapshot_items (
      id INTEGER PRIMARY KEY, snapshot_phase_id INTEGER, grade_id INTEGER, grade_revision_id INTEGER,
      role TEXT, reason TEXT, created_at TEXT
    );
  `);

  // No feedback_snapshots row: this fixture exercises windowed analytics
  // only, independent of the (separately tested) eligibility classifier.

  db.prepare(`INSERT INTO scans (id, started_at) VALUES (700, '2026-01-05T00:00:00.000Z')`).run();
  db.prepare(`INSERT INTO scans (id, started_at) VALUES (701, '2026-01-06T00:00:00.000Z')`).run();
  db.prepare(`INSERT INTO scans (id, started_at) VALUES (702, '2020-01-01T00:00:00.000Z')`).run();

  // --- Corpus cards: all-time, independent of the [FROM, TO] window ---
  insertGrade(db, {
    platform: "discord", gradedAt: "2026-01-10T00:00:00.000Z", schemaVersion: 3, needsRegrade: 0,
    relevanceJudgment: "correct", relevant: 1,
  }); // A: current
  insertGrade(db, {
    platform: "discord", gradedAt: "2020-01-01T00:00:00.000Z", schemaVersion: 1, needsRegrade: 0,
    relevanceJudgment: "correct", relevant: 1,
  }); // B: legacy, outside window
  insertGrade(db, {
    platform: "discord", gradedAt: "2026-01-11T00:00:00.000Z", schemaVersion: 3, needsRegrade: 1,
    relevanceJudgment: "correct", relevant: 1,
  }); // C: current AND needs_regrade (overlap)
  insertGrade(db, {
    platform: "discord", gradedAt: "2020-01-02T00:00:00.000Z", schemaVersion: 1, needsRegrade: 1,
    relevanceJudgment: "correct", relevant: 1,
  }); // D: legacy AND needs_regrade (overlap)

  // --- Coverage: scan 700 has 1/2 linked (schema-v2), scan 701 has 0/2
  // (its one grade is legacy) ---
  const cov1 = insertGrade(db, {
    platform: "discord", scanId: 700, gradedAt: "2026-01-05T01:00:00.000Z", schemaVersion: 3,
    needsRegrade: 0, relevanceJudgment: "correct", relevant: 1,
  });
  void cov1;
  db.prepare(`INSERT INTO posts (id, platform, platform_msg_id) VALUES (?, 'discord', ?)`).run(
    nextPostId, `msg-${nextPostId}`
  );
  db.prepare(
    `INSERT INTO evaluations (id, post_id, scan_id, relevant, score) VALUES (?, ?, 700, 1, 0.5)`
  ).run(nextEvalId, nextPostId);
  nextPostId++; nextEvalId++; // ungraded evaluation under scan 700

  const legacyUnderScan701 = insertGrade(db, {
    platform: "discord", scanId: 701, gradedAt: "2026-01-06T01:00:00.000Z", schemaVersion: 1,
    needsRegrade: 0, relevanceJudgment: "correct", relevant: 1,
  });
  void legacyUnderScan701;
  db.prepare(`INSERT INTO posts (id, platform, platform_msg_id) VALUES (?, 'discord', ?)`).run(
    nextPostId, `msg-${nextPostId}`
  );
  db.prepare(
    `INSERT INTO evaluations (id, post_id, scan_id, relevant, score) VALUES (?, ?, 701, 1, 0.5)`
  ).run(nextEvalId, nextPostId);
  nextPostId++; nextEvalId++; // ungraded evaluation under scan 701

  // --- Relevance / response acceptance / failure-dimension population ---
  insertGrade(db, {
    platform: "discord", gradedAt: "2026-01-12T00:00:00.000Z", schemaVersion: 3, needsRegrade: 0,
    relevanceJudgment: "correct", relevant: 1, actionJudgment: "accept", hasDraft: true,
    projectKey: "acme",
  }); // R1: correct+relevant, draft-quality, accept
  insertGrade(db, {
    platform: "discord", gradedAt: "2026-01-13T00:00:00.000Z", schemaVersion: 3, needsRegrade: 0,
    relevanceJudgment: "correct", relevant: 0, projectKey: "acme",
  }); // R2: correct but not relevant (true negative) — not draft-quality
  insertGrade(db, {
    platform: "farcaster", gradedAt: "2026-01-14T00:00:00.000Z", schemaVersion: 3, needsRegrade: 0,
    relevanceJudgment: "false_positive", relevant: 1, projectKey: "beta",
  }); // R3
  insertGrade(db, {
    platform: "farcaster", gradedAt: "2026-01-15T00:00:00.000Z", schemaVersion: 3, needsRegrade: 0,
    relevanceJudgment: "false_negative", relevant: 0, projectKey: "beta",
  }); // R4
  insertGrade(db, {
    platform: "discord", gradedAt: "2020-06-01T00:00:00.000Z", schemaVersion: 3, needsRegrade: 0,
    relevanceJudgment: "correct", relevant: 1,
  }); // R5: outside the [FROM, TO] window entirely
  insertGrade(db, {
    platform: "discord", gradedAt: "2026-01-12T00:00:00.000Z", schemaVersion: 1, needsRegrade: 0,
    relevanceJudgment: "correct", relevant: 1,
  }); // R6: legacy, excluded from relevance/segments despite being in-window
  insertGrade(db, {
    platform: "discord", gradedAt: "2026-01-16T00:00:00.000Z", schemaVersion: 3, needsRegrade: 0,
    relevanceJudgment: "correct", relevant: 1, actionJudgment: "fail", hasDraft: true,
    dimensions: ["tone", "posture"], projectKey: "acme",
  }); // R7: draft-quality, fail, day 2026-01-16
  insertGrade(db, {
    platform: "discord", gradedAt: "2026-01-16T00:00:00.000Z", schemaVersion: 3, needsRegrade: 0,
    relevanceJudgment: "correct", relevant: 1, actionJudgment: null, hasDraft: true, projectKey: "acme",
  }); // R8: draft-quality, not_applicable action
  insertGrade(db, {
    platform: "discord", gradedAt: "2026-01-17T00:00:00.000Z", schemaVersion: 3, needsRegrade: 0,
    relevanceJudgment: "correct", relevant: 1, actionJudgment: "fail", hasDraft: true,
    dimensions: ["tone"], projectKey: "acme",
  }); // R9: draft-quality, fail, day 2026-01-17 (different day than R7)

  db.close();
});

afterAll(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
  delete process.env.SCOUT_DB_PATH;
});

describe("getFeedbackSummary — corpus cards (all-time)", () => {
  it("shares one all-time denominator across overlapping, non-exclusive categories", async () => {
    const { getFeedbackSummary } = await import("@/lib/feedback-summary-queries");
    const summary = getFeedbackSummary(AS_OF, FROM, TO);
    // 4 corpus-card grades + 2 coverage grades (cov1, legacyUnderScan701) + 9 relevance/acceptance grades (R1..R9) = 15
    expect(summary.corpus.total).toEqual({ count: 15, denominator: 15 });
    expect(summary.corpus.current.denominator).toBe(15);
    expect(summary.corpus.legacy.denominator).toBe(15);
    // B, D, legacyUnderScan701, R6 are legacy (schema_version < 2); D is also needs_regrade.
    expect(summary.corpus.legacy.count).toBe(4);
    expect(summary.corpus.current.count).toBe(11);
    expect(summary.corpus.needs_regrade.count).toBe(2); // C and D
  });
});

describe("getFeedbackSummary — coverage", () => {
  it("groups linked-vs-total evaluations by scan and UTC day within the window", async () => {
    const { getFeedbackSummary } = await import("@/lib/feedback-summary-queries");
    const summary = getFeedbackSummary(AS_OF, FROM, TO);
    const byScan = new Map(summary.coverage.map((c) => [c.scan_id, c]));
    expect(byScan.get(700)).toEqual({ scan_id: 700, day: "2026-01-05", linked: 1, total: 2 });
    expect(byScan.get(701)).toEqual({ scan_id: 701, day: "2026-01-06", linked: 0, total: 2 });
    expect(byScan.has(702)).toBe(false); // scan 702 predates the window
  });
});

describe("getFeedbackSummary — relevance metrics", () => {
  it("computes correct/false_positive/false_negative only from valid schema-v2 linked grades in-window", async () => {
    const { getFeedbackSummary } = await import("@/lib/feedback-summary-queries");
    const summary = getFeedbackSummary(AS_OF, FROM, TO);
    expect(summary.relevance).toEqual({
      correct: 6, // A, R1, R2, R7, R9, and the scan-700 coverage grade
      false_positive: 1, // R3
      false_negative: 1, // R4
      reviewed_model_negative: 2, // R2 (correct negative) and R4 (false negative)
      correct_relevant: 5, // every valid "correct" row except R2 (relevant=0)
      precision_denominator: 6, // correct_relevant(5) + false_positive(1)
      recall_denominator: 6, // correct_relevant(5) + false_negative(1)
    });
  });
});

describe("getFeedbackSummary — response acceptance", () => {
  it("computes accept/fail/not_applicable on the draft-quality population only", async () => {
    const { getFeedbackSummary } = await import("@/lib/feedback-summary-queries");
    const summary = getFeedbackSummary(AS_OF, FROM, TO);
    expect(summary.response_acceptance).toEqual({
      accept: 1, // R1
      fail: 2, // R7, R9
      denominator: 3,
      not_applicable: 0, // R8 is contract-invalid and excluded
    });
  });
});

describe("getFeedbackSummary — failure dimension trends", () => {
  it("buckets by UTC day and dimension with a shared per-day failed-draft denominator", async () => {
    const { getFeedbackSummary } = await import("@/lib/feedback-summary-queries");
    const summary = getFeedbackSummary(AS_OF, FROM, TO);
    const byDayDim = new Map(summary.failure_dimension_trends.map((b) => [`${b.day}:${b.dimension}`, b]));
    expect(byDayDim.get("2026-01-16:tone")).toEqual({
      day: "2026-01-16", dimension: "tone", count: 1, failed_draft_denominator: 1,
    });
    expect(byDayDim.get("2026-01-16:posture")).toEqual({
      day: "2026-01-16", dimension: "posture", count: 1, failed_draft_denominator: 1,
    });
    expect(byDayDim.get("2026-01-17:tone")).toEqual({
      day: "2026-01-17", dimension: "tone", count: 1, failed_draft_denominator: 1,
    });
  });
});

describe("getFeedbackSummary — segments", () => {
  it("groups the relevance population by platform, ordered count desc then label", async () => {
    const { getFeedbackSummary } = await import("@/lib/feedback-summary-queries");
    const summary = getFeedbackSummary(AS_OF, FROM, TO);
    expect(summary.segments.platform.entries).toEqual([
      { label: "discord", count: 6, correct: 6, false_positive: 0, false_negative: 0 },
      { label: "farcaster", count: 2, correct: 0, false_positive: 1, false_negative: 1 },
    ]);
    expect(summary.segments.platform.other).toBeNull();
    expect(summary.segments.platform.denominator).toBe(8);
  });

  it("groups by project, omitting grades with no project_key", async () => {
    const { getFeedbackSummary } = await import("@/lib/feedback-summary-queries");
    const summary = getFeedbackSummary(AS_OF, FROM, TO);
    const acme = summary.segments.project.entries.find((e) => e.label === "acme")!;
    expect(acme).toEqual({ label: "acme", count: 4, correct: 4, false_positive: 0, false_negative: 0 });
    const beta = summary.segments.project.entries.find((e) => e.label === "beta")!;
    expect(beta).toEqual({ label: "beta", count: 2, correct: 0, false_positive: 1, false_negative: 1 });
    // A and the scan-700 coverage grade have no project_key and must not
    // appear as a segment label at all.
    expect(summary.segments.project.entries.some((e) => e.label === null)).toBe(false);
    expect(summary.segments.project.denominator).toBe(6);
  });
});

describe("getFeedbackSummary — segment cap and other bucket", () => {
  it("caps each dimension at 50 entries and folds the remainder into a server-computed other bucket", async () => {
    const db = new Database(dbPath);
    for (let i = 0; i < 52; i++) {
      const platform = `plat-${String(i).padStart(2, "0")}`;
      insertGrade(db, {
        platform, gradedAt: "2026-01-18T00:00:00.000Z", schemaVersion: 3, needsRegrade: 0,
        relevanceJudgment: "correct", relevant: 1,
      });
    }
    db.close();

    const { getFeedbackSummary } = await import("@/lib/feedback-summary-queries");
    const summary = getFeedbackSummary(AS_OF, FROM, TO);
    expect(summary.segments.platform.entries).toHaveLength(50);
    expect(summary.segments.platform.other).not.toBeNull();
    expect(summary.segments.platform.other?.label).toBe("other");
    expect(summary.segments.platform.other?.count).toBe(4); // plat-48..plat-51 spill over
    expect(summary.segments.platform.denominator).toBe(8 + 52);
  });
});
