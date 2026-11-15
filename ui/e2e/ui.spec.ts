import { expect, test } from "@playwright/test";
import { as, headers, post, releaseClose, type Role } from "./helpers";

const ROLES: Role[] = ["pm", "em", "architect", "security", "oncall", "audit"];

test.describe.configure({ mode: "serial" });

test("the page loads with a strict content security policy and no outside requests", async ({ page, baseURL }) => {
  const outside: string[] = [];
  page.on("request", (r) => {
    if (new URL(r.url()).origin !== new URL(baseURL!).origin) outside.push(r.url());
  });
  const res = await page.goto("/");
  const csp = res?.headers()["content-security-policy"] ?? "";
  for (const d of ["default-src 'self'", "connect-src 'self'", "script-src 'self'", "style-src 'self'", "form-action 'none'", "frame-ancestors 'none'", "base-uri 'none'", "object-src 'none'"]) {
    expect(csp).toContain(d);
  }
  await expect(page.getByText("Scout").first()).toBeVisible();
  await page.goto("/dashboard");
  await page.goto("/graph");
  await page.waitForTimeout(500);
  expect(outside).toEqual([]);
});

test("API paths are never answered by the UI", async ({ request, baseURL }) => {
  const res = await request.get("/api/does-not-exist", { headers: headers("pm", baseURL!) });
  expect(res.status()).toBe(404);
  expect(res.headers()["content-type"]).toContain("application/json");
});

for (const role of ROLES) {
  test(`the menu for ${role} is exactly what the server lists`, async ({ page, request, baseURL }) => {
    const nav = await (await request.get("/api/navigation", { headers: headers(role, baseURL!) })).json();
    const titles = (nav.groups as { pages: { title: string }[] }[]).flatMap((g) => g.pages.map((p) => p.title));
    await as(page, role);
    const links = page.locator("nav a");
    await expect(links).toHaveCount(titles.length);
    const shown = (await links.allTextContents()).map((t) => t.replace(/\d+$/, "").trim());
    expect(shown).toEqual(titles);
  });
}

test("a manual trigger shows its availability from the server", async ({ page }) => {
  await as(page, "pm", "/run-now");
  await expect(page.getByRole("heading", { name: "Run now" })).toBeVisible();
  expect(await page.getByRole("button", { name: /^Run/ }).count()).toBeGreaterThan(0);
});

test("a page for another role is locked, and a request for an ordinary item opens it read-only", async ({ page, request, baseURL }) => {
  await releaseClose(request, baseURL!);
  await as(page, "pm", "/latest-report/allocation");
  await expect(page.getByText("This page is for the engineering manager")).toBeVisible();
  await page.getByRole("button", { name: "Request access" }).click();
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("Granted until")).toBeVisible();
  await page.getByRole("link", { name: "Open" }).click();
  await expect(page.getByTestId("read-only")).toContainText("Read-only until");
  await expect(page.getByTestId("exception-note")).toContainText("exception for this demo");
});

test("a request for a sensitive item waits for the owning role, who can grant it", async ({ page, request, baseURL, browser }) => {
  const res = await post(request, baseURL!, "pm", "/api/access-requests", { object_type: "dead_letter", object_id: "list", reason: "browser test" });
  expect([200, 201]).toContain(res.status());
  expect((await res.json()).request.status).toBe("pending");
  await as(page, "pm", "/access-requests");
  await expect(page.getByText(/waiting|pending|requested/i).first()).toBeVisible();

  const auditCtx = await browser.newContext();
  const audit = await auditCtx.newPage();
  await as(audit, "audit", "/access-requests");
  await audit.getByText("To decide").click();
  await audit.getByRole("button", { name: /grant/i }).first().click();
  const confirm = audit.getByRole("dialog").getByRole("button", { name: /grant/i });
  if (await confirm.count()) await confirm.first().click();
  await expect
    .poll(async () => {
      const mine = await (await request.get("/api/access-requests?box=mine", { headers: headers("pm", baseURL!) })).json();
      return (mine.requests as { object_type: string; status: string }[]).find((r) => r.object_type === "dead_letter")?.status;
    })
    .toBe("granted");
  await auditCtx.close();
});

test("a kill switch drill shows a banner, locks Run now, and resume clears it", async ({ page }) => {
  await as(page, "audit", "/containment");
  await expect(page.getByText("All 11 ready")).toBeVisible();
  await page.getByRole("button", { name: "Run drill" }).first().click();
  await page.getByRole("dialog").getByRole("button", { name: "Run drill" }).click();
  await expect(page.getByText("1 of 11 tripped")).toBeVisible();
  await expect(page.locator(".containment-banner")).toBeVisible();

  await page.goto("/run-now");
  await expect(page.locator(".containment-banner")).toBeVisible();
  await expect(page.getByRole("button", { name: /^Run/ }).first()).toBeDisabled();

  await page.goto("/containment");
  await page.getByRole("button", { name: "Resume" }).first().click();
  await page.getByRole("dialog").getByRole("button", { name: "Resume" }).click();
  await expect(page.getByText("All 11 ready")).toBeVisible();
  await expect(page.locator(".containment-banner")).toHaveCount(0);
});

test("a home tile changes without a reload when a run finishes", async ({ page, request, baseURL }) => {
  await as(page, "audit", "/");
  const before = (await (await request.get("/api/projects/kubevirt/metrics", { headers: headers("audit", baseURL!) })).json()).runs_total as number;
  const tile = page.getByTestId("kpi-runs");
  await expect(tile).toContainText(String(before));
  await releaseClose(request, baseURL!);
  await expect(tile).toContainText(String(before + 1), { timeout: 40_000 });
});

test("switching project clears what the other project showed", async ({ page }) => {
  await as(page, "pm", "/");
  await expect(page.getByText("Next-release forecast")).toBeVisible();
  await page.getByLabel("Project").selectOption("argo-cd");
  await expect(page.getByText("#16386")).toHaveCount(0);
  await page.getByLabel("Project").selectOption("kubevirt");
  await expect(page.getByText("Next-release forecast")).toBeVisible();
});

test("switching role clears what the previous role saw", async ({ page }) => {
  await as(page, "audit", "/containment");
  await expect(page.getByText("All 11 ready")).toBeVisible();
  await page.getByLabel("Role").selectOption("pm");
  await expect(page.getByText("This page is for the security engineer")).toBeVisible();
  await expect(page.getByRole("button", { name: "Run drill" })).toHaveCount(0);
});
