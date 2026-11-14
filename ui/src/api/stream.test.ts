import { describe, expect, it, vi } from "vitest";
import { IdentityStore, headerIdentity } from "../identity/identity";
import { fakeFetch, frame, sseResponse } from "../test/fakes";
import { EVENT_NAMES, decodeFrame, openStream, parseFrames, type LiveEvent } from "./stream";

const noSleep = () => new Promise<void>((r) => setTimeout(r, 0));

describe("parseFrames and decodeFrame", () => {
  it("splits frames across chunk boundaries and keeps the remainder", () => {
    const text = frame("run.progress", "e1", { text: "a" }) + frame("run.progress", "e2", { text: "b" }).slice(0, 20);
    const { frames, rest } = parseFrames(text);
    expect(frames).toHaveLength(1);
    expect(frames[0]?.id).toBe("e1");
    expect(rest.length).toBeGreaterThan(0);
  });

  it("ignores comments and keep-alive lines", () => {
    const { frames } = parseFrames(": keepalive\n\n" + frame("run.progress", "e1", {}));
    expect(frames).toHaveLength(1);
  });

  it("decodes every shared event name and rejects unknown names", () => {
    for (const name of EVENT_NAMES) {
      const { frames } = parseFrames(frame(name, "x", {}));
      expect(decodeFrame(frames[0]!)?.event).toBe(name);
    }
    const { frames } = parseFrames(frame("made.up", "x", {}));
    expect(decodeFrame(frames[0]!)).toBeNull();
  });

  it("reads the version fields used to refresh views", () => {
    const { frames } = parseFrames(frame("report.corrected", "x", { agent: "sentinel" }));
    const ev = decodeFrame(frames[0]!)!;
    expect(ev.authoritative_generation).toBe(1);
    expect(ev.report_version).toBe(1);
    expect(ev.correction_version).toBe(0);
  });
});

describe("openStream", () => {
  it("sends the identity header, delivers events and resumes with Last-Event-ID", async () => {
    const identity = new IdentityStore(headerIdentity, { role: "em" });
    let n = 0;
    const { impl, calls } = fakeFetch({
      "/api/runs/r1/stream": () => (n++ === 0 ? sseResponse([frame("run.progress", "e1", { text: "step" })]) : sseResponse([])),
    });
    const events: LiveEvent[] = [];
    const close = openStream({ identity, path: "/api/runs/r1/stream", fetchImpl: impl, sleep: noSleep, handlers: { onEvent: (e) => events.push(e), onReset: () => {} } });
    await vi.waitFor(() => expect(calls.length).toBeGreaterThanOrEqual(2));
    close();
    expect(events.map((e) => e.event_id)).toEqual(["e1"]);
    expect(calls[0]?.headers["x-atlas-role"]).toBe("em");
    expect(calls[1]?.headers["last-event-id"]).toBe("e1");
  });

  it("on stream.reset tells the caller to reload and resumes from resume_from", async () => {
    const identity = new IdentityStore(headerIdentity, { role: "pm" });
    let n = 0;
    const { impl, calls } = fakeFetch({
      "/api/runs/r1/stream": () =>
        n++ === 0 ? sseResponse([frame("stream.reset", null, { reason: "buffer", resume_from: "e9" })]) : sseResponse([]),
    });
    const resets: string[] = [];
    const close = openStream({ identity, path: "/api/runs/r1/stream", fetchImpl: impl, sleep: noSleep, handlers: { onEvent: () => {}, onReset: (r) => resets.push(r) } });
    await vi.waitFor(() => expect(calls.length).toBeGreaterThanOrEqual(2));
    close();
    expect(resets).toEqual(["buffer"]);
    expect(calls[1]?.headers["last-event-id"]).toBe("e9");
  });

  it("delivers nothing from the old identity after a role switch", async () => {
    const identity = new IdentityStore(headerIdentity, { role: "security" });
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    const enc = new TextEncoder();
    const { impl } = fakeFetch({
      "/api/runs/r1/stream": async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            async start(c) {
              await gate;
              c.enqueue(enc.encode(frame("run.progress", "late", { text: "restricted" })));
              c.close();
            },
          }),
        ),
    });
    const events: LiveEvent[] = [];
    openStream({ identity, path: "/api/runs/r1/stream", fetchImpl: impl, sleep: noSleep, handlers: { onEvent: (e) => events.push(e), onReset: () => {} } });
    identity.transition({ principal: { role: "pm" } });
    release();
    await new Promise((r) => setTimeout(r, 20));
    expect(events).toEqual([]);
  });
});
