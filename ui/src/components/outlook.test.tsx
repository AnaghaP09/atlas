import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { App } from "../App";
import { AppProvider, createQueryClient } from "../app/context";
import { IdentityStore, headerIdentity, type Role } from "../identity/identity";
import { RouterProvider } from "../lib/router";
import { fakeFetch, frame, json, sseResponse, type Handler } from "../test/fakes";
import type { Outlook, Q1, Q2 } from "../lib/outlook";

const envelope = { data_mode: "REAL", environment: "normal", server_time: "2026-11-15T00:00:00Z" };
const MAIN: Role[] = ["pm", "em", "architect"];
const BETA: Role[] = ["security", "oncall", "audit"];
const PAGES = ["/", "/dashboard", "/ship-readiness", "/runs", "/notifications", "/latest-report/forecast", "/graph", "/help/index"];

function nav(role: Role) {
  const main = MAIN.includes(role);
  return {
    ...envelope, role, role_title: role, home: `${role}_home`, project_id: "kubevirt",
    groups: [{ title: "Overview", pages: [
      { page_id: "home", path: "/", title: "Home", for: "" },
      { page_id: "dashboard", path: "/dashboard", title: "Live dashboard", for: "" },
      ...(main ? [{ page_id: "ship_readiness", path: "/ship-readiness", title: "Ship readiness", for: "" }] : []),
    ] }],
    other_pages: [], outlook: main ? ["q1", "q2"] : [], beta: !main,
  };
}

const READY_Q1: Q1 = {
  state: "ready", run_action: { allowed: true, link: "/run-now?snapshot=s1" }, dataset: { title: "KubeVirt at release v1.9.0", snapshot_id: "s1", as_of: "2026-04-08T00:00:00Z" },
  run_id: "r1", run_link: "/runs/r1", report_link: "/reports/r1:forecast", report_access: "open", basis: "predicted", date_range: "illustrative",
  total: 8, split: { developers: 5, coding_agents: 3, decide_first: 0, not_allocated: 0 },
  items: [{ id: "item-1", issue_id: "16936", title: "flaky VSOCK test", component: "compute", owner: "coding_agents", reason: "One flaky test, one component", evidence_link: "/evidence/x?run=r1" }],
};

function q2(status: Q2["status"], extra: Partial<Q2> = {}): Q2 {
  const words = { ON_TRACK: "On track", AT_RISK: "At risk", BLOCKED: "Blocked", NOT_MEASURED: "Not measured" } as const;
  return {
    status, status_word: words[status], reasons: [{ rule: "r", status, text: `${words[status]} because` }], missing_inputs: [],
    inputs: { failing_ci: null, security_gate: null, investigations: null, critical_issues: null },
    blockers: [], blocker_count: 0, blocking_count: 0, affected_services: [], service_count: 0,
    thresholds: { failing_ci_at_risk: 3, critical_issues_at_risk: 1 }, ...extra,
  };
}

const AT_RISK = q2("AT_RISK", {
  blockers: [
    { kind: "failing_ci", title: "periodic-e2e-sig-compute", component: "compute", effect: "risk", link: "/dashboard" },
    { kind: "failing_ci", title: "periodic-e2e-sig-storage", component: "storage", effect: "risk", link: "/dashboard" },
    { kind: "critical_issue", title: "#1 boom", component: null, effect: "risk", link: "/dashboard" },
  ],
  blocker_count: 3, service_count: 2,
  affected_services: [{ component: "compute", blockers: 1, blocks: 0, kinds: { failing_ci: 1 }, link: "/graph" }, { component: "storage", blockers: 1, blocks: 0, kinds: { failing_ci: 1 }, link: "/graph" }],
});

function outlook(q1: Q1, second: Q2, version = 1): Outlook {
  return { project_id: "kubevirt", outlook_version: version, computed_at: "2026-11-15T10:00:00Z", sections: ["q1", "q2"], q1, q2: second };
}

