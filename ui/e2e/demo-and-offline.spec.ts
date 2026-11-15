import { expect, test } from "@playwright/test";
import { as } from "./helpers";

test.describe.configure({ mode: "serial" });

test("guided demo: a step passes, and a step that needs an earlier one says which", async ({ page }) => {
  await as(page, "audit", "/demo");
  const progress = page.locator(".demo-progress");
  await expect(progress).toContainText("0 of");

  const graph = page.getByTestId("row-C3");
  await graph.getByRole("button", { name: "Run this step" }).click();
  await expect(graph.getByText("Passed")).toBeVisible();
  await expect(progress).toContainText("1 of");

  const reports = page.getByTestId("row-R1-R6");
  await reports.getByRole("button", { name: "Run this step" }).click();
  await expect(reports.getByText("Needs attention")).toBeVisible();
  await expect(reports.getByText("Run step 1 first.")).toBeVisible();

  // Steps for the other server never offer a run button here, and use the plain server name.
  const stopped = page.getByTestId("row-K1");
  await expect(stopped.getByText("Needs the safety test server")).toBeVisible();
  await expect(stopped.getByRole("button", { name: "Run this step" })).toHaveCount(0);
  await expect(page.getByText("scenario server")).toHaveCount(0);

  // A step that spends model budget shows the expected cost before it runs.
  await expect(page.getByTestId("row-T1").getByText("About $0.10")).toBeVisible();
});

test("going offline shows a bar on every page and pauses Run now; going back clears it", async ({ page }) => {
  await as(page, "audit", "/system");
  await page.getByRole("button", { name: "Go offline" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Go offline" }).click();
  await expect(page.getByTestId("offline-banner")).toBeVisible();

  await page.goto("/run-now");
  await expect(page.getByTestId("offline-banner")).toBeVisible();
  await expect(page.getByTestId("unavailable-reason").first()).toContainText("Paused while offline");
  await expect(page.getByRole("button", { name: /^Run now/ }).first()).toBeDisabled();

  await page.goto("/system");
  await page.getByRole("button", { name: "Go connected" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Go connected" }).click();
  await expect(page.getByTestId("offline-banner")).toHaveCount(0);
});
