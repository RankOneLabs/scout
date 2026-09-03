import Database from "better-sqlite3";
import path from "path";

let db: Database.Database | null = null;

export function getTracesDb(): Database.Database {
  if (db) return db;

  const dbPath =
    process.env.TRACE_DB_PATH ||
    path.resolve(process.cwd(), "..", "scout_traces.db");
  db = new Database(dbPath, { readonly: true });
  return db;
}
