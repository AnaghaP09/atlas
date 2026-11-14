// A small client-side router. Paths never start with /api; the backend serves the UI for every
// other path and never answers /api paths with the UI.

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

interface RouterState {
  path: string;
  search: URLSearchParams;
  navigate: (to: string) => void;
}

const RouterContext = createContext<RouterState | null>(null);

export function RouterProvider({ children }: { children: ReactNode }) {
  const [location, setLocation] = useState(() => ({ path: window.location.pathname, search: window.location.search }));
  useEffect(() => {
    const onPop = () => setLocation({ path: window.location.pathname, search: window.location.search });
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const navigate = useCallback((to: string) => {
    if (to.startsWith("/api")) throw new Error("the UI never navigates to an API path");
    window.history.pushState(null, "", to);
    const url = new URL(to, window.location.origin);
    setLocation({ path: url.pathname, search: url.search });
  }, []);
  return (
    <RouterContext.Provider value={{ path: location.path, search: new URLSearchParams(location.search), navigate }}>
      {children}
    </RouterContext.Provider>
  );
}

export function useRouter(): RouterState {
  const ctx = useContext(RouterContext);
  if (!ctx) throw new Error("useRouter outside RouterProvider");
  return ctx;
}

/** Matches "/runs/:id" against a path. Returns the parameters or null. */
export function matchPath(pattern: string, path: string): Record<string, string> | null {
  const p = pattern.split("/").filter(Boolean);
  const a = path.split("/").filter(Boolean);
  if (p.length !== a.length) return null;
  const params: Record<string, string> = {};
  for (let i = 0; i < p.length; i++) {
    const seg = p[i] ?? "";
    const val = a[i] ?? "";
    if (seg.startsWith(":")) params[seg.slice(1)] = decodeURIComponent(val);
    else if (seg !== val) return null;
  }
  return params;
}

export function Link({
  to,
  children,
  className,
  title,
  "aria-label": ariaLabel,
}: {
  to: string;
  children: ReactNode;
  className?: string;
  title?: string;
  "aria-label"?: string;
}) {
  const { navigate } = useRouter();
  return (
    <a
      href={to}
      className={className}
      title={title}
      aria-label={ariaLabel}
      onClick={(e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
        e.preventDefault();
        navigate(to);
      }}
    >
      {children}
    </a>
  );
}
