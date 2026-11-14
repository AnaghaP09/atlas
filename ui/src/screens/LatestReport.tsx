import type { Envelope, ReportLink, RunView } from "../api/types";
import { useApi } from "../app/context";
import { Empty, ErrorView, Loading } from "../components/common";
import { REPORT_TYPE } from "../lib/nav";
import { ReportDetail } from "./Reports";

/** The newest report of one type across the project's runs, or null. Runs are listed newest first. */
export function latestReport(runs: RunView[], type: string): ReportLink | null {
  for (const r of runs) {
    const hit = r.reports.filter((rep) => rep.report_type === type && !rep.superseded);
    if (hit.length) return hit.sort((a, b) => b.report_version - a.report_version)[0] ?? null;
  }
  return null;
}

const START: Record<string, string> = {
  forecast: "No forecast yet.",
  allocation: "No allocation yet.",
  architecture: "No architecture report yet.",
  security_gate: "No security gate report yet.",
  investigation: "No investigation yet.",
};

export function LatestReport({ kind }: { kind: string }) {
  const type = REPORT_TYPE[kind] ?? kind;
  const runs = useApi<{ runs: RunView[] } & Envelope>("/api/runs");
  if (runs.isLoading) return <Loading what="the latest report" />;
  if (runs.error) return <ErrorView error={runs.error} />;
  const found = latestReport(runs.data?.runs ?? [], type);
  if (!found) return <Empty action={{ to: "/run-now", label: "Open Run now" }}>{START[type] ?? "No report of this kind yet."}</Empty>;
  return <ReportDetail reportId={found.report_id} />;
}
