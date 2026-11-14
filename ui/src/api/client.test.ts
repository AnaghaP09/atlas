import { describe, expect, it } from "vitest";
import { IdentityStore, headerIdentity } from "../identity/identity";
import { fakeFetch, json } from "../test/fakes";
import { ApiError, StaleEpochError, buildPath, createClient } from "./client";

describe("identity epoch", () => {
  it("a role or project switch starts a new epoch and aborts the old one", () => {
    const id = new IdentityStore(headerIdentity, { role: "pm" });
    const old = id.snapshot;
    id.transition({ principal: { role: "em" } });
    expect(old.signal.aborted).toBe(true);
    expect(id.snapshot.epoch).toBe(old.epoch + 1);
    const second = id.snapshot;
    id.transition({ projectId: "argo-cd" });
    expect(second.signal.aborted).toBe(true);
    expect(id.snapshot.projectId).toBe("argo-cd");
    expect(id.snapshot.principal?.role).toBe("em");
  });

  it("a same-role project switch also resets", () => {
    const id = new IdentityStore(headerIdentity, { role: "em" }, "kubevirt");
    const before = id.snapshot.epoch;
    id.transition({ projectId: "argo-cd" });
    expect(id.snapshot.epoch).toBe(before + 1);
  });

  it("no change keeps the epoch", () => {
    const id = new IdentityStore(headerIdentity, { role: "pm" });
    id.transition({ principal: { role: "pm" } });
    expect(id.snapshot.epoch).toBe(0);
  });
});

describe("client", () => {
  it("only allows /api paths", () => {
    expect(() => buildPath("/health")).toThrow();
    expect(buildPath("/api/runs", { a: 1, b: null })).toBe("/api/runs?a=1");
  });

  it("sends the role header and refuses a write without an Idempotency-Key", async () => {
    const id = new IdentityStore(headerIdentity, { role: "audit" });
    const { impl, calls } = fakeFetch({ "/api/mode": () => json({ mode: "connected" }) });
    const c = createClient(id, impl);
    await c.get("/api/mode");
    expect(calls[0]?.headers["x-atlas-role"]).toBe("audit");
    await expect(c.post("/api/mode", {}, "")).rejects.toThrow(/Idempotency-Key/);
  });

  it("turns error bodies into ApiError with the server's reason", async () => {
    const id = new IdentityStore(headerIdentity, { role: "pm" });
    const { impl } = fakeFetch({ "/api/tasks": () => json({ error: "not_connected", reason: "not connected yet" }, 503) });
    const err = await createClient(id, impl).get("/api/tasks").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).notConnected).toBe(true);
    expect((err as ApiError).reason).toBe("not connected yet");
  });

  it("a response that arrives after a role switch is dropped", async () => {
    const id = new IdentityStore(headerIdentity, { role: "security" });
    let answer!: (r: Response) => void;
    const { impl } = fakeFetch({ "/api/queue": () => new Promise<Response>((r) => (answer = r)) });
    const pending = createClient(id, impl).get("/api/queue");
    await Promise.resolve();
    id.transition({ principal: { role: "pm" } });
    answer(json({ entries: [{ reason: "restricted" }] }));
    await expect(pending).rejects.toBeInstanceOf(StaleEpochError);
  });
});
