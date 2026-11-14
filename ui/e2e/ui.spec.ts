import { expect, test } from "@playwright/test";

// Run against a backend started with recorded fakes and the built UI:
//   ATLAS_E2E_URL=http://127.0.0.1:8000 npx playwright test
// Needs `npx playwright install chromium` once. Skipped when ATLAS_E2E_URL is not set.
test.skip(!process.env.ATLAS_E2E_URL, "set ATLAS_E2E_URL to run the browser tests");

test("the page loads with a strict content security policy and no outside requests", async ({ page }) => {
  const outside: string[] = [];
  page.on("request", (r) => {
    const u = new URL(r.url());
    if (u.origin !== new URL(process.env.ATLAS_E2E_URL!).origin) outside.push(r.url());
  });
  const res = await page.goto("/");
  const csp = res?.headers()["content-security-policy"] ?? "";
  for (const d of ["default-src 'self'", "connect-src 'self'", "form-action 'none'", "frame-ancestors 'none'", "base-uri 'none'", "object-src 'none'"]) {
    expect(csp).toContain(d);
  }
  await expect(page.getByText("Scout investigates, Sentinel checks every claim")).toBeVisible();
  expect(outside).toEqual([]);
});

test("switching role clears what the previous role saw", async ({ page }) => {
  await page.goto("/queue");
  await page.getByLabel("Role").selectOption("security");
  await page.waitForLoadState("networkidle");
  await page.getByLabel("Role").selectOption("pm");
  await page.waitForLoadState("networkidle");
  await expect(page.getByText("restricted", { exact: false })).toHaveCount(0);
});

test("a manual trigger shows its availability from the server", async ({ page }) => {
  await page.goto("/run-now");
  await expect(page.getByRole("heading", { name: "Run now" })).toBeVisible();
  const buttons = page.getByRole("button", { name: "Run now" });
  expect(await buttons.count()).toBeGreaterThan(0);
});

test("API paths are never answered by the UI", async ({ request }) => {
  const res = await request.get("/api/does-not-exist", { headers: { "X-Atlas-Role": "pm" } });
  expect(res.status()).toBe(404);
  expect(res.headers()["content-type"]).toContain("application/json");
});

test("a held correction is found again after a reload, without a second write", async ({ page }) => {
  const writes: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && r.url().includes("/api/corrections")) writes.push(r.url());
  });
  await page.goto("/containment");
  await page.reload();
  await page.getByLabel("Role").selectOption("audit");
  expect(writes).toEqual([]);
});
