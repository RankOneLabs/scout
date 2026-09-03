import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import path from "path";
import fs from "fs";
import os from "os";

// Set SCOUT_DB_PATH before any module under test loads db.ts.
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-settings-"));
const dbPath = path.join(tmpDir, "scout.db");
process.env.SCOUT_DB_PATH = dbPath;

const SCHEMA = `
CREATE TABLE IF NOT EXISTS projects (
  key TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  link TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  dossier_summary_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_keywords (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_key TEXT NOT NULL,
  keyword TEXT NOT NULL,
  evaluate_prompt TEXT,
  respond_prompt TEXT,
  critique_prompt TEXT,
  match_type TEXT NOT NULL DEFAULT 'substring',
  intent TEXT,
  positive_context TEXT,
  negative_context TEXT,
  notes TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  priority INTEGER NOT NULL DEFAULT 100,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(project_key, keyword)
);
CREATE TABLE IF NOT EXISTS prompt_templates (
  name TEXT PRIMARY KEY,
  body TEXT NOT NULL,
  kind TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS draft_comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  post_id INTEGER NOT NULL,
  evaluation_id INTEGER NOT NULL,
  project_key TEXT,
  comment_text TEXT,
  created_at TEXT,
  scan_id INTEGER
);
CREATE TABLE IF NOT EXISTS blocked_authors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform TEXT NOT NULL,
  author_id TEXT NOT NULL,
  author_name TEXT,
  reason TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(platform, author_id)
);
`;

type MockRequest = {
  url: string;
  nextUrl: URL;
  json: () => Promise<unknown>;
  text: () => Promise<string>;
  headers: { get: (name: string) => string | null };
};

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

function makeNextRequest(url: string): MockRequest {
  return {
    url,
    nextUrl: new URL(url),
    json: async () => ({}),
    text: async () => "",
    headers: makeHeaders(),
  };
}

function makeJsonRequest(url: string, body: unknown): MockRequest {
  return {
    url,
    nextUrl: new URL(url),
    json: async () => body,
    text: async () => JSON.stringify(body),
    headers: makeHeaders(),
  };
}

function makeMethodRequest(url: string, _method: string, body?: unknown): MockRequest {
  return {
    url,
    nextUrl: new URL(url),
    json: async () => (body ?? {}),
    text: async () => (body !== undefined ? JSON.stringify(body) : ""),
    headers: makeHeaders(),
  };
}

function makeRequestWithHeaders(
  url: string,
  headerEntries: Record<string, string>,
  body?: unknown
): MockRequest {
  return {
    url,
    nextUrl: new URL(url),
    json: async () => (body ?? {}),
    text: async () => (body !== undefined ? JSON.stringify(body) : ""),
    headers: makeHeaders(headerEntries),
  };
}

beforeAll(() => {
  const db = new Database(dbPath);
  db.exec(SCHEMA);

  // Seed some data
  const now = new Date().toISOString();
  db.prepare(
    "INSERT INTO projects (key, name, description, link, active, created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?)"
  ).run("proj1", "Project One", "First project", "https://example.com", now, now);
  db.prepare(
    "INSERT INTO projects (key, name, description, link, active, created_at, updated_at) VALUES (?, ?, ?, ?, 0, ?, ?)"
  ).run("proj2", "Project Two (inactive)", "Second project", "https://example2.com", now, now);
  db.prepare(
    "INSERT INTO projects (key, name, description, link, active, created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?)"
  ).run("proj3", "Project Three", "Third project", "https://example3.com", now, now);

  db.prepare(
    "INSERT INTO prompt_templates (name, body, kind, active, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)"
  ).run("eval_default", "Evaluate: {{content}}", "evaluate", now, now);
  db.prepare(
    "INSERT INTO prompt_templates (name, body, kind, active, created_at, updated_at) VALUES (?, ?, ?, 0, ?, ?)"
  ).run("inactive_prompt", "Inactive", "respond", now, now);

  db.prepare(
    "INSERT INTO project_keywords (project_key, keyword, evaluate_prompt, respond_prompt, critique_prompt, match_type, intent, positive_context, negative_context, notes, priority, active, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 100, 1, ?, ?)"
  ).run(
    "proj1",
    "typescript",
    "eval_default",
    null,
    null,
    "substring",
    "Reduce bug reports",
    JSON.stringify(["trusted users", "clear context"]),
    JSON.stringify(["ignore"]),
    "internal note",
    now,
    now
  );
  // An inactive keyword on proj1 — must NOT be included in keyword_count.
  db.prepare(
    "INSERT INTO project_keywords (project_key, keyword, evaluate_prompt, respond_prompt, critique_prompt, match_type, intent, positive_context, negative_context, notes, priority, active, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 100, 0, ?, ?)"
  ).run(
    "proj1",
    "deprecated-kw",
    null,
    null,
    null,
    "phrase",
    null,
    null,
    null,
    null,
    now,
    now
  );

  db.close();
});

