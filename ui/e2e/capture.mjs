// Captures every screen for a visual review and reports sideways overflow and error text.
// Usage: node e2e/capture.mjs <base-url> <out-dir>
import { chromium } from "@playwright/test";
import { mkdirSync } from "node:fs";

const [base, out] = process.argv.slice(2);
const roles = (process.env.CAPTURE_ROLES ?? "pm,em,architect,security,oncall,audit").split(",");
const themes = (process.env.CAPTURE_THEMES ?? "light,dark").split(",");
const widths = (process.env.CAPTURE_WIDTHS ?? "1280,1440,1920").split(",").map(Number);
const browser = await chromium.launch();
const problems = [];
for (const role of roles) {
  const nav = await (await fetch(`${base}/api/navigation`, { headers: { "X-Atlas-Role": role } })).json();
  const extra = (process.env.CAPTURE_EXTRA ?? "").split(",").filter(Boolean).map((path, i) => ({ page_id: `extra-${i + 1}`, path }));
  const pages = process.env.CAPTURE_ONLY_EXTRA ? extra : [...nav.groups.flatMap((g) => g.pages), ...extra];
  for (const theme of themes) {
    for (const width of widths) {
      const ctx = await browser.newContext({ viewport: { width, height: 900 } });
      await ctx.addInitScript(([r, t]) => {
        window.localStorage.setItem("atlas.demo.role", r);
        window.localStorage.setItem("atlas.theme", t);
      }, [role, theme]);
      const page = await ctx.newPage();
      await page.goto(`${base}/`, { waitUntil: "domcontentloaded" }).catch(() => {});
      await page.waitForTimeout(1500);
      const errors = [];
      page.on("response", (r) => { if (r.status() >= 400) errors.push(`${r.status()} ${new URL(r.url()).pathname}`); });
      page.on("pageerror", (e) => errors.push(String(e).slice(0, 160)));
      for (const p of pages) {
        await page.goto(`${base}${p.path}`, { waitUntil: "domcontentloaded" }).catch(() => {});
        await page.waitForSelector("main h1, main h2", { timeout: 8000 }).catch(() => {});
        await page.waitForTimeout(1500);
        const info = await page.evaluate(() => {
          const el = document.documentElement;
          const wide = [...document.querySelectorAll("main *")].filter((e) => e.getBoundingClientRect().right > window.innerWidth + 1).slice(0, 3)
            .map((e) => `${e.tagName.toLowerCase()}.${String(e.className).slice(0, 40)}`);
          return { overflow: el.scrollWidth > el.clientWidth + 1, wide, theme: el.dataset.theme ?? el.className, text: document.body.innerText.slice(0, 4000) };
        });
        const bad = /something went wrong|undefined|NaN|\[object Object\]|not_found|Traceback/i.exec(info.text);
        if (info.overflow || info.wide.length || bad || errors.length) {
          problems.push({ role, theme, width, page: p.page_id, overflow: info.overflow, wide: info.wide, text: bad ? bad[0] : null, console: errors.splice(0) });
        }
        const dir = `${out}/${theme}-${width}/${role}`;
        mkdirSync(dir, { recursive: true });
        await page.screenshot({ path: `${dir}/${p.page_id}.png`, fullPage: false });
      }
      await ctx.close();
    }
  }
}
await browser.close();
console.log(JSON.stringify(problems, null, 1));
console.log(`problems: ${problems.length}`);
