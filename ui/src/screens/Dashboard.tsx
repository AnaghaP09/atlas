import { useState } from "react";
import type { DataMode } from "../api/types";
import type { LiveEvent } from "../api/stream";
import { useApi, useApp, useIdentity, useLiveStream } from "../app/context";
import { DataModeBadge, Empty, ErrorView, Loading, Section, Time } from "../components/common";
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

  if (!project) return <Empty>No project is configured on this server yet.</Empty>;
  if (first.isLoading && !board) return <Loading what="the dashboard" />;
  if (first.error && !board) return <ErrorView error={first.error} />;
  if (!board) return null;
  return (
    <div className="grid">
      <Section title={`Live dashboard: ${project}`} mode={board.data_mode}>
        {board.data_source_note ? <p className="muted">{board.data_source_note}</p> : null}
        <p data-testid="poll-line">
          Updated every {board.interval_seconds} s, last poll{" "}
          {board.last_poll ? <Time iso={board.last_poll} /> : "not yet"} · live updates: {status === "live" ? "on" : "paused"}
        </p>
      </Section>
      {board.panels.map((p) => (
        <Section key={p.panel_id} title={p.title} mode={p.status === "live" ? "REAL" : "STAGED"} reason={p.reason ?? null}>
          {p.items.length === 0 ? (
            <p className="muted">{p.status === "live" ? "Nothing here right now." : "No data yet."}</p>
          ) : (
            <ul>
              {p.items.map((item, i) => (
                <li key={i}>{String(item.title ?? item.name ?? JSON.stringify(item))}</li>
              ))}
            </ul>
          )}
        </Section>
      ))}
      <Section title="Activity">
        {activity.length === 0 ? <p className="muted">No activity yet.</p> : null}
        <ul data-testid="activity">
          {activity.map((a, i) => {
            const to = evidenceRoute({ ...a, project_id: a.project_id ?? project });
            return (
              <li key={a.activity_id ?? `${a.record_id}-${i}`}>
                <span className="muted">{a.kind}</span> {to ? <Link to={to}>{a.title}</Link> : a.title}{" "}
                {a.at ? <Time iso={a.at} /> : null}
              </li>
            );
          })}
        </ul>
      </Section>
    </div>
  );
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