function renderApp(role: Role, path: string, routes: Record<string, Handler> = {}) {
  window.history.pushState(null, "", path);
  const identity = new IdentityStore(headerIdentity, { role }, "kubevirt");
  const f = fakeFetch({
    "/api/health": () => json({ ...envelope, status: "ok", mode: "connected", identity: "header", components: {}, staged: [], unevaluated: [] }),
    "/api/navigation": () => json(nav(role)),
    "/api/projects/kubevirt/metrics": () => json({ ...envelope, project_id: "kubevirt", metrics_version: 1, computed_at: "2026-11-15T10:00:00Z", dashboard_counts: {} }),
    "/api/projects/kubevirt/stream": () => sseResponse([]),
    "/api/projects/kubevirt/outlook": () => json({ ...envelope, ...outlook(READY_Q1, AT_RISK) }),
    "/api/runs": () => json({ ...envelope, runs: [] }),
    ...routes,
  });
  render(
    <AppProvider identity={identity} queryClient={createQueryClient()} fetchImpl={f.impl}>
      <RouterProvider>
        <App />
      </RouterProvider>
    </AppProvider>,
  );
  return f;
}

afterEach(() => window.history.pushState(null, "", "/"));

describe("release outlook strip", () => {
  for (const role of MAIN) {
    it.each(PAGES)(`shows both questions for ${role} on %s`, async (path) => {
      renderApp(role, path);
      const strip = await screen.findByTestId("outlook-strip");
      await waitFor(() => expect(within(strip).getByTestId("outlook-q1").textContent).toContain("Next release inherits 8 items"));
      expect(within(strip).getByTestId("outlook-developers").textContent).toContain("5");
      expect(within(strip).getByTestId("outlook-coding_agents").textContent).toContain("3");
      expect(within(strip).getByTestId("outlook-decide_first").textContent).toContain("0");
      expect(within(strip).getByTestId("outlook-status").textContent).toContain("Ship status: At risk");
      expect(within(strip).getByTestId("outlook-status").className).toContain("pill-warn");
      expect(within(strip).getByTestId("outlook-blockers").textContent).toContain("3 blockers");
      expect(within(strip).getByTestId("outlook-services").textContent).toContain("2 services");
      expect(strip.textContent).toContain("Predicted");
    });
  }

  it.each(BETA)("is absent for %s, which shows a Beta pill instead", async (role) => {
    const f = renderApp(role, "/");
    expect(await screen.findByTestId("beta-pill")).toBeTruthy();
    expect(screen.queryByTestId("outlook-strip")).toBeNull();
    expect(f.calls.some((c) => c.url.includes("/outlook"))).toBe(false);
  });

  it("has no Beta pill for the main roles", async () => {
    renderApp("pm", "/");
    await screen.findByTestId("outlook-strip");
    expect(screen.queryByTestId("beta-pill")).toBeNull();
  });

  it("updates live when the project stream says the outlook changed", async () => {
    let n = 0;
    renderApp("em", "/dashboard", {
      "/api/projects/kubevirt/outlook": () => {
        n += 1;
        return json({ ...envelope, ...outlook(READY_Q1, n === 1 ? AT_RISK : q2("BLOCKED", { blocker_count: 4, blocking_count: 1, service_count: 3 }), n) });
      },
      "/api/projects/kubevirt/stream": () => sseResponse([frame("project.outlook_changed", "o2", { project_id: "kubevirt", outlook_version: 2, changed: ["q2"] })]),
    });
    await waitFor(() => expect(screen.getByTestId("outlook-status").textContent).toContain("Blocked"));
    expect(screen.getByTestId("outlook-status").className).toContain("pill-critical");
    expect(screen.getByTestId("outlook-blockers").textContent).toContain("4 blockers");
    expect(n).toBeGreaterThanOrEqual(2);
  });

  it("says Not measured and how many inputs are missing, with grey colour", async () => {
    renderApp("architect", "/", {
      "/api/projects/kubevirt/outlook": () => json({ ...envelope, ...outlook(READY_Q1, q2("NOT_MEASURED", { missing_inputs: [{ input: "failing_ci", reason: "x" }, { input: "security_gate", reason: "y" }] })) }),
    });
    const status = await screen.findByTestId("outlook-status");
    expect(status.textContent).toContain("Not measured");
    expect(status.className).toContain("pill-neutral");
    expect(screen.getByTestId("outlook-q2").textContent).toContain("2 inputs missing");
  });

  it("says No release close yet with a Run button for a role that may start one", async () => {
    const none: Q1 = { ...READY_Q1, state: "none", message: "No release close yet", total: 0, items: [], dataset: null, split: { developers: 0, coding_agents: 0, decide_first: 0, not_allocated: 0 } };
    renderApp("pm", "/dashboard", { "/api/projects/kubevirt/outlook": () => json({ ...envelope, ...outlook(none, AT_RISK) }) });
    const q1 = await screen.findByTestId("outlook-q1");
    expect(q1.textContent).toContain("No release close yet");
    expect(screen.getByTestId("outlook-run").getAttribute("href")).toBe("/run-now?snapshot=s1");
  });

  it("has no Run button when the role may not start a release close", async () => {
    const none: Q1 = { ...READY_Q1, state: "none", total: 0, items: [], dataset: null, run_action: { allowed: false, reason: "role", link: "/run-now" }, split: { developers: 0, coding_agents: 0, decide_first: 0, not_allocated: 0 } };
    renderApp("architect", "/dashboard", { "/api/projects/kubevirt/outlook": () => json({ ...envelope, ...outlook(none, AT_RISK) }) });
    expect((await screen.findByTestId("outlook-q1")).textContent).toContain("No release close yet");
    expect(screen.queryByTestId("outlook-run")).toBeNull();
  });

  it("opens a drawer with the items for one owner group", async () => {
    renderApp("pm", "/dashboard");
    fireEvent.click(await screen.findByTestId("outlook-coding_agents"));
    await waitFor(() => expect(screen.getByText("flaky VSOCK test")).toBeTruthy());
    expect(screen.getByText(/One flaky test, one component/)).toBeTruthy();
  });
});

