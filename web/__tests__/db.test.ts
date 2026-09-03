import { afterAll, beforeAll, describe, expect, it } from "vitest";
import Database from "better-sqlite3";
import { mkdtempSync, rmSync } from "fs";
import { tmpdir } from "os";
import path from "path";

// Regression for SQLITE_BUSY ("database is locked") on settings writes that
// raced the scout agent's scan-time writes: both connections must carry a
// generous busy_timeout so a contended write waits rather than throwing.
const EXPECTED_BUSY_TIMEOUT_MS = 15000;

describe("scout db connections", () => {
  let dir: string;

  beforeAll(() => {
    dir = mkdtempSync(path.join(tmpdir(), "scout-db-"));
    const dbPath = path.join(dir, "scout.db");
    // Create the file so the readonly connection can open it.
    const seed = new Database(dbPath);
    seed.exec("CREATE TABLE t (id INTEGER)");
    seed.close();
    process.env.SCOUT_DB_PATH = dbPath;
  });

  afterAll(() => {
    delete process.env.SCOUT_DB_PATH;
    rmSync(dir, { recursive: true, force: true });
  });

  it("write connection sets a generous busy_timeout", async () => {
    const { getWriteDb } = await import("@/lib/db");
    const wdb = getWriteDb();
    expect(wdb.pragma("busy_timeout", { simple: true })).toBe(
      EXPECTED_BUSY_TIMEOUT_MS
    );
  });

  it("read connection sets a generous busy_timeout", async () => {
    const { getDb } = await import("@/lib/db");
    const rdb = getDb();
    expect(rdb.pragma("busy_timeout", { simple: true })).toBe(
      EXPECTED_BUSY_TIMEOUT_MS
    );
  });
});
