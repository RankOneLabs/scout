// @vitest-environment jsdom

import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { ExperimentList } from "@/components/organisms/ExperimentList";
import type { ExperimentRunListResponse } from "@/types/feedback-experiments";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function makePage(): ExperimentRunListResponse {
  return {
    data: [
      {
        id: 1,
        name: "exp-one",
        status: "complete",
        phase: "relevance",
        grader_attached: false,
        created_at: "2026-01-01T00:00:00.000000+00:00",
        completed_at: "2026-01-01T00:01:00.000000+00:00",
        planned_case_count: 1,
        attempted_case_count: 1,
        skipped_case_count: 0,
        current_case_count: 1,
        retry_count: 0,
        status_counts: { queued: 0, running: 0, complete: 1, failed: 0 },
        total_llm_call_count: 3,
        total_cost: 0.02,
        verdict: "not_graded",
        correction_distance: { available: false, case_count: 0, baseline_mean: null, candidate_mean: null, mean_delta: null },
        cost: { available: true, case_count: 1, baseline_mean: 0.01, candidate_mean: 0.02, mean_delta: 0.01 },
        latency: { available: false, case_count: 0, baseline_mean: null, candidate_mean: null, mean_delta: null },
      },
    ],
    has_more: false,
    next_cursor: null,
  };
}

describe("ExperimentList", () => {
  it("renders the list with only navigation links and filter selects — no mutation controls", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => makePage(),
    } as Response);
    vi.stubGlobal("fetch", fetchMock);

    const { container } = render(React.createElement(ExperimentList, {}));

    await waitFor(() => screen.getByText("exp-one"));

    expect(container.querySelector("form")).toBeNull();
    // The only interactive controls are the status/phase filter <select>s
    // and (conditionally) a Load more button — neither triggers a write.
    const buttons = Array.from(container.querySelectorAll("button"));
    for (const button of buttons) {
      expect(button.textContent?.toLowerCase()).toContain("load more");
    }
    expect(container.querySelectorAll('input[type="text"], input[type="submit"]').length).toBe(0);

    // Every GET call this component issued was to the read-only list route.
    for (const call of fetchMock.mock.calls) {
      expect(String(call[0])).toMatch(/^\/api\/feedback\/experiment-runs/);
    }
  });
});
