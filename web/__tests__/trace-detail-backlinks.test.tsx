// @vitest-environment jsdom

import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { TraceDetail } from "@/components/organisms/TraceDetail";
import type { TraceSpan } from "@/types/schema";
import type { TraceComparisonBacklink } from "@/types/traces";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function makeRootSpan(traceId: string): TraceSpan {
  return {
    id: "span-root",
    trace_id: traceId,
    parent_id: null,
    kind: "agent_run",
    name: "scout_relevance",
    input: null,
    output: null,
    started_at: "2026-01-01T00:00:00Z",
    ended_at: "2026-01-01T00:00:01Z",
    duration_ms: 1000,
    metadata: null,
    error: null,
    usage_input_tokens: null,
    usage_output_tokens: null,
    usage_cost: null,
  };
}

function stubFetch(handlers: { phaseRun?: Response; comparisons?: TraceComparisonBacklink[] }) {
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (url.endsWith("/phase-run")) {
      return Promise.resolve(handlers.phaseRun ?? ({ ok: false, json: async () => ({}) } as Response));
    }
    if (url.endsWith("/comparisons")) {
      return Promise.resolve({ ok: true, json: async () => handlers.comparisons ?? [] } as Response);
    }
    return Promise.resolve({ ok: false, json: async () => ({}) } as Response);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("TraceDetail — comparison and phase-run backlinks", () => {
  it("shows explicit unavailable text for a historical trace with neither relation", async () => {
    stubFetch({});
    render(React.createElement(TraceDetail, { spans: [makeRootSpan("trace-historical")] }));

    await waitFor(() => screen.getByText(/No linked phase run for this trace/));
    await waitFor(() => screen.getByText(/No replay comparisons for this trace/));
  });

  it("lists a comparison backlink with its role and links to the experiment", async () => {
    const backlinks: TraceComparisonBacklink[] = [
      {
        experiment_id: 7,
        comparison_id: 3,
        role: "baseline",
        experiment_name: "my-experiment",
        experiment_status: "complete",
        experiment_url: "/feedback/experiments/7",
      },
    ];
    stubFetch({ comparisons: backlinks });
    render(React.createElement(TraceDetail, { spans: [makeRootSpan("trace-with-comparison")] }));

    await waitFor(() => screen.getByText("my-experiment"));
    const link = screen.getByText("my-experiment").closest("a");
    expect(link?.getAttribute("href")).toBe("/feedback/experiments/7");
    expect(screen.getByText(/baseline, complete/)).toBeDefined();
  });

  it("links exact phase-run, snapshot-phase, and evaluation identities", async () => {
    stubFetch({
      phaseRun: {
        ok: true,
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
          created_at: "2026-01-01T00:00:00Z",
        }),
      } as Response,
    });
    render(React.createElement(TraceDetail, { spans: [makeRootSpan("trace-navigation")] }));

    await waitFor(() => screen.getByText("#55"));
    expect(screen.getByText("#55").closest("a")?.getAttribute("href")).toBe(
      "/feedback/phase-runs/55"
    );
    expect(screen.getByText("#10").closest("a")?.getAttribute("href")).toBe(
      "/feedback?snapshotId=7#phase-relevance"
    );
    expect(screen.getByText("#44").closest("a")?.getAttribute("href")).toBe(
      "/scans/500#evaluation-44"
    );
    expect(screen.getByText("grade #33").closest("a")?.getAttribute("href")).toBe(
      "/feedback/grades/33"
    );
  });
});
