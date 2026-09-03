// @vitest-environment jsdom

import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render } from "@testing-library/react";
import SettingsPage from "@/app/settings/page";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

// The tabs each mount a data-fetching hook; return empty lists so the
// page renders without network. The tab-keyboard behavior is independent of
// the fetched data.
function stubEmptyFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: unknown) => {
      const u = String(url);
      const key = u.includes("/keywords")
        ? "keywords"
        : u.includes("/prompts")
          ? "prompts"
          : u.includes("/blocked-authors")
            ? "blocked_authors"
            : "projects";
      return {
        ok: true,
        status: 200,
        json: async () => ({ [key]: [] }),
      } as Response;
    })
  );
}

describe("SettingsPage tabs (WAI-ARIA keyboard nav)", () => {
  it("starts with Projects selected and a roving tabIndex", () => {
    stubEmptyFetch();
    const { getByRole } = render(React.createElement(SettingsPage));
    const projects = getByRole("tab", { name: "Projects" });
    const keywords = getByRole("tab", { name: "Keywords" });
    const prompts = getByRole("tab", { name: "Prompts" });
    const blockedAuthors = getByRole("tab", { name: "Blocked Authors" });

    expect(projects.getAttribute("aria-selected")).toBe("true");
    expect(projects.getAttribute("tabindex")).toBe("0");
    expect(keywords.getAttribute("tabindex")).toBe("-1");
    expect(prompts.getAttribute("tabindex")).toBe("-1");
    expect(blockedAuthors.getAttribute("tabindex")).toBe("-1");
  });

  it("ArrowRight selects the next tab and moves the roving tabIndex", () => {
    stubEmptyFetch();
    const { getByRole } = render(React.createElement(SettingsPage));
    const projects = getByRole("tab", { name: "Projects" });

    fireEvent.keyDown(projects, { key: "ArrowRight" });

    const keywords = getByRole("tab", { name: "Keywords" });
    expect(keywords.getAttribute("aria-selected")).toBe("true");
    expect(keywords.getAttribute("tabindex")).toBe("0");
    expect(projects.getAttribute("tabindex")).toBe("-1");
  });

  it("ArrowLeft from the first tab wraps to the last", () => {
    stubEmptyFetch();
    const { getByRole } = render(React.createElement(SettingsPage));
    const projects = getByRole("tab", { name: "Projects" });

    fireEvent.keyDown(projects, { key: "ArrowLeft" });

    expect(getByRole("tab", { name: "Blocked Authors" }).getAttribute("aria-selected")).toBe(
      "true"
    );
  });

  it("Home and End jump to the first and last tabs", () => {
    stubEmptyFetch();
    const { getByRole } = render(React.createElement(SettingsPage));
    const projects = getByRole("tab", { name: "Projects" });

    fireEvent.keyDown(projects, { key: "End" });
    expect(getByRole("tab", { name: "Blocked Authors" }).getAttribute("aria-selected")).toBe(
      "true"
    );

    fireEvent.keyDown(getByRole("tab", { name: "Blocked Authors" }), { key: "Home" });
    expect(getByRole("tab", { name: "Projects" }).getAttribute("aria-selected")).toBe(
      "true"
    );
  });
});