afterAll(() => {
  delete process.env.SCOUT_DB_PATH;
  fs.rmSync(tmpDir, { recursive: true, force: true });
});

// ---- Projects ----

describe("GET /api/settings/projects", () => {
  it("returns active projects by default", async () => {
    const { GET } = await import("@/app/api/settings/projects/route");
    const resp = await GET(
      makeNextRequest("http://localhost/api/settings/projects") as never
    );
    expect(resp.status).toBe(200);
    const body = (await resp.json()) as { projects: Array<{ key: string; active: boolean; keyword_count: number }> };
    expect(Array.isArray(body.projects)).toBe(true);
    expect(body.projects.every((p) => p.active)).toBe(true);
    const proj1 = body.projects.find((p) => p.key === "proj1");
    expect(proj1).toBeDefined();
    // proj1 has one active + one inactive keyword; only the active one counts.
    expect(proj1?.keyword_count).toBe(1);
    expect(body.projects.find((p) => p.key === "proj2")).toBeUndefined();
  });

  it("returns all projects with include_inactive=true", async () => {
    const { GET } = await import("@/app/api/settings/projects/route");
    const resp = await GET(
      makeNextRequest(
        "http://localhost/api/settings/projects?include_inactive=true"
      ) as never
    );
    expect(resp.status).toBe(200);
    const body = (await resp.json()) as { projects: Array<{ key: string }> };
    expect(body.projects.some((p) => p.key === "proj2")).toBe(true);
  });
});

describe("POST /api/settings/projects", () => {
  it("creates a new project and returns 201", async () => {
    const { POST } = await import("@/app/api/settings/projects/route");
    const resp = await POST(
      makeJsonRequest("http://localhost/api/settings/projects", {
        key: "new-proj",
        name: "New Project",
        description: "Created in test",
        link: "https://newproject.com",
      }) as never
    );
    expect(resp.status).toBe(201);
    const body = (await resp.json()) as { key: string };
    expect(body.key).toBe("new-proj");
  });

  it("creates a project without a link", async () => {
    const { POST } = await import("@/app/api/settings/projects/route");
    const resp = await POST(
      makeJsonRequest("http://localhost/api/settings/projects", {
        key: "no-link",
        name: "No Link",
        description: "Created without a link",
      }) as never
    );
    expect(resp.status).toBe(201);
  });

  it("returns 409 on duplicate key", async () => {
    const { POST } = await import("@/app/api/settings/projects/route");
    const resp = await POST(
      makeJsonRequest("http://localhost/api/settings/projects", {
        key: "proj1",
        name: "Dup",
        description: "d",
        link: "https://example.com",
      }) as never
    );
    expect(resp.status).toBe(409);
  });

  it("returns 400 on invalid key pattern", async () => {
    const { POST } = await import("@/app/api/settings/projects/route");
    const resp = await POST(
      makeJsonRequest("http://localhost/api/settings/projects", {
        key: "-bad",
        name: "n",
        description: "d",
        link: "https://example.com",
      }) as never
    );
    expect(resp.status).toBe(400);
  });
});

describe("PUT /api/settings/projects/[key]", () => {
  it("updates an existing project", async () => {
    const { PUT } = await import("@/app/api/settings/projects/[key]/route");
    const resp = await PUT(
      makeMethodRequest(
        "http://localhost/api/settings/projects/proj1",
        "PUT",
        { name: "Project One Updated" }
      ) as never,
      { params: Promise.resolve({ key: "proj1" }) }
    );
    expect(resp.status).toBe(200);
    const body = (await resp.json()) as { key: string };
    expect(body.key).toBe("proj1");
  });

  it("returns 404 for missing project", async () => {
    const { PUT } = await import("@/app/api/settings/projects/[key]/route");
    const resp = await PUT(
      makeMethodRequest(
        "http://localhost/api/settings/projects/nope",
        "PUT",
        { name: "x" }
      ) as never,
      { params: Promise.resolve({ key: "nope" }) }
    );
    expect(resp.status).toBe(404);
  });
});

