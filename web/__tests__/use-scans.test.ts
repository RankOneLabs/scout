// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { useScans } from "@/hooks/use-scans";
import type { Scan } from "@/types/schema";

function makeScan(id: number): Scan {
  return {
    id,
    started_at: "2026-05-15T00:00:00+00:00",
    completed_at: null,
    messages_scanned: 0,
    relevant_found: 0,
    fetch_started_at: null,
    safe_watermark_at: null,
    status: null,
    overflow_count: 0,
  };
}

describe("useScans", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("fetches scan list on mount", async () => {
    const scans = [makeScan(1), makeScan(2)];
    const fetchMock = vi.mocked(global.fetch);
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => scans,
    } as Response);

    const { result } = renderHook(() => useScans());

    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.scans).toEqual(scans);
    expect(result.current.error).toBeNull();
    expect(fetchMock).toHaveBeenCalledWith("/api/scans");
  });

  it("sets error state when res.ok is false", async () => {
    const fetchMock = vi.mocked(global.fetch);
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 500,
      json: async () => ({ detail: "server error" }),
    } as Response);

    const { result } = renderHook(() => useScans());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.error).toMatch(/500/);
    expect(result.current.scans).toEqual([]);
  });

  it("sets error state on network failure", async () => {
    const fetchMock = vi.mocked(global.fetch);
    fetchMock.mockRejectedValueOnce(new TypeError("network offline"));

    const { result } = renderHook(() => useScans());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.error).toMatch(/network offline/);
    expect(result.current.scans).toEqual([]);
  });

  it("does not feed error objects into the scans array on non-ok responses", async () => {
    const fetchMock = vi.mocked(global.fetch);
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 503,
      json: async () => ({ detail: "service unavailable" }),
    } as Response);

    const { result } = renderHook(() => useScans());
    await waitFor(() => expect(result.current.loading).toBe(false));

    // scans must remain an empty array, not an error object
    expect(Array.isArray(result.current.scans)).toBe(true);
    expect(result.current.scans).toHaveLength(0);
  });
});
