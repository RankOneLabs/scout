import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { decodeFeedbackItemCursor } from "@/lib/feedback-filters";

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-feedback-"));
const dbPath = path.join(tmpDir, "scout.db");
process.env.SCOUT_DB_PATH = dbPath;

// A trailing-space, mixed-indentation, emoji-bearing block — the exact
// bytes the exact-provenance test asserts survive untouched end to end.
const RELEVANCE_RENDERED_TEXT =
  "## Relevance Feedback (evaluation-feedback/v1)\n5 graded: 3 correct.  \n  - project:acme (n=3) 🎉\ntrailing space here \n";

beforeAll(() => {
  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE grades (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      evaluation_id INTEGER UNIQUE,
      post_id INTEGER NOT NULL,
      graded_at TEXT NOT NULL
    );
    CREATE TABLE grade_revisions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      grade_id INTEGER NOT NULL,
      payload TEXT NOT NULL,
      recorded_at TEXT NOT NULL
    );
    CREATE TABLE feedback_snapshots (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      scan_id INTEGER NOT NULL UNIQUE,
      policy_version TEXT NOT NULL,
      mode TEXT NOT NULL,
      as_of TEXT NOT NULL,
      lookback_days INTEGER NOT NULL,
      max_grades INTEGER NOT NULL,
      segment_min_grades INTEGER NOT NULL,
      note_max_chars INTEGER NOT NULL,
      population_count INTEGER NOT NULL,
      eligible_count INTEGER NOT NULL,
      excluded_count INTEGER NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE TABLE feedback_snapshot_phases (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      snapshot_id INTEGER NOT NULL,
      phase TEXT NOT NULL,
      token_budget INTEGER NOT NULL,
      token_estimate INTEGER NOT NULL,
      truncated INTEGER NOT NULL,
      structured_summary TEXT NOT NULL,
      rendered_text TEXT NOT NULL,
      rendered_sha256 TEXT NOT NULL
    );
    CREATE TABLE feedback_snapshot_items (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      snapshot_phase_id INTEGER NOT NULL,
      grade_id INTEGER NOT NULL,
      grade_revision_id INTEGER NOT NULL,
      role TEXT NOT NULL,
      reason TEXT,
      selection_reason TEXT NOT NULL,
      rank INTEGER
    );
    CREATE TABLE evaluation_phase_runs (
      id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL, post_id INTEGER NOT NULL,
      evaluation_id INTEGER, snapshot_phase_id INTEGER NOT NULL, phase TEXT NOT NULL,
      trace_id TEXT NOT NULL UNIQUE, model TEXT NOT NULL, status TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
  `);

  // --- Grades + pinned revisions (grade N -> revision N, 1:1 for the fixture) ---
  const insertGrade = db.prepare(`INSERT INTO grades (id, post_id, graded_at) VALUES (?, ?, ?)`);
  const insertRevision = db.prepare(
    `INSERT INTO grade_revisions (id, grade_id, payload, recorded_at) VALUES (?, ?, ?, ?)`
  );
  const grades: [number, number, string][] = [
    [1, 901, "2026-05-10T00:00:00.000Z"],
    [2, 902, "2026-05-11T00:00:00.000Z"],
    [3, 903, "2026-05-09T00:00:00.000Z"],
    [4, 904, "2026-05-08T00:00:00.000Z"],
    [5, 905, "2026-05-07T00:00:00.000Z"],
    [6, 906, "2026-05-16T00:00:00.000Z"],
  ];
  for (const [id, postId, gradedAt] of grades) {
    insertGrade.run(id, postId, gradedAt);
    insertRevision.run(
      id,
      id,
      JSON.stringify({ grade_id: id, post_id: postId, graded_at: gradedAt, note: "unicode – ✓" }),
      gradedAt
    );
  }
  db.prepare("UPDATE grades SET evaluation_id = 101 WHERE id = 1").run();
  db.prepare("UPDATE grades SET evaluation_id = 102 WHERE id = 2").run();
  insertRevision.run(
    7,
    1,
    JSON.stringify({
      grade_id: 1,
      post_id: 1901,
      graded_at: "2026-05-12T00:00:00.000Z",
      note: "newer pinned revision",
    }),
    "2026-05-12T00:00:00.000Z"
  );

  // --- Snapshot 1 (scan 100, shadow) — the main fixture for item ordering/pagination ---
  db.prepare(
    `INSERT INTO feedback_snapshots
       (id, scan_id, policy_version, mode, as_of, lookback_days, max_grades,
        segment_min_grades, note_max_chars, population_count, eligible_count,
        excluded_count, created_at)
     VALUES (1, 100, 'evaluation-feedback/v1', 'shadow', '2026-05-15T00:00:00.000Z',
             90, 200, 5, 240, 5, 3, 2, '2026-05-15T00:00:01.000Z')`
  ).run();

  const insertPhase = db.prepare(
    `INSERT INTO feedback_snapshot_phases
       (id, snapshot_id, phase, token_budget, token_estimate, truncated,
        structured_summary, rendered_text, rendered_sha256)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
  );
  insertPhase.run(
    10,
    1,
    "relevance",
    800,
    50,
    0,
    JSON.stringify({ phase: "relevance" }),
    RELEVANCE_RENDERED_TEXT,
    "sha-relevance-1"
  );
  insertPhase.run(
    11,
    1,
    "reply_draft",
    800,
    0,
    0,
    JSON.stringify({ phase: "reply_draft", reason: "no_eligible_feedback" }),
    "",
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
  );
  const insertPhaseRun = db.prepare(
    `INSERT INTO evaluation_phase_runs
       (id, scan_id, post_id, evaluation_id, snapshot_phase_id, phase,
        trace_id, model, status, created_at)
     VALUES (?, 100, ?, ?, ?, ?, ?, 'model-a', 'complete', ?)`
  );
  insertPhaseRun.run(
    100, 901, 101, 10, "relevance", "trace-relevance-1", "2026-05-15T00:00:01.000Z"
  );
  insertPhaseRun.run(
    101, 902, 102, 10, "relevance", "trace-relevance-2", "2026-05-15T00:00:02.000Z"
  );
  insertPhaseRun.run(
    102, 903, null, 12, "critic", "trace-critic-1", "2026-05-15T00:00:03.000Z"
  );
  insertPhase.run(
    12,
    1,
    "critic",
    1000,
    0,
    0,
    JSON.stringify({ phase: "critic", reason: "no_eligible_feedback" }),
    "",
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
  );

  const insertItem = db.prepare(
    `INSERT INTO feedback_snapshot_items
       (snapshot_phase_id, grade_id, grade_revision_id, role, reason, selection_reason, rank)
     VALUES (?, ?, ?, ?, ?, ?, ?)`
  );
  // Grade 1: aggregate + example (selected_note, rank 1)
  insertItem.run(10, 1, 1, "aggregate", null, "phase_population", null);
  // Deliberately mix revision ids across roles. The normalized row must use
  // the same revision selected as pinned_revision_id for all identity fields.
  insertItem.run(10, 1, 7, "example", null, "selected_recent_note", 1);
  // Grades 2, 3: aggregate only (no note selected)
  insertItem.run(10, 2, 2, "aggregate", null, "phase_population", null);
  insertItem.run(10, 3, 3, "aggregate", null, "phase_population", null);
  // Grades 4, 5: excluded
  insertItem.run(10, 4, 4, "excluded", "manual_exclude", "manual_exclude", null);
  insertItem.run(10, 5, 5, "excluded", "eligible_cap", "eligible_cap", null);

  // --- Snapshot 2 (scan 101, active) — shadow-vs-active used-count contrast ---
  db.prepare(
    `INSERT INTO feedback_snapshots
       (id, scan_id, policy_version, mode, as_of, lookback_days, max_grades,
        segment_min_grades, note_max_chars, population_count, eligible_count,
        excluded_count, created_at)
     VALUES (2, 101, 'evaluation-feedback/v1', 'active', '2026-05-16T00:00:00.000Z',
             90, 200, 5, 240, 1, 1, 0, '2026-05-16T00:00:01.000Z')`
  ).run();
  insertPhase.run(20, 2, "relevance", 800, 10, 0, JSON.stringify({ phase: "relevance" }), "one grade", "sha-relevance-2");
  insertPhase.run(21, 2, "reply_draft", 800, 0, 0, JSON.stringify({ phase: "reply_draft" }), "", "sha-empty");
  insertPhase.run(22, 2, "critic", 1000, 0, 0, JSON.stringify({ phase: "critic" }), "", "sha-empty");
  insertItem.run(20, 6, 6, "aggregate", null, "phase_population", null);

  // --- Snapshot 3 (scan 102, shadow, newest) — list-pagination fixture ---
  db.prepare(
    `INSERT INTO feedback_snapshots
       (id, scan_id, policy_version, mode, as_of, lookback_days, max_grades,
        segment_min_grades, note_max_chars, population_count, eligible_count,
        excluded_count, created_at)
     VALUES (3, 102, 'evaluation-feedback/v1', 'shadow', '2026-05-17T00:00:00.000Z',
             90, 200, 5, 240, 0, 0, 0, '2026-05-17T00:00:01.000Z')`
  ).run();
  insertPhase.run(30, 3, "relevance", 800, 0, 0, "{}", "", "sha-empty");
  insertPhase.run(31, 3, "reply_draft", 800, 0, 0, "{}", "", "sha-empty");
  insertPhase.run(32, 3, "critic", 1000, 0, 0, "{}", "", "sha-empty");

  // The current grade can change after a snapshot. Inspector identity and
  // ordering must continue to come from the pinned revision payload.
  db.prepare(
    `UPDATE grades SET post_id = 9999, graded_at = '2030-01-01T00:00:00.000Z' WHERE id = 1`
  ).run();

  db.close();
});