describe("PATCH /api/settings/projects/[key]", () => {
  it("deactivates a project", async () => {
    const { PATCH } = await import("@/app/api/settings/projects/[key]/route");
    const resp = await PATCH(
      makeMethodRequest(
        "http://localhost/api/settings/projects/new-proj",
        "PATCH",
        { active: false }
      ) as never,
      { params: Promise.resolve({ key: "new-proj" }) }
    );
    expect(resp.status).toBe(200);
  });
});

describe("DELETE /api/settings/projects/[key]", () => {
  it("returns 409 when project has keywords", async () => {
    const { DELETE } = await import("@/app/api/settings/projects/[key]/route");
    const resp = await DELETE(
      makeMethodRequest(
        "http://localhost/api/settings/projects/proj1",
        "DELETE"
      ) as never,
      { params: Promise.resolve({ key: "proj1" }) }
    );
    expect(resp.status).toBe(409);
  });

  it("hard-deletes an unreferenced project", async () => {
    const { DELETE } = await import("@/app/api/settings/projects/[key]/route");
    // proj2 has no keywords or draft_comments
    const resp = await DELETE(
      makeMethodRequest(
        "http://localhost/api/settings/projects/proj2",
        "DELETE"
      ) as never,
      { params: Promise.resolve({ key: "proj2" }) }
    );
    expect(resp.status).toBe(200);
  });

  it("returns 404 for missing project", async () => {
    const { DELETE } = await import("@/app/api/settings/projects/[key]/route");
    const resp = await DELETE(
      makeMethodRequest(
        "http://localhost/api/settings/projects/gone",
        "DELETE"
      ) as never,
      { params: Promise.resolve({ key: "gone" }) }
    );
    expect(resp.status).toBe(404);
  });
});

// ---- Keywords ----

describe("GET /api/settings/keywords", () => {
  it("returns active keywords by default with structured metadata", async () => {
    const { GET } = await import("@/app/api/settings/keywords/route");
    const resp = await GET(
      makeNextRequest("http://localhost/api/settings/keywords") as never
    );
    expect(resp.status).toBe(200);
    const body = (await resp.json()) as {
      keywords: Array<{
        keyword: string;
        match_type: string;
        intent: string | null;
        positive_context: string[];
        negative_context: string[];
        notes: string | null;
      }>;
    };
    expect(Array.isArray(body.keywords)).toBe(true);
    const kw = body.keywords.find((item) => item.keyword === "typescript");
    expect(kw?.match_type).toBe("substring");
    expect(kw?.positive_context).toEqual(["trusted users", "clear context"]);
    expect(kw?.negative_context).toEqual(["ignore"]);
    expect(kw?.notes).toBe("internal note");
  });

  it("filters by project_key", async () => {
    const { GET } = await import("@/app/api/settings/keywords/route");
    const resp = await GET(
      makeNextRequest(
        "http://localhost/api/settings/keywords?project_key=proj1"
      ) as never
    );
    expect(resp.status).toBe(200);
    const body = (await resp.json()) as { keywords: Array<{ project_key: string }> };
    expect(body.keywords.every((k) => k.project_key === "proj1")).toBe(true);
  });
});

