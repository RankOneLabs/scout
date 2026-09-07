// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useQueueReview } from "@/hooks/use-queue-review";
import type { QueueReviewItem, ReviewRequest } from "@/types/review-queues";

const item: QueueReviewItem = {
  source: { evaluation_id: 123, ranked_position: null, random_position: 1 },
  duplicate_key: "a".repeat(64),
  recorded: { evaluation: { id: 123, post_id: 456, scan_id: 1, project_key: "synthetic", reason: "irrelevant", relevant: 0, posture: null, dossier_revision: null, dossier_summary_id: null }, post: null, context: null, has_grade: false },
  score: null, grade: null, current_revision_id: null, disposition: null, status: "pending",
};
beforeEach(() => { sessionStorage.clear(); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("queue review browser lifecycle", () => {
  it("saves a No grade with distinct UUIDs when HTTP does not expose randomUUID", async () => {
    const getRandomValues = vi.fn(crypto.getRandomValues.bind(crypto));
    vi.stubGlobal("crypto", { getRandomValues });
    const fetcher = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetcher);
    const onSaved = vi.fn().mockResolvedValue(undefined);
    const { result } = renderHook(() => useQueueReview({ digest: "a".repeat(64), item, pricing: null, onSaved }));
    await waitFor(() => expect(result.current.ready).toBe(true));
    const action = { kind: "grade", grade: { relevance_judgment: "correct", action_judgment: "accept" } } as const;
    await act(async () => { await result.current.submit(action); });
    await act(async () => { await result.current.submit(action); });
    const requests: ReviewRequest[] = fetcher.mock.calls.map(([, init]) => JSON.parse(init.body));
    for (const request of requests) {
      expect(request.action_id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
      expect(request.action).toEqual(action);
    }
    expect(requests[0].action_id).not.toBe(requests[1].action_id);
    expect(getRandomValues).toHaveBeenCalledTimes(2);
    expect(onSaved).toHaveBeenCalledTimes(2);
    expect(result.current.hasPending).toBe(false);
  });
  it("reports unavailable secure randomness without freezing an action", async () => {
    vi.stubGlobal("crypto", { getRandomValues: vi.fn().mockImplementation(() => { throw new Error("Unavailable"); }) });
    vi.stubGlobal("fetch", vi.fn());
    const { result } = renderHook(() => useQueueReview({ digest: "a".repeat(64), item, pricing: null, onSaved: vi.fn() }));
    await waitFor(() => expect(result.current.ready).toBe(true));
    await act(async () => {
      await expect(result.current.submit({ kind: "skip", reason: "Unsure" })).rejects.toThrow("Cannot generate a secure review action ID");
    });
    expect(result.current.error).toContain("Cannot generate a secure review action ID");
    expect(result.current.hasPending).toBe(false);
    expect(fetch).not.toHaveBeenCalled();
  });
  it.each([
    [404, "<html>Not found</html>"], [413, ""], [400, "null"], [409, '{"detail":42}'],
  ])("clears rejected actions even with an unreadable %s error body", async (status, body) => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async () => new Response(body, { status })));
    const input = { digest: "a".repeat(64), item, pricing: null, onSaved: vi.fn() };
    const first = renderHook(() => useQueueReview(input));
    await waitFor(() => expect(first.result.current.ready).toBe(true));
    await act(async () => { await first.result.current.submit({ kind: "skip", reason: "Unsure" }).catch(() => undefined); });
    expect(first.result.current.error).toBe(`Review save failed (${status})`);
    expect(first.result.current.hasPending).toBe(false);
    first.unmount();
    const resumed = renderHook(() => useQueueReview(input));
    await waitFor(() => expect(resumed.result.current.ready).toBe(true));
    expect(resumed.result.current.hasPending).toBe(false);
  });
  it("keeps identical retry bytes after a non-JSON server error", async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(new Response("<html>Unavailable</html>", { status: 503 }))
      .mockResolvedValueOnce(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetcher);
    const { result } = renderHook(() => useQueueReview({ digest: "a".repeat(64), item, pricing: null, onSaved: vi.fn() }));
    await waitFor(() => expect(result.current.ready).toBe(true));
    await act(async () => { await result.current.submit({ kind: "skip", reason: "Unsure" }).catch(() => undefined); });
    expect(result.current.hasPending).toBe(true);
    await act(async () => { await result.current.retry(); });
    expect(fetcher.mock.calls[1][1].body).toBe(fetcher.mock.calls[0][1].body);
  });
  it("contains mid-session storage failures on ticks, activity, and cleanup", async () => {
    vi.stubGlobal("fetch", vi.fn());
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { result, unmount } = renderHook(() => useQueueReview({ digest: "a".repeat(64), item, pricing: null, onSaved: vi.fn() }));
    await waitFor(() => expect(result.current.ready).toBe(true));
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("Full", "QuotaExceededError"); });
    act(() => { vi.advanceTimersByTime(2000); window.dispatchEvent(new Event("pointerdown")); });
    expect(result.current.ready).toBe(false);
    expect(result.current.error).toContain("Cannot persist review timing");
    await expect(result.current.submit({ kind: "skip", reason: "Unsure" })).rejects.toThrow("Cannot persist");
    expect(fetch).not.toHaveBeenCalled();
    expect(() => unmount()).not.toThrow();
  });
  it("never sends a newly frozen request when its persistence fails", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const { result } = renderHook(() => useQueueReview({ digest: "a".repeat(64), item, pricing: null, onSaved: vi.fn() }));
    await waitFor(() => expect(result.current.ready).toBe(true));
    const write = Storage.prototype.setItem;
    vi.spyOn(Storage.prototype, "setItem").mockImplementationOnce(write)
      .mockImplementation(() => { throw new DOMException("Full", "QuotaExceededError"); });
    await act(async () => { await result.current.submit({ kind: "skip", reason: "Unsure" }).catch(() => undefined); });
    expect(result.current.ready).toBe(false);
    expect(result.current.hasPending).toBe(true);
    await expect(result.current.retry()).rejects.toThrow("Cannot persist");
    expect(fetch).not.toHaveBeenCalled();
  });
  it("preserves the persisted action when clearing it fails after a committed save", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetcher);
    const onSaved = vi.fn().mockImplementationOnce(async () => {
      vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("Full", "QuotaExceededError"); });
    });
    const input = { digest: "a".repeat(64), item, pricing: null, onSaved };
    const first = renderHook(() => useQueueReview(input));
    await waitFor(() => expect(first.result.current.ready).toBe(true));
    await act(async () => { await first.result.current.submit({ kind: "skip", reason: "Unsure" }).catch(() => undefined); });
    expect(first.result.current.ready).toBe(false);
    first.unmount();
    vi.restoreAllMocks();
    const resumed = renderHook(() => useQueueReview(input));
    await waitFor(() => expect(resumed.result.current.hasPending).toBe(true));
    await act(async () => { await resumed.result.current.retry(); });
    expect(fetcher.mock.calls[1][1].body).toBe(fetcher.mock.calls[0][1].body);
  });
  it.each([true, false])("retries after leaving with identical bytes (randomUUID available: %s)", async (hasRandomUUID) => {
    const randomUUID = vi.fn().mockReturnValue("12345678-1234-4234-8234-123456789abc");
    const getRandomValues = vi.fn((bytes: Uint8Array) => bytes.fill(0xab));
    vi.stubGlobal("crypto", { randomUUID: hasRandomUUID ? randomUUID : undefined, getRandomValues });
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
    expect(randomUUID).toHaveBeenCalledTimes(hasRandomUUID ? 1 : 0);
    expect(getRandomValues).toHaveBeenCalledTimes(hasRandomUUID ? 0 : 1);
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
  it.each([
    "Grade changed",
    "Grade is missing its pinned revision; remediate the grade first",
    "Grade differs from its pinned revision; remediate the grade first",
  ])("a known revision conflict releases the pending request: %s", async (detail) => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async () => new Response(JSON.stringify({ detail }), { status: 409 })));
    const onSaved = vi.fn();
    const { result } = renderHook(() => useQueueReview({ digest: "a".repeat(64), item, pricing: null, onSaved }));
    await waitFor(() => expect(result.current.ready).toBe(true));
    await act(async () => { await result.current.submit({ kind: "skip", reason: "Unsure" }).catch(() => undefined); });
    expect(onSaved).not.toHaveBeenCalled();
    expect(result.current.error).toBe(detail);
    expect(result.current.hasPending).toBe(false);
    await act(async () => { await result.current.submit({ kind: "skip", reason: "Try after remediation" }).catch(() => undefined); });
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});
