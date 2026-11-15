import { useState } from "react";
import type { DataMode } from "../api/types";
import type { LiveEvent } from "../api/stream";
import { useApi, useApp, useIdentity, useLiveStream } from "../app/context";
import { BarList } from "../components/charts";
import { DataModeBadge, Empty, ErrorView, KpiTile, Loading, Pagination, Section, Time, usePaged } from "../components/common";
import { Link } from "../lib/router";
import { EvidenceCard } from "./Reports";

export interface DashboardPanel {
  panel_id: string;
  title: string;
  status: "live" | "not_connected" | string;
  items: Record<string, unknown>[];
  updated_at: string | null;
  reason?: string;
}

export interface DashboardData {
  project_id: string;
  dashboard_version: number;
  last_poll: string | null;
  interval_seconds: number;
  panels: DashboardPanel[];
  data_mode: DataMode;
  data_source_note?: string;
}

export interface ActivityItem {
  activity_id?: string;
  project_id?: string;
  kind: string;
  title: string;
  record_id?: string | null;
  content_version_id?: string | null;
  link?: string;
  at?: string;
}

interface ProjectList {
  projects: { id: string; name: string; data_source_note?: string }[];
  current: string | null;
}

/** The project the screens act in: the selected one, else the server's current project. */
export function useCurrentProject(): string | null {
  const { projectId } = useIdentity();
  const list = useApi<ProjectList>(projectId ? null : "/api/projects");
  return projectId ?? list.data?.current ?? null;
}

/** Merge a fresh dashboard into the shown one, replacing only the panels that changed. */
export function mergePanels(shown: DashboardData | null, fresh: DashboardData, changed: string[] | null): DashboardData {
  if (!shown || changed === null) return fresh;
  const keep = new Set(changed);
  return {
    ...fresh,
    panels: shown.panels.map((p) => (keep.has(p.panel_id) ? (fresh.panels.find((f) => f.panel_id === p.panel_id) ?? p) : p)),
  };
}

function evidenceRoute(item: ActivityItem): string | null {
  if (!item.record_id || !item.project_id) return null;
  const q = new URLSearchParams({ project: item.project_id, version: item.content_version_id ?? "" });
  return `/activity-evidence/${encodeURIComponent(item.record_id)}?${q.toString()}`;
}

