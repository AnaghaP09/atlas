// The only way the UI talks to the backend. All paths start with /api on the same origin.
// Requests carry the identity headers, are tied to the current identity epoch, and are aborted when
// the epoch changes. Writes carry an Idempotency-Key chosen by the caller.

import type { IdentityStore } from "../identity/identity";

export const API_PREFIX = "/api";

export class ApiError extends Error {
  readonly status: number;
  readonly error: string;
  readonly reason: string;
  constructor(status: number, error: string, reason: string) {
    super(reason || error || `request failed (${status})`);
    this.status = status;
    this.error = error;
    this.reason = reason;
  }
  get refused(): boolean {
    return this.status === 401 || this.status === 403;
  }
  get notConnected(): boolean {
    return this.status === 503 && this.error === "not_connected";
  }
}

/** Thrown when a response arrives after the identity changed; callers ignore it. */
export class StaleEpochError extends Error {
  constructor() {
    super("identity changed while the request was in flight");
  }
}

export interface RequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
  idempotencyKey?: string;
  query?: Record<string, string | number | boolean | null | undefined>;
}

export function buildPath(path: string, query?: RequestOptions["query"]): string {
  if (!path.startsWith(API_PREFIX + "/")) throw new Error(`API paths must start with ${API_PREFIX}/`);
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query ?? {})) {
    if (v !== undefined && v !== null) params.set(k, String(v));
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

export function createClient(identity: IdentityStore, fetchImpl: typeof fetch = (...a) => fetch(...a)) {
  async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const { epoch, signal } = identity.snapshot;
    const method = options.method ?? "GET";
    const headers: Record<string, string> = { Accept: "application/json", ...identity.headers(method !== "GET") };
    if (method !== "GET") {
      if (!options.idempotencyKey) throw new Error("every write needs an Idempotency-Key");
      headers["Idempotency-Key"] = options.idempotencyKey;
      headers["Content-Type"] = "application/json";
    }
    let response: Response;
    try {
      response = await fetchImpl(buildPath(path, { ...identity.scope(), ...(options.query ?? {}) }), {
        method,
        headers,
        body: method === "GET" ? undefined : JSON.stringify(options.body ?? {}),
        signal,
        credentials: "same-origin",
        cache: "no-store",
        referrerPolicy: "no-referrer",
      });
    } catch (err) {
      if (!identity.isCurrent(epoch)) throw new StaleEpochError();
      throw err;
    }
    if (!identity.isCurrent(epoch)) throw new StaleEpochError();
    const text = await response.text();
    let data: unknown;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = null;
    }
    if (!identity.isCurrent(epoch)) throw new StaleEpochError();
    if (response.status === 401) identity.provider.onUnauthorised();
    if (!response.ok) {
      const d = (data ?? {}) as { error?: string; reason?: string; detail?: string };
      throw new ApiError(response.status, d.error ?? "error", d.reason ?? d.detail ?? response.statusText);
    }
    return data as T;
  }
  return {
    get: <T>(path: string, query?: RequestOptions["query"]) => request<T>(path, { query }),
    post: <T>(path: string, body: unknown, idempotencyKey: string) =>
      request<T>(path, { method: "POST", body, idempotencyKey }),
  };
}

export type ApiClient = ReturnType<typeof createClient>;
