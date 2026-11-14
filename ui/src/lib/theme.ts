// Theme and sidebar preferences. These are per-viewer conveniences, so browser storage is fine;
// every read and write is guarded because storage can be missing or blocked.

export type Theme = "light" | "dark";

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* storage blocked: the preference lasts for this page only */
  }
}

export function systemTheme(): Theme {
  try {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  } catch {
    return "light";
  }
}

export function storedTheme(): Theme | null {
  const v = read("atlas.theme");
  return v === "light" || v === "dark" ? v : null;
}

/** Applies the theme to <html> (an attribute, not an inline style, so the page security policy allows it). */
export function applyTheme(theme: Theme | null) {
  const root = document.documentElement;
  if (theme) root.setAttribute("data-theme", theme);
  else root.removeAttribute("data-theme");
}

export function setTheme(theme: Theme) {
  write("atlas.theme", theme);
  applyTheme(theme);
}

export function storedCollapsed(): boolean {
  return read("atlas.sidebar") === "collapsed";
}

export function setCollapsed(collapsed: boolean) {
  write("atlas.sidebar", collapsed ? "collapsed" : "open");
}
