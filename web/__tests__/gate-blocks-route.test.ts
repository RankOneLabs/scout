import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "scout-gate-blocks-"));
const dbPath = path.join(tmpDir, "scout.db");
process.env.SCOUT_DB_PATH = dbPath;

beforeAll(() => {
  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE gate_blocks (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      reason_code TEXT NOT NULL,
      offending_text TEXT,
      segment_index INTEGER,
      project_key TEXT,
      dossier_summary_id TEXT,
      dossier_revision TEXT,
      scan_id INTEGER,
      post_id INTEGER,
      evaluation_id INTEGER,
      context TEXT,
      created_at TEXT NOT NULL
    );
  `);
  const insert = db.prepare(
    `INSERT INTO gate_blocks
      (reason_code, project_key, scan_id, created_at)
     VALUES (?, ?, ?, ?)`
  );
  insert.run("first", "gateway", 7, "2026-07-01T00:00:00Z");
  insert.run("second", "gateway", 7, "2026-07-02T00:00:00Z");
  insert.run("other-scan", "gateway", 8, "2026-07-03T00:00:00Z");
  db.close();
});

afterAll(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
  delete process.env.SCOUT_DB_PATH;
});

describe("GET /api/gate-blocks", () => {
  it("applies limit to scan results and returns newest blocks first", async () => {
    const { GET } = await import("@/app/api/gate-blocks/route");
    const request = {
      nextUrl: new URL("http://localhost/api/gate-blocks?scan_id=7&limit=1"),
    };

    const response = await GET(request as never);
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.gate_blocks).toHaveLength(1);
    expect(body.gate_blocks[0].reason_code).toBe("second");
  });
});
