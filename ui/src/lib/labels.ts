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
