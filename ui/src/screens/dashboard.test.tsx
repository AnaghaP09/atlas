import { act, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AppProvider, createQueryClient } from "../app/context";
import { IdentityStore, headerIdentity } from "../identity/identity";
import { RouterProvider } from "../lib/router";
import { fakeFetch, frame, json, sseResponse, type Handler } from "../test/fakes";
import { Dashboard, mergePanels, type DashboardData } from "./Dashboard";

const envelope = { environment: "normal", server_time: "2026-11-14T00:00:00Z" };

function board(version: number, items: Record<string, string[]>): DashboardData {
  const titles: Record<string, string> = { open_issues: "Open issues", failing_ci: "Failing CI jobs" };
  return {
    project_id: "kubevirt",
    dashboard_version: version,
    last_poll: "2026-11-14T10:00:00Z",
    interval_seconds: 60,
    data_mode: "REAL",
    panels: Object.keys(titles).map((id) => ({
      panel_id: id,
      title: titles[id] ?? id,
      status: "live",
      items: (items[id] ?? []).map((t) => ({ title: t })),
      updated_at: null,
    })),
  };
}

function renderDashboard(routes: Record<string, Handler>, identity = new IdentityStore(headerIdentity, { role: "pm" }, "kubevirt")) {
  const f = fakeFetch(routes);
  render(
    <AppProvider identity={identity} queryClient={createQueryClient()} fetchImpl={f.impl}>
      <RouterProvider>
        <Dashboard />
      </RouterProvider>
    </AppProvider>,
  );
  return { calls: f.calls, identity };
}

describe("mergePanels", () => {
  it("replaces only the panels that changed", () => {
    const shown = board(1, { open_issues: ["old issue"], failing_ci: ["old job"] });
    const fresh = board(2, { open_issues: ["new issue"], failing_ci: ["new job"] });
    const merged = mergePanels(shown, fresh, ["open_issues"]);
    expect(merged.dashboard_version).toBe(2);
    expect(merged.panels.find((p) => p.panel_id === "open_issues")?.items[0]?.title).toBe("new issue");
    expect(merged.panels.find((p) => p.panel_id === "failing_ci")?.items[0]?.title).toBe("old job");
    expect(mergePanels(shown, fresh, null)).toBe(fresh);
  });
});

