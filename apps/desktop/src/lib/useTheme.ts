/**
 * Theme hook — single source of truth for light/dark mode.
 *
 * Design decisions:
 *
 * - **Class-based, not media-query-based.** We opted into Tailwind v4's
 *   class strategy (`@custom-variant dark (&:where(.dark, .dark *))` in
 *   index.css). That means the user's choice always wins over the OS
 *   preference — which is what the toggle button is for.
 *
 * - **Persisted in localStorage.** Choice survives reloads and window
 *   restarts. localStorage is scoped per Tauri webview origin so this
 *   works cleanly across the dev shell and the bundled app.
 *
 * - **Default = OS preference.** First-run users see the theme that
 *   matches their system. Only when they click the toggle do we start
 *   persisting.
 *
 * - **DOM side-effect in useEffect, not in state initializer.** Writing
 *   to `document.documentElement.classList` from a lazy initializer would
 *   fire during SSR or during React's strict-mode double-invoke; keeping
 *   it in `useEffect` runs it once per real render.
 */

import { useCallback, useEffect, useState } from "react";

export type Theme = "light" | "dark";

const STORAGE_KEY = "agentic-visualizer:theme";

function initialTheme(): Theme {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === "light" || stored === "dark") return stored;
  } catch {
    // localStorage may throw in some contexts; fall through to OS pref.
  }
  if (typeof window !== "undefined" && window.matchMedia) {
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  return "light";
}

export function useTheme(): {
  theme: Theme;
  toggle: () => void;
  setTheme: (t: Theme) => void;
} {
  const [theme, setThemeState] = useState<Theme>(initialTheme);

  useEffect(() => {
    const root = document.documentElement;
    if (theme === "dark") root.classList.add("dark");
    else root.classList.remove("dark");
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // Non-fatal — the toggle still works for the session.
    }
  }, [theme]);

  const setTheme = useCallback((t: Theme) => setThemeState(t), []);
  const toggle = useCallback(() => setThemeState((t) => (t === "dark" ? "light" : "dark")), []);

  return { theme, toggle, setTheme };
}
