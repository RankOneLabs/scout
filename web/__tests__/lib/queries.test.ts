import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-jev-web-"));
const dbPath = path.join(tmpDir, "scout.db");
const tracesPath = path.join(tmpDir, "traces.db");
process.env.SCOUT_DB_PATH = dbPath;
process.env.TRACE_DB_PATH = tracesPath;

beforeAll(() => {
  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE posts (
      id INTEGER PRIMARY KEY, platform TEXT NOT NULL, platform_msg_id TEXT NOT NULL,
      channel_name TEXT, channel_id TEXT, author_name TEXT, author_id TEXT,
      content TEXT, url TEXT, created_at TEXT, scan_id INTEGER,
      parent_lookup_status TEXT NOT NULL DEFAULT 'not_applicable',
      parent_id TEXT, parent_author_id TEXT, parent_author_name TEXT,
      parent_text TEXT, parent_url TEXT
    );
    CREATE TABLE evaluations (
      id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL, relevant INTEGER NOT NULL,
      score REAL NOT NULL, reason TEXT, relevant_to TEXT, keyword_route_id INTEGER,
      scan_id INTEGER, surface_status TEXT, posture TEXT, project_key TEXT,
      failure_reason TEXT, dossier_revision TEXT, dossier_summary_id TEXT
    );
    CREATE TABLE evaluation_phase_runs (
      id INTEGER PRIMARY KEY, evaluation_id INTEGER, phase TEXT NOT NULL,
      trace_id TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE TABLE draft_comments (
      id INTEGER PRIMARY KEY, post_id INTEGER, evaluation_id INTEGER, project_key TEXT,
      comment_text TEXT, created_at TEXT, scan_id INTEGER, posture TEXT,
      structured_output TEXT, dossier_revision TEXT, dossier_summary_id TEXT
    );
    CREATE TABLE critiques (
      id INTEGER PRIMARY KEY, draft_id INTEGER, evaluation_id INTEGER, verdict TEXT,
      feedback TEXT, created_at TEXT, scan_id INTEGER
    );
    CREATE TABLE project_keywords (
      id INTEGER PRIMARY KEY, project_key TEXT NOT NULL, keyword TEXT NOT NULL,
      match_type TEXT, intent TEXT, positive_context TEXT, negative_context TEXT,
      evaluate_prompt TEXT, respond_prompt TEXT, critique_prompt TEXT
    );
    CREATE TABLE prompt_templates (
      name TEXT PRIMARY KEY, body TEXT NOT NULL, kind TEXT NOT NULL,
      active INTEGER NOT NULL DEFAULT 1
    );
    INSERT INTO posts (id, platform, platform_msg_id, author_name, content, scan_id)
      VALUES (1, 'discord', 'llm-low', 'Ada', 'LLM 0.8', 7),
             (2, 'discord', 'jev-review', 'Bea', 'Jev review', 7),
             (3, 'discord', 'llm-high', 'Cy', 'LLM 0.95', 7);
    INSERT INTO evaluations
      (id, post_id, relevant, score, reason, relevant_to, scan_id, surface_status)
      VALUES (11, 1, 1, .8, 'llm low', '[]', 7, 'surfaced'),
             (12, 2, 0, 1, 'needs_thread', '[]', 7, 'not_relevant'),
             (13, 3, 1, .95, 'llm high', '[]', 7, 'surfaced');
    INSERT INTO evaluation_phase_runs
      (id, evaluation_id, phase, trace_id, model, status, created_at)
      VALUES (21, 11, 'relevance', 'trace-llm-low', 'openrouter/acme/model', 'complete', '2026-09-01T00:00:00Z'),
             (22, 12, 'relevance', 'trace-jev', 'jev:jev-latest', 'complete', '2026-09-01T00:00:01Z'),
             (23, 13, 'relevance', 'trace-llm-high', 'openrouter/acme/model', 'complete', '2026-09-01T00:00:02Z');
  `);
  db.close();

  const traces = new Database(tracesPath);
  traces.exec(`CREATE TABLE spans (trace_id TEXT, parent_id TEXT, output TEXT);`);
  traces.prepare("INSERT INTO spans VALUES (?, NULL, ?)").run(
    "trace-jev",
    JSON.stringify({
      output_kind: "structured",
      output_complete: {
        relevant: false,
        score: 1,
        reason: "needs_thread",
        relevant_to: [],
        action: "review",
        answers: {
          excl_hype: 0.1,
          needs_thread: 0.8,
          answerable_from_post: 0.6,
          about_agent_work: 0.7,
          points_somewhere: 0.2,
        },
        line: "needs_thread",
        margin: ["about_agent_work"],
        exclusion: null,
      },
    })
  );
  traces.close();
});

afterAll(() => {
  delete process.env.SCOUT_DB_PATH;
  delete process.env.TRACE_DB_PATH;
  fs.rmSync(tmpDir, { recursive: true, force: true });
});

describe("mixed Jev and LLM query presentation", () => {
  it("selects an action badge only for Jev relevance scores", async () => {
    const { selectScoreBar } = await import("@/components/atoms/ScoreBar");

    expect(selectScoreBar({
      score: 1,
      relevancePresentation: {
        classifier: "jev",
        model: "jev:jev-latest",
        jev: {
          action: "review",
          line: "needs_thread",
          exclusion: null,
          margin_features: [],
          feature_probabilities: {},
        },
      },
    })).toEqual({ kind: "action", action: "review" });
    expect(selectScoreBar({
      score: 0.8,
      relevancePresentation: { classifier: "llm", model: "openrouter/acme/model", jev: null },
    })).toEqual({ kind: "score", score: 0.8 });
    expect(selectScoreBar({ value: 2, max: 8, label: "distance" })).toEqual({
      kind: "distance", value: 2, max: 8, label: "distance",
    });
  });

  it("reads classifier and complete Jev routing evidence through the shared helper", async () => {
    const { getRelevancePresentations } = await import("@/lib/queries");

    expect(getRelevancePresentations([11, 12]).get(12)).toEqual({
      classifier: "jev",
      model: "jev:jev-latest",
      jev: {
        action: "review",
        line: "needs_thread",
        exclusion: null,
        margin_features: ["about_agent_work"],
        feature_probabilities: {
          excl_hype: 0.1,
          needs_thread: 0.8,
          answerable_from_post: 0.6,
          about_agent_work: 0.7,
          points_somewhere: 0.2,
        },
      },
    });
    expect(getRelevancePresentations([11]).get(11)).toMatchObject({
      classifier: "llm",
      jev: null,
    });
  });

  it("applies score_min and score_max only to LLM rows", async () => {
    const { getPosts } = await import("@/lib/queries");

    expect(getPosts({ score_min: 0.9 }).data.map((post) => post.id)).toEqual([3, 2]);
    expect(getPosts({ score_max: 0.85 }).data.map((post) => post.id)).toEqual([2, 1]);
  });

  it("applies repeated actions only to Jev rows", async () => {
    const { getPosts } = await import("@/lib/queries");

    expect(getPosts({ action: ["respond", "drop"] }).data.map((post) => post.id)).toEqual([3, 1]);
    expect(getPosts({ action: ["review"] }).data.map((post) => post.id)).toEqual([3, 2, 1]);
  });

  it("continues through bounded batches until a filtered page has a lookahead row", async () => {
    const { getPosts } = await import("@/lib/queries");

    expect(getPosts({ action: ["respond"], limit: 1 })).toMatchObject({
      data: [{ id: 3 }],
      has_more: true,
    });
  });

  it("orders LLM rows by score without using Jev's constant score", async () => {
    const { getEvaluationsByScan } = await import("@/lib/queries");

    expect(getEvaluationsByScan(7).map((evaluation) => evaluation.id)).toEqual([13, 11, 12]);
  });
});
