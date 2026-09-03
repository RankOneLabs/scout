import { getDb, getWriteDb } from "./db";
import type {
  KeywordMatchType,
  BlockedAuthorRow,
  ProjectListItem,
  ProjectKeywordRow,
  PromptTemplateListItem,
} from "@/types/schema";

// --- Internal SQLite row shapes (active stored as 0/1 integer) ---

interface ProjectDbRow {
  key: string;
  name: string;
  description: string;
  link: string;
  active: number;
  dossier_summary_id: string | null;
  created_at: string;
  updated_at: string;
}

interface KeywordDbRow {
  id: number;
  project_key: string;
  keyword: string;
  match_type: string;
  intent: string | null;
  positive_context: string | null;
  negative_context: string | null;
  notes: string | null;
  evaluate_prompt: string | null;
  respond_prompt: string | null;
  critique_prompt: string | null;
  active: number;
  priority: number;
  created_at: string;
  updated_at: string;
}

interface PromptTemplateDbRow {
  name: string;
  body: string;
  kind: string;
  active: number;
  created_at: string;
  updated_at: string;
}

interface BlockedAuthorDbRow {
  id: number;
  platform: string;
  author_id: string;
  author_name: string | null;
  reason: string | null;
  active: number;
  created_at: string;
  updated_at: string;
}

// --- Result union ---

export type QueryOk<T> = { ok: true; data: T };
export type QueryError = {
  ok: false;
  error: "not_found" | "conflict" | "bad_request";
  message: string;
};
export type QueryResult<T> = QueryOk<T> | QueryError;

// --- Row mappers ---

function projectFromRow(
  row: ProjectDbRow
): Omit<ProjectListItem, "keyword_count"> {
  return {
    key: row.key,
    name: row.name,
    description: row.description,
    link: row.link,
    active: row.active === 1,
    dossier_summary_id: row.dossier_summary_id ?? null,
    created_at: row.created_at,
    updated_at: row.updated_at,
  };
}

function keywordFromRow(row: KeywordDbRow): ProjectKeywordRow {
  return {
    id: row.id,
    project_key: row.project_key,
    keyword: row.keyword,
    match_type: normalizeKeywordMatchType(row.match_type),
    intent: normalizeNullableText(row.intent),
    positive_context: parseKeywordContext(row.positive_context),
    negative_context: parseKeywordContext(row.negative_context),
    notes: normalizeNullableText(row.notes),
    evaluate_prompt: row.evaluate_prompt,
    respond_prompt: row.respond_prompt,
    critique_prompt: row.critique_prompt,
    active: row.active === 1,
    priority: row.priority,
    created_at: row.created_at,
    updated_at: row.updated_at,
  };
}

function promptFromRow(
  row: PromptTemplateDbRow
): Omit<PromptTemplateListItem, "referenced_keyword_count"> {
  return {
    name: row.name,
    body: row.body,
    kind: row.kind as PromptTemplateListItem["kind"],
    active: row.active === 1,
    created_at: row.created_at,
    updated_at: row.updated_at,
  };
}

function blockedAuthorFromRow(row: BlockedAuthorDbRow): BlockedAuthorRow {
  return {
    ...row,
    active: row.active === 1,
  };
}

// --- Helper ---

function validatePromptNames(
  db: ReturnType<typeof getWriteDb>,
  names: (string | null | undefined)[]
): string | null {
  for (const name of names) {
    if (!name) continue;
    const row = db
      .prepare(
        "SELECT name FROM prompt_templates WHERE name = ? AND active = 1"
      )
      .get(name);
    if (!row) {
      return `prompt template '${name}' not found or inactive`;
    }
  }
  return null;
}

const MATCH_TYPES: readonly KeywordMatchType[] = [
  "substring",
  "phrase",
  "exact",
  "regex",
] as const;

function normalizeKeywordMatchType(
  value: string | null | undefined
): KeywordMatchType {
  if (value && MATCH_TYPES.includes(value as KeywordMatchType)) {
    return value as KeywordMatchType;
  }
  return "substring";
}