describe("POST /api/settings/keywords", () => {
  it("creates a keyword with routing metadata", async () => {
    const { POST } = await import("@/app/api/settings/keywords/route");
    const resp = await POST(
      makeJsonRequest("http://localhost/api/settings/keywords", {
        project_key: "proj1",
        keyword: "react",
        match_type: "regex",
        intent: "Reduce spam",
        positive_context: ["trusted"],
        negative_context: ["avoid bots"],
        notes: "new routing note",
        evaluate_prompt: "eval_default",
        active: false,
      }) as never
    );
    expect(resp.status).toBe(201);
    const body = (await resp.json()) as { id: number };
    expect(typeof body.id).toBe("number");
  });

  it("creates multiple keywords in one request", async () => {
    const { POST } = await import("@/app/api/settings/keywords/route");
    const resp = await POST(
      makeJsonRequest("http://localhost/api/settings/keywords", {
        project_key: "proj1",
        keywords: ["bulk-react", "bulk-vue"],
        match_type: "phrase",
        intent: "Bulk route",
        positive_context: ["asks for help"],
      }) as never
    );
    expect(resp.status).toBe(201);
    const body = (await resp.json()) as { ids: number[] };
    expect(body.ids).toHaveLength(2);
  });

  it("creates bulk keywords beyond SQLite's common variable limit", async () => {
    const { POST } = await import("@/app/api/settings/keywords/route");
    const keywords = Array.from(
      { length: 1001 },
      (_, i) => `large-bulk-${i}`
    );
    const resp = await POST(
      makeJsonRequest("http://localhost/api/settings/keywords", {
        project_key: "proj1",
        keywords,
      }) as never
    );
    expect(resp.status).toBe(201);
    const body = (await resp.json()) as { ids: number[] };
    expect(body.ids).toHaveLength(keywords.length);
  });

  it("treats a non-array keywords property as a single-keyword request", async () => {
    const { POST } = await import("@/app/api/settings/keywords/route");
    const resp = await POST(
      makeJsonRequest("http://localhost/api/settings/keywords", {
        project_key: "proj1",
        keyword: "single-with-null-keywords",
        keywords: null,
      }) as never
    );
    expect(resp.status).toBe(201);
    const body = (await resp.json()) as { id: number };
    expect(typeof body.id).toBe("number");
  });

  it("returns 400 for an empty bulk keyword list", async () => {
    const { POST } = await import("@/app/api/settings/keywords/route");
    const resp = await POST(
      makeJsonRequest("http://localhost/api/settings/keywords", {
        project_key: "proj1",
        keywords: [],
      }) as never
    );
    expect(resp.status).toBe(400);
  });

  it("returns 409 when a bulk keyword already exists", async () => {
    const { POST } = await import("@/app/api/settings/keywords/route");
    const resp = await POST(
      makeJsonRequest("http://localhost/api/settings/keywords", {
        project_key: "proj1",
        keywords: ["bulk-svelte", "typescript"],
      }) as never
    );
    expect(resp.status).toBe(409);
  });

  it("returns 400 when prompt template does not exist", async () => {
    const { POST } = await import("@/app/api/settings/keywords/route");
    const resp = await POST(
      makeJsonRequest("http://localhost/api/settings/keywords", {
        project_key: "proj1",
        keyword: "vue",
        evaluate_prompt: "nonexistent_prompt",
      }) as never
    );
    expect(resp.status).toBe(400);
  });

  it("returns 404 when project does not exist", async () => {
    const { POST } = await import("@/app/api/settings/keywords/route");
    const resp = await POST(
      makeJsonRequest("http://localhost/api/settings/keywords", {
        project_key: "missing-project",
        keyword: "something",
      }) as never
    );
    expect(resp.status).toBe(404);
  });

  it("returns 409 on duplicate project_key+keyword", async () => {
    const { POST } = await import("@/app/api/settings/keywords/route");
    const resp = await POST(
      makeJsonRequest("http://localhost/api/settings/keywords", {
        project_key: "proj1",
        keyword: "typescript",
      }) as never
    );
    expect(resp.status).toBe(409);
  });

  it("returns 400 on invalid match_type", async () => {
    const { POST } = await import("@/app/api/settings/keywords/route");
    const resp = await POST(
      makeJsonRequest("http://localhost/api/settings/keywords", {
        project_key: "proj1",
        keyword: "solid",
        match_type: "prefix",
      }) as never
    );
    expect(resp.status).toBe(400);
  });
});

describe("PUT /api/settings/keywords/[id]", () => {
  it("updates keyword metadata and project_key", async () => {
    const { PUT } = await import("@/app/api/settings/keywords/[id]/route");
    const resp = await PUT(
      makeMethodRequest(
        "http://localhost/api/settings/keywords/1",
        "PUT",
        {
          project_key: "proj3",
          keyword: "typescript-routes",
          match_type: "exact",
          intent: "Route to a new project",
          positive_context: ["clear intent"],
          negative_context: [],
          notes: null,
          evaluate_prompt: "eval_default",
          priority: 10,
        }
      ) as never,
      { params: Promise.resolve({ id: "1" }) }
    );
    expect(resp.status).toBe(200);
    const body = (await resp.json()) as { id: number };
    expect(body.id).toBe(1);
  });

  it("returns 404 when moving to a missing project", async () => {
    const { PUT } = await import("@/app/api/settings/keywords/[id]/route");
    const resp = await PUT(
      makeMethodRequest(
        "http://localhost/api/settings/keywords/1",
        "PUT",
        { project_key: "missing-project" }
      ) as never,
      { params: Promise.resolve({ id: "1" }) }
    );
    expect(resp.status).toBe(404);
  });
});

