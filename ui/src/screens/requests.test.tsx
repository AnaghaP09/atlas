import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AppProvider, createQueryClient } from "../app/context";
import { IdentityStore, headerIdentity } from "../identity/identity";
import { RouterProvider } from "../lib/router";
import { fakeFetch, json, type Handler } from "../test/fakes";
import { HandoffList, Requests, StoryHome } from "./Requests";

const envelope = { data_mode: "REAL", environment: "normal", server_time: "2026-11-15T00:00:00Z" };
const health = json({ ...envelope, status: "ok", environment: "normal", server: "127.0.0.1:8000", mode: "connected", identity: "header", components: {}, staged: [], unevaluated: [] });

function renderAs(role: "pm" | "em" | "architect", ui: React.ReactNode, routes: Record<string, Handler>) {
  const f = fakeFetch({ "/api/health": () => health.clone(), ...routes });
  render(
    <AppProvider identity={new IdentityStore(headerIdentity, { role })} queryClient={createQueryClient()} fetchImpl={f.impl}>
      <RouterProvider>{ui}</RouterProvider>
    </AppProvider>,
  );
}

const hand = (extra: Record<string, unknown>) => ({
  handoff_id: "hand-1", subject_type: "forecast_item", subject_title: "Flaky IPv6 tests", to_role: "em",
  note: "Please plan it", status: "waiting", created: "2026-11-15T00:00:00Z", decision: null, ...extra,
});

describe("hand-off decision labels", () => {
  it("shows both delegation labels and the waiting label to the product manager", async () => {
    renderAs("pm", <HandoffList />, {
      "/api/handoffs": () => json({ ...envelope, handoffs: [
        hand({ handoff_id: "h1", decision: "coding_agent", status: "accepted" }),
        hand({ handoff_id: "h2", decision: "oncall", status: "accepted" }),
        hand({ handoff_id: "h3" }),
      ] }),
    });
    const labels = (await screen.findAllByTestId("handoff-decision")).map((e) => e.textContent);
    expect(labels).toEqual(["Delegated to coding agent", "Delegated to on-call engineer", "Awaiting decision"]);
    expect(screen.queryByTestId("handoff-decide")).toBeNull();
  });

  it("gives the engineering manager exactly the two choices", async () => {
    renderAs("em", <HandoffList />, { "/api/handoffs": () => json({ ...envelope, handoffs: [hand({})] }) });
    const decide = await screen.findByTestId("handoff-decide");
    expect(decide.textContent).toContain("Awaiting your decision");
    const buttons = [...decide.querySelectorAll("button")].map((b) => b.textContent);
    expect(buttons).toEqual(["Delegated to coding agent", "Delegated to on-call engineer"]);
  });
});

describe("requests", () => {
  const req = { request_id: "req-1", kind: "technical_dependency", title: "Shared cache", description: "Needed first", owner_role: "architect", info_role: "em", status: "new", created: "2026-11-15T00:00:00Z" };

  it("offers New request to the product manager only", async () => {
    renderAs("pm", <Requests />, { "/api/requests": () => json({ ...envelope, requests: [req] }) });
    expect(await screen.findByText("New request")).toBeTruthy();
    expect(await screen.findByText("Technical dependency")).toBeTruthy();
  });

  it("lets the owner answer and marks the other role as for information", async () => {
    renderAs("architect", <Requests />, { "/api/requests": () => json({ ...envelope, requests: [req] }) });
    expect(await screen.findByTestId("answer-request")).toBeTruthy();
    expect(screen.queryByText("New request")).toBeNull();
  });

  it("shows the request tile on the engineering manager's home", async () => {
    renderAs("em", <StoryHome />, {
      "/api/story/home": () => json({ ...envelope, handoffs_waiting: 1, handoffs: [hand({})],
        requests: { new: 1, owned: 0, for_information: 1, items: [{ ...req, for_information: true }] } }),
    });
    expect((await screen.findByTestId("requests-from-product")).textContent).toContain("1");
    expect(screen.getByTestId("handoffs-waiting").textContent).toContain("1");
    expect(screen.getByText(/for your information/)).toBeTruthy();
  });
});
