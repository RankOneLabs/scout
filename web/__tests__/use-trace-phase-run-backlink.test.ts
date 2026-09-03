// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { useTracePhaseRunBacklink } from "@/hooks/use-trace-phase-run-backlink";
import type { PhaseRunDetail } from "@/types/feedback-grades";

function makeBacklink(): PhaseRunDetail {
  return {
    id: 1,
    scan_id: 500,
    post_id: 1,
    evaluation_id: 1,
    grade_id: 20,
    snapshot_id: 5,
    snapshot_phase_id: 10,
    phase: "relevance",
    trace_id: "trace-abc-123",
    model: "model-a",
    status: "complete",
    created_at: "2026-01-01T00:00:00.000Z",
  };
}

describe("useTracePhaseRunBacklink", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("skips the request entirely when traceId is empty (root span not yet loaded)", async () => {
    const fetchMock = vi.mocked(global.fetch);

    const { result } = renderHook(() => useTracePhaseRunBacklink(""));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.backlink).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("fetches and resolves the backlink for a real traceId", async () => {
    const backlink = makeBacklink();
    const fetchMock = vi.mocked(global.fetch);
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => backlink,
    } as Response);

    const { result } = renderHook(() => useTracePhaseRunBacklink("trace-abc-123"));

    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.backlink).toEqual(backlink);
    expect(fetchMock).toHaveBeenCalledWith("/api/traces/trace-abc-123/phase-run");
  });

  it("resolves to null when no backlink exists (404)", async () => {
    const fetchMock = vi.mocked(global.fetch);
    fetchMock.mockResolvedValueOnce({ ok: false, json: async () => ({}) } as Response);

    const { result } = renderHook(() => useTracePhaseRunBacklink("trace-no-backlink"));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.backlink).toBeNull();
  });
});