describe("PATCH /api/settings/keywords/[id]", () => {
  it("toggles active state only", async () => {
    const { PATCH } = await import("@/app/api/settings/keywords/[id]/route");
    const resp = await PATCH(
      makeMethodRequest(
        "http://localhost/api/settings/keywords/1",
        "PATCH",
        { active: false }
      ) as never,
      { params: Promise.resolve({ id: "1" }) }
    );
    expect(resp.status).toBe(200);
  });
});

describe("DELETE /api/settings/keywords/[id]", () => {
  it("soft-deletes a keyword", async () => {
    const { DELETE } = await import("@/app/api/settings/keywords/[id]/route");
    // Keyword 1 is the seeded "typescript" keyword
    const resp = await DELETE(
      makeMethodRequest(
        "http://localhost/api/settings/keywords/1",
        "DELETE"
      ) as never,
      { params: Promise.resolve({ id: "1" }) }
    );
    expect(resp.status).toBe(200);
  });

  it("returns 404 for missing keyword", async () => {
    const { DELETE } = await import("@/app/api/settings/keywords/[id]/route");
    const resp = await DELETE(
      makeMethodRequest(
        "http://localhost/api/settings/keywords/9999",
        "DELETE"
      ) as never,
      { params: Promise.resolve({ id: "9999" }) }
    );
    expect(resp.status).toBe(404);
  });

  it("returns 400 for non-numeric id", async () => {
    const { DELETE } = await import("@/app/api/settings/keywords/[id]/route");
    const resp = await DELETE(
      makeMethodRequest(
        "http://localhost/api/settings/keywords/abc",
        "DELETE"
      ) as never,
      { params: Promise.resolve({ id: "abc" }) }
    );
    expect(resp.status).toBe(400);
  });
});

// ---- Blocked authors ----

describe("/api/settings/blocked-authors", () => {
  it("creates, lists, and reactivates a stable author identity", async () => {
    const { POST, GET } = await import(
      "@/app/api/settings/blocked-authors/route"
    );
    const first = await POST(
      makeJsonRequest("http://localhost/api/settings/blocked-authors", {
        platform: "Bluesky",
        author_id: "did:plc:linkbot",
        author_name: "Link Bot",
        reason: "link aggregator",
      }) as never
    );
    expect(first.status).toBe(201);
    const { id } = (await first.json()) as { id: number };

    const listed = await GET(
      makeNextRequest("http://localhost/api/settings/blocked-authors") as never
    );
    expect(listed.status).toBe(200);
    const body = (await listed.json()) as {
      blocked_authors: Array<{
        id: number;
        platform: string;
        author_id: string;
        reason: string | null;
        active: boolean;
      }>;
    };
    expect(body.blocked_authors).toContainEqual(
      expect.objectContaining({
        id,
        platform: "bluesky",
        author_id: "did:plc:linkbot",
        reason: "link aggregator",
        active: true,
      })
    );

    const again = await POST(
      makeJsonRequest("http://localhost/api/settings/blocked-authors", {
        platform: "bluesky",
        author_id: "did:plc:linkbot",
        author_name: "Renamed Bot",
      }) as never
    );
    expect(again.status).toBe(201);
    expect((await again.json()) as { id: number }).toEqual({ id });
  });

  it("rejects blank stable identities", async () => {
    const { POST } = await import("@/app/api/settings/blocked-authors/route");
    const response = await POST(
      makeJsonRequest("http://localhost/api/settings/blocked-authors", {
        platform: "bluesky",
        author_id: "   ",
      }) as never
    );
    expect(response.status).toBe(400);
  });

  it("soft-deactivates a block", async () => {
    const { GET } = await import("@/app/api/settings/blocked-authors/route");
    const before = await GET(
      makeNextRequest("http://localhost/api/settings/blocked-authors") as never
    );
    const rows = (await before.json()) as {
      blocked_authors: Array<{ id: number; author_id: string }>;
    };
    const block = rows.blocked_authors.find(
      (row) => row.author_id === "did:plc:linkbot"
    );
    expect(block).toBeDefined();

    const { DELETE } = await import(
      "@/app/api/settings/blocked-authors/[id]/route"
    );
    const response = await DELETE(
      makeMethodRequest(
        `http://localhost/api/settings/blocked-authors/${block!.id}`,
        "DELETE"
      ) as never,
      { params: Promise.resolve({ id: String(block!.id) }) }
    );
    expect(response.status).toBe(200);

    const after = await GET(
      makeNextRequest("http://localhost/api/settings/blocked-authors") as never
    );
    const afterRows = (await after.json()) as {
      blocked_authors: Array<{ author_id: string }>;
    };
    expect(
      afterRows.blocked_authors.some(
        (row) => row.author_id === "did:plc:linkbot"
      )
    ).toBe(false);
  });
});

