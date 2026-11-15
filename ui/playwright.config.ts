import { defineConfig } from "@playwright/test";

// Browser tests. By default they start their own server: the real application with recorded model
// answers and a temporary data folder (tests/e2e/server.py). Build the UI first (`npm run build`).
// Set ATLAS_E2E_URL to test a server that is already running instead.
// Browsers are installed once with `npx playwright install chromium`.
const port = Number(process.env.ATLAS_E2E_PORT ?? 8791);
const external = process.env.ATLAS_E2E_URL;
const python = process.env.ATLAS_PYTHON ?? "python";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.spec.ts",
  timeout: 45_000,
  workers: 1,
  fullyParallel: false,
  reporter: [["list"]],
  use: { baseURL: external ?? `http://127.0.0.1:${port}`, trace: "off", viewport: { width: 1440, height: 900 } },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
  webServer: external
    ? undefined
    : {
        command: `"${python}" ../tests/e2e/server.py --port ${port}`,
        url: `http://127.0.0.1:${port}/api/health`,
        reuseExistingServer: false,
        timeout: 90_000,
      },
});
