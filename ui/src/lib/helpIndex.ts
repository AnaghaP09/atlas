// Loads the list of available guide pages once. If the docs are not served, every help link is hidden.
import { HELP_INDEX, HELP_SLUG, helpUrl } from "../help";

let available: Set<string> | null = null;
let started = false;
const listeners = new Set<() => void>();

export function loadHelpIndex(fetchImpl: typeof fetch = fetch): void {
  if (started) return;
  started = true;
  void fetchImpl(HELP_INDEX, { headers: { Accept: "application/json" } })
    .then(async (r) => {
      const type = r.headers.get("content-type") ?? "";
      if (!r.ok || !type.includes("json")) return;
      const body = (await r.json()) as { pages?: string[] } | string[];
      const list = Array.isArray(body) ? body : (body.pages ?? []);
      available = new Set(list.map(String));
      listeners.forEach((l) => l());
    })
    .catch(() => {
      available = null;
    });
}

export function onHelpIndex(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The guide link for a page, or null when the docs do not have that page. */
export function helpFor(pageId: string): string | null {
  const slug = HELP_SLUG[pageId];
  if (!slug || !available || !available.has(slug)) return null;
  return helpUrl(slug);
}