afterAll(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
  delete process.env.SCOUT_DB_PATH;
});

function makeHeaders(entries: Record<string, string> = {}): { get: (name: string) => string | null } {
  const allEntries = { host: "localhost", ...entries };
  const map = new Map(Object.entries(allEntries).map(([k, v]) => [k.toLowerCase(), v]));
  return { get: (name: string) => map.get(name.toLowerCase()) ?? null };
}

function makeNextRequest(
  url: string,
  headerEntries: Record<string, string> = {}
): { nextUrl: URL; headers: { get: (name: string) => string | null } } {
  return { nextUrl: new URL(url), headers: makeHeaders(headerEntries) };
}

describe("feedback query layer", () => {
  it("normalizes items to one row per grade, ordered included-before-excluded, note-rank, graded_at desc", async () => {
    const { getFeedbackSnapshotDetail } = await import("@/lib/feedback-queries");
    const detail = getFeedbackSnapshotDetail(1, { phase: "relevance", itemLimit: 100 });
    expect(detail).not.toBeNull();
    expect(detail!.items.data.map((i) => i.grade_id)).toEqual([1, 2, 3, 4, 5]);
  });

  it("marks the example-selected grade with selected_note and rank 1", async () => {
    const { getFeedbackSnapshotDetail } = await import("@/lib/feedback-queries");
    const detail = getFeedbackSnapshotDetail(1, { phase: "relevance", itemLimit: 100 });
    const grade1 = detail!.items.data.find((i) => i.grade_id === 1)!;
    expect(grade1.selected_note).toBe(true);
    expect(grade1.example_rank).toBe(1);
    expect(new Set(grade1.roles)).toEqual(new Set(["aggregate", "example"]));
    expect(new Set(grade1.selection_reasons)).toEqual(
      new Set(["phase_population", "selected_recent_note"])
    );
    expect(grade1.primary_use).toBe("included");

    const grade2 = detail!.items.data.find((i) => i.grade_id === 2)!;
    expect(grade2.selected_note).toBe(false);
    expect(grade2.example_rank).toBeNull();
  });

  it("uses pinned revision identity after the mutable current grade changes", async () => {
    const { getFeedbackSnapshotDetail } = await import("@/lib/feedback-queries");
    const detail = getFeedbackSnapshotDetail(1, { phase: "relevance", itemLimit: 100 });
    const grade1 = detail!.items.data.find((item) => item.grade_id === 1)!;
    expect(grade1.pinned_grade_revision_id).toBe(7);
    expect(grade1.post_id).toBe(1901);
    expect(grade1.graded_at).toBe("2026-05-12T00:00:00.000Z");
  });

  it("carries every stored exclusion reason on excluded grades", async () => {
    const { getFeedbackSnapshotDetail } = await import("@/lib/feedback-queries");
    const detail = getFeedbackSnapshotDetail(1, { phase: "relevance", use: "excluded", itemLimit: 100 });
    expect(detail!.items.data.map((i) => i.grade_id)).toEqual([4, 5]);
    expect(detail!.items.data[0].exclusion_reasons).toEqual(["manual_exclude"]);
    expect(detail!.items.data[1].exclusion_reasons).toEqual(["eligible_cap"]);
    expect(detail!.items.data.every((i) => i.primary_use === "excluded")).toBe(true);
  });

  it("filters use=included to only aggregate/example grades", async () => {
    const { getFeedbackSnapshotDetail } = await import("@/lib/feedback-queries");
    const detail = getFeedbackSnapshotDetail(1, { phase: "relevance", use: "included", itemLimit: 100 });
    expect(detail!.items.data.map((i) => i.grade_id)).toEqual([1, 2, 3]);
  });

  it("paginates the item page without skipping or duplicating rows", async () => {
    const { getFeedbackSnapshotDetail } = await import("@/lib/feedback-queries");
    const seen: number[] = [];
    let cursor: string | undefined;
    for (let i = 0; i < 10; i++) {
      const detail = getFeedbackSnapshotDetail(1, {
        phase: "relevance",
        itemLimit: 2,
        itemCursor: cursor ? decodeFeedbackItemCursor(cursor)! : undefined,
      });
      seen.push(...detail!.items.data.map((row) => row.grade_id));
      if (!detail!.items.has_more) break;
      cursor = detail!.items.next_cursor!;
    }
    expect(seen).toEqual([1, 2, 3, 4, 5]);
  });

  it("returns snapshot-wide counts on the header and phase-specific included/excluded/example counts", async () => {
    const { getFeedbackSnapshotDetail } = await import("@/lib/feedback-queries");
    const detail = getFeedbackSnapshotDetail(1, { phase: "relevance", itemLimit: 100 });
    expect(detail!.snapshot.population_count).toBe(5);
    expect(detail!.snapshot.eligible_count).toBe(3);
    expect(detail!.snapshot.excluded_count).toBe(2);
    expect(detail!.phases.relevance.counts.included_count).toBe(3);
    expect(detail!.phases.relevance.counts.excluded_count).toBe(2);
    expect(detail!.phases.relevance.counts.example_count).toBe(1);
  });

  it("computes actually_used_count as 0 for a shadow snapshot despite a nonzero included_count", async () => {
    const { getFeedbackSnapshotDetail } = await import("@/lib/feedback-queries");
    const detail = getFeedbackSnapshotDetail(1, { phase: "relevance", itemLimit: 100 });
    expect(detail!.snapshot.mode).toBe("shadow");
    expect(detail!.phases.relevance.counts.included_count).toBeGreaterThan(0);
    expect(detail!.phases.relevance.counts.actually_used_count).toBe(0);
  });

  it("computes actually_used_count equal to included_count for an active snapshot", async () => {
    const { getFeedbackSnapshotDetail } = await import("@/lib/feedback-queries");
    const detail = getFeedbackSnapshotDetail(2, { phase: "relevance", itemLimit: 100 });
    expect(detail!.snapshot.mode).toBe("active");
    expect(detail!.phases.relevance.counts.included_count).toBe(1);
    expect(detail!.phases.relevance.counts.actually_used_count).toBe(1);
  });

  it("preserves rendered_text bytes exactly — trailing whitespace, mixed indentation, unicode", async () => {
    const { getFeedbackSnapshotDetail } = await import("@/lib/feedback-queries");
    const detail = getFeedbackSnapshotDetail(1, { phase: "relevance", itemLimit: 100 });
    expect(detail!.phases.relevance.rendered_text).toBe(RELEVANCE_RENDERED_TEXT);
  });

  it("surfaces an empty rendered_text verbatim as the explicit no-eligible-feedback state", async () => {
    const { getFeedbackSnapshotDetail } = await import("@/lib/feedback-queries");
    const detail = getFeedbackSnapshotDetail(1, { phase: "reply_draft", itemLimit: 100 });
    expect(detail!.phases.reply_draft.rendered_text).toBe("");
    expect(detail!.items.phase).toBe("reply_draft");
  });

  it("returns null for a nonexistent snapshot id", async () => {
    const { getFeedbackSnapshotDetail } = await import("@/lib/feedback-queries");
    expect(getFeedbackSnapshotDetail(9999, {})).toBeNull();
  });

  it("paginates the snapshot list with a stable compound created_at/id cursor", async () => {
    const { listFeedbackSnapshots } = await import("@/lib/feedback-queries");
    const page1 = listFeedbackSnapshots({ limit: 2 });
    expect(page1.data.map((s) => s.snapshot_id)).toEqual([3, 2]);
    expect(page1.has_more).toBe(true);

    const { decodeSnapshotListCursor } = await import("@/lib/feedback-filters");
    const cursor = decodeSnapshotListCursor(page1.next_cursor!)!;
    const page2 = listFeedbackSnapshots({ limit: 2, cursor });
    expect(page2.data.map((s) => s.snapshot_id)).toEqual([1]);
    expect(page2.has_more).toBe(false);
  });

  it("filters the snapshot list by scanId and mode, returning an empty page for an unmatched filter", async () => {
    const { listFeedbackSnapshots } = await import("@/lib/feedback-queries");
    expect(listFeedbackSnapshots({ scanId: 101 }).data.map((s) => s.snapshot_id)).toEqual([2]);
    expect(listFeedbackSnapshots({ mode: "active" }).data.map((s) => s.snapshot_id)).toEqual([2]);
    expect(listFeedbackSnapshots({ scanId: 999999 })).toEqual({
      data: [],
      has_more: false,
      next_cursor: null,
    });
  });

  it("resolves a scan's feedback snapshot summary, and null when none exists", async () => {
    const { getFeedbackSnapshotSummaryForScan } = await import("@/lib/feedback-queries");
    expect(getFeedbackSnapshotSummaryForScan(100)).toEqual({
      snapshot_id: 1,
      policy_version: "evaluation-feedback/v1",
      mode: "shadow",
      population_count: 5,
      eligible_count: 3,
      excluded_count: 2,
    });
    expect(getFeedbackSnapshotSummaryForScan(999999)).toBeNull();
  });

  it("computes grade revision meta (count + latest recorded_at)", async () => {
    const { getGradeRevisionMeta, getGradeRevisionMetaBatch } = await import("@/lib/feedback-queries");
    expect(getGradeRevisionMeta(1)).toEqual({
      revision_count: 2,
      latest_recorded_at: "2026-05-12T00:00:00.000Z",
    });
    expect(getGradeRevisionMeta(999999)).toBeNull();

    const batch = getGradeRevisionMetaBatch([1, 2, 999999]);
    expect(batch.get(1)?.revision_count).toBe(2);
    expect(batch.get(2)?.revision_count).toBe(1);
    expect(batch.has(999999)).toBe(false);
  });
});

