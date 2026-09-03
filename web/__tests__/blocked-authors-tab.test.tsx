// @vitest-environment jsdom

import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { BlockedAuthorsTab } from "@/components/organisms/BlockedAuthorsTab";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const blockedAuthors = [
  {
    id: 1,
    platform: "discord",
    author_id: "alice-id",
    author_name: "alice",
    reason: null,
    active: true,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  },
  {
    id: 2,
    platform: "discord",
    author_id: "bob-id",
    author_name: "bob",
    reason: null,
    active: true,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  },
];

describe("BlockedAuthorsTab", () => {
  it("allows only one unblock request at a time and disables every row", async () => {
    let finishDelete: ((response: Response) => void) | undefined;
    const pendingDelete = new Promise<Response>((resolve) => {
      finishDelete = resolve;
    });
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (init?.method === "DELETE") return pendingDelete;
      return Promise.resolve({
        ok: true,
        json: async () => ({ blocked_authors: blockedAuthors }),
      } as Response);
    });
    vi.stubGlobal("fetch", fetchMock);

    const { findAllByRole } = render(React.createElement(BlockedAuthorsTab));
    const buttons = await findAllByRole("button", { name: "Unblock" });

    fireEvent.click(buttons[0]);

    await waitFor(() => {
      expect((buttons[0] as HTMLButtonElement).disabled).toBe(true);
      expect((buttons[1] as HTMLButtonElement).disabled).toBe(true);
    });
    fireEvent.click(buttons[1]);
    expect(
      fetchMock.mock.calls.filter(([, init]) => init?.method === "DELETE")
    ).toHaveLength(1);

    finishDelete?.({ ok: true } as Response);
    await waitFor(() =>
      expect((buttons[1] as HTMLButtonElement).disabled).toBe(false)
    );
  });
});
