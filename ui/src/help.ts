// Help pages live in the docs, not in the product. Each screen links to one guide page by slug.
// The link shows only when the docs are present: the server must serve /docs/guide/pages.json listing
// the available slugs (the guide pages themselves are /docs/guide/<slug>.html).

export const HELP_SLUG: Record<string, string> = {
  home: "home",
  dashboard: "live-dashboard",
  report_forecast: "forecast-report",
  report_allocation: "allocation-report",
  report_architecture: "architecture-report",
  report_security: "security-gate-report",
  report_investigation: "investigation-report",
  run_now: "run-now",
  schedules: "automatic-runs",
  runs: "runs",
  queue: "needs-a-person",
  approvals: "approvals",
  tasks: "tasks",
  notifications: "notifications",
  containment: "kill-switches",
  system: "system-and-mode",
  scorecard: "scorecard",
  graph: "knowledge-graph",
  feedback: "record-a-decision",
  evaluation: "quality-checks",
  demo: "guided-demo",
  data_modes: "data-labels",
};

export const HELP_INDEX = "/docs/guide/pages.json";

export function helpUrl(slug: string): string {
  return `/docs/guide/${slug}.html`;
}
