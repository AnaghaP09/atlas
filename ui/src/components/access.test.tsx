import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AppProvider, createQueryClient } from "../app/context";
import { IdentityStore, headerIdentity } from "../identity/identity";
import { RouterProvider } from "../lib/router";
import { fakeFetch, json } from "../test/fakes";
import { LockedChip, ReadOnlyBanner } from "./access";

const expires = new Date(Date.now() + 24 * 3600 * 1000).toISOString();

describe("read-only banner", () => {
  it("shows the exception line only on automatic grants for another role", () => {
    render(<ReadOnlyBanner access={{ mode: "read_only", expires_at: expires, automatic: true, owning_role: "security", owning_role_title: "security engineer" }} myRole="pm" />);
    expect(screen.getByTestId("read-only").textContent).toMatch(/Read-only until/);
    expect(screen.getByTestId("exception-note").textContent).toBe("Meant for the security engineer only. Opened as an exception for this demo.");
  });

  it("no exception line for a grant a person approved", () => {
    render(<ReadOnlyBanner access={{ mode: "read_only", expires_at: expires, automatic: false, owning_role: "security" }} myRole="pm" />);
    expect(screen.queryByTestId("exception-note")).toBeNull();
  });

  it("no exception line for the owning role itself", () => {
    render(<ReadOnlyBanner access={{ mode: "read_only", expires_at: expires, automatic: true, owning_role: "pm" }} myRole="pm" />);
    expect(screen.queryByTestId("exception-note")).toBeNull();
  });
});

describe("locked chip", () => {
  function renderChip(status: "granted" | "pending") {
    const f = fakeFetch({
      "POST /api/access-requests": () => json({ request_id: "q1", status, expires_at: status === "granted" ? expires : null }, 201),
    });
    render(
      <AppProvider identity={new IdentityStore(headerIdentity, { role: "pm" })} queryClient={createQueryClient()} fetchImpl={f.impl}>
        <RouterProvider>
          <LockedChip objectType="report" objectId="rep-1" />
        </RouterProvider>
      </AppProvider>,
    );
    return f;
  }

  it("shows the type only and unlocks at once for an ordinary item", async () => {
    const f = renderChip("granted");
    expect(screen.getByText("Report")).toBeTruthy();
    fireEvent.click(screen.getByText("Request access"));
    fireEvent.click(screen.getByText("Send"));
    expect((await screen.findByTestId("access-state")).textContent).toMatch(/Granted until/);
    const post = f.calls.find((c) => c.method === "POST");
    expect(post?.headers["idempotency-key"]).toBeTruthy();
    expect(post?.body).toEqual({ object_type: "report", object_id: "rep-1" });
  });

  it("waits for a decision on a sensitive item", async () => {
    renderChip("pending");
    fireEvent.click(screen.getByText("Request access"));
    fireEvent.click(screen.getByText("Send"));
    expect((await screen.findByTestId("access-state")).textContent).toBe("Requested");
  });
});
