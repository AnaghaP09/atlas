// Help pages live in the docs, not in the product. Each screen links to one guide page by slug.
// The link shows only when the docs are present: the server must serve /docs/guide/pages.json listing
// the available slugs. Pages are read inside ATLAS at /help/<slug>, from /docs/guide/<slug>.md.

export const HELP_SLUG: Record<string, string> = {
  home: "home",
  dashboard: "live-dashboard",
  ship_readiness: "release-outlook",
  report_forecast: "reports",
  report_allocation: "reports",
  report_architecture: "reports",
  report_security: "reports",
  report_investigation: "reports",
  run_now: "run-now",
  schedules: "automatic-runs",
  runs: "runs",
  queue: "needs-a-person",
  approvals: "approvals",
  tasks: "tasks",
  notifications: "notifications",
  containment: "kill-switches",
  system: "system-and-mode",
  access_requests: "access-requests",
  add_project: "add-project",
  scorecard: "scorecard",
  graph: "knowledge-graph",
  feedback: "record-a-decision",
  evaluation: "quality-checks",
  demo: "guided-demo",
  data_labels: "glossary",
  data_as_of: "data-as-of",
};

/** Guide pages that must exist in the docs for every help link to show. */
export const HELP_PAGES = [...new Set(Object.values(HELP_SLUG))].sort();

export const HELP_INDEX = "/docs/guide/pages.json";

export function helpUrl(slug: string): string {
  return `/help/${slug}`;
}
