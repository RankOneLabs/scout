import Database from "better-sqlite3";
import path from "path";

// The scout agent (Python) and this web app share a single scout.db file. The
// agent writes in frequent short bursts during a scan; better-sqlite3's default
// 5s busy timeout was occasionally too short, surfacing SQLITE_BUSY ("database
// is locked") on settings writes made while a scan was in progress. Wait longer
// for the write lock instead of erroring — the agent never holds a long-running
// transaction, so a waiting writer acquires the lock between bursts.
const BUSY_TIMEOUT_MS = 15000;

let db: Database.Database | null = null;
let writeDb: Database.Database | null = null;

function resolveDbPath(): string {
  return process.env.SCOUT_DB_PATH || path.resolve(process.cwd(), "..", "scout.db");
}

export function getDb(): Database.Database {
  if (db) return db;
  db = new Database(resolveDbPath(), { readonly: true });
  db.pragma(`busy_timeout = ${BUSY_TIMEOUT_MS}`);
  return db;
}

export function getWriteDb(): Database.Database {
  if (writeDb) return writeDb;
  writeDb = new Database(resolveDbPath());
  writeDb.pragma(`busy_timeout = ${BUSY_TIMEOUT_MS}`);
  return writeDb;
}
