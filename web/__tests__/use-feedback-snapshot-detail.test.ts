// @vitest-environment jsdom

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useFeedbackSnapshotDetail } from "@/hooks/use-feedback-snapshot-detail";
import type { FeedbackPhase } from "@/types/feedback";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function responseFor(phase: FeedbackPhase, consumerId: number, hasMore = false) {
  const summary = (name: FeedbackPhase, id: number) => ({
    snapshot_phase_id: id,
    phase: name,
    token_budget: 100,
    token_estimate: 10,
    truncated: false,
    structured_summary: "{}",
    rendered_text: "evidence",
    rendered_sha256: `sha-${name}`,
    counts: {
      included_count: 0,
      excluded_count: 0,
      example_count: 0,
      actually_used_count: 0,
    },
  });
  return {
    snapshot: {
      snapshot_id: 1,
      scan_id: 500,
      policy_version: "v1",
      mode: "active",
      as_of: "2026-01-01T00:00:00.000Z",
      created_at: "2026-01-01T00:00:00.000Z",
      population_count: 0,
      eligible_count: 0,
      excluded_count: 0,
      lookback_days: 14,
      lookback_started_at: "2025-12-18T00:00:00.000Z",
      max_grades: 50,
      segment_min_grades: 5,
      note_max_chars: 240,
    },
    phases: {
      relevance: summary("relevance", 10),
      reply_draft: summary("reply_draft", 11),
      critic: summary("critic", 12),
    },
    items: {
      phase,
      use_filter: "all",
      data: [],
      has_more: false,
      next_cursor: null,
    },
    consumers: {
      phase,
      data: [
        {
          id: consumerId,
          scan_id: 500,
          post_id: consumerId,
          evaluation_id: null,
          grade_id: null,
          phase,
          trace_id: `trace-${consumerId}`,
          status: "complete",
          created_at: `2026-01-01T00:00:0${consumerId}.000Z`,
        },
      ],
      has_more: hasMore,
      next_cursor: hasMore ? "relevance-next" : null,
    },
  };
}

describe("useFeedbackSnapshotDetail consumer pagination", () => {
  it("keeps a separate consumer page and cursor for every phase", async () => {
    const fetchMock = vi.fn().mockImplementation((rawUrl: string) => {
      const url = new URL(rawUrl, "http://localhost");
      const phase = url.searchParams.get("phase") as FeedbackPhase;
      const isNextRelevance = url.searchParams.get("consumerCursor") === "relevance-next";
      const body = isNextRelevance
        ? responseFor("relevance", 4, false)
        : responseFor(
            phase,
            phase === "relevance" ? 1 : phase === "reply_draft" ? 2 : 3,
            phase === "relevance"
          );
      return Promise.resolve({ ok: true, status: 200, json: async () => body } as Response);
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useFeedbackSnapshotDetail(1));
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.consumers.relevance.data.map((run) => run.id)).toEqual([1]);
    expect(result.current.consumers.reply_draft.data.map((run) => run.id)).toEqual([2]);
    expect(result.current.consumers.critic.data.map((run) => run.id)).toEqual([3]);

    act(() => result.current.loadMoreConsumers("relevance"));
    await waitFor(() =>
      expect(result.current.consumers.relevance.data.map((run) => run.id)).toEqual([1, 4])
    );
    expect(result.current.consumers.reply_draft.data.map((run) => run.id)).toEqual([2]);
    expect(result.current.consumers.critic.data.map((run) => run.id)).toEqual([3]);
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });
});
