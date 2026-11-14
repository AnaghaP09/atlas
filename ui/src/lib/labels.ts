// Plain names for things the API identifies by a code. One place, so every screen says the same.
import type { RunView } from "../api/types";

const REPORT_NAME: Record<string, string> = {
  forecast: "Forecast",
  allocation: "Allocation plan",
  architecture: "Architecture",
  investigation: "Investigation",
  security_gate: "Security gate",
  scorecard: "Scorecard",
  question: "Answer",
};

export function reportName(type: string | null | undefined): string {
  const t = String(type ?? "");
  return REPORT_NAME[t] ?? (t ? t.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()) : "Report");
}

/** "Release close · KubeVirt at release v1.9.0". Falls back to the kind of run, never to a bare id. */
export function runTitle(r: Pick<RunView, "title" | "kind_label" | "subject" | "trigger" | "run_id">): string {
  if (r.title) return r.title;
  const kind = typeof r.trigger?.trigger === "string" ? r.trigger.trigger : "";
  const word = kind === "release_close" ? "Release close" : kind === "failure" ? "Investigation" : kind === "question" ? "Question" : kind === "nightly" ? "Nightly sweep" : "Run";
  return r.subject ? `${word} · ${r.subject}` : word;
}

const OWNER_NAME: Record<string, string> = {
  coding_agent: "Coding agent",
  developer: "Developer",
  decision_maker: "Decision needed",
  infra_owner: "Infrastructure owner",
};

export function ownerName(owner: string | null | undefined): string | null {
  if (!owner) return null;
  return OWNER_NAME[owner] ?? owner.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

export function ownerTone(owner: string | null | undefined): string {
  return owner === "coding_agent" ? "accent" : owner === "decision_maker" ? "warn" : owner === "developer" ? "info" : "neutral";
}

const STARTER_ROLE: Record<string, string> = {
  pm: "Product manager",
  em: "Engineering manager",
  architect: "Solution architect",
  security: "Security engineer",
  oncall: "On-call engineer",
  audit: "Auditor",
};

export function starterName(s: RunView["started_by"] | null | undefined): string {
  if (!s) return "Unknown";
  if (s.kind === "person") return STARTER_ROLE[String(s.role ?? "")] ?? "A person";
  if (s.kind === "watcher") return "Watcher";
  return "Schedule";
}

const CLASSIFICATION: Record<string, string> = {
  DEPENDENCY: "Dependency problem",
  CODE_DEFECT: "Code defect",
  TEST_DEFECT: "Test defect",
  ENVIRONMENT: "Environment problem",
  DUPLICATE: "Duplicate",
  CARRY_OVER_FORECAST: "Carry-over forecast",
  UNKNOWN: "Not enough evidence",
  OUT_OF_SCOPE: "Out of scope",
};

const ACTION: Record<string, string> = {
  DELEGATE_TO_CODING_AGENT: "Give to the coding agent",
  ASSIGN_DEVELOPER: "Assign a developer",
  ESCALATE_DECISION: "Needs a decision",
  HAND_TO_INFRA_OWNER: "Hand to the infrastructure owner",
  LINK_DUPLICATE: "Link as a duplicate",
  PUBLISH_FORECAST: "Publish the forecast",
  REJECT: "Held for a person",
};

function words(code: string): string {
  return code.replace(/_/g, " ").toLowerCase().replace(/^./, (c) => c.toUpperCase());
}

export function classificationName(code: string | null | undefined): string | null {
  return code ? (CLASSIFICATION[code] ?? words(code)) : null;
}

export function actionName(code: string | null | undefined): string | null {
  return code ? (ACTION[code] ?? words(code)) : null;
}

const TOOL: Record<string, string> = {
  get_issue: "Read the issue",
  get_issue_timeline: "Read the issue history",
  search_issues: "Searched related issues",
  get_release_issues: "Listed the release's issues",
  get_changed_components: "Checked which components changed",
  get_failed_pipeline_jobs: "Checked failed CI jobs",
  get_cluster_events: "Checked cluster events",
  get_blocking_vulnerabilities: "Checked for blocking vulnerabilities",
  get_error_rate: "Checked error rates",
  trace_dependency_impact: "Traced dependencies",
};

export function toolName(tool: string): string {
  return TOOL[tool] ?? words(tool);
}

const STAGE: Record<string, string> = {
  investigating: "Started investigating",
  verifying: "Checking the claims",
  verified: "Claims checked",
  forecasting: "Building the forecast",
  rendering: "Writing the reports",
  complete: "Finished",
  partial: "Stopped at a limit",
  failed: "Failed",
  unverified: "Could not be checked",
};

export function stageName(stage: string): string {
  return STAGE[stage] ?? words(stage);
}
