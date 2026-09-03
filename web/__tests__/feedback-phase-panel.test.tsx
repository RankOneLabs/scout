// @vitest-environment jsdom

import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { FeedbackPhasePanel } from "@/components/organisms/FeedbackPhasePanel";

afterEach(cleanup);

describe("FeedbackPhasePanel phase consumers", () => {
  it("renders exact audit links and pages consumers independently", () => {
    const onLoadMoreConsumers = vi.fn();
    const { container } = render(
      <FeedbackPhasePanel
        summary={{
          snapshot_phase_id: 10,
          phase: "relevance",
          token_budget: 100,
          token_estimate: 20,
          truncated: false,
          structured_summary: "{}",
          rendered_text: "policy evidence",
          rendered_sha256: "sha",
          counts: {
            included_count: 1,
            excluded_count: 0,
            example_count: 0,
            actually_used_count: 1,
          },
        }}
        mode="active"
        itemsState={{ data: [], has_more: false, use_filter: "all" }}
        consumersState={{
          has_more: true,
          data: [
            {
              id: 55,
              scan_id: 500,
              post_id: 12,
              evaluation_id: 44,
              grade_id: 33,
              phase: "relevance",
              trace_id: "trace-navigation",
              status: "complete",
              created_at: "2026-01-01T00:00:00.000Z",
            },
          ],
        }}
        onUseFilterChange={vi.fn()}
        onLoadMore={vi.fn()}
        onLoadMoreConsumers={onLoadMoreConsumers}
      />
    );

    expect(container.firstElementChild?.id).toBe("phase-relevance");
    expect(screen.getByText("Phase run #55").closest("a")?.getAttribute("href")).toBe(
      "/feedback/phase-runs/55"
    );
    expect(container.querySelector('a[href="/traces/trace-navigation"]')).not.toBeNull();
    expect(screen.getByText("evaluation #44").closest("a")?.getAttribute("href")).toBe(
      "/scans/500#evaluation-44"
    );
    expect(screen.getByText("grade #33").closest("a")?.getAttribute("href")).toBe(
      "/feedback/grades/33"
    );
    fireEvent.click(screen.getByRole("button", { name: "Load more phase consumers" }));
    expect(onLoadMoreConsumers).toHaveBeenCalledOnce();
  });
});
