// @vitest-environment jsdom

import { afterEach, describe, expect, it } from "vitest";
import {
  DEFAULT_THEME,
  THEME_STORAGE_KEY,
  applyTheme,
  getThemeInitScript,
  isTheme,
  resolveTheme,
  setTheme,
} from "@/lib/theme";

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.classList.remove("dark");
  document.documentElement.style.colorScheme = "";
});

describe("isTheme", () => {
  it("accepts only the two valid theme values", () => {
    expect(isTheme("light")).toBe(true);
    expect(isTheme("dark")).toBe(true);
    expect(isTheme("system")).toBe(false);
    expect(isTheme(null)).toBe(false);
    expect(isTheme(undefined)).toBe(false);
  });
});

describe("resolveTheme", () => {
  it("falls back to the default when nothing is stored", () => {
    expect(resolveTheme()).toBe(DEFAULT_THEME);
  });

  it("falls back to the default when the stored value is invalid", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "not-a-theme");
    expect(resolveTheme()).toBe(DEFAULT_THEME);
  });

  it.each(["light", "dark"] as const)("returns the stored %s preference", (theme) => {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
    expect(resolveTheme()).toBe(theme);
  });
});

describe("applyTheme", () => {
  it("toggles the root dark class and color-scheme without persisting", () => {
    applyTheme("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(document.documentElement.style.colorScheme).toBe("light");

    applyTheme("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(document.documentElement.style.colorScheme).toBe("dark");

    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();
  });
});

describe("setTheme", () => {
  it("applies and persists the preference under the documented key", () => {
    setTheme("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");

    setTheme("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
  });
});

describe("getThemeInitScript", () => {
  it("embeds the storage key and default theme as literals", () => {
    const script = getThemeInitScript();
    expect(script).toContain(JSON.stringify(THEME_STORAGE_KEY));
    expect(script).toContain(JSON.stringify(DEFAULT_THEME));
  });

  it("reproduces resolveTheme's fallback behavior when executed", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "light");
    new Function(getThemeInitScript())();
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(document.documentElement.style.colorScheme).toBe("light");
  });

  it("defaults to dark when the stored value is invalid", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "not-a-theme");
    new Function(getThemeInitScript())();
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(document.documentElement.style.colorScheme).toBe("dark");
  });

  it("defaults to dark when no preference is stored", () => {
    new Function(getThemeInitScript())();
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(document.documentElement.style.colorScheme).toBe("dark");
  });
});