export function Dashboard() {
  const project = useCurrentProject();
  const { client } = useApp();
  const first = useApi<DashboardData>(project ? `/api/projects/${encodeURIComponent(project)}/dashboard` : null);
  const firstActivity = useApi<{ activity: ActivityItem[] }>(project ? `/api/projects/${encodeURIComponent(project)}/activity` : null);
  // What the stream changed since the first read. The screen shows these over the first read.
  const [liveBoard, setLiveBoard] = useState<DashboardData | null>(null);
  const [liveActivity, setLiveActivity] = useState<ActivityItem[] | null>(null);
  const [newActivity, setNewActivity] = useState<ActivityItem[]>([]);
  const board = liveBoard ?? first.data ?? null;
  const activity = [...newActivity, ...(liveActivity ?? firstActivity.data?.activity ?? [])].slice(0, 100);

  async function refetch(changed: string[] | null) {
    if (!project) return;
    const fresh = await client.get<DashboardData>(`/api/projects/${encodeURIComponent(project)}/dashboard`);
    // Without an earlier live copy the fresh read is the whole current dashboard.
    setLiveBoard((prev) => mergePanels(prev, fresh, changed));
  }

  function onEvent(e: LiveEvent) {
    if (e.event === "project.dashboard_changed") {
      const changed = Array.isArray(e.data.changed_panels) ? (e.data.changed_panels as string[]) : null;
      void refetch(changed);
    } else if (e.event === "project.activity") {
      setNewActivity((prev) => [e.data as unknown as ActivityItem, ...prev].slice(0, 100));
    }
  }

  function onReset() {
    void refetch(null);
    if (project)
      void client.get<{ activity: ActivityItem[] }>(`/api/projects/${encodeURIComponent(project)}/activity`).then((a) => {
        setNewActivity([]);
        setLiveActivity(a.activity);
      });
  }

  const projects = useApi<ProjectList>("/api/projects");
  const nav = useApi<{ groups: { pages: { page_id: string }[] }[] }>("/api/navigation");
  const canInvestigate = Boolean(nav.data?.groups.some((g) => g.pages.some((x) => x.page_id === "run_now")));
  const status = useLiveStream(project ? `/api/projects/${encodeURIComponent(project)}/stream` : null, onEvent, onReset);

  if (!project) return <Empty>No project is configured on this server yet. Add one to the projects file and restart ATLAS.</Empty>;
  if (first.isLoading && !board) return <Loading what="the dashboard" />;
  if (first.error && !board) return <ErrorView error={first.error} />;
  if (!board) return null;
  const panel = (id: string) => board.panels.find((p) => p.panel_id === id);
  const issues = panel("open_issues");
  const ci = panel("failing_ci");
  const release = panel("latest_release");
  const security = panel("security");
  const issueItems = (issues?.items ?? []).filter(isItem);
  const ciItems = (ci?.items ?? []).filter(isItem);
  const ciNote = (ci?.items ?? []).find((i) => !isItem(i));
  const ciPolled = Boolean(ci && ci.status === "live" && !ciNote);
  const releaseItem = (release?.items ?? []).filter(isItem)[0];
  const projectName = projects.data?.projects.find((x) => x.id === project)?.name ?? project;
  return (
    <>
      <div className="dash-status">
        <strong>{projectName}</strong>
        <DataModeBadge mode={board.data_mode} />
        {board.last_poll ? (
          <span className={`mode-pill${status === "live" ? "" : " offline"}`}>
            <span className="pulse" aria-hidden="true" />
            {status === "live" ? "Live" : "Paused"}
          </span>
        ) : (
          <span className="pill pill-neutral" title="No poll has finished yet">Waiting for first poll</span>
        )}
        <span className="muted" data-testid="poll-line">
          Updated every {board.interval_seconds} s, last poll {board.last_poll ? <Time iso={board.last_poll} short /> : "not yet"}
        </span>
      </div>
      <div className="kpis">
        <KpiTile icon="list" label="Open bugs and flaky tests" value={issueItems.length} hint="stored from live polls" />
        <KpiTile
          icon="power"
          tone={ciPolled && ciItems.length ? "critical" : "good"}
          label="Failing CI runs"
          value={ciPolled ? ciItems.length : <span className="pill pill-neutral" title={typeof ciNote?.note === "string" ? ciNote.note : "CI is not polled for this project"}>Not polled</span>}
          small={!ciPolled}
        />
        <KpiTile
          icon="database"
          tone="teal"
          label="Latest release"
          value={releaseItem ? String(releaseItem.tag ?? releaseItem.tag_name ?? releaseItem.name ?? "—") : "—"}
          small
          hint={releaseItem && typeof releaseItem.published_at === "string" ? <Time iso={releaseItem.published_at} short /> : undefined}
        />
        <KpiTile
          icon="shield"
          tone="warn"
          label="Security findings"
          value={security && security.status === "live" ? security.items.filter(isItem).length : <span className="pill pill-neutral" title={security?.reason ?? "The live security scan is not connected in this build"}>Not in this demo</span>}
          small={!(security && security.status === "live")}
        />
      </div>
      <div className="grid-2">
        <div>
          <Section title="Open issues" icon="list" mode={issues?.status === "live" ? "REAL" : "STAGED"} end={<span className="muted num">{issueItems.length} shown</span>}>
            {issueItems.length === 0 ? (
              <Empty>No open issues stored yet. They appear after the next poll.</Empty>
            ) : (
              <IssueTable items={issueItems} project={project} canInvestigate={canInvestigate} />
            )}
          </Section>
        </div>
        <div>
          <Section title="Open issues by area" icon="chart">
            <AreaBars items={issueItems} />
          </Section>
          <Section title="Failing CI by workflow" icon="power">
            {ciPolled ? (
              <CiBars items={ciItems} />
            ) : (
              <p className="quiet-note"><span className="pill pill-neutral">Not polled</span> {typeof ciNote?.note === "string" ? shortNote(ciNote.note) : "CI is not polled for this project."}</p>
            )}
          </Section>
          <Section title="Activity" icon="pulse">
            {activity.length === 0 ? <p className="muted">No activity yet.</p> : null}
            <ul className="feed" data-testid="activity">
              {activity.slice(0, 12).map((a, i) => {
                const to = evidenceRoute({ ...a, project_id: a.project_id ?? project });
                return (
                  <li key={a.activity_id ?? `${a.record_id}-${i}`}>
                    <span className={`dot ${a.kind}`} aria-hidden="true" />
                    <span className="feed-text">{to ? <Link to={to}>{a.title}</Link> : a.title}</span>
                    {a.at ? <Time iso={a.at} short /> : <span />}
                  </li>
                );
              })}
            </ul>
          </Section>
        </div>
      </div>
    </>
  );
}

