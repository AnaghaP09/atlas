// The top of the home page, different for each role. The server says which layout a role gets
// (GET /api/navigation "home"); the data comes from the same projected API reads as everywhere else.
import type { ContainmentView, Envelope, Health, Notification, QueueEntry, ReportView, RunView, TaskView } from "../api/types";
import { useApi } from "../app/context";
import { ButtonLink, Empty, ErrorView, KpiTile, Loading, Section, StatusPill, Truncate } from "../components/common";
import { Link } from "../lib/router";
import { runTitle } from "../lib/labels";
import { latestReport } from "./LatestReport";
import { useCurrentProject } from "./Dashboard";

export function useLatest(type: string) {
  const runs = useApi<{ runs: RunView[] } & Envelope>("/api/runs");
  const link = latestReport(runs.data?.runs ?? [], type);
  const rep = useApi<ReportView>(link ? `/api/reports/${encodeURIComponent(link.report_id)}` : null);
  return { loading: runs.isLoading || rep.isLoading, error: runs.error ?? rep.error, link, report: rep.data };
}

interface ForecastItem {
  title?: string;
  issue_id?: string;
  component?: string;
  score?: number;
}

function ForecastFocus() {
  const { loading, error, link, report } = useLatest("forecast");
  const items: ForecastItem[] = (report?.report?.forecast as { items?: ForecastItem[] } | undefined)?.items ?? [];
  const components = new Set(items.map((i) => i.component).filter(Boolean));
  return (
    <>
      <div className="kpis">
        <KpiTile icon="chart" label="Items likely to carry over" value={report ? items.length : "—"} hint={report ? `in ${components.size} components` : undefined} />
        <KpiTile icon="shield" tone="teal" label="Claims checked by Sentinel" value={report ? (report.report.claims ?? []).length : "—"} hint="by Sentinel" />
        <KpiTile icon="clock" tone="warn" label="Date risk" value={<span className="pill pill-neutral" title="ATLAS never sets dates">Not measured</span>} small />
      </div>
      <Section title="Next-release forecast" icon="chart" mode={report?.data_mode} reason={report ? "scores are illustrative" : null} end={link ? <Link to={`/reports/${encodeURIComponent(link.report_id)}`}>Open the full report</Link> : null}>
        {loading ? <Loading what="the forecast" /> : <ErrorView error={error} />}
        {!loading && !link ? (
          <Empty action={{ to: "/run-now", label: "Start a release close" }}>No forecast yet.</Empty>
        ) : null}
        {items.length ? (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Issue</th><th>Component</th><th className="num">Score</th></tr></thead>
              <tbody>
                {items.slice(0, 10).map((i, n) => (
                  <tr key={`${i.issue_id}-${n}`}>
                    <td><Truncate text={i.title ?? `Issue ${i.issue_id ?? ""}`} max={70} /> <span className="muted num">#{i.issue_id}</span></td>
                    <td>{i.component ?? "—"}</td>
                    <td className="num">{typeof i.score === "number" ? i.score.toFixed(2) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : link && !loading ? <p className="muted">The forecast lists no carry-over items.</p> : null}
      </Section>
    </>
  );
}

function EmFocus() {
  const queue = useApi<{ entries: QueueEntry[] }>("/api/queue");
  const tasks = useApi<{ tasks: TaskView[] }>("/api/tasks");
  const { link, report } = useLatest("allocation");
  const allocation = (report?.report?.items as { id?: string; title?: string; issue_id?: string; allocation?: string }[] | undefined) ?? [];
  const waiting = (tasks.data?.tasks ?? []).filter((t) => ["proposed", "prepared", "awaiting_reapproval"].includes(String((t as { state?: string }).state)));
  return (
    <>
      <div className="kpis">
        <KpiTile icon="hand" tone={queue.data?.entries.length ? "warn" : "good"} label="Needs a decision" value={queue.data ? queue.data.entries.length : "—"} hint={<Link to="/queue">Open the queue</Link>} />
        <KpiTile icon="check" tone={waiting.length ? "warn" : "good"} label="Tasks waiting for approval" value={tasks.data ? waiting.length : "—"} hint={<Link to="/approvals">Open approvals</Link>} />
        <KpiTile icon="task" tone="teal" label="Items in the allocation" value={report ? allocation.length : "—"} hint={link ? <Link to={`/reports/${encodeURIComponent(link.report_id)}`}>Allocation report</Link> : undefined} />
      </div>
      <Section title="Allocation" icon="task">
        {allocation.length === 0 ? (
          <Empty action={{ to: "/run-now", label: "Start a release close" }}>No allocation yet.</Empty>
        ) : (
          <ul className="list">
            {allocation.slice(0, 10).map((a, i) => (
              <li key={`${a.id}-${i}`}>
                <span className="grow"><Truncate text={a.title ?? String(a.id ?? "")} max={80} /> <span className="muted num">{a.issue_id ? `#${a.issue_id}` : ""}</span></span>
                {a.allocation ? (
                  <span className="pill pill-accent">{String(a.allocation).replace(/_/g, " ").toLowerCase()}</span>
                ) : (
                  <span className="pill pill-neutral" title="No owner proposed yet">Not allocated</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </Section>
    </>
  );
}

function ArchitectFocus() {
  const { link, report, loading } = useLatest("architecture");
  return (
    <Section title="Hotspots and debt to accept or fix" icon="graph" mode={report?.data_mode} end={<Link to="/graph">Open the knowledge graph</Link>}>
      {loading ? <Loading what="the architecture report" /> : null}
      {!loading && !link ? (
        <Empty action={{ to: "/graph", label: "Knowledge graph" }}>No architecture report yet.</Empty>
      ) : null}
      {link ? <ButtonLink to={`/reports/${encodeURIComponent(link.report_id)}`} variant="primary" icon="arrow">Read the hotspot report</ButtonLink> : null}
    </Section>
  );
}

function SecurityFocus() {
  const { link, report } = useLatest("security_gate");
  const containment = useApi<ContainmentView>("/api/containment");
  const sc = (report?.report?.security_check as { state?: string; findings?: { finding_id?: string; severity?: string; blocking?: boolean }[] } | undefined) ?? null;
  const blocking = (sc?.findings ?? []).filter((f) => f.blocking);
  const tripped = (containment.data?.switches ?? []).filter((s) => s.state !== "ok").length;
  return (
    <>
      <div className="kpis">
        <KpiTile icon="shield" tone={sc?.state === "passed" ? "good" : "warn"} label="Security gate" value={sc ? <StatusPill status={sc.state ?? "unknown"} /> : "—"} small hint={sc ? "latest gate" : undefined} />
        <KpiTile icon="power" tone={blocking.length ? "critical" : "good"} label="Blocking findings" value={sc ? blocking.length : "—"} hint="stop a release or change" />
        <KpiTile icon="power" tone={tripped ? "critical" : "good"} label="Kill switches tripped" value={containment.data ? tripped : "—"} hint={<Link to="/containment">View the switches</Link>} />
      </div>
      <Section title="Blocking findings" icon="shield" end={link ? <Link to={`/reports/${encodeURIComponent(link.report_id)}`}>Open the gate report</Link> : null}>
        {!sc ? <Empty>No security gate report yet.</Empty> : null}
        {sc && blocking.length === 0 ? <p className="muted">None.</p> : null}
        <ul className="list">
          {blocking.map((f) => (
            <li key={f.finding_id}><span className="grow mono">{f.finding_id}</span><span className="sev sev-high">{f.severity}</span></li>
          ))}
        </ul>
      </Section>
    </>
  );
}

function OncallFocus() {
  const project = useCurrentProject();
  const runs = useApi<{ runs: RunView[] } & Envelope>("/api/runs");
  const board = useApi<{ panels: { panel_id: string; items: Record<string, unknown>[]; status: string }[] }>(
    project ? `/api/projects/${encodeURIComponent(project)}/dashboard` : null,
  );
  const failing = (board.data?.panels.find((p) => p.panel_id === "failing_ci")?.items ?? []).filter((i) => !("note" in i));
  const investigations = (runs.data?.runs ?? []).filter((r) => r.reports.some((rep) => rep.report_type === "investigation")).slice(0, 5);
  return (
    <>
      <div className="kpis">
        <KpiTile icon="power" tone={failing.length ? "critical" : "good"} label="Failing CI runs" value={board.data ? failing.length : "—"} hint={<Link to="/dashboard">Open the live dashboard</Link>} />
        <KpiTile icon="compass" tone="teal" label="Investigations" value={runs.data ? investigations.length : "—"} hint={<Link to="/run-now">Investigate a failure</Link>} />
      </div>
      <Section title="Recent investigations" icon="compass">
        {investigations.length === 0 ? <Empty action={{ to: "/run-now", label: "Investigate a failure" }}>No investigations yet.</Empty> : null}
        <ul className="list">
          {investigations.map((r) => (
            <li key={r.run_id}><span className="grow"><Link to={`/runs/${encodeURIComponent(r.run_id)}`}>{runTitle(r)}</Link></span><StatusPill status={r.run_status} /></li>
          ))}
        </ul>
      </Section>
    </>
  );
}

function AuditFocus() {
  const health = useApi<Health>("/api/health");
  const containment = useApi<ContainmentView>("/api/containment");
  const notes = useApi<{ notifications: Notification[] }>("/api/notifications");
  if (health.isLoading) return <Loading what="system status" />;
  const tripped = (containment.data?.switches ?? []).filter((s) => s.state !== "ok").length;
  const critical = (notes.data?.notifications ?? []).filter((n) => n.severity === "critical" && !n.acknowledged).length;
  const parts = Object.values(health.data?.components ?? {});
  return (
    <div className="kpis">
      <KpiTile icon="gauge" tone={parts.every((v) => v === "connected") ? "good" : "warn"} label="System health" value={`${parts.filter((v) => v === "connected").length} of ${parts.length}`} hint={<Link to="/system">System</Link>} />
      <KpiTile icon="power" tone={tripped ? "critical" : "good"} label="Open incidents" value={containment.data ? tripped : "—"} hint={<Link to="/containment">Kill switches</Link>} />
      <KpiTile icon="bell" tone={critical ? "critical" : "good"} label="Critical alerts" value={notes.data ? critical : "—"} hint="unacknowledged" />
      <KpiTile icon="bell" tone={(health.data?.notifier?.dead_letters ?? 0) ? "warn" : "good"} label="Failed deliveries" value={health.data?.notifier?.dead_letters ?? 0} hint="outside channels" />
    </div>
  );
}

export function RoleFocus({ home }: { home: string | undefined }) {
  switch (home) {
    case "pm_home":
      return <ForecastFocus />;
    case "em_home":
      return <EmFocus />;
    case "architect_home":
      return <ArchitectFocus />;
    case "security_home":
      return <SecurityFocus />;
    case "oncall_home":
      return <OncallFocus />;
    case "audit_home":
      return <AuditFocus />;
    default:
      return null;
  }
}
