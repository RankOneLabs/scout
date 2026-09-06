// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useQueueReview } from "@/hooks/use-queue-review";
import type { QueueReviewItem } from "@/types/review-queues";

const item: QueueReviewItem = {
  source: { evaluation_id: 123, ranked_position: null, random_position: 1 },
  duplicate_key: "a".repeat(64),
  recorded: { evaluation: { id: 123, post_id: 456, scan_id: 1, project_key: "synthetic", reason: "irrelevant", relevant: 0, posture: null, dossier_revision: null, dossier_summary_id: null }, post: null, context: null, has_grade: false },
  score: null, grade: null, current_revision_id: null, disposition: null, status: "pending",
};
beforeEach(() => { sessionStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("queue review browser lifecycle", () => {
  it("retries a lost response after leaving and resuming with identical action and timing bytes", async () => {
    const fetcher = vi.fn().mockRejectedValueOnce(new Error("lost response"))
      .mockResolvedValueOnce(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetcher);
    const onSaved = vi.fn().mockResolvedValue(undefined);
    const input = { digest: "a".repeat(64), item, pricing: null, onSaved };
    const first = renderHook(() => useQueueReview(input));
    await waitFor(() => expect(first.result.current.ready).toBe(true));
    await act(async () => { await first.result.current.submit({ kind: "skip", reason: "Need context" }).catch(() => undefined); });
    expect(onSaved).not.toHaveBeenCalled();
    const originalBody = fetcher.mock.calls[0][1].body;
    first.unmount();
    const resumed = renderHook(() => useQueueReview(input));
    await waitFor(() => expect(resumed.result.current.hasPending).toBe(true));
    await act(async () => { await resumed.result.current.retry(); });
    expect(fetcher.mock.calls[1][1].body).toBe(originalBody);
    expect(fetcher.mock.calls[1][0]).toContain("/evaluations/123/actions");
    expect(onSaved).toHaveBeenCalledOnce();
    expect(resumed.result.current.hasPending).toBe(false);
  });
  it("keeps a second action from racing a pending save", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    const onSaved = vi.fn();
    const { result } = renderHook(() => useQueueReview({ digest: "a".repeat(64), item, pricing: null, onSaved }));
    await waitFor(() => expect(result.current.ready).toBe(true));
    await act(async () => { await result.current.submit({ kind: "skip", reason: "First" }).catch(() => undefined); });
    await expect(result.current.submit({ kind: "skip", reason: "Second" })).rejects.toThrow("pending");
    expect(fetch).toHaveBeenCalledOnce();
  });
  it("a revision conflict never marks the item saved", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "Grade changed" }), { status: 409 })));
    const onSaved = vi.fn();
    const { result } = renderHook(() => useQueueReview({ digest: "a".repeat(64), item, pricing: null, onSaved }));
    await waitFor(() => expect(result.current.ready).toBe(true));
    await act(async () => { await result.current.submit({ kind: "skip", reason: "Unsure" }).catch(() => undefined); });
    expect(onSaved).not.toHaveBeenCalled();
    expect(result.current.error).toBe("Grade changed");
    expect(result.current.hasPending).toBe(false);
  });
});
