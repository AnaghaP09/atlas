import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { App } from "../App";
import { AppProvider, createQueryClient, useAction } from "../app/context";
import { ActionResult, DataModeBadge } from "../components/common";
import { IdentityStore, headerIdentity } from "../identity/identity";
import { RouterProvider } from "../lib/router";
import { fakeFetch, json, type Handler } from "../test/fakes";
import { Approvals } from "./Approvals";
import { Evaluation } from "./Evaluation";
import { RunNow } from "./RunNow";
import { Schedules } from "./Schedules";

const envelope = { data_mode: "REAL", environment: "normal", server_time: "2026-11-14T00:00:00Z" };
const health = json({ ...envelope, status: "ok", environment: "normal", server: "127.0.0.1:8000", mode: "connected", identity: "header", components: {}, staged: [], unevaluated: [] });

function renderWith(ui: React.ReactNode, routes: Record<string, Handler>, identity = new IdentityStore(headerIdentity, { role: "pm" })) {
  const f = fakeFetch({ "/api/health": () => health.clone(), ...routes });
  const result = render(
    <AppProvider identity={identity} queryClient={createQueryClient()} fetchImpl={f.impl}>
      <RouterProvider>{ui}</RouterProvider>
    </AppProvider>,
  );
  return { ...result, calls: f.calls, identity };
}

describe("honest labels", () => {
  it.each([
    ["REAL", "Real data"],
    ["SYNTHETIC", "Sample data"],
    ["STAGED", "Not in this demo"],
    ["UNEVALUATED", "Not measured"],
  ])("%s shows as %s with its reason", (mode, text) => {
    render(<DataModeBadge mode={mode} reason="no GPU" />);
    expect(screen.getByTestId("data-mode").textContent).toContain(text);
    expect(screen.getByTestId("data-mode").textContent).toContain("no GPU");
  });
});

describe("Evaluation", () => {
  it("always shows the proxy note, even before any evaluation exists", async () => {
    renderWith(<Evaluation />, { "/api/evaluations/latest": () => json({ error: "not_found", reason: "no completed evaluation yet" }, 404) });
    expect(await screen.findByText(/No evaluation has finished yet\./)).toBeTruthy();
    expect(screen.getByTestId("proxy-note-always").textContent).toMatch(/stand-in definition of a release blocker/);
  });

  it("shows an unevaluated gate as not evaluated, never as passed, and G-13 with its proxy note", async () => {
    renderWith(<Evaluation />, {
      "/api/evaluations/latest": () =>
        json({
          evaluation_id: "ev1",
          gates: [
            { gate_id: "G-5", status: "unevaluated", summary: "needs real CI" },
            { gate_id: "G-13", status: "passed", pooled: { recall: 0.6, precision: 0.5 }, per_close: [{ close: "v1.5.0", recall: 0.7, precision: 0.5 }] },
          ],
          ...envelope,
        }),
    });
    const g5 = await screen.findByTestId("gate-G-5");
    expect(g5.textContent).toContain("Not measured");
    expect(g5.textContent).not.toContain("Passed");
    expect(screen.getByTestId("g13-proxy").textContent).toMatch(/stand-in definition/);
    expect(screen.getByTestId("gate-G-13").textContent).toContain("60%");
  });

  it("puts the forecast note beside each release, says not measurable for empty releases, and names one checker", async () => {
    renderWith(<Evaluation />, {
      "/api/evaluations/latest": () =>
        json({
          evaluation_id: "ev2",
          gates: [
            { gate_id: "G-3", status: "passed", checked_by: "one person", summary: "Checked by one person" },
            {
              gate_id: "G-13", status: "failed", checked_by: "one person", summary: "not measurable: v1.6.0; Checked by one person",
              pooled: { matched: 3, blockers: 5, named_blockers: 2, predictions: 4, recall: 0.6, precision: 0.5 },
              per_close: [
                { close: "v1.5.0", matched: 3, blockers: 5, named_blockers: 2, predictions: 4, recall: 0.6, precision: 0.5 },
                { close: "v1.6.0", matched: 0, blockers: 0, named_blockers: 0, predictions: 3, recall: null, precision: 0 },
              ],
            },
          ],
          ...envelope,
        }),
    });
    const close = await screen.findByTestId("g13-close-v1.5.0");
    expect(close.textContent).toContain("recall 3 of 5 (60%)");
    expect(close.textContent).toContain(
      "Counts only blockers already open when the forecast was made; uses a substitute for release blockers because KubeVirt does not record them.",
    );
    expect(screen.getByTestId("g13-close-v1.6.0").textContent).toContain("recall not measurable");
    expect(screen.getByTestId("g13-close-v1.6.0").textContent).toContain("Counts only blockers already open");
    expect(screen.getByTestId("g13-pooled").textContent).toContain("precision 2 of 4 (50%)");
    expect(screen.getByTestId("checked-G-13").textContent).toBe("Checked by one person");
    expect(screen.getByTestId("checked-G-3").textContent).toBe("Checked by one person");
    expect(screen.getByTestId("gate-G-13").textContent?.match(/Checked by one person/g)?.length).toBe(1);
  });
});

