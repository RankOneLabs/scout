import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-feedback-overview-routes-"));
const dbPath = path.join(tmpDir, "scout.db");
process.env.SCOUT_DB_PATH = dbPath;

beforeAll(() => {
  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE scans (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL);
    CREATE TABLE posts (
      id INTEGER PRIMARY KEY, platform TEXT NOT NULL, platform_msg_id TEXT NOT NULL,
      author_name TEXT, author_id TEXT, content TEXT, url TEXT, created_at TEXT
    );
    CREATE TABLE evaluations (
      id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL, scan_id INTEGER, relevant INTEGER NOT NULL,
      score REAL NOT NULL, reason TEXT, project_key TEXT, posture TEXT, surface_status TEXT, failure_reason TEXT,
      dossier_summary_id TEXT, dossier_revision TEXT
    );
    CREATE TABLE draft_comments (
      id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL, evaluation_id INTEGER NOT NULL,
      project_key TEXT, comment_text TEXT, created_at TEXT, posture TEXT,
      dossier_summary_id TEXT, dossier_revision TEXT
    );
    CREATE TABLE critiques (id INTEGER PRIMARY KEY, draft_id INTEGER, evaluation_id INTEGER, verdict TEXT, feedback TEXT, created_at TEXT);
    CREATE TABLE grades (
      id INTEGER PRIMARY KEY, evaluation_id INTEGER, post_id INTEGER NOT NULL, scan_id INTEGER,
      source TEXT NOT NULL, graded_at TEXT NOT NULL, schema_version INTEGER NOT NULL DEFAULT 1,
      needs_regrade INTEGER NOT NULL DEFAULT 0, relevance_judgment TEXT NOT NULL,
      action_judgment TEXT, dimensions TEXT, failure_note TEXT, factual_offending_claim TEXT,
      factual_disposition TEXT, factual_contradicting_evidence TEXT,
      context_missing_input TEXT, posture_should_have_been TEXT,
      implication_implied_claim TEXT, implication_missing_support TEXT
    );
    CREATE TABLE grade_revisions (
      id INTEGER PRIMARY KEY, grade_id INTEGER NOT NULL, evaluation_id INTEGER, revision INTEGER NOT NULL,
      schema_version INTEGER NOT NULL DEFAULT 2,
      source TEXT NOT NULL, payload TEXT NOT NULL, recorded_at TEXT NOT NULL
    );
    CREATE TABLE grade_usage_overrides (id INTEGER PRIMARY KEY, grade_id INTEGER UNIQUE, mode TEXT, reason TEXT, updated_at TEXT);
    CREATE TABLE feedback_snapshots (
      id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL UNIQUE, policy_version TEXT NOT NULL, mode TEXT NOT NULL,
      as_of TEXT NOT NULL, lookback_days INTEGER NOT NULL, max_grades INTEGER NOT NULL, created_at TEXT NOT NULL,
      population_count INTEGER NOT NULL, eligible_count INTEGER NOT NULL, excluded_count INTEGER NOT NULL
    );
    CREATE TABLE feedback_snapshot_phases (id INTEGER PRIMARY KEY, snapshot_id INTEGER, phase TEXT);
    CREATE TABLE feedback_snapshot_items (
      id INTEGER PRIMARY KEY, snapshot_phase_id INTEGER, grade_id INTEGER, grade_revision_id INTEGER,
      role TEXT, reason TEXT, created_at TEXT
    );
    CREATE TABLE evaluation_phase_runs (
      id INTEGER PRIMARY KEY, scan_id INTEGER, post_id INTEGER, evaluation_id INTEGER,
      snapshot_phase_id INTEGER, phase TEXT, trace_id TEXT UNIQUE, model TEXT, status TEXT,
      created_at TEXT
    );
  `);

  db.prepare(`INSERT INTO scans (id, started_at) VALUES (1, '2026-01-01T00:00:00.000Z')`).run();
  db.prepare(`INSERT INTO posts (id, platform, platform_msg_id) VALUES (1, 'discord', 'm1')`).run();
  db.prepare(
    `INSERT INTO evaluations (id, post_id, scan_id, relevant, score) VALUES (1, 1, 1, 1, 0.9)`
  ).run();
  db.prepare(
    `INSERT INTO grades (id, evaluation_id, post_id, scan_id, source, graded_at, schema_version, needs_regrade, relevance_judgment, action_judgment)
     VALUES (1, 1, 1, 1, 'web', '2026-01-15T00:00:00.000Z', 2, 0, 'correct', 'accept')`
  ).run();
  db.prepare(
    `INSERT INTO grade_revisions (id, grade_id, evaluation_id, revision, source, payload, recorded_at)
     VALUES (1, 1, 1, 1, 'web', '{"graded_at":"2026-01-15T00:00:00.000Z"}', '2026-01-15T00:00:00.000Z')`
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

const UNTRUSTED = { host: "attacker.example.com" };

describe("GET /api/feedback/summary", () => {
  it("rejects an untrusted host with 403", async () => {
    const { GET } = await import("@/app/api/feedback/summary/route");
    const resp = await GET(makeNextRequest("http://localhost/api/feedback/summary", UNTRUSTED) as never);
    expect(resp.status).toBe(403);
  });

  it("rejects a non-ISO-8601 from/to with 400", async () => {
    const { GET } = await import("@/app/api/feedback/summary/route");
    const resp = await GET(
      makeNextRequest("http://localhost/api/feedback/summary?from=not-a-date") as never
    );
    expect(resp.status).toBe(400);
  });

  it("requires explicit UTC-Z from/to timestamps", async () => {
    const { GET } = await import("@/app/api/feedback/summary/route");
    for (const from of ["2026-01-01T00:00:00.000", "2026-01-01T01:00:00.000%2B01:00"]) {
      const resp = await GET(
        makeNextRequest(`http://localhost/api/feedback/summary?from=${from}`) as never
      );
      expect(resp.status).toBe(400);
    }
  });

  it("rejects from > to with 400", async () => {
    const { GET } = await import("@/app/api/feedback/summary/route");
    const resp = await GET(
      makeNextRequest(
        "http://localhost/api/feedback/summary?from=2026-02-01T00:00:00.000Z&to=2026-01-01T00:00:00.000Z"
      ) as never
    );
    expect(resp.status).toBe(400);
  });

  it("defaults to a 90-day window ending at as_of when from/to are omitted", async () => {
    const { GET } = await import("@/app/api/feedback/summary/route");
    const resp = await GET(makeNextRequest("http://localhost/api/feedback/summary") as never);
    expect(resp.status).toBe(200);
    const body = await resp.json();
    expect(body.timezone).toBe("UTC");
    expect(body.to).toBe(body.as_of);
    const spanDays = (Date.parse(body.to) - Date.parse(body.from)) / (24 * 60 * 60 * 1000);
    expect(spanDays).toBeCloseTo(90, 5);
  });

  it("returns policy inputs and corpus totals for an explicit window", async () => {
    const { GET } = await import("@/app/api/feedback/summary/route");
    const resp = await GET(
      makeNextRequest(
        "http://localhost/api/feedback/summary?from=2026-01-01T00:00:00.000Z&to=2026-02-01T00:00:00.000Z"
      ) as never
    );
    expect(resp.status).toBe(200);
    const body = await resp.json();
    expect(body.from).toBe("2026-01-01T00:00:00.000Z");
    expect(body.to).toBe("2026-02-01T00:00:00.000Z");
    expect(body.corpus.total).toEqual({ count: 1, denominator: 1 });
    expect(body.policy).toBeNull(); // no feedback_snapshots row in this fixture
  });
});