describe("ship readiness and home", () => {
  it("shows the status, the rule that fired, blockers and services", async () => {
    renderApp("em", "/ship-readiness");
    expect((await screen.findByTestId("ship-status")).textContent).toContain("At risk");
    expect(screen.getByTestId("ship-reasons").textContent).toContain("At risk because");
    expect(screen.getByTestId("blockers-table").textContent).toContain("periodic-e2e-sig-compute");
    expect(screen.getByTestId("services-table").textContent).toContain("storage");
  });

  it("labels allocation items as predicted, with the data date, split, reason and owner team", async () => {
    const items = [
      { id: "item-1", issue_id: "1", title: "flaky", component: "compute", allocation: "coding_agent", allocation_reason: "One flaky test, one component", owner_team: "sig/compute", evidence: ["e1"] },
      { id: "item-2", issue_id: "2", title: "s390x", component: "compute", allocation: "developer", allocation_reason: "Needs special hardware", evidence: [] },
    ];
    renderApp("em", "/reports/r1:allocation", {
      "/api/reports/r1%3Aallocation": () => json({ ...envelope, report_id: "r1:allocation", run_id: "r1", report_type: "allocation", authoritative_generation: 1, generation: 1, superseded_by: null, report_version: 1, correction_version: 0, corrections: [], markdown: "", withheld: 0, report: { items, claims: [] } }),
      "/api/runs/r1": () => json({ ...envelope, run_id: "r1", snapshot_id: "s1", reports: [] }),
      "/api/snapshots/s1": () => json({ ...envelope, snapshot: { snapshot_id: "s1", cutoff: "2026-04-08T00:00:00Z" } }),
    });
    const header = await screen.findByTestId("predicted-header");
    expect(header.textContent).toContain("Predicted");
    await waitFor(() => expect(header.textContent).toContain("Data as of 8 Apr 2026"));
    expect(screen.getByTestId("allocation-split").textContent).toMatch(/Devs 1 · Agents 1 · Decide first 0/);
    expect(screen.getAllByTestId("allocation-reason").map((e) => e.textContent)).toEqual(["One flaky test, one component", "Needs special hardware"]);
    expect(screen.getByText("Owner team sig/compute")).toBeTruthy();
  });

  it.each(MAIN)("leads the %s home page with both questions", async (role) => {
    renderApp(role, "/");
    expect((await screen.findByTestId("home-q1")).textContent).toContain("8 items");
    expect(screen.getByTestId("home-q2").textContent).toContain("3 blockers");
  });
});