describe("Automatic runs", () => {
  const schedules = () =>
    json({
      ...envelope,
      schedules: [
        {
          schedule_id: "kubevirt-nightly", kind: "nightly", title: "Nightly sweep of new failures", state: "paused",
          reason: "Offline mode: automatic runs wait until the server is connected", last_run: "2026-11-14T02:00:00Z",
          last_event_id: "18234", next_run: null, interval_seconds: 86400, spends_model_budget: true, external: true,
        },
        { schedule_id: "release-watch", kind: "release", title: "release-watch", state: "enabled", interval_seconds: 300, spends_model_budget: false },
      ],
    });

  it("shows plain titles, interval, spend, state reason and last event, with controls for the engineering manager", async () => {
    renderWith(<Schedules />, { "/api/schedules": schedules }, new IdentityStore(headerIdentity, { role: "em" }));
    const row = await screen.findByTestId("schedule-kubevirt-nightly");
    expect(row.textContent).toContain("Nightly sweep of new failures");
    expect(row.textContent).toContain("Every day");
    expect(row.textContent).toContain("spends model budget");
    expect(row.textContent).toContain("reads outside data");
    expect(row.textContent).toContain("Offline mode");
    expect(row.textContent).toContain("last event 18234");
    expect(row.textContent).toContain("Turn on");
    expect(row.textContent).not.toContain("kubevirt-nightly");
    const other = screen.getByTestId("schedule-release-watch");
    expect(other.textContent).toContain("New release");
    expect(other.textContent).toContain("Every 5 minutes");
    expect(other.textContent).toContain("Pause");
  });

  it("is read-only for roles that cannot control automatic runs", async () => {
    renderWith(<Schedules />, { "/api/schedules": schedules });
    expect(await screen.findByTestId("schedules-read-only")).toBeTruthy();
    expect(screen.queryByText("Run once")).toBeNull();
  });
});

describe("Approvals", () => {
  it("shows destination, generation, security validity, change hash and expiry", async () => {
    renderWith(<Approvals />, {
      "/api/approvals": () =>
        json({
          approvals: [
            {
              approval_id: "ap1", task_id: "t1", kind: "B", state: "pending",
              destination_repository: "example/target", base_ref: "main", publication_ref: "atlas/a1", base_commit: "abc123",
              verification_generation: 2, security_decision_id: "sd1", security_valid_until: "2026-11-15T00:00:00Z",
              diff_hash: "sha256:feed", diff: "--- a/x\n+++ b/x\n", expires_at: "2099-01-01T00:00:00Z",
            },
          ],
          ...envelope,
        }),
    });
    const card = await screen.findByTestId("approval");
    expect(card.textContent).toContain("example/target");
    expect(card.textContent).toContain("2");
    expect(card.textContent).toContain("sd1");
    expect(screen.getByTestId("diff-hash").textContent).toBe("sha256:feed");
    expect(card.textContent).toMatch(/h \d+ min left/);
    expect(card.textContent).toContain("+++ b/x");
  });
});

