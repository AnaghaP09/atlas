/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

// The development server follows the same rules as the backend: it listens on loopback only,
// accepts only listed Host values, refuses foreign Origins, and passes /api to the backend
// unchanged. Every value comes from the environment; the defaults match the backend defaults.
const env = process.env;
const devHost = env.ATLAS_UI_DEV_HOST ?? "127.0.0.1";
const devPort = Number(env.ATLAS_UI_DEV_PORT ?? "5173");
const apiTarget = env.ATLAS_API_URL ?? "http://127.0.0.1:8000";
const allowedHosts = (env.ATLAS_UI_DEV_ALLOWED_HOSTS ?? `127.0.0.1:${devPort},localhost:${devPort},[::1]:${devPort}`)
  .split(",")
  .map((h) => h.trim().toLowerCase())
  .filter(Boolean);
const allowedOrigins = new Set(allowedHosts.map((h) => `http://${h}`));

function hostOriginGuard(): Plugin {
  return {
    name: "atlas-host-origin-guard",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const host = (req.headers.host ?? "").toLowerCase();
        const origin = req.headers.origin;
        if (!allowedHosts.includes(host) || (origin !== undefined && !allowedOrigins.has(origin))) {
          res.statusCode = 403;
          res.end("Host or Origin not allowed");
          return;
        }
        next();
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), hostOriginGuard()],
  server: {
    host: devHost,
    port: devPort,
    strictPort: true,
    allowedHosts: allowedHosts.map((h) => h.replace(/:\d+$/, "").replace(/^\[|\]$/g, "")),
    proxy: {
      "/api": { target: apiTarget, changeOrigin: true },
    },
  },
  build: { outDir: "dist", sourcemap: false, assetsInlineLimit: 0 },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
