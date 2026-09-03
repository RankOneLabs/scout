// @vitest-environment jsdom

import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { PhaseRunDetail } from "@/components/organisms/PhaseRunDetail";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("PhaseRunDetail navigation", () => {
  it("links back to the exact snapshot phase, trace, scan, and evaluation grade", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({
          id: 55,
          scan_id: 500,
          post_id: 12,
          evaluation_id: 44,
          grade_id: 33,
          snapshot_id: 7,
          snapshot_phase_id: 10,
          phase: "relevance",
          trace_id: "trace-navigation",
          model: "model-a",
          status: "complete",
          created_at: "2026-01-01T00:00:00.000Z",
        }),
      } as Response)
    );

    render(<PhaseRunDetail phaseRunId={55} />);
    await waitFor(() => screen.getByText(/Phase run #55/));

    expect(screen.getByText("Scan #500").closest("a")?.getAttribute("href")).toBe("/scans/500");
    expect(screen.getAllByText("#10")[0].closest("a")?.getAttribute("href")).toBe(
      "/feedback?snapshotId=7#phase-relevance"
    );
    expect(screen.getByText("trace-navigation").closest("a")?.getAttribute("href")).toBe(
      "/traces/trace-navigation"
    );
    expect(screen.getAllByText("#44")[0].closest("a")?.getAttribute("href")).toBe(
      "/scans/500#evaluation-44"
    );
    expect(screen.getByText("#33").closest("a")?.getAttribute("href")).toBe(
      "/feedback/grades/33"
    );
  });
});