describe("GET /api/feedback/snapshots", () => {
  it("rejects an untrusted host with 403", async () => {
    const { GET } = await import("@/app/api/feedback/snapshots/route");
    const resp = await GET(makeNextRequest("http://localhost/api/feedback/snapshots", {
      host: "attacker.example.com",
    }) as never);
    expect(resp.status).toBe(403);
  });

  it("rejects an invalid mode filter with 400", async () => {
    const { GET } = await import("@/app/api/feedback/snapshots/route");
    const resp = await GET(
      makeNextRequest("http://localhost/api/feedback/snapshots?mode=bogus") as never
    );
    expect(resp.status).toBe(400);
  });

  it("rejects a malformed cursor with 400", async () => {
    const { GET } = await import("@/app/api/feedback/snapshots/route");
    const resp = await GET(
      makeNextRequest("http://localhost/api/feedback/snapshots?cursor=not-valid-base64!!") as never
    );
    expect(resp.status).toBe(400);
  });

  it("rejects snapshot cursors with invalid keyset fields", async () => {
    const { GET } = await import("@/app/api/feedback/snapshots/route");
    for (const cursorValue of [
      { created_at: "not-a-date", snapshot_id: 1 },
      { created_at: "2026-05-17T00:00:01.000Z", snapshot_id: 0 },
      { created_at: "2026-05-17T00:00:01.000Z", snapshot_id: 1.5 },
    ]) {
      const cursor = Buffer.from(JSON.stringify(cursorValue), "utf8").toString("base64url");
      const resp = await GET(
        makeNextRequest(`http://localhost/api/feedback/snapshots?cursor=${cursor}`) as never
      );
      expect(resp.status).toBe(400);
    }
  });

  it("returns the newest-first snapshot page for a trusted request", async () => {
    const { GET } = await import("@/app/api/feedback/snapshots/route");
    const resp = await GET(
      makeNextRequest("http://localhost/api/feedback/snapshots?limit=1") as never
    );
    expect(resp.status).toBe(200);
    const body = await resp.json();
    expect(body.data[0].snapshot_id).toBe(3);
    expect(body.has_more).toBe(true);
  });
});

