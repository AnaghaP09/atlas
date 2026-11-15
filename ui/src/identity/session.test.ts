import { describe, expect, it, vi } from "vitest";
import { createClient } from "../api/client";
import { withScope } from "../api/stream";
import { fakeFetch, json } from "../test/fakes";
import { bootstrapIdentity, signOut } from "./bootstrap";
import { IdentityStore, sessionIdentity, type SessionInfo } from "./identity";

const SESSION: SessionInfo = {
  user: "pm",
  display_name: "Product manager (demo)",
  projects: { kubevirt: "pm", "argo-cd": "em" },
  default_project: "kubevirt",
  csrf_token: "csrf-123",
  expires_at: 0,
  idle_seconds: 1800,
};

function signedIn(navigate = vi.fn()) {
  return { identity: IdentityStore.fromSession(sessionIdentity(navigate), SESSION), navigate };
}

describe("page load", () => {
  it("uses sign-in mode when the server has a session", async () => {
    const f = fakeFetch({ "/api/session": () => json(SESSION) });
    const boot = await bootstrapIdentity(f.impl);
    expect(boot.kind).toBe("ready");
    if (boot.kind !== "ready") return;
    expect(boot.identity.provider.mode).toBe("session");
    expect(boot.identity.snapshot.projectId).toBe("kubevirt");
    expect(boot.identity.snapshot.principal?.role).toBe("pm");
  });

  it("goes to the sign-in page when there is no session", async () => {
    const f = fakeFetch({ "/api/session": () => json({ error: "unauthorised" }, 401) });
    expect(await bootstrapIdentity(f.impl)).toEqual({ kind: "redirect", to: "/login" });
  });

  it("uses the demo role header when the server says sign-in is off", async () => {
    const f = fakeFetch({ "/api/session": () => json({ sign_in: false, identity: "header" }) });
    const boot = await bootstrapIdentity(f.impl);
    expect(boot.kind === "ready" && boot.identity.provider.mode).toBe("header");
  });

  it("uses the demo role header when an older server has no session route", async () => {
    const f = fakeFetch({});
    const boot = await bootstrapIdentity(f.impl);
    expect(boot.kind === "ready" && boot.identity.provider.mode).toBe("header");
  });
});

describe("requests in sign-in mode", () => {
  it("sends the CSRF token on writes only, the project on every request, and never a role header", async () => {
    const { identity } = signedIn();
    const f = fakeFetch({ "/api/runs": () => json([]), "POST /api/triggers/question": () => json({ run_id: "r1" }) });
    const client = createClient(identity, f.impl);
    await client.get("/api/runs");
    await client.post("/api/triggers/question", { question: "q" }, "key-1");
    const [get, post] = f.calls;
    expect(get?.headers["x-csrf-token"]).toBeUndefined();
    expect(get?.url).toBe("/api/runs?project_id=kubevirt");
    expect(post?.headers["x-csrf-token"]).toBe("csrf-123");
    expect(post?.url).toBe("/api/triggers/question?project_id=kubevirt");
    expect(get?.headers["x-atlas-role"]).toBeUndefined();
    expect(post?.headers["x-atlas-role"]).toBeUndefined();
  });

  it("goes to the sign-in page on 401", async () => {
    const { identity, navigate } = signedIn();
    const f = fakeFetch({ "/api/runs": () => json({ error: "unauthorised", reason: "sign-in required" }, 401) });
    await expect(createClient(identity, f.impl).get("/api/runs")).rejects.toThrow();
    expect(navigate).toHaveBeenCalledWith("/login");
  });

  it("adds the project to stream paths", () => {
    expect(withScope("/api/notifications/stream", { project_id: "kubevirt" })).toBe(
      "/api/notifications/stream?project_id=kubevirt",
    );
    expect(withScope("/api/runs/r1/stream?follow=false", { project_id: "a" })).toBe(
      "/api/runs/r1/stream?follow=false&project_id=a",
    );
    expect(withScope("/api/x", {})).toBe("/api/x");
  });
});

describe("project switch", () => {
  it("takes the user's role in the new project and resets the epoch", () => {
    const { identity } = signedIn();
    const before = identity.snapshot;
    identity.selectProject("argo-cd");
    expect(before.signal.aborted).toBe(true);
    expect(identity.snapshot.epoch).toBe(before.epoch + 1);
    expect(identity.snapshot.projectId).toBe("argo-cd");
    expect(identity.snapshot.principal?.role).toBe("em");
  });

  it("drops a response that arrives after the switch", async () => {
    const { identity } = signedIn();
    let release: (r: Response) => void = () => undefined;
    const f = fakeFetch({ "/api/runs": () => new Promise<Response>((r) => (release = r)) });
    const pending = createClient(identity, f.impl).get("/api/runs");
    identity.selectProject("argo-cd");
    release(json([{ run_id: "old" }]));
    await expect(pending).rejects.toThrow();
  });
});

describe("sign-out", () => {
  it("posts to /logout with the CSRF token, then shows the sign-in page", async () => {
    const { identity } = signedIn();
    const navigate = vi.fn();
    const f = fakeFetch({ "POST /logout": () => new Response(null, { status: 303 }) });
    await signOut(identity, f.impl, navigate);
    expect(f.calls[0]?.headers["x-csrf-token"]).toBe("csrf-123");
    expect(navigate).toHaveBeenCalledWith("/login");
  });
});