function shortNote(note: string): string {
  return /prow/i.test(note) ? "This project runs its CI on Prow, which ATLAS does not poll." : note;
}

function labelsOf(item: Record<string, unknown>): string[] {
  return Array.isArray(item.labels) ? (item.labels as unknown[]).map((l) => (typeof l === "string" ? l : String((l as { name?: string }).name ?? ""))).filter(Boolean) : [];
}

/** The area an issue belongs to, from its labels ("sig/network", "area/operator", "component/ui"). */
export function areaOf(item: Record<string, unknown>): string {
  const labels = labelsOf(item);
  const hit = labels.find((l) => /^(sig|area|component|comp)[/:]/i.test(l));
  return hit ? hit.replace(/^[^/:]+[/:]/, "") : "No area label";
}

function AreaBars({ items }: { items: Record<string, unknown>[] }) {
  const counts = new Map<string, number>();
  for (const it of items) counts.set(areaOf(it), (counts.get(areaOf(it)) ?? 0) + 1);
  const rows = [...counts.entries()].map(([label, value]) => ({ label, value })).sort((x, y) => y.value - x.value);
  if (!rows.length) return <p className="muted">No issues to count yet.</p>;
  const top = rows.slice(0, 7);
  const rest = rows.slice(7).reduce((n, r) => n + r.value, 0);
  return <BarList rows={rest ? [...top, { label: "Other areas", value: rest }] : top} unit="open issues" />;
}

function IssueTable({ items, project, canInvestigate }: { items: Record<string, unknown>[]; project: string; canInvestigate: boolean }) {
  const paged = usePaged(items, 12);
  return (
    <>
      <div className="table-wrap">
        <table className="table-items">
          <thead>
            <tr><th>Issue</th><th>Area</th><th>Labels</th>{canInvestigate ? <th /> : null}</tr>
          </thead>
          <tbody>
            {paged.slice.map((item, i) => {
              const number = item.number ?? null;
              const title = String(item.title ?? item.name ?? "Untitled");
              const labels = labelsOf(item).filter((l) => !/^(sig|area|component|comp)[/:]/i.test(l));
              const record = typeof item.record_id === "string" ? item.record_id : null;
              const version = typeof item.content_version_id === "string" ? item.content_version_id : "";
              const to = record ? `/activity-evidence/${encodeURIComponent(record)}?${new URLSearchParams({ project, version }).toString()}` : null;
              return (
                <tr key={String(number ?? i)}>
                  <td>
                    {to ? <Link to={to} className="row-title" title={title}>{title}</Link> : <span className="row-title" title={title}>{title}</span>}
                    {number !== null ? <span className="row-sub num">#{String(number)}</span> : null}
                  </td>
                  <td>{areaOf(item)}</td>
                  <td>{labels.slice(0, 2).map((l) => <span key={l} className="chip chip-quiet">{l}</span>)}</td>
                  {canInvestigate ? (
                    <td className="row-actions">
                      {number !== null ? <Link className="btn btn-ghost btn-sm" to={`/run-now?investigate=${encodeURIComponent(String(number))}`}>Investigate</Link> : null}
                    </td>
                  ) : null}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <Pagination page={paged.page} pages={paged.pages} setPage={paged.setPage} total={items.length} />
    </>
  );
}


/** A panel row is a real item unless it is only a note from the server. */
function isItem(item: Record<string, unknown>): boolean {
  return !("note" in item) || Object.keys(item).length > 1;
}

function CiBars({ items }: { items: Record<string, unknown>[] }) {
  const counts = new Map<string, number>();
  for (const it of items) {
    const name = String(it.name ?? "workflow");
    counts.set(name, (counts.get(name) ?? 0) + 1);
  }
  const rows = [...counts.entries()].map(([label, value]) => ({ label, value })).sort((a, b) => b.value - a.value);
  if (rows.length === 0)
    return <p className="quiet-note"><span className="pill pill-good">All passing</span> No failing runs in the latest poll.</p>;
  return <BarList rows={rows} unit="failing runs" />;
}

export function ActivityEvidence({ recordId, project, version }: { recordId: string; project: string; version: string }) {
  const record = useApi<{ record: Record<string, unknown>; data_mode?: DataMode }>(
    `/api/projects/${encodeURIComponent(project)}/evidence/${encodeURIComponent(recordId)}`,
    { content_version_id: version || null },
  );
  if (record.isLoading) return <Loading what="the record" />;
  if (record.error) return <ErrorView error={record.error} />;
  const r = record.data?.record ?? {};
  return <EvidenceCard rec={r as never} mode={record.data?.data_mode} />;
}
