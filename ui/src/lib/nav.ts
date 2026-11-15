// Role-based navigation. The server decides which pages each role sees (GET /api/navigation); the UI
// renders only that list. The API still refuses data a role may not read, so this is never the control.
import type { IconName } from "../components/icons";
import { matchPath } from "./router";

export interface NavPage {
  page_id: string;
  path: string;
  title: string;
  for: string;
}

export interface HiddenPage extends NavPage {
  roles: string[];
  role_titles: string[];
}

export interface Navigation {
  role: string | null;
  role_title: string;
  home: string;
  project_id?: string;
  groups: { title: string; pages: NavPage[] }[];
  other_pages: HiddenPage[];
}

export const PAGE_ICON: Record<string, IconName> = {
  home: "home",
  dashboard: "pulse",
  report_forecast: "chart",
  report_allocation: "task",
  report_architecture: "graph",
  report_security: "shield",
  report_investigation: "compass",
  run_now: "play",
  schedules: "clock",
  runs: "list",
  queue: "hand",
  approvals: "check",
  requests: "hand",
  tasks: "task",
  notifications: "bell",
  containment: "power",
  system: "gauge",
  scorecard: "chart",
  graph: "graph",
  feedback: "pen",
  evaluation: "gauge",
  demo: "compass",
  access_requests: "key",
  add_project: "database",
};

/** Report page id -> the report type the API stores. */
export const REPORT_TYPE: Record<string, string> = {
  forecast: "forecast",
  allocation: "allocation",
  architecture: "architecture",
  security: "security_gate",
  investigation: "investigation",
};

/** Which menu page a screen path belongs to. Detail pages count as their list page. Null means "any role". */
export function pageIdForPath(path: string): string | null {
  const fixed: [string, string][] = [
    ["/", "home"],
    ["/dashboard", "dashboard"],
    ["/run-now", "run_now"],
    ["/schedules", "schedules"],
    ["/runs", "runs"],
    ["/runs/:id", "runs"],
    ["/queue", "queue"],
    ["/requests", "requests"],
    ["/approvals", "approvals"],
    ["/tasks", "tasks"],
    ["/tasks/:id", "tasks"],
    ["/notifications", "notifications"],
    ["/containment", "containment"],
    ["/system", "system"],
    ["/scorecard", "scorecard"],
    ["/graph", "graph"],
    ["/feedback", "feedback"],
    ["/evaluation", "evaluation"],
    ["/demo", "demo"],
    ["/access-requests", "access_requests"],
    ["/add-project", "add_project"],
  ];
  for (const [pattern, id] of fixed) if (matchPath(pattern, path)) return id;
  const latest = matchPath("/latest-report/:type", path);
  if (latest) return `report_${latest.type ?? ""}`;
  // Report, evidence and activity-record pages open from links the caller already saw; the API decides.
  return null;
}

export function allowedPages(nav: Navigation | undefined): Set<string> {
  return new Set((nav?.groups ?? []).flatMap((g) => g.pages.map((p) => p.page_id)));
}

export function findPage(nav: Navigation | undefined, pageId: string): NavPage | HiddenPage | undefined {
  for (const g of nav?.groups ?? []) for (const p of g.pages) if (p.page_id === pageId) return p;
  return nav?.other_pages.find((p) => p.page_id === pageId);
}

export function joinRoles(titles: string[]): string {
  if (titles.length <= 1) return titles[0] ?? "";
  return `${titles.slice(0, -1).join(", ")} or ${titles[titles.length - 1]}`;
}
