import { describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

const listExperimentRuns = vi.fn();
vi.mock("@/lib/feedback-experiment-queries", async (original) => ({
  ...await original<typeof import("@/lib/feedback-experiment-queries")>(),
  listExperimentRuns,
  DataIntegrityError: class DataIntegrityError extends Error {},
}));
vi.mock("@/lib/write-guard", () => ({ isTrustedWriteContext: vi.fn(() => true) }));

describe("experiment runs API route", () => {
  it("returns the read-only list response", async () => {
    listExperimentRuns.mockReturnValue({ data: [{ id: 1 }], has_more: false, next_cursor: null });
    const { GET } = await import("@/app/api/feedback/experiment-runs/route");
    const response = await GET(new NextRequest("http://localhost/api/feedback/experiment-runs"));
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ data: [{ id: 1 }], has_more: false, next_cursor: null });
  });

  it("converts retained data-integrity failures to 500", async () => {
    const { DataIntegrityError } = await import("@/lib/feedback-experiment-queries");
    listExperimentRuns.mockImplementation(() => { throw new DataIntegrityError("bad evidence"); });
    const { GET } = await import("@/app/api/feedback/experiment-runs/route");
    const response = await GET(new NextRequest("http://localhost/api/feedback/experiment-runs"));
    expect(response.status).toBe(500);
    expect(await response.json()).toEqual({ errors: ["internal data-integrity error"] });
  });
});
