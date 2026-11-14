import { defineConfig } from "@playwright/test";

// End-to-end tests run against a backend started with recorded fakes and the built UI.
// Set ATLAS_E2E_URL to the server address. Browsers are installed separately with
// `npx playwright install chromium`; the tests are skipped when ATLAS_E2E_URL is not set.
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  use: { baseURL: process.env.ATLAS_E2E_URL ?? "http://127.0.0.1:8000", trace: "off" },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
