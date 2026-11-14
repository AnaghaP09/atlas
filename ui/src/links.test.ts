import { describe, expect, it } from "vitest";
import { resolveLink, targets, type Entity } from "./links";
import { pageIdForPath } from "./lib/nav";

const ALL = new Set(["runs", "tasks", "approvals", "containment", "graph", "run_now", "notifications", "queue"]);

describe("link map", () => {
  const entities: Entity[] = ["run", "report", "evidence", "task", "approval", "incident", "snapshot", "component", "notification", "queue"];

  it.each(entities)("%s resolves to a page when the role has it", (entity) => {
    const t = resolveLink(entity, "x1", ALL, { runId: "r1", snapshotId: "s1" });
    expect(t).not.toBeNull();
    expect(t!.to.startsWith("/")).toBe(true);
    expect(t!.to.startsWith("/api")).toBe(false);
  });

  it("hides a link when the role lacks the page", () => {
    expect(resolveLink("task", "t1", new Set(["runs"]))).toBeNull();
    expect(resolveLink("approval", "a1", new Set(["runs"]))).toBeNull();
  });

  it("detail pages any role may open stay linked", () => {
    expect(resolveLink("report", "rep1", new Set())).not.toBeNull();
  });

  it("falls back to the next allowed page", () => {
    expect(resolveLink("snapshot", "s1", new Set(["run_now"]))!.to).toBe("/run-now?snapshot=s1");
    expect(resolveLink("snapshot", "s1", new Set(["graph", "run_now"]))!.to).toBe("/graph?snapshot=s1");
  });

  it("evidence needs its run", () => {
    expect(targets("evidence", "e1")).toEqual([]);
  });

  it("every menu path maps back to its page", () => {
    expect(pageIdForPath("/runs/abc")).toBe("runs");
    expect(pageIdForPath("/latest-report/forecast")).toBe("report_forecast");
    expect(pageIdForPath("/reports/abc")).toBeNull();
  });
});
