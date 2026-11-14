// Policy for links that leave ATLAS. Only https links to listed hosts are allowed, the query
// string and fragment are removed (they could carry data out), and the browser sends no referrer.
// Evidence is always opened inside ATLAS, never through an external link.

export const DEFAULT_ALLOWED_HOSTS = [
  "github.com",
  "prow.ci.kubevirt.io",
  "storage.googleapis.com",
  "osv.dev",
] as const;

export interface SafeExternal {
  href: string;
  host: string;
}

export function safeExternalUrl(raw: string, allowedHosts: readonly string[] = DEFAULT_ALLOWED_HOSTS): SafeExternal | null {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  if (url.protocol !== "https:") return null;
  if (url.username || url.password) return null;
  if (url.port && url.port !== "443") return null;
  const host = url.hostname.toLowerCase();
  if (!allowedHosts.includes(host)) return null;
  return { href: `https://${host}${url.pathname}`, host };
}