describe("GET /api/feedback/grades", () => {
  it("rejects an untrusted host with 403", async () => {
    const { GET } = await import("@/app/api/feedback/grades/route");
    const resp = await GET(makeNextRequest("http://localhost/api/feedback/grades", UNTRUSTED) as never);
    expect(resp.status).toBe(403);
  });

  it("rejects an invalid filter value with 400", async () => {
    const { GET } = await import("@/app/api/feedback/grades/route");
    const resp = await GET(
      makeNextRequest("http://localhost/api/feedback/grades?relevanceJudgment=bogus") as never
    );
    expect(resp.status).toBe(400);
  });

  it("rejects malformed and inverted grade/edit time bounds", async () => {
    const { GET } = await import("@/app/api/feedback/grades/route");
    const malformed = await GET(
      makeNextRequest("http://localhost/api/feedback/grades?gradedFrom=yesterday") as never
    );
    expect(malformed.status).toBe(400);
    const inverted = await GET(
      makeNextRequest(
        "http://localhost/api/feedback/grades?editedFrom=2026-02-01T00:00:00.000Z&editedTo=2026-01-01T00:00:00.000Z"
      ) as never
    );
    expect(inverted.status).toBe(400);
  });

  it("rejects a malformed cursor with 400", async () => {
    const { GET } = await import("@/app/api/feedback/grades/route");
    const resp = await GET(
      makeNextRequest("http://localhost/api/feedback/grades?cursor=not-valid-base64!!") as never
    );
    expect(resp.status).toBe(400);
  });

  it("returns the grade explorer page with total_matching for a trusted request", async () => {
    const { GET } = await import("@/app/api/feedback/grades/route");
    const resp = await GET(makeNextRequest("http://localhost/api/feedback/grades") as never);
    expect(resp.status).toBe(200);
    const body = await resp.json();
    expect(body.total_matching).toBe(1);
    expect(body.data[0].grade_id).toBe(1);
    expect(body.data[0].trace_available).toBe(false);
  });
});

