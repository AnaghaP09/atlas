import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AppProvider, createQueryClient } from "../app/context";
import { IdentityStore, headerIdentity } from "../identity/identity";
import { RouterProvider } from "../lib/router";
import { fakeFetch, json, type Handler } from "../test/fakes";
import { AddProject, projectBody, type Proposal } from "./AddProject";
import { releaseOf } from "./RunNow";

const envelope = { data_mode: "REAL", environment: "normal", server_time: "2026-11-15T00:00:00Z" };

const proposal: Proposal = {
  repository: "example-org/widget",
  api_requests: 9,
  block: "",
  project: {},
  facts: { releases: [], open_issues_and_pull_requests: 3, size_kb: 100 },
  fields: [
    { key: "project_id", label: "Project id", value: "widget", status: "detected", evidence: "from the repository name" },
    { key: "name", label: "Name", value: "Widget", status: "detected", evidence: "from the repository name" },
    { key: "ci", label: "CI", value: "github_actions", status: "detected", evidence: "2 GitHub Actions workflows" },
    { key: "release_tag_pattern", label: "Release tags", value: "^v[0-9]+$", status: "detected", evidence: "tags" },
    { key: "labels.bug", label: "'bug' label", value: "bug", status: "detected", evidence: "label 'bug'" },
    { key: "labels.flaky", label: "'flaky' label", value: "", status: "needs you", evidence: "no label matches" },
    { key: "labels.critical", label: "'critical' label", value: "", status: "needs you", evidence: "no label matches" },
    { key: "labels.regression", label: "'regression' label", value: "", status: "needs you", evidence: "no label matches" },
    { key: "issue_query", label: "Issue query", value: 'is:issue label:"bug"', status: "detected", evidence: "x" },
    { key: "security_inventory", label: "Dependency list", value: "uv.lock", status: "detected", evidence: "x" },
    { key: "go_module", label: "Go module", value: "", status: "detected", evidence: "no go.mod" },
    { key: "publish_targets", label: "Publish targets", value: [], status: "needs you", evidence: "never set here" },
  ],
};

function renderAdd(routes: Record<string, Handler>) {
  const identity = new IdentityStore(headerIdentity, { role: "audit" });
  const f = fakeFetch({
    "/api/projects/add/defaults": () => json({ ...envelope, repository: "https://github.com/example-org/widget", connected: true, github_token: true }),
    ...routes,
  });
  render(
    <AppProvider identity={identity} queryClient={createQueryClient()} fetchImpl={f.impl}>
      <RouterProvider><AddProject /></RouterProvider>
    </AppProvider>,
  );
  return f.calls;
}

describe("Add project", () => {
  it("builds the confirmed project from the detected values and the person's changes", () => {
    const body = projectBody(proposal, { "labels.bug": "", name: "Widget Lib" });
    expect(body).toMatchObject({ id: "widget", name: "Widget Lib", repository: "example-org/widget", ci: "github_actions",
      labels: { bug: "", flaky: "", critical: "", regression: "" }, sources: {} });
    expect(body).not.toHaveProperty("publish_targets");
    expect(body).not.toHaveProperty("export");
  });

  it("pre-fills the link, shows detected and needs-you chips, and sends one confirmed block", async () => {
    const calls = renderAdd({
      "POST /api/projects/detect": () => json({ ...envelope, ...proposal }),
      "POST /api/projects": () => json({ ...envelope, id: "widget", name: "Widget", repository: "example-org/widget", block: "", live: { polling: true } }, 201),
    });
    const input = (await screen.findByLabelText("Public GitHub link")) as HTMLInputElement;
    await waitFor(() => expect(input.value).toBe("https://github.com/example-org/widget"));
    fireEvent.click(screen.getByRole("button", { name: /Detect/ }));
    const table = await screen.findByTestId("detected");
    expect(table.textContent).toContain("detected");
    expect(table.textContent).toContain("needs you");
    expect(table.textContent).toContain("never set here");
    fireEvent.click(screen.getByRole("button", { name: /Add project/ }));
    expect(await screen.findByText(/Live polling started/)).toBeTruthy();
    const post = calls.find((c) => c.method === "POST" && c.url.startsWith("/api/projects?") || (c.method === "POST" && c.url === "/api/projects"));
    expect(post?.headers["idempotency-key"]).toBeTruthy();
    expect((post?.body as { project: Record<string, unknown> }).project.id).toBe("widget");
    expect(screen.getByRole("button", { name: /Switch to Widget/ })).toBeTruthy();
  });
});

describe("release of a data set", () => {
  it.each([
    ["release-close-v1.9.0", "v1.9.0"],
    ["widget.release-close-3.1.3", "3.1.3"],
    ["case-g04", ""],
  ])("%s -> %s", (id, release) => {
    expect(releaseOf(id)).toBe(release);
  });
});
