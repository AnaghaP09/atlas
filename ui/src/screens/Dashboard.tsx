import { useState } from "react";
import type { DataMode } from "../api/types";
import type { LiveEvent } from "../api/stream";
import { useApi, useApp, useIdentity, useLiveStream } from "../app/context";
import { BarList, Sparkline, type Point } from "../components/charts";
import { DataModeBadge, Empty, ErrorView, Loading, Section, Time } from "../components/common";
import type { IconName } from "../components/icons";
import { Link } from "../lib/router";

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

  const status = useLiveStream(project ? `/api/projects/${encodeURIComponent(project)}/stream` : null, onEvent, onReset);

  if (!project) return <Empty>No project is configured on this server yet. Add one to the projects file and restart ATLAS.</Empty>;
  if (first.isLoading && !board) return <Loading what="the dashboard" />;
  if (first.error && !board) return <ErrorView error={first.error} />;
  if (!board) return null;
  const issues = board.panels.find((p) => p.panel_id === "open_issues");
  const ci = board.panels.find((p) => p.panel_id === "failing_ci");
  return (
    <>
      <Section title={`Project ${project}`} icon="pulse" mode={board.data_mode} end={<span className={`mode-pill${status === "live" ? "" : " offline"}`}><span className="pulse" aria-hidden="true" />{status === "live" ? "Live updates on" : "Live updates paused"}</span>}>
        {board.data_source_note ? <p className="muted">{board.data_source_note}</p> : null}
        <p className="poll-ticker" data-testid="poll-line">
          Updated every {board.interval_seconds} s, last poll{" "}
          {board.last_poll ? <Time iso={board.last_poll} /> : "not yet"} · live updates: {status === "live" ? "on" : "paused"}
        </p>
      </Section>
      <div className="dash-grid">
        <Section title="Open issues seen on this page" icon="chart">
          <IssueTrend count={issues?.items.filter(isItem).length ?? 0} version={board.dashboard_version} />
        </Section>
        <Section title="Failing CI runs by workflow" icon="chart">
          <CiBars items={(ci?.items ?? []).filter(isItem)} note={(ci?.items ?? []).find((i) => !isItem(i))} />
        </Section>
        {board.panels.map((p) => (
          <Section key={p.panel_id} title={p.title} icon={PANEL_ICON[p.panel_id] ?? "list"} mode={p.status === "live" ? "REAL" : "STAGED"} reason={p.reason ?? null}>
            {p.items.length === 0 ? (
              <p className="muted">{p.status === "live" ? "Nothing here right now." : "No data yet. This panel fills in after the first successful poll."}</p>
            ) : (
              <ul className="panel-items">
                {p.items.map((item, i) => (
                  <PanelItem key={i} item={item} />
                ))}
              </ul>
            )}
          </Section>
        ))}
        <Section title="Activity" icon="list">
          {activity.length === 0 ? <p className="muted">No activity yet. New records appear here as soon as a poll finds them.</p> : null}
          <ul className="feed" data-testid="activity">
            {activity.map((a, i) => {
              const to = evidenceRoute({ ...a, project_id: a.project_id ?? project });
              return (
                <li key={a.activity_id ?? `${a.record_id}-${i}`}>
                  <span className={`dot ${a.kind}`} aria-hidden="true" />
                  <span>
                    <span className="muted">{a.kind}</span> {to ? <Link to={to}>{a.title}</Link> : a.title}
                  </span>
                  {a.at ? <Time iso={a.at} /> : <span />}
                </li>
              );
            })}
          </ul>
        </Section>
      </div>
    </>
  );
}

const PANEL_ICON: Record<string, IconName> = { open_issues: "list", failing_ci: "power", latest_release: "database", security: "shield" };

/** A panel row is a real item unless it is only a note from the server. */
function isItem(item: Record<string, unknown>): boolean {
  return !("note" in item) || Object.keys(item).length > 1;
}

function PanelItem({ item }: { item: Record<string, unknown> }) {
  if ("note" in item && Object.keys(item).length === 1) return <li className="panel-note">{String(item.note)}</li>;
  const number = item.number ?? item.run_id ?? item.tag ?? null;
  const title = String(item.title ?? item.name ?? item.tag_name ?? JSON.stringify(item));
  const labels = Array.isArray(item.labels) ? (item.labels as unknown[]).map((l) => (typeof l === "string" ? l : String((l as { name?: string }).name ?? ""))) : [];
  return (
    <li>
      {number !== null ? <span className="num">{typeof item.number === "number" ? `#${String(number)}` : String(number)}</span> : null}
      <span>
        {title}
        {typeof item.conclusion === "string" ? <> <span className="pill pill-critical">{item.conclusion}</span></> : null}
        {labels.length ? (
          <span> {labels.slice(0, 3).map((l) => <span key={l} className="chip">{l}</span>)}</span>
        ) : null}
      </span>
    </li>
  );
}

function IssueTrend({ count, version }: { count: number; version: number }) {
  // Readings are kept for this page only: the server does not store a history of panel sizes.
  const [points, setPoints] = useState<Point[]>([]);
  const [seen, setSeen] = useState<number | null>(null);
  if (seen !== version) {
    setSeen(version);
    setPoints((prev) => [...prev, { label: new Date().toLocaleTimeString(), value: count }].slice(-40));
  }
  return (
    <>
      <Sparkline points={points} unit="open issues" />
      <p className="muted">Counted at each dashboard update since this page opened.</p>
    </>
  );
}

function CiBars({ items, note }: { items: Record<string, unknown>[]; note?: Record<string, unknown> }) {
  const counts = new Map<string, number>();
  for (const it of items) {
    const name = String(it.name ?? "workflow");
    counts.set(name, (counts.get(name) ?? 0) + 1);
  }
  const rows = [...counts.entries()].map(([label, value]) => ({ label, value })).sort((a, b) => b.value - a.value);
  if (rows.length === 0)
    return <p className="muted">{note && typeof note.note === "string" ? note.note : "No failing runs in the latest poll."}</p>;
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
  return (
    <Section title={`Record ${recordId}`} mode={record.data?.data_mode}>
      <p className="muted">
        Version {String(r.content_version_id ?? version)} · project {project} <DataModeBadge mode={record.data?.data_mode} />
      </p>
      <pre className="record-text">{String(r.text ?? r.title ?? "")}</pre>
    </Section>
  );
}
