import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Reset module between tests so env vars and fetch mock take effect cleanly.
const fetchMock = vi.fn();
vi.stubGlobal("fetch", fetchMock);

// Import after stubbing global fetch.
const { callSidecar, parseObjectBody } = await import("@/lib/sidecar-bridge");

afterAll(() => {
  vi.unstubAllGlobals();
});

describe("parseObjectBody", () => {
  it("returns ok:true with an empty body when allowEmpty is set", async () => {
    const req = { text: async () => "" } as never;
    const result = await parseObjectBody(req, { allowEmpty: true });
    expect(result).toEqual({ ok: true, body: {} });
  });

  it("returns ok:false for an empty body when allowEmpty is not set", async () => {
    const req = { text: async () => "" } as never;
    const result = await parseObjectBody(req);
    expect(result.ok).toBe(false);
  });

  it("returns ok:false for malformed JSON", async () => {
    const req = { text: async () => "not json{" } as never;
    const result = await parseObjectBody(req);
    expect(result.ok).toBe(false);
  });

  it("returns ok:false for a JSON array", async () => {
    const req = { text: async () => "[1,2,3]" } as never;
    const result = await parseObjectBody(req);
    expect(result.ok).toBe(false);
  });

  it("returns ok:false for a JSON null", async () => {
    const req = { text: async () => "null" } as never;
    const result = await parseObjectBody(req);
    expect(result.ok).toBe(false);
  });

  it("returns ok:false for a JSON primitive string", async () => {
    const req = { text: async () => '"hello"' } as never;
    const result = await parseObjectBody(req);
    expect(result.ok).toBe(false);
  });

  it("returns ok:true with parsed object body", async () => {
    const req = { text: async () => '{"key":"val"}' } as never;
    const result = await parseObjectBody(req);
    expect(result).toEqual({ ok: true, body: { key: "val" } });
  });
});

describe("callSidecar — timeout", () => {
  beforeEach(() => {
    process.env.SCOUT_SIDECAR_URL = "http://127.0.0.1:8799";
    fetchMock.mockReset();
  });

  afterEach(() => {
    delete process.env.SCOUT_SIDECAR_URL;
  });

  it("returns 504 with SIDECAR_TIMEOUT code when fetch throws a TimeoutError", async () => {
    const err = new DOMException("signal timed out", "TimeoutError");
    fetchMock.mockRejectedValueOnce(err);

    const result = await callSidecar("/test", {});
    expect(result.status).toBe(504);
    expect((result.body as Record<string, unknown>).code).toBe("SIDECAR_TIMEOUT");
  });

  it("returns 504 with SIDECAR_TIMEOUT code when fetch throws an AbortError", async () => {
    const err = new DOMException("aborted", "AbortError");
    fetchMock.mockRejectedValueOnce(err);

    const result = await callSidecar("/test", {});
    expect(result.status).toBe(504);
    expect((result.body as Record<string, unknown>).code).toBe("SIDECAR_TIMEOUT");
  });

  it("re-throws non-abort fetch errors", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("network error"));

    await expect(callSidecar("/test", {})).rejects.toThrow("network error");
  });

  it("returns sidecar status and body on success", async () => {
    fetchMock.mockResolvedValueOnce({
      status: 200,
      text: async () => JSON.stringify({ ok: true }),
    });

    const result = await callSidecar("/test", { payload: 1 });
    expect(result.status).toBe(200);
    expect(result.body).toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8799/test",
      expect.objectContaining({ method: "POST", signal: expect.anything() })
    );
  });

  it("returns the sidecar status code unchanged (e.g. 500 stays 500)", async () => {
    fetchMock.mockResolvedValueOnce({
      status: 500,
      text: async () => JSON.stringify({ detail: "internal error" }),
    });

    const result = await callSidecar("/test", {});
    expect(result.status).toBe(500);
    expect((result.body as Record<string, unknown>).detail).toBe("internal error");
  });
});

// SCOUT_SIDECAR_TOKEN is a server-to-server credential: read from this
// process's own environment and attached only to the outgoing bridge
// request. callSidecar takes no request/headers parameter at all, so
// there is no way for a browser-supplied header to reach the sidecar
// through it — these tests pin the only two token behaviors that exist.
describe("callSidecar — sidecar token attachment", () => {
  beforeEach(() => {
    process.env.SCOUT_SIDECAR_URL = "http://127.0.0.1:8799";
    fetchMock.mockReset();
    fetchMock.mockResolvedValue({
      status: 200,
      text: async () => JSON.stringify({ ok: true }),
    });
  });

  afterEach(() => {
    delete process.env.SCOUT_SIDECAR_URL;
    delete process.env.SCOUT_SIDECAR_TOKEN;
  });

  it("attaches X-Scout-Sidecar-Token from the server environment when configured", async () => {
    process.env.SCOUT_SIDECAR_TOKEN = "server-secret";
    await callSidecar("/test", {});
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8799/test",
      expect.objectContaining({
        headers: expect.objectContaining({ "X-Scout-Sidecar-Token": "server-secret" }),
      })
    );
  });

  it("sends no sidecar-token header at all when SCOUT_SIDECAR_TOKEN is unset", async () => {
    delete process.env.SCOUT_SIDECAR_TOKEN;
    await callSidecar("/test", {});
    const [, init] = fetchMock.mock.calls[0];
    expect(Object.keys(init.headers as Record<string, string>)).not.toContain(
      "X-Scout-Sidecar-Token"
    );
  });
});
