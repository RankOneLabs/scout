import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-phase-run-queries-"));
const dbPath = path.join(tmpDir, "scout.db");
process.env.SCOUT_DB_PATH = dbPath;

// Deterministic created_at ordering: phase run N is one second after N-1,
// so created_at DESC, id DESC paging has an unambiguous expected order.
function stamp(n: number): string {
  return `2026-01-01T00:00:${String(n).padStart(2, "0")}.000Z`;
}

beforeAll(() => {
  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE feedback_snapshot_phases (
      id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, phase TEXT NOT NULL
    );
    CREATE TABLE grades (
      id INTEGER PRIMARY KEY, evaluation_id INTEGER UNIQUE
    );
    CREATE TABLE evaluation_phase_runs (
      id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL, post_id INTEGER NOT NULL,
      evaluation_id INTEGER, snapshot_phase_id INTEGER NOT NULL, phase TEXT NOT NULL,
      trace_id TEXT NOT NULL UNIQUE, model TEXT NOT NULL, status TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    INSERT INTO feedback_snapshot_phases VALUES
      (10, 77, 'relevance'), (11, 77, 'reply_draft'), (12, 77, 'critic');
    INSERT INTO grades VALUES (101, 1), (102, 2);
  `);

  const insert = db.prepare(
    `INSERT INTO evaluation_phase_runs
       (id, scan_id, post_id, evaluation_id, snapshot_phase_id, phase, trace_id, model, status, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  );

  // Evaluation 1's three contributor phase runs, all against snapshot phase
  // ids 10 (relevance), 11 (reply_draft), 12 (critic).
  insert.run(1, 500, 1, 1, 10, "relevance", "trace-eval1-relevance", "model-a", "complete", stamp(1));
  insert.run(2, 500, 1, 1, 11, "reply_draft", "trace-eval1-draft", "model-a", "complete", stamp(2));
  insert.run(3, 500, 1, 1, 12, "critic", "trace-eval1-critic", "model-a", "complete", stamp(3));

  // Evaluation 2: not_relevant, relevance only, unlinked snapshot phase 10
  // (a different post than evaluation 1's, same snapshot phase — both
  // consume the same feedback_snapshot_phases row).
  insert.run(4, 500, 2, 2, 10, "relevance", "trace-eval2-relevance", "model-a", "complete", stamp(4));

  // An error attempt for evaluation 3 — never a contributor, never
  // returned as "linked" for trace_available purposes at the query layer
  // (hasLinkedPhaseRuns only cares about evaluation_id presence here,
  // matching how the web layer treats any linked row as available).
  insert.run(5, 500, 3, null, 10, "relevance", "trace-error-unlinked", "model-a", "error", stamp(5));

  db.close();
});

afterAll(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
  delete process.env.SCOUT_DB_PATH;
});

describe("getPhaseRunDetail / getPhaseRunByTraceId", () => {
  it("returns full stored-key identity for a known id", async () => {
    const { getPhaseRunDetail } = await import("@/lib/feedback-phase-run-queries");
    const detail = getPhaseRunDetail(2);
    expect(detail).toEqual({
      id: 2,
      scan_id: 500,
      post_id: 1,
      evaluation_id: 1,
      grade_id: 101,
      snapshot_id: 77,
      snapshot_phase_id: 11,
      phase: "reply_draft",
      trace_id: "trace-eval1-draft",
      model: "model-a",
      status: "complete",
      created_at: stamp(2),
    });
  });

  it("returns null for an unknown id", async () => {
    const { getPhaseRunDetail } = await import("@/lib/feedback-phase-run-queries");
    expect(getPhaseRunDetail(9999)).toBeNull();
  });

  it("resolves the same row by trace_id as by id — the one approved backlink join", async () => {
    const { getPhaseRunDetail, getPhaseRunByTraceId } = await import(
      "@/lib/feedback-phase-run-queries"
    );
    const byId = getPhaseRunDetail(3);
    const byTrace = getPhaseRunByTraceId("trace-eval1-critic");
    expect(byTrace).toEqual(byId);
  });

  it("returns null for a trace_id with no linked phase run", async () => {
    const { getPhaseRunByTraceId } = await import("@/lib/feedback-phase-run-queries");
    expect(getPhaseRunByTraceId("no-such-trace")).toBeNull();
  });
});

describe("hasLinkedPhaseRuns", () => {
  it("is true for an evaluation with linked phase runs", async () => {
    const { hasLinkedPhaseRuns } = await import("@/lib/feedback-phase-run-queries");
    expect(hasLinkedPhaseRuns(1)).toBe(true);
    expect(hasLinkedPhaseRuns(2)).toBe(true);
  });

  it("is false for an evaluation with none linked", async () => {
    const { hasLinkedPhaseRuns } = await import("@/lib/feedback-phase-run-queries");
    expect(hasLinkedPhaseRuns(3)).toBe(false);
    expect(hasLinkedPhaseRuns(9999)).toBe(false);
  });
});

