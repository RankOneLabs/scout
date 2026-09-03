"use client";

import { useCallback, useSyncExternalStore } from "react";
import { Moon, Sun } from "lucide-react";
import {
  DEFAULT_THEME,
  getCurrentTheme,
  setTheme,
  subscribeToThemeChange,
} from "@/lib/theme";

function getServerSnapshot() {
  return DEFAULT_THEME;
}

function subscribeNever() {
  return () => {};
}

function getMounted() {
  return true;
}

function getMountedServerSnapshot() {
  return false;
}

export function ThemeToggle() {
  // Client-only mount check via useSyncExternalStore rather than a manual
  // setState-in-effect: React reconciles it against getMounted() right after commit,
  // the same mechanism that keeps `theme` below correct after hydration.
  const mounted = useSyncExternalStore(subscribeNever, getMounted, getMountedServerSnapshot);

  const theme = useSyncExternalStore(subscribeToThemeChange, getCurrentTheme, getServerSnapshot);
  const isDark = theme === "dark";

  const toggleTheme = useCallback(() => {
    setTheme(isDark ? "light" : "dark");
  }, [isDark]);

  // The server (and first client render) can't know the persisted preference — it's
  // applied to the DOM by an inline script that runs before hydration. Rendering
  // nothing until mounted avoids ever announcing an aria-pressed/label that doesn't
  // match the page's actual theme.
  if (!mounted) {
    return null;
  }

  const label = isDark ? "Switch to light theme" : "Switch to dark theme";

  return (
    <button
      type="button"
      onClick={toggleTheme}
      aria-pressed={isDark}
      aria-label={label}
      title={label}
      className="inline-flex items-center justify-center rounded-md p-1.5 text-gray-500 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-500 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200"
    >
      {isDark ? <Moon className="h-4 w-4" aria-hidden="true" /> : <Sun className="h-4 w-4" aria-hidden="true" />}
    </button>
  );
}
