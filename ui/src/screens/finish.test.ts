import { describe, expect, it } from "vitest";
import { placeLabels } from "./Graph";
import { fill, missingValues, serverName, waitDone } from "./GuidedDemo";

describe("graph labels", () => {
  it("keeps the most-used label and drops one that would overlap it", () => {
    const spots = placeLabels([
      { id: "a", x: 100, y: 100, r: 6, text: "virt-controller", weight: 9 },
      { id: "b", x: 104, y: 100, r: 6, text: "virt-handler", weight: 3 },
      { id: "c", x: 400, y: 300, r: 6, text: "util", weight: 1 },
    ]);
    const ids = spots.map((s) => s.id);
    expect(ids).toContain("a");
    expect(ids).toContain("c");
    const a = spots.find((s) => s.id === "a")!;
    const b = spots.find((s) => s.id === "b");
    // b either moved below its dot or was left out; it never sits on top of a.
    if (b) expect(b.y).not.toBe(a.y);
  });

  it("always places the selected component's label", () => {
    const spots = placeLabels([
      { id: "a", x: 100, y: 100, r: 6, text: "virt-controller", weight: 9 },
      { id: "sel", x: 101, y: 100, r: 6, text: "virt-api", weight: 0, must: true },
    ]);
    expect(spots.map((s) => s.id)).toContain("sel");
  });
});

describe("guided demo helpers", () => {
  it("names the servers in plain words", () => {
    expect(serverName("scenario")).toBe("safety test server");
    expect(serverName("normal")).toBe("main server");
  });

  it("finds values an earlier step has not saved yet", () => {
    const call = fill({ path: "/api/runs/{t1_run}", body: { snapshot_id: "{release_snapshot}" } }, { release_snapshot: "r1" });
    expect(missingValues(call)).toEqual(["t1_run"]);
  });

  it("waits until the answer reaches the wanted state", () => {
    const wait = { path: "run_status", op: "in", value: ["complete", "failed"] };
    expect(waitDone({ run_status: "running" }, wait)).toBe(false);
    expect(waitDone({ run_status: "complete" }, wait)).toBe(true);
    expect(waitDone({}, undefined)).toBe(true);
  });
});