describe("getEvaluationIdsWithLinkedPhaseRuns (batched, for listGrades)", () => {
  it("returns exactly the linked ids from one query, matching hasLinkedPhaseRuns per-id", async () => {
    const { getEvaluationIdsWithLinkedPhaseRuns } = await import(
      "@/lib/feedback-phase-run-queries"
    );
    const linked = getEvaluationIdsWithLinkedPhaseRuns([1, 2, 3, 9999]);
    expect(linked).toEqual(new Set([1, 2]));
  });

  it("returns an empty set for an empty input without querying", async () => {
    const { getEvaluationIdsWithLinkedPhaseRuns } = await import(
      "@/lib/feedback-phase-run-queries"
    );
    expect(getEvaluationIdsWithLinkedPhaseRuns([])).toEqual(new Set());
  });
});

describe("listPhaseRunsForEvaluation", () => {
  it("orders created_at DESC, id DESC and is independently stable per evaluation", async () => {
    const { listPhaseRunsForEvaluation } = await import("@/lib/feedback-phase-run-queries");
    const page = listPhaseRunsForEvaluation(1, {});
    expect(page.data.map((r) => r.id)).toEqual([3, 2, 1]);
    expect(page.has_more).toBe(false);
    expect(page.next_cursor).toBeNull();

    // Evaluation 2's page is unaffected by evaluation 1's rows.
    const other = listPhaseRunsForEvaluation(2, {});
    expect(other.data.map((r) => r.id)).toEqual([4]);
  });

  it("pages with a stable cursor that resumes exactly where it left off", async () => {
    const { decodePhaseRunCursor } = await import("@/lib/feedback-phase-run-filters");
    const { listPhaseRunsForEvaluation } = await import("@/lib/feedback-phase-run-queries");

    const page1 = listPhaseRunsForEvaluation(1, { limit: 2 });
    expect(page1.data.map((r) => r.id)).toEqual([3, 2]);
    expect(page1.has_more).toBe(true);
    expect(page1.next_cursor).not.toBeNull();

    const cursor = decodePhaseRunCursor(page1.next_cursor!)!;
    const page2 = listPhaseRunsForEvaluation(1, { limit: 2, cursor });
    expect(page2.data.map((r) => r.id)).toEqual([1]);
    expect(page2.has_more).toBe(false);
    expect(page2.next_cursor).toBeNull();
  });
});

describe("listPhaseRunsForSnapshotPhase (consumer paging)", () => {
  it("lists every phase run governed by one snapshot phase, across posts and evaluations", async () => {
    const { listPhaseRunsForSnapshotPhase } = await import("@/lib/feedback-phase-run-queries");
    // snapshot_phase_id 10 is consumed by rows 1 (eval 1), 4 (eval 2), and 5
    // (error, evaluation_id null) — consumer listing is not evaluation-scoped.
    const page = listPhaseRunsForSnapshotPhase(10, {});
    expect(page.data.map((r) => r.id)).toEqual([5, 4, 1]);
    expect(page.data.map((r) => r.grade_id)).toEqual([null, 102, 101]);
  });

  it("is independent of another snapshot phase's consumer listing", async () => {
    const { listPhaseRunsForSnapshotPhase } = await import("@/lib/feedback-phase-run-queries");
    const page = listPhaseRunsForSnapshotPhase(11, {});
    expect(page.data.map((r) => r.id)).toEqual([2]);
  });

  it("paginates independently with its own cursor space", async () => {
    const { decodePhaseRunCursor } = await import("@/lib/feedback-phase-run-filters");
    const { listPhaseRunsForSnapshotPhase } = await import("@/lib/feedback-phase-run-queries");

    const page1 = listPhaseRunsForSnapshotPhase(10, { limit: 2 });
    expect(page1.data.map((r) => r.id)).toEqual([5, 4]);
    expect(page1.has_more).toBe(true);

    const cursor = decodePhaseRunCursor(page1.next_cursor!)!;
    const page2 = listPhaseRunsForSnapshotPhase(10, { limit: 2, cursor });
    expect(page2.data.map((r) => r.id)).toEqual([1]);
    expect(page2.has_more).toBe(false);
  });
});

describe("phase-run cursor codec", () => {
  it("round-trips through encode/decode", async () => {
    const { encodePhaseRunCursor, decodePhaseRunCursor } = await import(
      "@/lib/feedback-phase-run-filters"
    );
    const encoded = encodePhaseRunCursor("2026-01-01T00:00:02.000Z", 7);
    expect(decodePhaseRunCursor(encoded)).toEqual({ created_at: "2026-01-01T00:00:02.000Z", id: 7 });
  });

  it("returns null for malformed input", async () => {
    const { decodePhaseRunCursor } = await import("@/lib/feedback-phase-run-filters");
    expect(decodePhaseRunCursor("not-valid-base64!!")).toBeNull();
    expect(decodePhaseRunCursor(Buffer.from(JSON.stringify({ id: 1 })).toString("base64url"))).toBeNull();
    expect(
      decodePhaseRunCursor(
        Buffer.from(JSON.stringify({ created_at: "not-a-date", id: 1 })).toString("base64url")
      )
    ).toBeNull();
  });
});
