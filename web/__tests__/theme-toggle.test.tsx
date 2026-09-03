// @vitest-environment jsdom

import React from "react";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ThemeToggle } from "@/components/atoms/ThemeToggle";
import { THEME_STORAGE_KEY } from "@/lib/theme";

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  document.documentElement.classList.remove("dark");
  document.documentElement.style.colorScheme = "";
});

describe("ThemeToggle", () => {
  it("reflects the current dark theme with pressed state and an accessible name", () => {
    document.documentElement.classList.add("dark");
    render(React.createElement(ThemeToggle));

    const button = screen.getByRole("button", { name: "Switch to light theme" });
    expect(button.tagName).toBe("BUTTON");
    expect(button.getAttribute("type")).toBe("button");
    expect(button.getAttribute("aria-pressed")).toBe("true");
    expect(button.className).toContain("focus-visible:outline");
  });

  it("toggles the root class, color-scheme, persisted value, and its own label on click", () => {
    document.documentElement.classList.add("dark");
    render(React.createElement(ThemeToggle));

    const button = screen.getByRole("button", { name: "Switch to light theme" });
    fireEvent.click(button);

    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(document.documentElement.style.colorScheme).toBe("light");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
    const toggledButton = screen.getByRole("button", { name: "Switch to dark theme" });
    expect(toggledButton.getAttribute("aria-pressed")).toBe("false");
  });

  // Enter/Space activation is native <button> behavior guaranteed by the HTML spec in
  // every real browser; jsdom doesn't translate those keydowns into a click (and
  // @testing-library/user-event, which does, isn't a dependency here), so this only
  // exercises the light-to-dark click path — the mirror of the test above.
  it("toggles from light to dark on click", () => {
    document.documentElement.classList.remove("dark");
    render(React.createElement(ThemeToggle));

    const button = screen.getByRole("button", { name: "Switch to dark theme" });
    fireEvent.click(button);

    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
  });
});
