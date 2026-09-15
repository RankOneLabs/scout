import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-scans-"));
const dbPath = path.join(tmpDir, "scout.db");
process.env.SCOUT_DB_PATH = dbPath;

beforeAll(() => {
  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE scans (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      started_at TEXT NOT NULL,
      completed_at TEXT,
      messages_scanned INTEGER DEFAULT 0,
      relevant_found INTEGER DEFAULT 0,
      fetch_started_at TEXT,
      safe_watermark_at TEXT,
      status TEXT,
      overflow_count INTEGER DEFAULT 0,
      environment TEXT NOT NULL DEFAULT 'unknown',
      run_kind TEXT NOT NULL DEFAULT 'unknown',
      role TEXT NOT NULL DEFAULT 'canonical_live',
      lease_fence INTEGER,
      coverage_outcome TEXT,
      watermark_advanced INTEGER NOT NULL DEFAULT 0,
      coverage_classifier_version INTEGER,
      canonical_scan_id INTEGER REFERENCES scans(id)
    );
    CREATE TABLE scan_watermark_blockers (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      scan_id INTEGER NOT NULL,
      failure_id INTEGER NOT NULL,
      created_at TEXT NOT NULL,
      UNIQUE (scan_id, failure_id)
    );
    CREATE TABLE source_checkpoints (
      source_key TEXT PRIMARY KEY,
      platform TEXT NOT NULL,
      source_kind TEXT NOT NULL,
      provider_key TEXT NOT NULL,
      required INTEGER NOT NULL DEFAULT 1,
      active INTEGER NOT NULL DEFAULT 1,
      checkpoint_at TEXT,
      bootstrapped_from_legacy INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE environment_leases (
      environment TEXT PRIMARY KEY,
      fence INTEGER NOT NULL DEFAULT 0,
      updated_at TEXT NOT NULL,
      owner_id TEXT,
      expires_at TEXT
    );
    CREATE TABLE source_probe_runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      environment TEXT NOT NULL,
      started_at TEXT NOT NULL,
      completed_at TEXT,
      passed INTEGER,
      source_count INTEGER NOT NULL DEFAULT 0,
      page_count INTEGER NOT NULL DEFAULT 0,
      window_hours REAL NOT NULL DEFAULT 0,
      limits_json TEXT NOT NULL DEFAULT '{}',
      detail_json TEXT NOT NULL DEFAULT '{}',
      created_at TEXT NOT NULL
    );
    CREATE TABLE recovery_operations (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      environment TEXT NOT NULL,
      operation TEXT NOT NULL,
      operator TEXT NOT NULL,
      rationale TEXT NOT NULL,
      policy TEXT,
      source_evidence TEXT,
      probe_run_id INTEGER REFERENCES source_probe_runs(id),
      expected_old_watermark TEXT,
      accepted_new_watermark TEXT,
      outcome TEXT NOT NULL,
      detail TEXT,
      created_at TEXT NOT NULL
    );
    CREATE TABLE posts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      platform TEXT NOT NULL,
      platform_msg_id TEXT NOT NULL,
      channel_name TEXT,
      channel_id TEXT,
      author_name TEXT,
      author_id TEXT,
      content TEXT,
      url TEXT,
      created_at TEXT,
      scan_id INTEGER,
      UNIQUE(platform, platform_msg_id)
    );
    CREATE TABLE evaluations (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      post_id INTEGER NOT NULL,
      relevant INTEGER NOT NULL,
      score REAL NOT NULL,
      reason TEXT,
      relevant_to TEXT,
      scan_id INTEGER,
      posture TEXT
    );
    CREATE TABLE draft_comments (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      post_id INTEGER NOT NULL,
      evaluation_id INTEGER NOT NULL,
      project_key TEXT,
      comment_text TEXT,
      created_at TEXT,
      scan_id INTEGER
    );
    CREATE TABLE critiques (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      draft_id INTEGER NOT NULL,
      verdict TEXT NOT NULL,
      feedback TEXT,
      created_at TEXT,
      scan_id INTEGER
    );
    CREATE TABLE grades (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      evaluation_id INTEGER,
      post_id INTEGER NOT NULL,
      scan_id INTEGER,
      source TEXT NOT NULL,
      graded_at TEXT NOT NULL,
      schema_version INTEGER NOT NULL DEFAULT 1,
      needs_regrade INTEGER NOT NULL DEFAULT 0,
      relevance_judgment TEXT NOT NULL,
      action_judgment TEXT,
      dimensions TEXT,
      failure_note TEXT,
      factual_offending_claim TEXT,
      factual_disposition TEXT,
      factual_contradicting_evidence TEXT,
      context_missing_input TEXT,
      posture_should_have_been TEXT,
      implication_implied_claim TEXT,
      implication_missing_support TEXT
    );
    CREATE UNIQUE INDEX grades_evaluation_id_unique
      ON grades(evaluation_id) WHERE evaluation_id IS NOT NULL;
    CREATE TABLE gate_blocks (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      scan_id INTEGER
    );
    CREATE TABLE scan_fetch_failures (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      scan_id INTEGER NOT NULL,
      platform TEXT NOT NULL,
      context TEXT,
      kind TEXT NOT NULL,
      message TEXT,
      http_status INTEGER,
      retry_after TEXT,
      retryable INTEGER NOT NULL DEFAULT 1,
      operation_phase TEXT NOT NULL DEFAULT 'unknown',
      blocks_watermark_advance INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL,
      UNIQUE (scan_id, id)
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
  `);

  const now = "2026-05-15T00:00:00Z";
  const insertScan = db.prepare(
    `INSERT INTO scans (id, started_at, completed_at, messages_scanned, relevant_found)
     VALUES (?, ?, ?, ?, ?)`
  );

  insertScan.run(1, now, now, 0, 0);
  insertScan.run(2, now, now, 0, 0);
  insertScan.run(3, now, null, 0, 0);
  insertScan.run(4, now, now, 1, 0);

  db.prepare(
    `INSERT INTO posts
      (id, platform, platform_msg_id, channel_name, channel_id, author_name,
       author_id, content, url, created_at, scan_id)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).run(
    1,
    "discord",
    "msg-1",
    "general",
    "ch-1",
    "alice",
    "user-1",
    "hello",
    "https://example.com/1",
    now,
    2
  );

  // Evaluation linking post 1 to scan 2 (required for evaluation-scoped grading)
  db.prepare(
    `INSERT INTO evaluations (post_id, relevant, score, scan_id) VALUES (?, ?, ?, ?)`
  ).run(1, 1, 0.9, 2);
  db.prepare(
    `INSERT INTO evaluations (post_id, relevant, score, scan_id) VALUES (?, ?, ?, ?)`
  ).run(1, 1, 0.5, 4);

  // Scan 4 has a feedback snapshot; scan 2 (queried below) does not.
  db.prepare(
    `INSERT INTO feedback_snapshots
       (scan_id, policy_version, mode, as_of, lookback_days, max_grades,
        segment_min_grades, note_max_chars, population_count, eligible_count,
        excluded_count, created_at)
     VALUES (4, 'evaluation-feedback/v1', 'shadow', ?, 90, 200, 5, 240, 0, 0, 0, ?)`
  ).run(now, now);

  // Coverage/watermark fixtures — status and coverage_outcome are
  // independently set, per decision 4: neither is inferable from the
  // other.
  const insertCoverageScan = db.prepare(
    `INSERT INTO scans
       (id, started_at, completed_at, fetch_started_at, safe_watermark_at,
        messages_scanned, relevant_found, status, environment, run_kind, role,
        coverage_outcome, watermark_advanced, coverage_classifier_version,
        canonical_scan_id)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  );

  // Scan 5: status='partial' (non-blocking parent-lookup degradation) but
  // coverage_outcome='complete' and the watermark still advanced.
  insertCoverageScan.run(
    5, now, now, now, now, 3, 1, "partial", "production", "live", "canonical_live",
    "complete", 1, 1, null
  );
  // Scan 6: status='complete' (processing finished cleanly) but
  // coverage_outcome='blocked' — a primary source failed to cover, so the
  // watermark never advanced despite complete processing.
  insertCoverageScan.run(
    6, now, now, now, null, 5, 2, "complete", "production", "live", "canonical_live",
    "blocked", 0, 1, null
  );
  // Scan 7: a secondary (--mode both second pass) linked to scan 6's
  // canonical ownership — never eligible to advance regardless of its own
  // outcome.
  insertCoverageScan.run(
    7, now, now, now, null, 5, 2, "complete", "production", "live", "secondary",
    null, 0, null, 6
  );
  // Scan 8: failed outright — never advances regardless of any coverage
  // read.
  insertCoverageScan.run(
    8, now, null, now, null, 0, 0, "failed", "production", "live", "canonical_live",
    null, 0, null, null
  );
  // Scan 9: a canonical owner interrupted mid-flight and reconciled under a
  // stale fence. Finalization never ran, so its blocking-eligible failure
  // has no scan_watermark_blockers row — it was never adjudicated, which
  // is distinct from being non-blocking.
  insertCoverageScan.run(
    9, now, now, now, null, 0, 0, "interrupted", "production", "live", "canonical_live",
    null, 0, null, null
  );
  // Scan 10: a terminal partial fetch with no counters is failure evidence,
  // not historical empty noise.
  insertCoverageScan.run(
    10, now, now, now, null, 0, 0, "partial", "production", "live", "canonical_live",
    null, 0, null, null
  );
  // Scan 11: a finalized empty success is meaningful coverage evidence.
  insertCoverageScan.run(
    11, now, now, now, now, 0, 0, "complete", "production", "live", "canonical_live",
    "complete", 1, 1, null
  );
  // Scan 12: safe_watermark_at alone is not advancement provenance.
  insertCoverageScan.run(
    12, now, now, now, "2027-01-01T00:00:00Z", 0, 0, "complete", "production", "live", "canonical_live",
    "blocked", 0, 1, null
  );
  db.prepare(
    `INSERT INTO scan_fetch_failures
       (id, scan_id, platform, context, kind, message, operation_phase,
        blocks_watermark_advance, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).run(
    1003, 9, "scan_runner", "reconciliation", "abandoned_owner",
    "canonical owner abandoned under a stale lease fence; reconciled at startup",
    "scan", 1, now
  );

  // Scan 5's non-blocking degradation: a parent_lookup failure that never
  // blocked the watermark (operation_phase excluded from the blocking set).
  db.prepare(
    `INSERT INTO scan_fetch_failures
       (id, scan_id, platform, context, kind, message, operation_phase,
        blocks_watermark_advance, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).run(1001, 5, "bluesky", "parent-ctx", "parent_lookup_failed", "timeout", "parent_lookup", 0, now);

  // Scan 6's blocking failure: a primary fetch failure, durably recorded
  // as the exact reason coverage was blocked (scan_watermark_blockers).
  db.prepare(
    `INSERT INTO scan_fetch_failures
       (id, scan_id, platform, context, kind, message, operation_phase,
        blocks_watermark_advance, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).run(1002, 6, "bluesky", "src-a", "page_ceiling", "hit page ceiling", "fetch", 1, now);
  db.prepare(
    `INSERT INTO scan_watermark_blockers (scan_id, failure_id, created_at)
     VALUES (?, ?, ?)`
  ).run(6, 1002, now);

  db.prepare(
    `INSERT INTO source_checkpoints
       (source_key, platform, source_kind, provider_key, required, active,
        checkpoint_at, bootstrapped_from_legacy, created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).run("bluesky:search:src-a", "bluesky", "search", "src-a", 1, 1, now, 0, now, now);

  db.prepare(
    `INSERT INTO environment_leases (environment, fence, updated_at, owner_id, expires_at)
     VALUES (?, ?, ?, ?, ?)`
  ).run("production", 3, now, "owner-1", now);

  db.prepare(
    `INSERT INTO source_probe_runs
       (id, environment, started_at, completed_at, passed, source_count,
        page_count, window_hours, limits_json, detail_json, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).run(1, "production", now, now, 1, 3, 6, 6.0, "{}", "{}", now);

  db.prepare(
    `INSERT INTO recovery_operations
       (id, environment, operation, operator, rationale, policy, source_evidence,
        probe_run_id, expected_old_watermark, accepted_new_watermark, outcome,
        detail, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).run(
    1, "production", "backfill", "steve", "recover from gap", null, null,
    null, null, null, "accepted", null, now
  );

  db.close();
});

afterAll(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
  delete process.env.SCOUT_DB_PATH;
});

function makeHeaders(
  entries: Record<string, string> = {}
): { get: (name: string) => string | null } {
  const allEntries = {
    host: "localhost",
    ...entries,
  };
  const map = new Map(
    Object.entries(allEntries).map(([k, v]) => [k.toLowerCase(), v])
  );
  return { get: (name: string) => map.get(name.toLowerCase()) ?? null };
}

function makeNextRequest(url: string): {
  nextUrl: URL;
  headers: { get: (name: string) => string | null };
} {
  return { nextUrl: new URL(url), headers: makeHeaders() };
}

describe("scans query layer", () => {
  it("hides historical true-empty completed scans from the list", async () => {
    const { getScans } = await import("@/lib/queries");

    expect(getScans().map((scan) => scan.id)).toEqual([
      12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2,
    ]);
  });

  it("returns 404 for a hidden true-empty completed scan id", async () => {
    const { GET } = await import("@/app/api/scans/route");

    const resp = await GET(makeNextRequest("http://localhost/api/scans?id=1") as never);
    expect(resp.status).toBe(404);
  });

  it.each([10, 11])("keeps meaningful zero-count scan %i visible", async (id) => {
    const { GET } = await import("@/app/api/scans/route");
    const resp = await GET(makeNextRequest(`http://localhost/api/scans?id=${id}`) as never);
    expect(resp.status).toBe(200);
  });

  it("includes the scan's feedback snapshot summary when one is recorded", async () => {
    const { GET } = await import("@/app/api/scans/route");
    const resp = await GET(makeNextRequest("http://localhost/api/scans?id=4") as never);
    expect(resp.status).toBe(200);
    const body = await resp.json();
    expect(body.feedback).toMatchObject({ mode: "shadow", policy_version: "evaluation-feedback/v1" });
  });

  it("returns feedback: null for a scan with no recorded snapshot", async () => {
    const { GET } = await import("@/app/api/scans/route");
    const resp = await GET(makeNextRequest("http://localhost/api/scans?id=2") as never);
    expect(resp.status).toBe(200);
    const body = await resp.json();
    expect(body.feedback).toBeNull();
  });
});

describe("coverage and watermark facts are independent of processing status", () => {
  it("reports status:partial with coverage complete and an advanced watermark", async () => {
    const { GET } = await import("@/app/api/scans/route");
    const resp = await GET(makeNextRequest("http://localhost/api/scans?id=5") as never);
    const body = await resp.json();
    expect(body.status).toBe("partial");
    expect(body.coverage_outcome).toBe("complete");
    expect(body.watermark_advanced).toBe(true);
  });

  it("reports status:complete with coverage blocked and no watermark advance", async () => {
    const { GET } = await import("@/app/api/scans/route");
    const resp = await GET(makeNextRequest("http://localhost/api/scans?id=6") as never);
    const body = await resp.json();
    expect(body.status).toBe("complete");
    expect(body.coverage_outcome).toBe("blocked");
    expect(body.watermark_advanced).toBe(false);
  });

  it("links a non-advancing secondary scan to its canonical owner", async () => {
    const { GET } = await import("@/app/api/scans/route");
    const resp = await GET(makeNextRequest("http://localhost/api/scans?id=7") as never);
    const body = await resp.json();
    expect(body.role).toBe("secondary");
    expect(body.canonical_scan_id).toBe(6);
    expect(body.watermark_advanced).toBe(false);
  });

  it("never advances an interrupted canonical owner, and leaves its blocking-eligible failure unadjudicated", async () => {
    const { GET } = await import("@/app/api/scans/route");
    const resp = await GET(makeNextRequest("http://localhost/api/scans?id=9") as never);
    const body = await resp.json();
    expect(body.status).toBe("interrupted");
    expect(body.role).toBe("canonical_live");
    expect(body.watermark_advanced).toBe(false);
    expect(body.coverage_outcome).toBeNull();
    expect(body.failures).toHaveLength(1);
    // Eligible to block, but never counted: finalization never ran.
    expect(body.failures[0]).toMatchObject({
      id: 1003,
      kind: "abandoned_owner",
      blocks_watermark_advance: true,
      blocked_watermark: false,
    });
  });

  it("reports the environment's current watermark separately from the selected scan's own", async () => {
    const { GET } = await import("@/app/api/scans/route");
    // Scan 6 is blocked (own watermark null) but scan 5 advanced the
    // environment — staleness must be judged against scan 5's cursor.
    const resp = await GET(makeNextRequest("http://localhost/api/scans?id=6") as never);
    const body = await resp.json();
    expect(body.safe_watermark_at).toBeNull();
    expect(body.environment_watermark_at).toBe("2026-05-15T00:00:00Z");
  });

  it("ignores a safe watermark lacking advancement provenance", async () => {
    const { GET } = await import("@/app/api/scans/route");
    const resp = await GET(makeNextRequest("http://localhost/api/scans?id=12") as never);
    const body = await resp.json();
    expect(body.safe_watermark_at).toBe("2027-01-01T00:00:00Z");
    expect(body.environment_watermark_at).toBe("2026-05-15T00:00:00Z");
  });

  it("never advances a failed scan's watermark", async () => {
    const { GET } = await import("@/app/api/scans/route");
    const resp = await GET(makeNextRequest("http://localhost/api/scans?id=8") as never);
    const body = await resp.json();
    expect(body.status).toBe("failed");
    expect(body.watermark_advanced).toBe(false);
    expect(body.coverage_outcome).toBeNull();
  });

  it("identifies the exact blocking failure by id and operation phase, distinct from non-blocking degradation", async () => {
    const { GET } = await import("@/app/api/scans/route");

    const blockedResp = await GET(makeNextRequest("http://localhost/api/scans?id=6") as never);
    const blocked = await blockedResp.json();
    expect(blocked.failures).toHaveLength(1);
    expect(blocked.failures[0]).toMatchObject({
      id: 1002,
      operation_phase: "fetch",
      blocks_watermark_advance: true,
      blocked_watermark: true,
    });

    const degradedResp = await GET(makeNextRequest("http://localhost/api/scans?id=5") as never);
    const degraded = await degradedResp.json();
    expect(degraded.failures).toHaveLength(1);
    expect(degraded.failures[0]).toMatchObject({
      id: 1001,
      operation_phase: "parent_lookup",
      blocks_watermark_advance: false,
      blocked_watermark: false,
    });
  });

  it("exposes the environment's current source checkpoints on scan detail", async () => {
    const { GET } = await import("@/app/api/scans/route");
    const resp = await GET(makeNextRequest("http://localhost/api/scans?id=6") as never);
    const body = await resp.json();
    expect(body.source_checkpoints).toEqual([
      expect.objectContaining({
        source_key: "bluesky:search:src-a",
        platform: "bluesky",
        required: true,
        active: true,
        bootstrapped_from_legacy: false,
      }),
    ]);
  });

  it("exposes the environment's current lease, recovery audit trail, and latest probe", async () => {
    const { GET } = await import("@/app/api/scans/route");
    const resp = await GET(makeNextRequest("http://localhost/api/scans?id=6") as never);
    const body = await resp.json();

    expect(body.environment_lease).toMatchObject({ environment: "production", fence: 3, owner_id: "owner-1" });
    expect(body.recent_recovery_operations).toHaveLength(1);
    expect(body.recent_recovery_operations[0]).toMatchObject({ operation: "backfill", outcome: "accepted" });
    expect(body.latest_probe_run).toMatchObject({ environment: "production", passed: true, page_count: 6 });
  });
});

describe("grade write context guard", () => {
  function makeGradeRequest(headers: Record<string, string> = {}, body?: unknown) {
    return {
      nextUrl: new URL("http://localhost/api/scans/2/posts/1/grade"),
      headers: makeHeaders(headers),
      json: async () => body ?? {},
      text: async () => (body !== undefined ? JSON.stringify(body) : ""),
    };
  }

  it("rejects POST grade from an untrusted host", async () => {
    const { POST } = await import("@/app/api/scans/[id]/posts/[postId]/grade/route");
    const resp = await POST(
      makeGradeRequest({ host: "attacker.example.com" }) as never,
      { params: Promise.resolve({ id: "2", postId: "1" }) }
    );
    expect(resp.status).toBe(403);
  });

  it("rejects POST grade when x-forwarded-for is set", async () => {
    const { POST } = await import("@/app/api/scans/[id]/posts/[postId]/grade/route");
    const resp = await POST(
      makeGradeRequest({ "x-forwarded-for": "1.2.3.4" }) as never,
      { params: Promise.resolve({ id: "2", postId: "1" }) }
    );
    expect(resp.status).toBe(403);
  });

  it("rejects POST grade when origin is external", async () => {
    const { POST } = await import("@/app/api/scans/[id]/posts/[postId]/grade/route");
    const resp = await POST(
      makeGradeRequest({ "origin": "https://evil.example.com" }) as never,
      { params: Promise.resolve({ id: "2", postId: "1" }) }
    );
    expect(resp.status).toBe(403);
  });

  it("returns 400 for non-numeric scan id", async () => {
    const { POST } = await import("@/app/api/scans/[id]/posts/[postId]/grade/route");
    const resp = await POST(
      makeGradeRequest() as never,
      { params: Promise.resolve({ id: "abc", postId: "1" }) }
    );
    expect(resp.status).toBe(400);
  });

  it("returns 400 for path-injection post id", async () => {
    const { POST } = await import("@/app/api/scans/[id]/posts/[postId]/grade/route");
    const injectedId = "1%2Fgrade%3Fx%3D";
    const resp = await POST(
      makeGradeRequest() as never,
      { params: Promise.resolve({ id: "2", postId: injectedId }) }
    );
    expect(resp.status).toBe(400);
  });

  it("returns 404 when post has no evaluation in the requested scan", async () => {
    const { POST } = await import("@/app/api/scans/[id]/posts/[postId]/grade/route");
    const resp = await POST(
      makeGradeRequest({}, { relevance_judgment: "correct", action_judgment: "accept" }) as never,
      { params: Promise.resolve({ id: "3", postId: "1" }) }
    );
    expect(resp.status).toBe(404);
  });
});

// Grade validation, persistence, and legacy-adoption behavior now live in
// the sidecar (see tests/test_grading_api_sidecar.py::TestGrade) and this
// route's own forwarding contract (see __tests__/grade-routes.test.ts) —
// this file retains only the guard checks above, which run entirely
// before the route ever calls the sidecar.
