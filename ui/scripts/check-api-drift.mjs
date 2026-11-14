// Fails when the UI's API types no longer match the backend.
//  1. Regenerates the backend's OpenAPI document (Python from ATLAS_PYTHON, or "python" on PATH)
//     and compares it with the committed ui/openapi.json.
//  2. Regenerates src/api/schema.ts from ui/openapi.json and compares it with the committed file.
// Set ATLAS_SKIP_BACKEND_EXPORT=1 to run only step 2 (for example when Python is not available).
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const uiDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const normalise = (s) => s.replace(/\r\n/g, "\n");
let failed = false;

if (process.env.ATLAS_SKIP_BACKEND_EXPORT !== "1") {
  const python = process.env.ATLAS_PYTHON ?? "python";
  const live = execFileSync(python, [join(uiDir, "scripts", "export_openapi.py")], { encoding: "utf8" });
  const committed = readFileSync(join(uiDir, "openapi.json"), "utf8");
  if (normalise(live) !== normalise(committed)) {
    console.error("ui/openapi.json is out of date. Run: python ui/scripts/export_openapi.py > ui/openapi.json");
    failed = true;
  }
}

const tmp = mkdtempSync(join(tmpdir(), "atlas-ui-"));
try {
  const out = join(tmp, "schema.ts");
  const bin = join(uiDir, "node_modules", "openapi-typescript", "bin", "cli.js");
  execFileSync(process.execPath, [bin, join(uiDir, "openapi.json"), "-o", out], { stdio: "ignore" });
  if (normalise(readFileSync(out, "utf8")) !== normalise(readFileSync(join(uiDir, "src", "api", "schema.ts"), "utf8"))) {
    console.error("src/api/schema.ts is out of date. Run: npm run api:types");
    failed = true;
  }
} finally {
  rmSync(tmp, { recursive: true, force: true });
}

if (failed) process.exit(1);
console.log("API types match the backend.");