// ---- Prompt templates ----

describe("GET /api/settings/prompts", () => {
  it("returns active prompts by default", async () => {
    const { GET } = await import("@/app/api/settings/prompts/route");
    const resp = await GET(
      makeNextRequest("http://localhost/api/settings/prompts") as never
    );
    expect(resp.status).toBe(200);
    const body = (await resp.json()) as { prompts: Array<{ name: string; active: boolean; referenced_keyword_count: number }> };
    expect(Array.isArray(body.prompts)).toBe(true);
    expect(body.prompts.every((p) => p.active)).toBe(true);
    // inactive_prompt should not appear
    expect(body.prompts.find((p) => p.name === "inactive_prompt")).toBeUndefined();
    // referenced_keyword_count should be a number
    const evalDefault = body.prompts.find((p) => p.name === "eval_default");
    expect(evalDefault).toBeDefined();
    expect(typeof evalDefault?.referenced_keyword_count).toBe("number");
  });

  it("includes inactive prompts with include_inactive=true", async () => {
    const { GET } = await import("@/app/api/settings/prompts/route");
    const resp = await GET(
      makeNextRequest(
        "http://localhost/api/settings/prompts?include_inactive=true"
      ) as never
    );
    expect(resp.status).toBe(200);
    const body = (await resp.json()) as { prompts: Array<{ name: string }> };
    expect(body.prompts.some((p) => p.name === "inactive_prompt")).toBe(true);
  });
});

describe("POST /api/settings/prompts", () => {
  it("creates a prompt template and returns 201", async () => {
    const { POST } = await import("@/app/api/settings/prompts/route");
    const resp = await POST(
      makeJsonRequest("http://localhost/api/settings/prompts", {
        name: "new_prompt",
        body: "Do something: {{content}}",
        kind: "shared",
      }) as never
    );
    expect(resp.status).toBe(201);
    const body = (await resp.json()) as { name: string };
    expect(body.name).toBe("new_prompt");
  });

  it("returns 409 on duplicate name", async () => {
    const { POST } = await import("@/app/api/settings/prompts/route");
    const resp = await POST(
      makeJsonRequest("http://localhost/api/settings/prompts", {
        name: "eval_default",
        body: "duplicate",
        kind: "evaluate",
      }) as never
    );
    expect(resp.status).toBe(409);
  });

  it("returns 400 on invalid kind", async () => {
    const { POST } = await import("@/app/api/settings/prompts/route");
    const resp = await POST(
      makeJsonRequest("http://localhost/api/settings/prompts", {
        name: "badkind",
        body: "body",
        kind: "invalid",
      }) as never
    );
    expect(resp.status).toBe(400);
  });
});

describe("PUT /api/settings/prompts/[name]", () => {
  it("updates a prompt body", async () => {
    const { PUT } = await import("@/app/api/settings/prompts/[name]/route");
    const resp = await PUT(
      makeMethodRequest(
        "http://localhost/api/settings/prompts/new_prompt",
        "PUT",
        { body: "Updated body" }
      ) as never,
      { params: Promise.resolve({ name: "new_prompt" }) }
    );
    expect(resp.status).toBe(200);
    const body = (await resp.json()) as { name: string };
    expect(body.name).toBe("new_prompt");
  });

  it("returns 404 for missing prompt", async () => {
    const { PUT } = await import("@/app/api/settings/prompts/[name]/route");
    const resp = await PUT(
      makeMethodRequest(
        "http://localhost/api/settings/prompts/nonexistent",
        "PUT",
        { body: "x" }
      ) as never,
      { params: Promise.resolve({ name: "nonexistent" }) }
    );
    expect(resp.status).toBe(404);
  });
});