describe("Live dashboard", () => {
  it("shows the poll interval and last poll, and applies project events from the stream", async () => {
    let dashboardCalls = 0;
    const { calls } = renderDashboard({
      "/api/projects/kubevirt/dashboard": () => {
        dashboardCalls += 1;
        return json({ ...envelope, ...board(dashboardCalls, { open_issues: [dashboardCalls === 1 ? "first" : "second"] }) });
      },
      "/api/projects/kubevirt/activity": () => json({ ...envelope, activity: [] }),
      "/api/projects/kubevirt/stream": () =>
        sseResponse([
          frame("project.activity", "e1", {
            project_id: "kubevirt", kind: "issue", title: "New flaky test", record_id: "rec-1", content_version_id: "cv1",
          }),
          frame("project.dashboard_changed", "e2", { project_id: "kubevirt", dashboard_version: 2, changed_panels: ["open_issues"] }),
        ]),
    });
    expect((await screen.findByTestId("poll-line")).textContent).toMatch(/Updated every 60 s, last poll/);
    await waitFor(() => expect(screen.getByTestId("activity").textContent).toContain("New flaky test"));
    await waitFor(() => expect(screen.getByText("second")).toBeTruthy());
    const link = screen.getByText("New flaky test").closest("a");
    expect(link?.getAttribute("href")).toBe("/activity-evidence/rec-1?project=kubevirt&version=cv1");
    expect(calls.every((c) => !c.url.includes("argo-cd"))).toBe(true);
    expect(calls.some((c) => c.url.startsWith("/api/projects/kubevirt/stream"))).toBe(true);
  });

  it("shows real open counts, a trend from stored history and the CI job listing", async () => {
    const data: DashboardData = {
      ...board(3, { open_issues: ["recent issue"] }),
      history: [
        { at: "2026-11-14T09:00:00Z", open_issues: 140, failing_ci: 3 },
        { at: "2026-11-14T09:05:00Z", open_issues: 142, failing_ci: 4 },
      ],
    };
    data.panels = data.panels.map((p) =>
      p.panel_id === "open_issues"
        ? { ...p, total: 142 }
        : {
            ...p,
            total: 4,
            items: [
              { note: "4 of 30 periodic jobs that ran in the last 7 days are failing" },
              { job: "periodic-e2e-network", result: "FAILURE", build_id: "77", finished_at: "2026-11-14T08:00:00Z", kind: "periodic" },
            ],
          },
    );
    renderDashboard({
      "/api/projects/kubevirt/dashboard": () => json({ ...envelope, ...data }),
      "/api/projects/kubevirt/activity": () => json({ ...envelope, activity: [] }),
      "/api/projects/kubevirt/stream": () => sseResponse([]),
    });
    await waitFor(() => expect(screen.getByTestId("kpi-open").textContent).toContain("142"));
    expect(screen.getByTestId("issues-shown").textContent).toBe("1 most recently changed of 142");
    expect(screen.getByTestId("kpi-ci").textContent).toContain("4");
    expect(screen.getByTestId("kpi-ci").textContent).toContain("Failing CI jobs");
    expect(screen.getByTestId("ci-jobs").textContent).toContain("periodic-e2e-network");
    expect(screen.getByTestId("ci-jobs").textContent).toContain("Failure");
    expect(screen.getByTestId("ci-coverage").textContent).toContain("4 of 30 periodic jobs");
    expect(screen.getByTestId("trend").textContent).toContain("last 2 polls");
  });

  it("is honest before the first poll and when CI is not polled", async () => {
    const data: DashboardData = { ...board(1, {}), last_poll: null, history: [] };
    data.panels = data.panels.map((p) =>
      p.panel_id === "failing_ci" ? { ...p, items: [{ note: "CI runs for this project (not set) are not polled in this build" }], total: 0 }
        : { ...p, status: "not_connected" },
    );
    renderDashboard({
      "/api/projects/kubevirt/dashboard": () => json({ ...envelope, ...data }),
      "/api/projects/kubevirt/activity": () => json({ ...envelope, activity: [] }),
      "/api/projects/kubevirt/stream": () => sseResponse([]),
    });
    await waitFor(() => expect(screen.getByTestId("kpi-open").textContent).toContain("Not yet"));
    expect(screen.getByTestId("kpi-ci").textContent).toContain("Not polled");
    expect(screen.getByTestId("trend-empty").textContent).toContain("after two finished polls");
    expect(screen.getByText(/They appear after the first poll/)).toBeTruthy();
  });

  it("drops the old project's dashboard on a project switch and asks only for the new one", async () => {
    const identity = new IdentityStore(headerIdentity, { role: "pm" }, "kubevirt");
    const { calls } = renderDashboard(
      {
        "/api/projects/kubevirt/dashboard": () => json({ ...envelope, ...board(1, { open_issues: ["kubevirt item"] }) }),
        "/api/projects/kubevirt/activity": () => json({ ...envelope, activity: [] }),
        "/api/projects/kubevirt/stream": () => sseResponse([]),
        "/api/projects/argo-cd/dashboard": () => json({ ...envelope, ...board(1, { open_issues: ["argo item"] }), project_id: "argo-cd" }),
        "/api/projects/argo-cd/activity": () => json({ ...envelope, activity: [] }),
        "/api/projects/argo-cd/stream": () => sseResponse([]),
      },
      identity,
    );
    expect(await screen.findByText("kubevirt item")).toBeTruthy();
    const before = calls.length;
    act(() => identity.transition({ projectId: "argo-cd", principal: { role: "pm" } }));
    // The component remounts in the app; here the same component re-reads with the new project.
    await waitFor(() => expect(calls.slice(before).some((c) => c.url.startsWith("/api/projects/argo-cd/dashboard"))).toBe(true));
    expect(calls.slice(before).every((c) => !c.url.startsWith("/api/projects/kubevirt/"))).toBe(true);
    expect(calls.slice(before).every((c) => c.headers["x-atlas-role"] === "pm")).toBe(true);
  });
});
