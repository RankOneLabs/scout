// @vitest-environment jsdom

import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Sidebar } from "@/components/organisms/Sidebar";

vi.mock("next/navigation", () => ({ usePathname: () => "/" }));

const installMatchMedia = (isDesktop: boolean) => {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: isDesktop,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
};

beforeEach(() => {
  document.documentElement.classList.add("dark");
});

afterEach(() => {
  cleanup();
  document.documentElement.classList.remove("dark");
  document.body.style.overflow = "";
  vi.restoreAllMocks();
});

describe("Sidebar theme control", () => {
  it("exposes the theme toggle in persistent desktop navigation", () => {
    installMatchMedia(true);
    render(React.createElement(Sidebar));

    expect(screen.getByRole("button", { name: "Switch to light theme" })).toBeTruthy();
    expect(document.getElementById("mobile-nav")?.hasAttribute("inert")).toBe(false);
  });

  it("exposes the theme toggle when the mobile drawer opens", () => {
    installMatchMedia(false);
    render(React.createElement(Sidebar));

    const drawer = document.getElementById("mobile-nav");
    expect(drawer?.hasAttribute("inert")).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Open menu" }));

    expect(drawer?.hasAttribute("inert")).toBe(false);
    expect(screen.getByRole("button", { name: "Switch to light theme" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Close menu" }).getAttribute("aria-expanded")).toBe("true");
  });
});
