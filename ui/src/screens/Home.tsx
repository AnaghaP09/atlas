import type { Envelope, Health, RunView } from "../api/types";
import { useApi } from "../app/context";
import { DataModeBadge, Empty, ErrorView, KpiTile, Loading, Section, StatusPill, Time } from "../components/common";
import { Link } from "../lib/router";
import { runTitle } from "../lib/labels";
import { useCurrentProject, type ActivityItem } from "./Dashboard";
import { LiveNumber, useMetrics } from "../lib/metrics";
import type { Navigation } from "../lib/nav";
import { RoleFocus } from "./RoleHome";
import { DATASET_GROUPS, kindOf, visibleDatasets } from "../components/dataset";

export interface SnapshotItem {
  snapshot_id: string;
  kind?: string;
  release_or_case?: string;
  release?: string;
  cutoff?: string;
  alias_variant?: string | null;
  records?: number;
  data_mode?: string;
  title?: string;
  display_kind?: "release" | "case" | "live" | string;
  is_test_copy?: boolean;
  as_of?: string | null;
  as_of_label?: string;
  open_issues?: number | null;
  linked_issues?: number | null;
}

function DataAvailable() {
  const snaps = useApi<{ snapshots: SnapshotItem[] } & Envelope>("/api/snapshots");
  if (snaps.isLoading) return <Loading what="data sets" />;
  if (snaps.error) return <ErrorView error={snaps.error} />;
  const list = visibleDatasets(snaps.data?.snapshots ?? []);
  if (list.length === 0) return <Empty action={{ to: "/run-now", label: "Open Run now" }}>No data sets yet.</Empty>;
  return (
    <>
      {DATASET_GROUPS.map((g) => {
        const items = list.filter((s) => kindOf(s) === g.kind);
        if (!items.length) return null;
        return (
          <div className="data-group" key={g.kind}>
            <h3>
              {g.title} <span className="pill pill-neutral num">{items.length}</span>
            </h3>
            {items.map((s) => (
              <div className="snap-row" key={s.snapshot_id}>
                <span>
                  <strong>{s.title ?? "Data set"}</strong>
                  <span className="muted"> · as of {s.as_of_label ?? "—"}</span>
                </span>
                <span className="muted num">
                  {g.kind === "release" && typeof s.open_issues === "number" ? `${s.open_issues} open issues` : null}
                  {g.kind === "case" && typeof s.linked_issues === "number" ? `${s.linked_issues} linked issues` : null}
                </span>
                {g.kind === "release" ? (
                  <Link to={`/run-now?snapshot=${encodeURIComponent(s.snapshot_id)}`} className="btn btn-sm btn-secondary">Release close</Link>
                ) : (
                  <DataModeBadge mode={s.data_mode ?? "REAL"} />
                )}
              </div>
            ))}
          </div>
        );
      })}
    </>
  );
}

function ActivityFeed({ project }: { project: string }) {
  const act = useApi<{ activity: ActivityItem[] }>(`/api/projects/${encodeURIComponent(project)}/activity`);
  if (act.isLoading) return <Loading what="activity" lines={2} />;
  if (act.error) return <ErrorView error={act.error} />;
  const items = (act.data?.activity ?? []).slice(0, 8);
  if (items.length === 0)
    return <Empty action={{ to: "/dashboard", label: "Live dashboard" }}>No live activity yet.</Empty>;
  return (
    <ul className="feed">
      {items.map((a, i) => (
        <li key={a.activity_id ?? `${a.record_id}-${i}`}>
          <span className={`dot ${a.kind}`} aria-hidden="true" />
          <span>
            <span className="muted">{a.kind}</span> {a.title}
          </span>
          {a.at ? <Time iso={a.at} /> : <span />}
        </li>
      ))}
    </ul>
  );
}

