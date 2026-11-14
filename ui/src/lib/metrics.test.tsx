import { act, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AppProvider, createQueryClient } from "../app/context";
import { IdentityStore, headerIdentity } from "../identity/identity";
import { fakeFetch, frame, json, sseResponse, type Handler } from "../test/fakes";
import { RouterProvider } from "./router";
import { LiveNumber, MetricsSync, useMetrics } from "./metrics";

const envelope = { environment: "normal", server_time: "2026-11-14T00:00:00Z", data_mode: "REAL" };

function metrics(v: number, unread: number) {
  return json({ ...envelope, project_id: "kubevirt", metrics_version: v, computed_at: "2026-11-14T10:00:00Z", unread_notifications: unread, dashboard_counts: {} });
}

function Probe() {
  const m = useMetrics();
  return <span data-testid="unread"><LiveNumber value={m.data?.unread_notifications} /></span>;
}

function renderStore(routes: Record<string, Handler>, identity = new IdentityStore(headerIdentity, { role: "pm" }, "kubevirt")) {
  const f = fakeFetch({ "/api/health": () => json({ ...envelope, status: "ok" }), ...routes });
  render(
    <AppProvider identity={identity} queryClient={createQueryClient()} fetchImpl={f.impl}>
      <RouterProvider>
        <MetricsSync />
        <Probe />
      </RouterProvider>
    </AppProvider>,
  );
  return { ...f, identity };
}

describe("metrics store", () => {
  it("refetches when the project stream says the numbers changed", async () => {
    let n = 0;
    renderStore({
      "/api/projects/kubevirt/metrics": () => metrics(++n, n === 1 ? 1 : 3),
      "/api/projects/kubevirt/stream": () => sseResponse([frame("project.metrics_changed", "m1", { project_id: "kubevirt", metrics_version: 2, changed: ["unread_notifications"] })]),
    });
    await waitFor(() => expect(screen.getByTestId("unread").textContent).toContain("3"));
    expect(n).toBeGreaterThanOrEqual(2);
  });

  it("asks again for a new role after a switch", async () => {
    const { calls, identity } = renderStore({
      "/api/projects/kubevirt/metrics": (req) => metrics(1, req.headers["x-atlas-role"] === "audit" ? 7 : 2),
      "/api/projects/kubevirt/stream": () => sseResponse([]),
    });
    await waitFor(() => expect(screen.getByTestId("unread").textContent).toContain("2"));
    act(() => identity.transition({ principal: { role: "audit" }, projectId: "kubevirt" }));
    await waitFor(() => expect(screen.getByTestId("unread").textContent).toContain("7"));
    expect(calls.some((c) => c.url.includes("/metrics") && c.headers["x-atlas-role"] === "audit")).toBe(true);
  });
});
