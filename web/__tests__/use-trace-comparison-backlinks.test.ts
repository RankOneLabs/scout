// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { useTraceComparisonBacklinks } from "@/hooks/use-trace-comparison-backlinks";
import type { TraceComparisonBacklink } from "@/types/traces";

function makeBacklinks(): TraceComparisonBacklink[] {
  return [
    {
      experiment_id: 2,
      comparison_id: 2,
      role: "candidate",
      experiment_name: "exp-two",
      experiment_status: "complete",
      experiment_url: "/feedback/experiments/2",
    },
    {
      experiment_id: 1,
      comparison_id: 1,
      role: "baseline",
      experiment_name: "exp-one",
      experiment_status: "complete",
      experiment_url: "/feedback/experiments/1",
    },
  ];
}

describe("useTraceComparisonBacklinks", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("skips the request when traceId is empty", async () => {
    const fetchMock = vi.mocked(global.fetch);
    const { result } = renderHook(() => useTraceComparisonBacklinks(""));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.backlinks).toEqual([]);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("fetches and resolves the ordered backlinks for a real traceId", async () => {
    const backlinks = makeBacklinks();
    const fetchMock = vi.mocked(global.fetch);
    fetchMock.mockResolvedValueOnce({ ok: true, json: async () => backlinks } as Response);

    const { result } = renderHook(() => useTraceComparisonBacklinks("trace-shared"));

    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.backlinks).toEqual(backlinks);
    expect(fetchMock).toHaveBeenCalledWith("/api/traces/trace-shared/comparisons");
  });

  it("resolves to an empty array on a non-ok response", async () => {
    const fetchMock = vi.mocked(global.fetch);
    fetchMock.mockResolvedValueOnce({ ok: false, json: async () => ({}) } as Response);

    const { result } = renderHook(() => useTraceComparisonBacklinks("trace-error"));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.backlinks).toEqual([]);
  });
});