describe("DELETE /api/settings/prompts/[name]", () => {
  it("returns 404 for missing prompt", async () => {
    const { DELETE } = await import(
      "@/app/api/settings/prompts/[name]/route"
    );
    const resp = await DELETE(
      makeMethodRequest(
        "http://localhost/api/settings/prompts/gone",
        "DELETE"
      ) as never,
      { params: Promise.resolve({ name: "gone" }) }
    );
    expect(resp.status).toBe(404);
  });

  it("soft-deletes a prompt with no active keyword refs", async () => {
    const { DELETE } = await import(
      "@/app/api/settings/prompts/[name]/route"
    );
    const resp = await DELETE(
      makeMethodRequest(
        "http://localhost/api/settings/prompts/new_prompt",
        "DELETE"
      ) as never,
      { params: Promise.resolve({ name: "new_prompt" }) }
    );
    expect(resp.status).toBe(200);
  });

  it("returns 409 when prompt is referenced by an active keyword", async () => {
    // Create a fresh referencing keyword here so this test is self-contained
    // and doesn't depend on state created in other describe blocks.
    const { POST: postKeyword } = await import(
      "@/app/api/settings/keywords/route"
    );
    const kwResp = await postKeyword(
      makeJsonRequest("http://localhost/api/settings/keywords", {
        project_key: "proj1",
        keyword: "409-test-kw",
        evaluate_prompt: "eval_default",
      }) as never
    );
    expect(kwResp.status).toBe(201);

    const { DELETE } = await import(
      "@/app/api/settings/prompts/[name]/route"
    );
    const resp = await DELETE(
      makeMethodRequest(
        "http://localhost/api/settings/prompts/eval_default",
        "DELETE"
      ) as never,
      { params: Promise.resolve({ name: "eval_default" }) }
    );
    expect(resp.status).toBe(409);
    const body = (await resp.json()) as { error: string };
    expect(body.error).toMatch(/active keyword/);
  });

  it("succeeds with ?confirm=true even when referenced", async () => {
    const { DELETE } = await import(
      "@/app/api/settings/prompts/[name]/route"
    );
    const resp = await DELETE(
      makeMethodRequest(
        "http://localhost/api/settings/prompts/eval_default?confirm=true",
        "DELETE"
      ) as never,
      { params: Promise.resolve({ name: "eval_default" }) }
    );
    expect(resp.status).toBe(200);
    const body = (await resp.json()) as { name: string };
    expect(body.name).toBe("eval_default");
  });
});

describe("PATCH /api/settings/prompts/[name]", () => {
  it("returns 404 for missing prompt", async () => {
    const { PATCH } = await import(
      "@/app/api/settings/prompts/[name]/route"
    );
    const resp = await PATCH(
      makeMethodRequest(
        "http://localhost/api/settings/prompts/gone",
        "PATCH",
        { active: false }
      ) as never,
      { params: Promise.resolve({ name: "gone" }) }
    );
    expect(resp.status).toBe(404);
  });

  it("reactivates a deactivated prompt", async () => {
    // eval_default was deactivated in the confirm=true DELETE test above.
    const { PATCH } = await import(
      "@/app/api/settings/prompts/[name]/route"
    );
    const resp = await PATCH(
      makeMethodRequest(
        "http://localhost/api/settings/prompts/eval_default",
        "PATCH",
        { active: true }
      ) as never,
      { params: Promise.resolve({ name: "eval_default" }) }
    );
    expect(resp.status).toBe(200);
    const body = (await resp.json()) as { name: string };
    expect(body.name).toBe("eval_default");
  });
});

describe("PUT /api/settings/keywords/[id]", () => {
  it("updates a keyword's text", async () => {
    // Keyword id=2 is the "react" keyword created in POST /keywords tests.
    const { PUT } = await import("@/app/api/settings/keywords/[id]/route");
    const resp = await PUT(
      makeMethodRequest(
        "http://localhost/api/settings/keywords/2",
        "PUT",
        { keyword: "react-hooks" }
      ) as never,
      { params: Promise.resolve({ id: "2" }) }
    );
    expect(resp.status).toBe(200);
    const body = (await resp.json()) as { id: number };
    expect(body.id).toBe(2);
  });

  it("returns 404 for missing keyword", async () => {
    const { PUT } = await import("@/app/api/settings/keywords/[id]/route");
    const resp = await PUT(
      makeMethodRequest(
        "http://localhost/api/settings/keywords/9999",
        "PUT",
        { keyword: "anything" }
      ) as never,
      { params: Promise.resolve({ id: "9999" }) }
    );
    expect(resp.status).toBe(404);
  });

  it("returns 400 for non-numeric id", async () => {
    const { PUT } = await import("@/app/api/settings/keywords/[id]/route");
    const resp = await PUT(
      makeMethodRequest(
        "http://localhost/api/settings/keywords/abc",
        "PUT",
        { keyword: "anything" }
      ) as never,
      { params: Promise.resolve({ id: "abc" }) }
    );
    expect(resp.status).toBe(400);
  });
});

