import { randomUUID } from "node:crypto";
import { expect, type APIRequestContext, type Page } from "@playwright/test";

export type Role = "pm" | "em" | "architect" | "security" | "oncall" | "audit";

/** Open the app as a demo role (header identity keeps the picked role in browser storage). */
export async function as(page: Page, role: Role, path = "/") {
  await page.addInitScript((r) => {
    // Runs in the browser, where localStorage exists.
    (globalThis as unknown as { localStorage: { setItem(k: string, v: string): void } }).localStorage.setItem("atlas.demo.role", r);
  }, role);
  await page.goto(path);
  await expect(page.locator("main")).toBeVisible();
}

export function headers(role: Role, base: string, write = false): Record<string, string> {
  const h: Record<string, string> = { "X-Atlas-Role": role, Accept: "application/json" };
  if (write) {
    h["Idempotency-Key"] = randomUUID();
    h["Origin"] = new URL(base).origin;
    h["Content-Type"] = "application/json";
  }
  return h;
}

export async function post(request: APIRequestContext, base: string, role: Role, path: string, data: unknown = {}) {
  return request.post(path, { headers: headers(role, base, true), data });
}

/** Start a release close and wait until its reports exist. Returns the run id. */
export async function releaseClose(request: APIRequestContext, base: string): Promise<string> {
  const res = await post(request, base, "pm", "/api/triggers/release-close", { snapshot_id: "release-close-v1.9.0", release_id: "v1.9.0" });
  expect(res.status()).toBe(202);
  const runId = (await res.json()).run_id as string;
  await expect
    .poll(async () => (await (await request.get(`/api/runs/${runId}`, { headers: headers("pm", base) })).json()).run_status, { timeout: 30_000 })
    .toBe("complete");
  return runId;
}
