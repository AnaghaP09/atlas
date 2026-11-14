// Reads the backend's live event streams with fetch, so the identity headers are sent (the
// browser's EventSource cannot send headers). Reconnects with back-off and resumes from the last
// event id. On `stream.reset` the caller drops every cached view for the stream and refetches.

import type { IdentityStore } from "../identity/identity";

export const EVENT_NAMES = [
  "run.progress",
  "run.status_changed",
  "run.generation_changed",
  "report.superseded",
  "report.corrected",
  "report.withdrawn",
  "task.state_changed",
  "approval.voided",
  "notification.created",
  "containment.tripped",
  "containment.resumed",
  "stream.reset",
] as const;
export type EventName = (typeof EVENT_NAMES)[number];

export interface LiveEvent {
  event: EventName;
  event_id: string;
  emitted_at: string;
  run_id: string | null;
  authoritative_generation: number | null;
  report_version: number | null;
  correction_version: number | null;
  data: Record<string, unknown>;
}

export interface SseFrame {
  id?: string;
  event?: string;
  data: string;
}

/** Splits text into SSE frames. Returns the frames and the unparsed remainder. */
export function parseFrames(buffer: string): { frames: SseFrame[]; rest: string } {
  const frames: SseFrame[] = [];
  const normalised = buffer.replace(/\r\n/g, "\n");
  const parts = normalised.split("\n\n");
  const rest = parts.pop() ?? "";
  for (const part of parts) {
    const frame: SseFrame = { data: "" };
    const dataLines: string[] = [];
    for (const line of part.split("\n")) {
      if (line.startsWith(":") || line === "") continue;
      const idx = line.indexOf(":");
      const field = idx === -1 ? line : line.slice(0, idx);
      const value = idx === -1 ? "" : line.slice(idx + 1).replace(/^ /, "");
      if (field === "id") frame.id = value;
      else if (field === "event") frame.event = value;
      else if (field === "data") dataLines.push(value);
    }
    if (dataLines.length === 0 && !frame.event) continue;
    frame.data = dataLines.join("\n");
    frames.push(frame);
  }
  return { frames, rest };
}

/** Turns a frame into a known event, or null for anything outside the shared event list. */
export function decodeFrame(frame: SseFrame): LiveEvent | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(frame.data);
  } catch {
    return null;
  }
  const ev = parsed as Partial<LiveEvent>;
  const name = (frame.event ?? ev.event) as EventName | undefined;
  if (!name || !(EVENT_NAMES as readonly string[]).includes(name)) return null;
  return {
    event: name,
    event_id: String(ev.event_id ?? frame.id ?? ""),
    emitted_at: String(ev.emitted_at ?? ""),
    run_id: ev.run_id ?? null,
    authoritative_generation: ev.authoritative_generation ?? null,
    report_version: ev.report_version ?? null,
    correction_version: ev.correction_version ?? null,
    data: (ev.data as Record<string, unknown>) ?? {},
  };
}

export interface StreamHandlers {
  onEvent: (event: LiveEvent) => void;
  onReset: (reason: string) => void;
  onStatus?: (status: "live" | "reconnecting" | "closed") => void;
}

export interface OpenStreamOptions {
  identity: IdentityStore;
  path: string;
  handlers: StreamHandlers;
  fetchImpl?: typeof fetch;
  maxBackoffMs?: number;
  sleep?: (ms: number, signal: AbortSignal) => Promise<void>;
}

const defaultSleep = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve) => {
    const t = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(t);
      resolve();
    });
  });

/**
 * Opens a stream bound to the current identity epoch. It stops for good when the epoch ends or the
 * returned close function is called. Events from an old epoch are never delivered.
 */
export function openStream(opts: OpenStreamOptions): () => void {
  const { identity, path, handlers } = opts;
  const fetchImpl = opts.fetchImpl ?? ((...a: Parameters<typeof fetch>) => fetch(...a));
  const sleep = opts.sleep ?? defaultSleep;
  const { epoch, signal: epochSignal } = identity.snapshot;
  const local = new AbortController();
  const stop = () => local.abort();
  epochSignal.addEventListener("abort", stop);
  let lastEventId: string | undefined;
  let backoff = 500;
  const max = opts.maxBackoffMs ?? 15_000;
  const current = () => !local.signal.aborted && identity.isCurrent(epoch);

  (async () => {
    while (current()) {
      try {
        const headers: Record<string, string> = { Accept: "text/event-stream", ...identity.headers() };
        if (lastEventId) headers["Last-Event-ID"] = lastEventId;
        const res = await fetchImpl(path, { headers, signal: local.signal, credentials: "same-origin", cache: "no-store" });
        if (!res.ok || !res.body) throw new Error(`stream refused (${res.status})`);
        handlers.onStatus?.("live");
        backoff = 500;
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done || !current()) break;
          buffer += decoder.decode(value, { stream: true });
          const { frames, rest } = parseFrames(buffer);
          buffer = rest;
          for (const frame of frames) {
            if (!current()) return;
            const ev = decodeFrame(frame);
            if (!ev) continue;
            if (ev.event === "stream.reset") {
              const resumeFrom = typeof ev.data.resume_from === "string" ? ev.data.resume_from : undefined;
              lastEventId = resumeFrom;
              handlers.onReset(String(ev.data.reason ?? "replay buffer exceeded"));
              continue;
            }
            if (frame.id) lastEventId = frame.id;
            handlers.onEvent(ev);
          }
        }
      } catch {
        // fall through to reconnect
      }
      if (!current()) break;
      handlers.onStatus?.("reconnecting");
      await sleep(backoff, local.signal);
      backoff = Math.min(backoff * 2, max);
    }
    handlers.onStatus?.("closed");
  })();

  return () => {
    epochSignal.removeEventListener("abort", stop);
    stop();
  };
}
