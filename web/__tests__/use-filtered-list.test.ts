// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { useFilteredList } from "@/hooks/use-filtered-list";

interface Item {
  id: number;
  label: string;
}

function response(body: unknown, init: { ok?: boolean; status?: number } = {}): Response {
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    json: async () => body,
  } as Response;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

describe("useFilteredList", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("sets error state on non-ok initial fetches", async () => {
    vi.mocked(global.fetch).mockResolvedValueOnce(
      response({ detail: "server failed" }, { ok: false, status: 503 })
    );

    const { result } = renderHook(() =>
      useFilteredList<Item>({
        path: "/api/items",
        getLastId: (item) => item.id,
      })
    );

    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.items).toEqual([]);
    expect(result.current.hasMore).toBe(false);
    expect(result.current.error).toBe("HTTP 503");
  });

  it("sets error state when a paginated response has the wrong shape", async () => {
    vi.mocked(global.fetch).mockResolvedValueOnce(
      response({ error: "not a page" })
    );

    const { result } = renderHook(() =>
      useFilteredList<Item>({
        path: "/api/items",
        getLastId: (item) => item.id,
      })
    );

    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.items).toEqual([]);
    expect(result.current.error).toBe("invalid response shape");
  });

  it("preserves current items and reports an error when load more fails", async () => {
    vi.mocked(global.fetch)
      .mockResolvedValueOnce(
        response({
          data: [{ id: 2, label: "second" }],
          has_more: true,
        })
      )
      .mockResolvedValueOnce(
        response({ detail: "server failed" }, { ok: false, status: 500 })
      );

    const { result } = renderHook(() =>
      useFilteredList<Item>({
        path: "/api/items",
        getLastId: (item) => item.id,
      })
    );

    await waitFor(() => expect(result.current.loading).toBe(false));

    act(() => {
      result.current.loadMore();
    });
    await waitFor(() => expect(result.current.isLoadingMore).toBe(false));

    expect(result.current.items).toEqual([{ id: 2, label: "second" }]);
    expect(result.current.error).toBe("HTTP 500");
  });

  it("clears loading-more state when a new initial fetch supersedes load more", async () => {
    const loadMoreResponse = deferred<Response>();
    vi.mocked(global.fetch)
      .mockResolvedValueOnce(
        response({
          data: [{ id: 2, label: "second" }],
          has_more: true,
        })
      )
      .mockReturnValueOnce(loadMoreResponse.promise)
      .mockResolvedValueOnce(
        response({
          data: [{ id: 1, label: "filtered" }],
          has_more: false,
        })
      );

    const { result } = renderHook(() =>
      useFilteredList<Item>({
        path: "/api/items",
        getLastId: (item) => item.id,
      })
    );

    await waitFor(() => expect(result.current.loading).toBe(false));

    act(() => {
      result.current.loadMore();
    });
    await waitFor(() => expect(result.current.isLoadingMore).toBe(true));

    act(() => {
      result.current.setFilters("label", "filtered");
    });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.isLoadingMore).toBe(false);
    expect(result.current.items).toEqual([{ id: 1, label: "filtered" }]);

    await act(async () => {
      loadMoreResponse.resolve(
        response({
          data: [{ id: 0, label: "stale" }],
          has_more: false,
        })
      );
      await loadMoreResponse.promise;
    });

    expect(result.current.isLoadingMore).toBe(false);
    expect(result.current.items).toEqual([{ id: 1, label: "filtered" }]);
  });
});
