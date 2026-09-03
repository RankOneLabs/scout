import { afterAll, beforeEach, describe, expect, it, vi } from "vitest";

// This route is a thin authenticated adapter: it only enforces the trusted
// write context, validates the evaluation id shape, parses the JSON body,
// and forwards to the sidecar's POST /grades/{id}/usage-override endpoint,
// relaying its status and body unchanged. All grade_id resolution and
// mode/reason validation live in the sidecar/StateManager — these tests
// cover only the adapter seam.

process.env.SCOUT_SIDECAR_URL = "http://127.0.0.1:8799";

const fetchMock = vi.fn();
vi.stubGlobal("fetch", fetchMock);

afterAll(() => {
  delete process.env.SCOUT_SIDECAR_URL;
  vi.unstubAllGlobals();
});

beforeEach(() => {
  fetchMock.mockReset();
});

function makeHeaders(trustedHost = true): { get: (name: string) => string | null } {
  const map = new Map(
    Object.entries({
      host: trustedHost ? "localhost" : "attacker.example.com",
    })
  );
  return { get: (name: string) => map.get(name.toLowerCase()) ?? null };
}

function makePostRequest(url: string, body: unknown, trustedHost = true) {
  return {
    nextUrl: new URL(url),
    headers: makeHeaders(trustedHost),
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

function sidecarResponse(status: number, body: unknown) {
  return { status, text: async () => JSON.stringify(body) };
}

describe("POST /api/grades/[evaluationId]/usage-override", () => {
  it("requires a trusted write context and never reaches the sidecar without it", async () => {
    const { POST } = await import("@/app/api/grades/[evaluationId]/usage-override/route");
    const resp = await POST(
      makePostRequest(
        "http://localhost/api/grades/1/usage-override",
        { mode: "auto" },
        false
      ) as never,
      { params: Promise.resolve({ evaluationId: "1" }) }
    );
    expect(resp.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects an invalid evaluation id without calling the sidecar", async () => {
    const { POST } = await import("@/app/api/grades/[evaluationId]/usage-override/route");
    const resp = await POST(
      makePostRequest("http://localhost/api/grades/not-a-number/usage-override", { mode: "auto" }) as never,
      { params: Promise.resolve({ evaluationId: "not-a-number" }) }
    );
    expect(resp.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects a non-object JSON body without calling the sidecar", async () => {
    const { POST } = await import("@/app/api/grades/[evaluationId]/usage-override/route");
    const resp = await POST(
      makePostRequest("http://localhost/api/grades/1/usage-override", ["not", "an", "object"]) as never,
      { params: Promise.resolve({ evaluationId: "1" }) }
    );
    expect(resp.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("forwards the request to the sidecar and relays a success response", async () => {
    fetchMock.mockResolvedValueOnce(
      sidecarResponse(200, { grade_id: 7, mode: "auto", reason: null, updated_at: "2026-05-15T00:00:00Z" })
    );
    const { POST } = await import("@/app/api/grades/[evaluationId]/usage-override/route");
    const resp = await POST(
      makePostRequest("http://localhost/api/grades/1/usage-override", { mode: "auto" }) as never,
      { params: Promise.resolve({ evaluationId: "1" }) }
    );
    expect(resp.status).toBe(200);
    const body = await resp.json();
    expect(body).toEqual({ grade_id: 7, mode: "auto", reason: null, updated_at: "2026-05-15T00:00:00Z" });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8799/grades/1/usage-override",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ mode: "auto" }),
      })
    );
  });

  it("forwards a sidecar 404 (no grade for this evaluation) unchanged", async () => {
    fetchMock.mockResolvedValueOnce(
      sidecarResponse(404, { detail: "no grade found for evaluation 999" })
    );
    const { POST } = await import("@/app/api/grades/[evaluationId]/usage-override/route");
    const resp = await POST(
      makePostRequest("http://localhost/api/grades/999/usage-override", { mode: "auto" }) as never,
      { params: Promise.resolve({ evaluationId: "999" }) }
    );
    expect(resp.status).toBe(404);
  });

  it("forwards a sidecar validation failure (400, e.g. exclude with no reason) unchanged", async () => {
    fetchMock.mockResolvedValueOnce(
      sidecarResponse(400, { errors: ["usage override reason is required and must be a non-blank string when mode is 'exclude'"] })
    );
    const { POST } = await import("@/app/api/grades/[evaluationId]/usage-override/route");
    const resp = await POST(
      makePostRequest("http://localhost/api/grades/1/usage-override", { mode: "exclude" }) as never,
      { params: Promise.resolve({ evaluationId: "1" }) }
    );
    expect(resp.status).toBe(400);
    const body = await resp.json();
    expect(Array.isArray(body.errors)).toBe(true);
  });
});