function normalizeNullableText(value: string | null | undefined): string | null {
  if (value === null) return null;
  if (value === undefined) return null;
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

function parseKeywordContext(value: string | null): string[] {
  if (value === null) return [];
  const text = value.trim();
  if (!text) return [];
  try {
    const parsed = JSON.parse(text) as unknown;
    if (Array.isArray(parsed)) {
      return parsed
        .filter((item): item is string => typeof item === "string")
        .map((item) => item.trim())
        .filter(Boolean);
    }
  } catch {
    // Fall back to newline-delimited storage for compatibility with older rows.
  }
  return text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}

function serializeKeywordContext(values: string[]): string {
  return JSON.stringify(values);
}

// --- Blocked authors ---

export function listBlockedAuthors(includeInactive = false): BlockedAuthorRow[] {
  const db = getDb();
  const where = includeInactive ? "" : "WHERE active = 1";
  const rows = db
    .prepare(
      `SELECT * FROM blocked_authors ${where}
       ORDER BY active DESC, updated_at DESC, id DESC`
    )
    .all() as BlockedAuthorDbRow[];
  return rows.map(blockedAuthorFromRow);
}

export function blockAuthor(data: {
  platform: string;
  author_id: string;
  author_name?: string | null;
  reason?: string | null;
}): QueryResult<{ id: number }> {
  const db = getWriteDb();
  const platform = data.platform.trim().toLowerCase();
  const authorId = data.author_id.trim();
  const authorName = normalizeNullableText(data.author_name);
  const reason = normalizeNullableText(data.reason);
  const now = new Date().toISOString();
  db.prepare(
    `INSERT INTO blocked_authors
       (platform, author_id, author_name, reason, active, created_at, updated_at)
     VALUES (?, ?, ?, ?, 1, ?, ?)
     ON CONFLICT(platform, author_id) DO UPDATE SET
       author_name = excluded.author_name,
       reason = excluded.reason,
       active = 1,
       updated_at = excluded.updated_at`
  ).run(platform, authorId, authorName, reason, now, now);
  const row = db
    .prepare("SELECT id FROM blocked_authors WHERE platform = ? AND author_id = ?")
    .get(platform, authorId) as { id: number } | undefined;
  if (!row) {
    return { ok: false, error: "not_found", message: "blocked author was not saved" };
  }
  return { ok: true, data: { id: row.id } };
}

export function unblockAuthor(id: number): QueryResult<{ id: number }> {
  const db = getWriteDb();
  const existing = db
    .prepare("SELECT id FROM blocked_authors WHERE id = ?")
    .get(id);
  if (!existing) {
    return {
      ok: false,
      error: "not_found",
      message: `blocked author #${id} not found`,
    };
  }
  db.prepare(
    "UPDATE blocked_authors SET active = 0, updated_at = ? WHERE id = ?"
  ).run(new Date().toISOString(), id);
  return { ok: true, data: { id } };
}

// --- Projects ---

export function listProjects(includeInactive = false): ProjectListItem[] {
  const db = getDb();
  const where = includeInactive ? "" : "WHERE p.active = 1";
  const rows = db
    .prepare(
      `SELECT p.*,
              (SELECT COUNT(*) FROM project_keywords pk
               WHERE pk.project_key = p.key AND pk.active = 1) AS keyword_count
       FROM projects p
       ${where}
       ORDER BY p.key`
    )
    .all() as (ProjectDbRow & { keyword_count: number })[];
  return rows.map((row) => ({
    ...projectFromRow(row),
    keyword_count: row.keyword_count,
  }));
}

export function createProject(data: {
  key: string;
  name: string;
  description: string;
  link: string;
  dossier_summary_id?: string | null;
}): QueryResult<{ key: string }> {
  const db = getWriteDb();
  const existing = db
    .prepare("SELECT key FROM projects WHERE key = ?")
    .get(data.key);
  if (existing) {
    return {
      ok: false,
      error: "conflict",
      message: `project with key '${data.key}' already exists`,
    };
  }
  const now = new Date().toISOString();
  db.prepare(
    "INSERT INTO projects (key, name, description, link, active, dossier_summary_id, created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?)"
  ).run(data.key, data.name, data.description, data.link, data.dossier_summary_id ?? null, now, now);
  return { ok: true, data: { key: data.key } };
}

export function updateProject(
  key: string,
  data: { name?: string; description?: string; link?: string; dossier_summary_id?: string | null }
): QueryResult<{ key: string }> {
  const db = getWriteDb();
  const existing = db
    .prepare("SELECT key FROM projects WHERE key = ?")
    .get(key);
  if (!existing) {
    return {
      ok: false,
      error: "not_found",
      message: `project '${key}' not found`,
    };
  }
  const now = new Date().toISOString();
  const setClauses: string[] = ["updated_at = ?"];
  const params: (string | number | null)[] = [now];
  if (data.name !== undefined) {
    setClauses.push("name = ?");
    params.push(data.name);
  }
  if (data.description !== undefined) {
    setClauses.push("description = ?");
    params.push(data.description);
  }
  if (data.link !== undefined) {
    setClauses.push("link = ?");
    params.push(data.link);
  }
  if (data.dossier_summary_id !== undefined) {
    setClauses.push("dossier_summary_id = ?");
    params.push(data.dossier_summary_id ?? null);
  }
  params.push(key);
  db.prepare(
    `UPDATE projects SET ${setClauses.join(", ")} WHERE key = ?`
  ).run(...params);
  return { ok: true, data: { key } };
}

export function setProjectActive(
  key: string,
  active: boolean
): QueryResult<{ key: string }> {
  const db = getWriteDb();
  const existing = db
    .prepare("SELECT key FROM projects WHERE key = ?")
    .get(key);
  if (!existing) {
    return {
      ok: false,
      error: "not_found",
      message: `project '${key}' not found`,
    };
  }
  const now = new Date().toISOString();
  db.prepare(
    "UPDATE projects SET active = ?, updated_at = ? WHERE key = ?"
  ).run(active ? 1 : 0, now, key);
  return { ok: true, data: { key } };
}

export function deleteProjectIfUnreferenced(
  key: string
): QueryResult<{ key: string }> {
  const db = getWriteDb();
  const existing = db
    .prepare("SELECT key FROM projects WHERE key = ?")
    .get(key);
  if (!existing) {
    return {
      ok: false,
      error: "not_found",
      message: `project '${key}' not found`,
    };
  }
  const kwCount = (
    db
      .prepare(
        "SELECT COUNT(*) AS cnt FROM project_keywords WHERE project_key = ?"
      )
      .get(key) as { cnt: number }
  ).cnt;
  if (kwCount > 0) {
    return {
      ok: false,
      error: "conflict",
      message: `project '${key}' has ${kwCount} keyword(s); remove all keyword records before deleting the project`,
    };
  }
  const dcCount = (
    db
      .prepare(
        "SELECT COUNT(*) AS cnt FROM draft_comments WHERE project_key = ?"
      )
      .get(key) as { cnt: number }
  ).cnt;
  if (dcCount > 0) {
    return {
      ok: false,
      error: "conflict",
      message: `project '${key}' is referenced by ${dcCount} draft comment(s)`,
    };
  }
  db.prepare("DELETE FROM projects WHERE key = ?").run(key);
  return { ok: true, data: { key } };
}

// --- Keywords ---

export function listKeywords(
  opts: { projectKey?: string; includeInactive?: boolean } = {}
): ProjectKeywordRow[] {
  const db = getDb();
  const clauses: string[] = [];
  const params: (string | number)[] = [];
  if (!opts.includeInactive) {
    clauses.push("active = 1");
  }
  if (opts.projectKey) {
    clauses.push("project_key = ?");
    params.push(opts.projectKey);
  }
  const where = clauses.length > 0 ? `WHERE ${clauses.join(" AND ")}` : "";
  const rows = db
    .prepare(
      `SELECT * FROM project_keywords ${where}
       ORDER BY priority ASC, length(keyword) DESC, id ASC`
    )
    .all(...params) as KeywordDbRow[];
  return rows.map(keywordFromRow);
}

export function createKeyword(data: {
  project_key: string;
  keyword: string;
  match_type?: string;
  intent?: string | null;
  positive_context?: string[];
  negative_context?: string[];
  notes?: string | null;
  evaluate_prompt?: string | null;
  respond_prompt?: string | null;
  critique_prompt?: string | null;
  priority?: number;
  active?: boolean;
}): QueryResult<{ id: number }> {
  const db = getWriteDb();
  const project = db
    .prepare("SELECT key FROM projects WHERE key = ?")
    .get(data.project_key);
  if (!project) {
    return {
      ok: false,
      error: "not_found",
      message: `project '${data.project_key}' not found`,
    };
  }
  const existing = db
    .prepare(
      "SELECT id FROM project_keywords WHERE project_key = ? AND keyword = ?"
    )
    .get(data.project_key, data.keyword);
  if (existing) {
    return {
      ok: false,
      error: "conflict",
      message: `keyword '${data.keyword}' already exists for project '${data.project_key}'`,
    };
  }
  const promptError = validatePromptNames(db, [
    data.evaluate_prompt,
    data.respond_prompt,
    data.critique_prompt,
  ]);
  if (promptError) {
    return { ok: false, error: "bad_request", message: promptError };
  }
  const now = new Date().toISOString();
  const result = db
    .prepare(
      `INSERT INTO project_keywords
       (project_key, keyword, evaluate_prompt, respond_prompt, critique_prompt,
        match_type, intent, positive_context, negative_context, notes,
        priority, active, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
    )
    .run(
      data.project_key,
      data.keyword,
      data.evaluate_prompt ?? null,
      data.respond_prompt ?? null,
      data.critique_prompt ?? null,
      normalizeKeywordMatchType(data.match_type),
      normalizeNullableText(data.intent),
      serializeKeywordContext(data.positive_context ?? []),
      serializeKeywordContext(data.negative_context ?? []),
      normalizeNullableText(data.notes),
      data.priority ?? 100,
      data.active === false ? 0 : 1,
      now,
      now
    );
  return { ok: true, data: { id: result.lastInsertRowid as number } };
}

export function createKeywords(data: {
  project_key: string;
  keywords: string[];
  match_type?: string;
  intent?: string | null;
  positive_context?: string[];
  negative_context?: string[];
  notes?: string | null;
  evaluate_prompt?: string | null;
  respond_prompt?: string | null;
  critique_prompt?: string | null;
  priority?: number;
  active?: boolean;
}): QueryResult<{ ids: number[] }> {
  const db = getWriteDb();
  if (data.keywords.length === 0) {
    return {
      ok: false,
      error: "bad_request",
      message: "at least one keyword is required",
    };
  }
  const project = db
    .prepare("SELECT key FROM projects WHERE key = ?")
    .get(data.project_key);
  if (!project) {
    return {
      ok: false,
      error: "not_found",
      message: `project '${data.project_key}' not found`,
    };
  }
  const seen = new Set<string>();
  for (const keyword of data.keywords) {
    if (seen.has(keyword)) {
      return {
        ok: false,
        error: "conflict",
        message: `keyword '${keyword}' appears more than once in the request`,
      };
    }
    seen.add(keyword);
  }
  const existingKeywordStmt = db.prepare(
    "SELECT id FROM project_keywords WHERE project_key = ? AND keyword = ?"
  );
  const existingKeyword = data.keywords.find((keyword) =>
    existingKeywordStmt.get(data.project_key, keyword)
  );
  if (existingKeyword) {
    return {
      ok: false,
      error: "conflict",
      message: `keyword '${existingKeyword}' already exists for project '${data.project_key}'`,
    };
  }
  const promptError = validatePromptNames(db, [
    data.evaluate_prompt,
    data.respond_prompt,
    data.critique_prompt,
  ]);
  if (promptError) {
    return { ok: false, error: "bad_request", message: promptError };
  }
  const insert = db.prepare(
    `INSERT INTO project_keywords
     (project_key, keyword, evaluate_prompt, respond_prompt, critique_prompt,
      match_type, intent, positive_context, negative_context, notes,
      priority, active, created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  );
  const now = new Date().toISOString();
  const ids = db.transaction(() =>
    data.keywords.map((keyword) => {
      const result = insert.run(
        data.project_key,
        keyword,
        data.evaluate_prompt ?? null,
        data.respond_prompt ?? null,
        data.critique_prompt ?? null,
        normalizeKeywordMatchType(data.match_type),
        normalizeNullableText(data.intent),
        serializeKeywordContext(data.positive_context ?? []),
        serializeKeywordContext(data.negative_context ?? []),
        normalizeNullableText(data.notes),
        data.priority ?? 100,
        data.active === false ? 0 : 1,
        now,
        now
      );
      return result.lastInsertRowid as number;
    })
  )();
  return { ok: true, data: { ids } };
}

export function updateKeyword(
  id: number,
  data: {
    project_key?: string;
    keyword?: string;
    match_type?: string;
    intent?: string | null;
    positive_context?: string[];
    negative_context?: string[];
    notes?: string | null;
    evaluate_prompt?: string | null;
    respond_prompt?: string | null;
    critique_prompt?: string | null;
    priority?: number;
    active?: boolean;
  }
): QueryResult<{ id: number }> {
  const db = getWriteDb();
  const existing = db
    .prepare(
      "SELECT * FROM project_keywords WHERE id = ?"
    )
    .get(id) as KeywordDbRow | undefined;
  if (!existing) {
    return {
      ok: false,
      error: "not_found",
      message: `keyword #${id} not found`,
    };
  }
  const targetProjectKey = data.project_key ?? existing.project_key;
  const targetKeyword = data.keyword ?? existing.keyword;
  if (
    targetProjectKey !== existing.project_key ||
    targetKeyword !== existing.keyword
  ) {
    const projectExists = db
      .prepare("SELECT key FROM projects WHERE key = ?")
      .get(targetProjectKey);
    if (!projectExists) {
      return {
        ok: false,
        error: "not_found",
        message: `project '${targetProjectKey}' not found`,
      };
    }
    const duplicate = db
      .prepare(
        "SELECT id FROM project_keywords WHERE project_key = ? AND keyword = ? AND id <> ?"
      )
      .get(targetProjectKey, targetKeyword, id);
    if (duplicate) {
      return {
        ok: false,
        error: "conflict",
        message: `keyword '${targetKeyword}' already exists for project '${targetProjectKey}'`,
      };
    }
  }
  const hasPromptUpdate =
    "evaluate_prompt" in data ||
    "respond_prompt" in data ||
    "critique_prompt" in data;
  if (hasPromptUpdate) {
    const promptError = validatePromptNames(db, [
      "evaluate_prompt" in data
        ? data.evaluate_prompt ?? null
        : existing.evaluate_prompt,
      "respond_prompt" in data
        ? data.respond_prompt ?? null
        : existing.respond_prompt,
      "critique_prompt" in data
        ? data.critique_prompt ?? null
        : existing.critique_prompt,
    ]);
    if (promptError) {
      return { ok: false, error: "bad_request", message: promptError };
    }
  }
  const now = new Date().toISOString();
  const setClauses: string[] = ["updated_at = ?"];
  const params: (string | number | null)[] = [now];
  if (data.project_key !== undefined) {
    setClauses.push("project_key = ?");
    params.push(data.project_key);
  }
  if (data.keyword !== undefined) {
    setClauses.push("keyword = ?");
    params.push(data.keyword);
  }
  if (data.match_type !== undefined) {
    setClauses.push("match_type = ?");
    params.push(normalizeKeywordMatchType(data.match_type));
  }
  if (data.intent !== undefined) {
    setClauses.push("intent = ?");
    params.push(normalizeNullableText(data.intent));
  }
  if (data.positive_context !== undefined) {
    setClauses.push("positive_context = ?");
    params.push(serializeKeywordContext(data.positive_context));
  }
  if (data.negative_context !== undefined) {
    setClauses.push("negative_context = ?");
    params.push(serializeKeywordContext(data.negative_context));
  }
  if (data.notes !== undefined) {
    setClauses.push("notes = ?");
    params.push(normalizeNullableText(data.notes));
  }
  if ("evaluate_prompt" in data) {
    setClauses.push("evaluate_prompt = ?");
    params.push(data.evaluate_prompt ?? null);
  }
  if ("respond_prompt" in data) {
    setClauses.push("respond_prompt = ?");
    params.push(data.respond_prompt ?? null);
  }
  if ("critique_prompt" in data) {
    setClauses.push("critique_prompt = ?");
    params.push(data.critique_prompt ?? null);
  }
  if (data.priority !== undefined) {
    setClauses.push("priority = ?");
    params.push(data.priority);
  }
  if (data.active !== undefined) {
    setClauses.push("active = ?");
    params.push(data.active ? 1 : 0);
  }
  params.push(id);
  try {
    db.prepare(
      `UPDATE project_keywords SET ${setClauses.join(", ")} WHERE id = ?`
    ).run(...params);
  } catch (err) {
    if (err instanceof Error && err.message.includes("UNIQUE")) {
      return {
        ok: false,
        error: "conflict",
        message: `keyword '${data.keyword}' already exists for that project`,
      };
    }
    throw err;
  }
  return { ok: true, data: { id } };
}

export function setKeywordActive(
  id: number,
  active: boolean
): QueryResult<{ id: number }> {
  const db = getWriteDb();
  const existing = db
    .prepare("SELECT id FROM project_keywords WHERE id = ?")
    .get(id);
  if (!existing) {
    return {
      ok: false,
      error: "not_found",
      message: `keyword #${id} not found`,
    };
  }
  const now = new Date().toISOString();
  db.prepare(
    "UPDATE project_keywords SET active = ?, updated_at = ? WHERE id = ?"
  ).run(active ? 1 : 0, now, id);
  return { ok: true, data: { id } };
}

export function deleteKeyword(id: number): QueryResult<{ id: number }> {
  return setKeywordActive(id, false);
}

// --- Prompt templates ---

export function listPromptTemplates(
  includeInactive = false
): PromptTemplateListItem[] {
  const db = getDb();
  const where = includeInactive ? "" : "WHERE pt.active = 1";
  const rows = db
    .prepare(
      `SELECT pt.*,
              (SELECT COUNT(*) FROM project_keywords pk
               WHERE pk.active = 1
                 AND (pk.evaluate_prompt = pt.name
                      OR pk.respond_prompt = pt.name
                      OR pk.critique_prompt = pt.name)
              ) AS referenced_keyword_count
       FROM prompt_templates pt
       ${where}
       ORDER BY pt.name`
    )
    .all() as (PromptTemplateDbRow & { referenced_keyword_count: number })[];
  return rows.map((row) => ({
    ...promptFromRow(row),
    referenced_keyword_count: row.referenced_keyword_count,
  }));
}

export function createPromptTemplate(data: {
  name: string;
  body: string;
  kind: string;
}): QueryResult<{ name: string }> {
  const db = getWriteDb();
  const existing = db
    .prepare("SELECT name FROM prompt_templates WHERE name = ?")
    .get(data.name);
  if (existing) {
    return {
      ok: false,
      error: "conflict",
      message: `prompt template '${data.name}' already exists`,
    };
  }
  const now = new Date().toISOString();
  db.prepare(
    "INSERT INTO prompt_templates (name, body, kind, active, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)"
  ).run(data.name, data.body, data.kind, now, now);
  return { ok: true, data: { name: data.name } };
}

export function updatePromptTemplate(
  name: string,
  data: { body?: string; kind?: string }
): QueryResult<{ name: string }> {
  const db = getWriteDb();
  const existing = db
    .prepare("SELECT name FROM prompt_templates WHERE name = ?")
    .get(name);
  if (!existing) {
    return {
      ok: false,
      error: "not_found",
      message: `prompt template '${name}' not found`,
    };
  }
  const now = new Date().toISOString();
  const setClauses: string[] = ["updated_at = ?"];
  const params: (string | number)[] = [now];
  if (data.body !== undefined) {
    setClauses.push("body = ?");
    params.push(data.body);
  }
  if (data.kind !== undefined) {
    setClauses.push("kind = ?");
    params.push(data.kind);
  }
  params.push(name);
  db.prepare(
    `UPDATE prompt_templates SET ${setClauses.join(", ")} WHERE name = ?`
  ).run(...params);
  return { ok: true, data: { name } };
}

export function setPromptTemplateActive(
  name: string,
  active: boolean,
  force = false
): QueryResult<{ name: string }> {
  const db = getWriteDb();
  const existing = db
    .prepare("SELECT name FROM prompt_templates WHERE name = ?")
    .get(name);
  if (!existing) {
    return {
      ok: false,
      error: "not_found",
      message: `prompt template '${name}' not found`,
    };
  }
  if (!active && !force) {
    const refCount = (
      db
        .prepare(
          `SELECT COUNT(*) AS cnt FROM project_keywords
           WHERE active = 1
             AND (evaluate_prompt = ? OR respond_prompt = ? OR critique_prompt = ?)`
        )
        .get(name, name, name) as { cnt: number }
    ).cnt;
    if (refCount > 0) {
      return {
        ok: false,
        error: "conflict",
        message: `prompt '${name}' is referenced by ${refCount} active keyword(s); pass confirm=true to force`,
      };
    }
  }
  const now = new Date().toISOString();
  db.prepare(
    "UPDATE prompt_templates SET active = ?, updated_at = ? WHERE name = ?"
  ).run(active ? 1 : 0, now, name);
  return { ok: true, data: { name } };
}