describe("Run now", () => {
  it("shows the server's reason and disables an unavailable trigger", async () => {
    renderWith(<RunNow />, {
      "/api/triggers/catalog": () =>
        json({
          manual: [{ trigger_id: "nightly", title: "Run the nightly sweep", method: "POST", path: "/api/triggers/nightly", allowed_roles: ["em"], parameters: [], availability: "disabled", reason: "offline" }],
          automatic: [],
          ...envelope,
        }),
    });
    expect((await screen.findByTestId("unavailable-reason")).textContent).toMatch(/offline/i);
    expect((screen.getByRole("button", { name: "Run now" }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("retries keep the same Idempotency-Key", () => {
  function Probe() {
    const a = useAction<{ x: number }, unknown>("/api/feedback");
    return (
      <>
        <button onClick={() => void a.run({ x: 1 })}>send</button>
        <ActionResult state={a.state} retry={() => void a.retrySame()} />
      </>
    );
  }

  it("a lost response offers 'Check result', which resends with the same key and payload", async () => {
    let n = 0;
    const { calls } = renderWith(<Probe />, {
      "POST /api/feedback": () => {
        if (n++ === 0) throw new TypeError("network lost");
        return json({ ok: true });
      },
    });
    fireEvent.click(screen.getByText("send"));
    fireEvent.click(await screen.findByText("Check result"));
    await screen.findByText("Done.");
    const posts = calls.filter((c) => c.method === "POST");
    expect(posts).toHaveLength(2);
    expect(posts[0]?.headers["idempotency-key"]).toBeTruthy();
    expect(posts[1]?.headers["idempotency-key"]).toBe(posts[0]?.headers["idempotency-key"]);
    expect(posts[1]?.body).toEqual(posts[0]?.body);
  });

  it("a new action uses a new key", async () => {
    const { calls } = renderWith(<Probe />, { "POST /api/feedback": () => json({ ok: true }) });
    fireEvent.click(screen.getByText("send"));
    await screen.findByText("Done.");
    fireEvent.click(screen.getByText("send"));
    await waitFor(() => expect(calls.filter((c) => c.method === "POST")).toHaveLength(2));
    const posts = calls.filter((c) => c.method === "POST");
    expect(posts[1]?.headers["idempotency-key"]).not.toBe(posts[0]?.headers["idempotency-key"]);
  });
});

describe("role switch", () => {
  it("drops data shown for the old role and ignores its late responses", async () => {
    window.history.pushState(null, "", "/queue");
    let answerSecurity!: (r: Response) => void;
    const { identity } = renderWith(
      <App />,
      {
        "/api/queue": (req) =>
          req.headers["x-atlas-role"] === "security"
            ? new Promise<Response>((r) => (answerSecurity = r))
            : json({ entries: [{ entry_id: "e1", trigger: "review", reason: "visible to pm" }], ...envelope }),
      },
      new IdentityStore(headerIdentity, { role: "security" }),
    );
    await waitFor(() => expect(answerSecurity).toBeTypeOf("function"));
    act(() => identity.transition({ principal: { role: "pm" } }));
    answerSecurity(json({ entries: [{ entry_id: "s1", trigger: "security", reason: "restricted finding" }], ...envelope }));
    expect(await screen.findByText(/visible to pm/i)).toBeTruthy();
    expect(screen.queryByText(/restricted finding/i)).toBeNull();
  });
});

describe("agents", () => {
  it("names Scout and Sentinel as agents in the header", async () => {
    renderWith(<App />, {});
    expect(screen.getAllByText(/Scout/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Sentinel/).length).toBeGreaterThan(0);
    expect(screen.getByText(/Demo identity \(not sign-in\)/)).toBeTruthy();
  });
});
