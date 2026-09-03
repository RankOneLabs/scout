export type Theme = "light" | "dark";

/** The single localStorage key used to persist an explicit theme choice. */
export const THEME_STORAGE_KEY = "scout-theme";

/** Applied whenever no valid persisted preference exists. */
export const DEFAULT_THEME: Theme = "dark";

const THEME_CHANGE_EVENT = "scout-theme-change";

export function isTheme(value: unknown): value is Theme {
  return value === "light" || value === "dark";
}

function readStoredTheme(): Theme | null {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    return isTheme(stored) ? stored : null;
  } catch {
    return null;
  }
}

function writeStoredTheme(theme: Theme): void {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // localStorage unavailable (private mode, disabled) — theme still applies for this tab.
  }
}

/** The persisted preference if valid, otherwise the default. */
export function resolveTheme(): Theme {
  return readStoredTheme() ?? DEFAULT_THEME;
}

/** Applies a theme to the document without persisting it. */
export function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  root.classList.toggle("dark", theme === "dark");
  root.style.colorScheme = theme;
}

/** Applies a theme and persists it as the explicit preference. */
export function setTheme(theme: Theme): void {
  applyTheme(theme);
  writeStoredTheme(theme);
  window.dispatchEvent(new Event(THEME_CHANGE_EVENT));
}

/**
 * Source for a before-interactive inline script tag. Must stay a self-contained
 * string (no module imports are available yet) so it can run ahead of hydration
 * and apply a persisted preference before first paint.
 */
export function getThemeInitScript(): string {
  const key = JSON.stringify(THEME_STORAGE_KEY);
  const fallback = JSON.stringify(DEFAULT_THEME);
  return `(function(){var t=${fallback};try{var s=window.localStorage.getItem(${key});if(s==="light"||s==="dark")t=s;}catch(e){}var r=document.documentElement;if(t==="dark"){r.classList.add("dark");}else{r.classList.remove("dark");}r.style.colorScheme=t;})();`;
}

/** For useSyncExternalStore: notified whenever any setTheme() call changes the theme. */
export function subscribeToThemeChange(callback: () => void): () => void {
  window.addEventListener(THEME_CHANGE_EVENT, callback);
  return () => window.removeEventListener(THEME_CHANGE_EVENT, callback);
}

/** For useSyncExternalStore: the live theme as currently reflected on the root element. */
export function getCurrentTheme(): Theme {
  return document.documentElement.classList.contains("dark") ? "dark" : "light";
}