describe("GET /api/feedback/snapshots/[snapshotId]", () => {
  async function callDetail(snapshotId: string, query = "") {
    const { GET } = await import("@/app/api/feedback/snapshots/[snapshotId]/route");
    return GET(
      makeNextRequest(`http://localhost/api/feedback/snapshots/${snapshotId}${query}`) as never,
      { params: Promise.resolve({ snapshotId }) }
    );
  }

  it("rejects an untrusted host with 403", async () => {
    const { GET } = await import("@/app/api/feedback/snapshots/[snapshotId]/route");
    const resp = await GET(
      makeNextRequest("http://localhost/api/feedback/snapshots/1", { host: "attacker.example.com" }) as never,
      { params: Promise.resolve({ snapshotId: "1" }) }
    );
    expect(resp.status).toBe(403);
  });

  it("rejects a non-numeric snapshot id with 400", async () => {
    const resp = await callDetail("abc");
    expect(resp.status).toBe(400);
  });

  it("rejects an invalid phase filter with 400", async () => {
    const resp = await callDetail("1", "?phase=bogus");
    expect(resp.status).toBe(400);
  });

  it("rejects a malformed itemCursor with 400", async () => {
    const resp = await callDetail("1", "?itemCursor=not-valid-base64!!");
    expect(resp.status).toBe(400);
  });

  it("rejects a malformed consumerCursor with 400", async () => {
    const resp = await callDetail("1", "?consumerCursor=not-valid-base64!!");
    expect(resp.status).toBe(400);
  });

  it("rejects item cursors with invalid or inconsistent keyset fields", async () => {
    const valid = {
      primary_use: "included",
      selected_note: true,
      example_rank: 1,
      graded_at: "2026-05-12T00:00:00.000Z",
      grade_id: 1,
    };
    for (const cursorValue of [
      { ...valid, graded_at: "not-a-date" },
      { ...valid, grade_id: 0 },
      { ...valid, grade_id: 1.5 },
      { ...valid, example_rank: 0 },
      { ...valid, example_rank: 1.5 },
      { ...valid, selected_note: false },
    ]) {
      const cursor = Buffer.from(JSON.stringify(cursorValue), "utf8").toString("base64url");
      const resp = await callDetail("1", `?itemCursor=${cursor}`);
      expect(resp.status).toBe(400);
    }
  });

  it("404s for a nonexistent snapshot", async () => {
    const resp = await callDetail("9999");
    expect(resp.status).toBe(404);
  });

  it("returns header, all three phase summaries, and the requested phase's item page", async () => {
    const resp = await callDetail("1", "?phase=critic&itemLimit=100");
    expect(resp.status).toBe(200);
    const body = await resp.json();
    expect(body.snapshot.snapshot_id).toBe(1);
    expect(body.snapshot.lookback_started_at).toBe(
      new Date(Date.parse("2026-05-15T00:00:00.000Z") - 90 * 24 * 60 * 60 * 1000).toISOString()
    );
    expect(Object.keys(body.phases).sort()).toEqual(["critic", "relevance", "reply_draft"]);
    expect(body.phases.critic.snapshot_phase_id).toBe(12);
    expect(body.items.phase).toBe("critic");
    expect(body.consumers.phase).toBe("critic");
    expect(body.consumers.data.map((run: { id: number }) => run.id)).toEqual([102]);
  });

  it("paginates phase consumers independently from grade items", async () => {
    const first = await callDetail("1", "?phase=relevance&itemLimit=1&consumerLimit=1");
    expect(first.status).toBe(200);
    const firstBody = await first.json();
    expect(firstBody.items.data).toHaveLength(1);
    expect(firstBody.consumers.data.map((run: { id: number }) => run.id)).toEqual([101]);
    expect(firstBody.consumers.has_more).toBe(true);

    const second = await callDetail(
      "1",
      `?phase=relevance&itemLimit=1&consumerLimit=1&consumerCursor=${firstBody.consumers.next_cursor}`
    );
    expect(second.status).toBe(200);
    const secondBody = await second.json();
    expect(secondBody.items.data).toHaveLength(1);
    expect(secondBody.consumers.data.map((run: { id: number }) => run.id)).toEqual([100]);
    expect(secondBody.consumers.has_more).toBe(false);
  });
});