describe("PATCH /api/settings/keywords/[id]", () => {
  it("toggles a keyword active state", async () => {
    const { PATCH } = await import("@/app/api/settings/keywords/[id]/route");
    const resp = await PATCH(
      makeMethodRequest(
        "http://localhost/api/settings/keywords/2",
        "PATCH",
        { active: false }
      ) as never,
      { params: Promise.resolve({ id: "2" }) }
    );
    expect(resp.status).toBe(200);
    const body = (await resp.json()) as { id: number };
    expect(body.id).toBe(2);
  });

  it("returns 404 for missing keyword", async () => {
    const { PATCH } = await import("@/app/api/settings/keywords/[id]/route");
    const resp = await PATCH(
      makeMethodRequest(
        "http://localhost/api/settings/keywords/9999",
        "PATCH",
        { active: true }
      ) as never,
      { params: Promise.resolve({ id: "9999" }) }
    );
    expect(resp.status).toBe(404);
  });
});

describe("write context guard — settings mutations", () => {
  it("rejects POST /api/settings/projects from an untrusted host", async () => {
    const { POST } = await import("@/app/api/settings/projects/route");
    const resp = await POST(
      makeRequestWithHeaders(
        "http://localhost/api/settings/projects",
        { host: "attacker.example.com" },
        { key: "guard-missing", name: "G", description: "d", link: "https://x.com" }
      ) as never
    );
    expect(resp.status).toBe(403);
  });

  it("rejects POST /api/settings/projects when x-forwarded-for is set", async () => {
    const { POST } = await import("@/app/api/settings/projects/route");
    const resp = await POST(
      makeRequestWithHeaders(
        "http://localhost/api/settings/projects",
        { "x-forwarded-for": "1.2.3.4" },
        { key: "guard-test", name: "G", description: "d", link: "https://x.com" }
      ) as never
    );
    expect(resp.status).toBe(403);
  });

  it("rejects POST /api/settings/projects when origin is external", async () => {
    const { POST } = await import("@/app/api/settings/projects/route");
    const resp = await POST(
      makeRequestWithHeaders(
        "http://localhost/api/settings/projects",
        { "origin": "https://attacker.example.com" },
        { key: "guard-test2", name: "G", description: "d", link: "https://x.com" }
      ) as never
    );
    expect(resp.status).toBe(403);
  });

  it("rejects DELETE /api/settings/keywords/[id] when x-forwarded-host is set", async () => {
    const { DELETE } = await import("@/app/api/settings/keywords/[id]/route");
    const resp = await DELETE(
      makeRequestWithHeaders(
        "http://localhost/api/settings/keywords/1",
        { "x-forwarded-host": "proxy.example.com" }
      ) as never,
      { params: Promise.resolve({ id: "1" }) }
    );
    expect(resp.status).toBe(403);
  });

  it("allows PUT /api/settings/keywords/[id] from loopback host", async () => {
    const { PUT } = await import("@/app/api/settings/keywords/[id]/route");
    const resp = await PUT(
      makeRequestWithHeaders(
        "http://localhost/api/settings/keywords/1",
        { "host": "localhost:3000" },
        { keyword: "typescript-updated", project_key: "proj1" }
      ) as never,
      { params: Promise.resolve({ id: "1" }) }
    );
    expect(resp.status).toBe(200);
  });
});

describe("strict route param validation — settings keywords", () => {
  it("returns 400 for a path-injection keyword id on PUT", async () => {
    const { PUT } = await import("@/app/api/settings/keywords/[id]/route");
    const injectedId = "1%2Fdelete%3Fconfirm%3Dtrue";
    const resp = await PUT(
      makeMethodRequest(
        `http://localhost/api/settings/keywords/${injectedId}`,
        "PUT",
        { keyword: "x", project_key: "proj1" }
      ) as never,
      { params: Promise.resolve({ id: injectedId }) }
    );
    expect(resp.status).toBe(400);
  });

  it("returns 400 for negative keyword id on DELETE", async () => {
    const { DELETE } = await import("@/app/api/settings/keywords/[id]/route");
    const resp = await DELETE(
      makeNextRequest("http://localhost/api/settings/keywords/-5") as never,
      { params: Promise.resolve({ id: "-5" }) }
    );
    expect(resp.status).toBe(400);
  });
});
