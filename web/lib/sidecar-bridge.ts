// Thin fetch wrapper around grading_api_sidecar. Reads pass through to
// scout.db; everything in this module is a POST proxy to the sidecar's
// /grades/... routes.

import { NextRequest } from "next/server";

const DEFAULT_PORT = 8799;
const SIDECAR_TIMEOUT_MS = 30_000;

// Parse a request JSON body and reject anything that isn't a plain object
// (null, arrays, primitives). When `allowEmpty` is true a missing/empty
// body resolves to `{}` so endpoints with only optional fields still work.
export type JsonBodyResult =
  | { ok: true; body: Record<string, unknown> }
  | { ok: false };

export async function parseObjectBody(
  request: NextRequest,
  { allowEmpty = false }: { allowEmpty?: boolean } = {}
): Promise<JsonBodyResult> {
  let body: unknown;
  try {
    const text = await request.text();
    if (text.length === 0) {
      if (allowEmpty) return { ok: true, body: {} };
      return { ok: false };
    }
    body = JSON.parse(text);
  } catch {
    return { ok: false };
  }
  if (body === null || typeof body !== "object" || Array.isArray(body)) {
    return { ok: false };
  }
  return { ok: true, body: body as Record<string, unknown> };
}

export interface SidecarResponse {
  status: number;
  body: unknown;
}

function sidecarBaseUrl(): string {
  const explicit = process.env.SCOUT_SIDECAR_URL?.trim();
  if (explicit) return explicit.replace(/\/$/, "");
  const port = process.env.SCOUT_SIDECAR_PORT?.trim() || String(DEFAULT_PORT);
  return `http://127.0.0.1:${port}`;
}

function tokenHeader(): Record<string, string> {
  const token = process.env.SCOUT_SIDECAR_TOKEN?.trim();
  return token ? { "X-Scout-Sidecar-Token": token } : {};
}

function makeTimeoutSignal(ms: number): { signal: AbortSignal; clear: () => void } {
  if (typeof AbortSignal.timeout === "function") {
    return { signal: AbortSignal.timeout(ms), clear: () => undefined };
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), ms);
  return { signal: controller.signal, clear: () => clearTimeout(timer) };
}

async function requestSidecar(
  path: string,
  init: { method: "GET" | "POST"; body?: unknown; timeoutMs?: number }
): Promise<SidecarResponse> {
  const url = `${sidecarBaseUrl()}${path}`;
  const { signal, clear } = makeTimeoutSignal(init.timeoutMs ?? SIDECAR_TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch(url, {
      method: init.method,
      headers: {
        ...(init.method === "POST" ? { "Content-Type": "application/json" } : {}),
        ...tokenHeader(),
      },
      ...(init.method === "POST"
        ? { body: init.body === undefined ? "{}" : JSON.stringify(init.body) }
        : {}),
      signal,
    });
    clear();
  } catch (err) {
    clear();
    const name = err instanceof Error ? err.name : "";
    const isTimeout = name === "TimeoutError" || name === "AbortError";
    if (isTimeout) {
      return {
        status: 504,
        body: { detail: "sidecar timeout", code: "SIDECAR_TIMEOUT" },
      };
    }
    throw err;
  }
  const text = await response.text();
  let parsed: unknown = text;
  if (text.length > 0) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = { detail: text };
    }
  } else {
    parsed = {};
  }
  return { status: response.status, body: parsed };
}

export async function callSidecar(
  path: string,
  body: unknown,
  options: { timeoutMs?: number } = {}
): Promise<SidecarResponse> {
  return requestSidecar(path, { method: "POST", body, ...options });
}
