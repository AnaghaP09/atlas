// Test helpers: a fake fetch that answers from a route table and records every request.

export interface RecordedRequest {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: unknown;
}

export type Handler = (req: RecordedRequest) => Response | Promise<Response>;

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

export function fakeFetch(routes: Record<string, Handler>) {
  const calls: RecordedRequest[] = [];
  const impl = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const headers: Record<string, string> = {};
    new Headers(init?.headers).forEach((v, k) => (headers[k.toLowerCase()] = v));
    const req: RecordedRequest = {
      url,
      method: init?.method ?? "GET",
      headers,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    };
    calls.push(req);
    if (init?.signal?.aborted) throw new DOMException("aborted", "AbortError");
    const path = url.split("?")[0] ?? url;
    const handler = routes[`${req.method} ${path}`] ?? routes[path];
    if (!handler) return json({ error: "not_found", reason: `no fake for ${req.method} ${path}` }, 404);
    return handler(req);
  };
  return { impl: impl as typeof fetch, calls };
}

/** A response whose body streams the given SSE text chunks, then ends. */
export function sseResponse(chunks: string[]): Response {
  const enc = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(enc.encode(c));
      controller.close();
    },
  });
  return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

export function frame(event: string, id: string | null, data: Record<string, unknown>): string {
  const body = {
    event,
    event_id: id ?? "",
    emitted_at: "2026-11-14T00:00:00Z",
    run_id: "r1",
    authoritative_generation: 1,
    report_version: 1,
    correction_version: 0,
    data,
  };
  return `${id ? `id: ${id}\n` : ""}event: ${event}\ndata: ${JSON.stringify(body)}\n\n`;
}