export function Home() {
  const project = useCurrentProject();
  const m = useMetrics();
  const nav = useApi<Navigation>("/api/navigation");
  const health = useApi<Health>("/api/health");
  const runs = useApi<{ runs: RunView[] } & Envelope>("/api/runs");
  const snaps = useApi<{ snapshots: SnapshotItem[] }>("/api/snapshots");

  const runList = runs.data?.runs ?? [];
  const visible = visibleDatasets(snaps.data?.snapshots ?? []);
  const releaseSnaps = visible.filter((s) => kindOf(s) === "release");
  const latest = releaseSnaps.slice().sort((a, b) => String(b.as_of ?? "").localeCompare(String(a.as_of ?? "")))[0];

  return (
    <>
      <RoleFocus home={nav.data?.home} />
      <div className="kpis">
        <KpiTile icon="database" label="Open issues at latest release" value={latest && typeof latest.open_issues === "number" ? latest.open_issues : "—"} hint={latest ? `${latest.title} · ${latest.as_of_label}` : undefined} />
        <KpiTile icon="list" tone="teal" label="Runs" value={<LiveNumber value={m.data?.runs_total} />} hint={m.data?.runs_by_status ? `${m.data.runs_by_status.complete ?? 0} done` : undefined} updated={m.data?.computed_at} />
        <KpiTile icon="hand" tone={m.data?.needs_a_person ? "warn" : "good"} label="Needs a person" value={<LiveNumber value={m.data?.needs_a_person} />} updated={m.data?.computed_at} />
        <KpiTile icon="power" tone={m.data?.kill_switches_tripped ? "critical" : "good"} label="Kill switches tripped" value={<LiveNumber value={m.data?.kill_switches_tripped} />} hint={typeof m.data?.kill_switches_total === "number" ? `of ${m.data.kill_switches_total}` : undefined} updated={m.data?.computed_at} />
        <KpiTile icon="bell" tone={m.data?.unread_notifications ? "warn" : "good"} label="Unread notifications" value={<LiveNumber value={m.data?.unread_notifications} />} hint={<Link to="/notifications">Inbox</Link>} updated={m.data?.computed_at} />
      </div>

      <div className="grid-2">
        <div>
          <Section title="Data sets" icon="database" mode={snaps.data ? "REAL" : undefined} >
            <DataAvailable />
          </Section>
        </div>
        <div>
          <Section title="Recent runs" icon="list" end={<Link to="/runs">All runs</Link>}>
            {runs.isLoading ? <Loading what="runs" /> : <ErrorView error={runs.error} />}
            {runs.data && runList.length === 0 ? (
              <Empty action={{ to: "/run-now", label: "Start a release close" }}>No runs yet.</Empty>
            ) : null}
            <ul className="list">
              {runList.slice().sort((a, b) => String(b.started_at ?? "").localeCompare(String(a.started_at ?? ""))).slice(0, 6).map((r) => (
                <li key={r.run_id}>
                  <span className="grow">
                    <Link to={`/runs/${encodeURIComponent(r.run_id)}`} className="row-title">{r.kind_label ?? runTitle(r)}</Link>
                    <span className="row-sub">{[r.subject, null].filter(Boolean).join(" ")}{r.subject && r.started_at ? " · " : ""}<Time iso={r.started_at} short /></span>
                  </span>
                  <StatusPill status={r.run_status} />
                </li>
              ))}
            </ul>
          </Section>
          <Section title="Live activity" icon="pulse">
            {project ? <ActivityFeed project={project} /> : <Empty>No project is configured on this server yet.</Empty>}
          </Section>
          <Section title="Not in this demo" icon="shield">
            {health.isLoading ? <Loading what="status" lines={2} /> : <ErrorView error={health.error} />}
            <ul className="list">
              {(health.data?.staged ?? []).map((s) => (
                <li key={s}><span className="grow">{s.replace(/^./, (c) => c.toUpperCase())}</span></li>
              ))}
            </ul>
            {health.data?.unevaluated?.length ? (
              <>
                <h3>Not evaluated in this build</h3>
                <ul>{health.data.unevaluated.map((s) => <li key={s}>{s.replace(/^./, (c) => c.toUpperCase())}</li>)}</ul>
              </>
            ) : null}
          </Section>
        </div>
      </div>
    </>
  );
}