describe("GET /api/feedback/grades/[gradeId]", () => {
  async function callDetail(gradeId: string, query = "") {
    const { GET } = await import("@/app/api/feedback/grades/[gradeId]/route");
    return GET(makeNextRequest(`http://localhost/api/feedback/grades/${gradeId}${query}`) as never, {
      params: Promise.resolve({ gradeId }),
    });
  }

  it("rejects an untrusted host with 403", async () => {
    const { GET } = await import("@/app/api/feedback/grades/[gradeId]/route");
    const resp = await GET(
      makeNextRequest("http://localhost/api/feedback/grades/1", UNTRUSTED) as never,
      { params: Promise.resolve({ gradeId: "1" }) }
    );
    expect(resp.status).toBe(403);
  });

  it("rejects a non-numeric grade id with 400", async () => {
    const resp = await callDetail("abc");
    expect(resp.status).toBe(400);
  });

  it("rejects a malformed revisionCursor with 400", async () => {
    const resp = await callDetail("1", "?revisionCursor=not-valid-base64!!");
    expect(resp.status).toBe(400);
  });

  it("rejects a malformed phaseRunCursor with 400", async () => {
    const resp = await callDetail("1", "?phaseRunCursor=opaque");
    expect(resp.status).toBe(400);
  });

  it("404s for a nonexistent grade", async () => {
    const resp = await callDetail("9999");
    expect(resp.status).toBe(404);
  });

  it("returns the canonical grade envelope, evidence, and independent history pages", async () => {
    const resp = await callDetail("1");
    expect(resp.status).toBe(200);
    const body = await resp.json();
    expect(body.grade.id).toBe(1);
    expect(body.evidence.post.id).toBe(1);
    expect(body.revision_history.data).toHaveLength(1);
    expect(body.snapshot_use_history.data).toHaveLength(0);
    expect(body.phase_runs).toEqual({ data: [], has_more: false, next_cursor: null });
    expect(body.trace_available).toBe(false);
  });
});

describe("GET /api/feedback/grades/by-evaluation/[evaluationId]", () => {
  async function callByEvaluation(evaluationId: string) {
    const { GET } = await import(
      "@/app/api/feedback/grades/by-evaluation/[evaluationId]/route"
    );
    return GET(
      makeNextRequest(
        `http://localhost/api/feedback/grades/by-evaluation/${evaluationId}`
      ) as never,
      { params: Promise.resolve({ evaluationId }) }
    );
  }

  it("resolves the current authoritative grade from an evaluation id", async () => {
    const response = await callByEvaluation("1");
    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body.grade.id).toBe(1);
    expect(body.grade.evaluation_id).toBe(1);
  });

  it("rejects invalid ids and returns 404 when no grade is linked", async () => {
    expect((await callByEvaluation("abc")).status).toBe(400);
    expect((await callByEvaluation("9999")).status).toBe(404);
  });
});
