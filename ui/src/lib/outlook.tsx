// The release outlook: what the next release inherits (Q1) and what stops shipping now (Q2).
// One store per project and role. It refetches when the project stream says the outlook changed
// (the shell's MetricsSync forwards the event), after a stream reset, and every 30 s as a safety net.
import { useQuery, type QueryClient } from "@tanstack/react-query";
import type { LiveEvent } from "../api/stream";
import { useApi, useApp, useScopedKey } from "../app/context";
import { useCurrentProject } from "../screens/Dashboard";
import type { Navigation } from "./nav";

const SAFETY_REFETCH_MS = 30_000;

export type OwnerBucket = "developers" | "coding_agents" | "decide_first" | "not_allocated";
export type ShipStatus = "ON_TRACK" | "AT_RISK" | "BLOCKED" | "NOT_MEASURED";

export interface OutlookItem {
  id?: string;
  issue_id?: string | null;
  title: string;
  component?: string | null;
  owner_team?: string | null;
  score?: number | null;
  owner: OwnerBucket;
  reason?: string | null;
  evidence_link?: string | null;
}

export interface Q1 {
  state: "none" | "ready";
  message?: string | null;
  run_action: { allowed: boolean; reason?: string | null; link: string; dataset?: string | null };
  in_progress?: { run_id: string; link: string } | null;
  run_id?: string;
  run_link?: string;
  report_link?: string;
  report_access?: "open" | "locked";
  allocation_link?: string;
  allocation_access?: "open" | "locked";
  dataset: { title?: string | null; snapshot_id?: string | null; as_of?: string | null } | null;
  closed_at?: string | null;
  basis?: "predicted";
  date_range?: string;
  total: number;
  withheld?: number;
  split: Record<OwnerBucket, number>;
  items: OutlookItem[];
  claims_checked?: number | null;
  claims_rejected?: number | null;
  data_mode?: string;
}

export interface Blocker {
  kind: "failing_ci" | "security" | "investigation" | "critical_issue";
  title: string;
  component?: string | null;
  effect: "blocks" | "risk";
  reason?: string;
  link?: string | null;
  at?: string | null;
  count?: number;
}

export interface AffectedService {
  component: string;
  blockers: number;
  blocks: number;
  kinds: Record<string, number>;
  link: string | null;
}

export interface Q2 {
  status: ShipStatus;
  status_word: string;
  reasons: { rule: string; status: ShipStatus; text: string }[];
  missing_inputs: { input: string; reason: string }[];
  inputs: {
    failing_ci: { count: number; link: string; note?: string | null } | null;
    security_gate: { state: string; blocking_findings: number; findings: number; link: string; note?: string | null } | null;
    investigations: { escalated: number; held: number; link: string } | null;
    critical_issues: { count: number; label: string; link: string } | null;
  };
  blockers: Blocker[];
  blocker_count: number;
  blocking_count: number;
  affected_services: AffectedService[];
  service_count: number;
  thresholds: { failing_ci_at_risk: number; critical_issues_at_risk: number };
  last_poll?: string | null;
  graph_link?: string | null;
}

export interface Outlook {
  project_id: string;
  outlook_version: number;
  computed_at: string;
  sections: ("q1" | "q2")[];
  q1: Q1 | null;
  q2: Q2 | null;
}

export const OUTLOOK_KEY = "outlook";

export const OWNER_LABEL: Record<OwnerBucket, string> = {
  developers: "Devs",
  coding_agents: "Agents",
  decide_first: "Decide first",
  not_allocated: "Not allocated",
};

export const STATUS_TONE: Record<ShipStatus, string> = {
  ON_TRACK: "good",
  AT_RISK: "warn",
  BLOCKED: "critical",
  NOT_MEASURED: "neutral",
};

/** The same owner buckets the outlook uses, counted from report items (allocation page, EM home). */
export function ownerSplit(items: { allocation?: string | null }[]): Record<OwnerBucket, number> {
  const out: Record<OwnerBucket, number> = { developers: 0, coding_agents: 0, decide_first: 0, not_allocated: 0 };
  for (const i of items) {
    const b: OwnerBucket = i.allocation === "developer" ? "developers" : i.allocation === "coding_agent" ? "coding_agents" : i.allocation === "decision_maker" ? "decide_first" : "not_allocated";
    out[b] += 1;
  }
  return out;
}

export function outlookPath(project: string | null): string | null {
  return project ? `/api/projects/${encodeURIComponent(project)}/outlook` : null;
}

/** The outlook sections the server offers this role (empty for roles in beta). */
export function useOutlookSections(): ("q1" | "q2")[] {
  const nav = useApi<Navigation>("/api/navigation");
  return nav.data?.outlook ?? [];
}

export function useOutlook() {
  const project = useCurrentProject();
  const sections = useOutlookSections();
  const { client } = useApp();
  const path = sections.length ? outlookPath(project) : null;
  const key = useScopedKey(path, OUTLOOK_KEY);
  return useQuery<Outlook, Error>({
    queryKey: key,
    queryFn: () => client.get<Outlook>(path as string),
    enabled: path !== null,
    refetchInterval: SAFETY_REFETCH_MS,
    refetchIntervalInBackground: false,
  });
}

/** Called by the shell's project-stream listener: refetch the outlook when the server says it changed. */
export function onOutlookEvent(qc: QueryClient, e: LiveEvent | null): void {
  if (e === null || e.event === "project.outlook_changed") {
    void qc.invalidateQueries({ predicate: (q) => q.queryKey.includes(OUTLOOK_KEY) });
  }
}

/** "3 Nov 2026" from an ISO date, or null. */
export function dayLabel(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric" }).format(d);
}
