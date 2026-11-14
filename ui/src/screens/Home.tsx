import type { ContainmentView, Envelope, Health, Notification, QueueEntry, RunView } from "../api/types";
import { useApi } from "../app/context";
import { DataModeBadge, Empty, ErrorView, KpiTile, Loading, Section, StatusPill, Time } from "../components/common";
import { Link } from "../lib/router";
import { useCurrentProject, type ActivityItem } from "./Dashboard";

export interface SnapshotItem {
  snapshot_id: string;
  kind?: string;
  release_or_case?: string;
  release?: string;
  cutoff?: string;
  alias_variant?: string | null;
  records?: number;
  data_mode?: string;
}

const GROUPS: { kind: string; title: string; help: string }[] = [
  { kind: "releases", title: "Releases", help: "" },
  { kind: "cases", title: "Test cases", help: "" },
  { kind: "releases-aliased", title: "Releases, names hidden", help: "" },
  { kind: "cases-aliased", title: "Test cases, names hidden", help: "" },
];

export function groupSnapshots(snaps: SnapshotItem[]) {
  const known = new Set(GROUPS.map((g) => g.kind));
  const groups = GROUPS.map((g) => ({ ...g, items: snaps.filter((s) => s.kind === g.kind) })).filter((g) => g.items.length);
  const other = snaps.filter((s) => !s.kind || !known.has(s.kind));
  if (other.length) groups.push({ kind: "other", title: "Frozen from live data", help: "", items: other });
  return groups;
}

function DataAvailable() {
  const snaps = useApi<{ snapshots: SnapshotItem[] } & Envelope>("/api/snapshots");
  if (snaps.isLoading) return <Loading what="snapshots" />;
  if (snaps.error) return <ErrorView error={snaps.error} />;
  const list = snaps.data?.snapshots ?? [];
  if (list.length === 0)
    return <Empty action={{ to: "/run-now", label: "Open Run now" }}>No data sets yet.</Empty>;
  return (
    <>
      {groupSnapshots(list).map((g) => (
        <div className="data-group" key={g.kind}>
          <h3>
            {g.title} <span className="pill pill-neutral num">{g.items.length}</span>
          </h3>
          {g.items.map((s) => (
            <div className="snap-row" key={s.snapshot_id}>
              <span className="snap-id">{s.snapshot_id}</span>
              <span className="muted num">{typeof s.records === "number" ? `${s.records.toLocaleString()} records` : ""}</span>
              {g.kind === "releases" ? (
                <Link to={`/run-now?snapshot=${encodeURIComponent(s.snapshot_id)}`} className="btn btn-sm btn-secondary">Release close</Link>
              ) : (
                <DataModeBadge mode={s.data_mode ?? "REAL"} />
              )}
            </div>
          ))}
        </div>
      ))}
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
  const health = useApi<Health>("/api/health");
  const runs = useApi<{ runs: RunView[] } & Envelope>("/api/runs");
  const notes = useApi<{ notifications: Notification[] }>("/api/notifications");
  const queue = useApi<{ entries: QueueEntry[] }>("/api/queue");
  const containment = useApi<ContainmentView>("/api/containment");
  const snaps = useApi<{ snapshots: SnapshotItem[] }>("/api/snapshots");

  const runList = runs.data?.runs ?? [];
  const tripped = (containment.data?.switches ?? []).filter((s) => s.state !== "ok").length;
  const unread = (notes.data?.notifications ?? []).filter((n) => !n.acknowledged).length;
  const releaseSnaps = (snaps.data?.snapshots ?? []).filter((s) => s.kind === "releases");
  const records = (snaps.data?.snapshots ?? []).reduce((n, s) => n + (s.records ?? 0), 0);

  return (
    <>
      <div className="kpis">
        <KpiTile icon="database" label="Data sets" value={snaps.data ? snaps.data.snapshots.length : "—"} hint={snaps.data ? `${records.toLocaleString()} records · ${releaseSnaps.length} releases` : undefined} />
        <KpiTile icon="list" tone="teal" label="Runs" value={runs.data ? runList.length : "—"} hint={runs.data ? `${runList.filter((r) => r.run_status === "complete").length} done` : undefined} />
        <KpiTile icon="hand" tone={queue.data && queue.data.entries.length ? "warn" : "good"} label="Needs a person" value={queue.data ? queue.data.entries.length : "—"} hint="waiting for a decision" />
        <KpiTile icon="power" tone={tripped ? "critical" : "good"} label="Kill switches tripped" value={containment.data ? tripped : "—"} hint={containment.error ? "not visible to your role" : `of ${containment.data?.switches.length ?? 0} switches`} />
        <KpiTile icon="bell" tone={unread ? "warn" : "good"} label="Unread notifications" value={notes.data ? unread : "—"} hint={<Link to="/notifications">Inbox</Link>} />
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
              {runList.slice(0, 6).map((r) => (
                <li key={r.run_id}>
                  <span className="grow"><Link to={`/runs/${encodeURIComponent(r.run_id)}`}>{r.run_id}</Link></span>
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
                <li key={s}><span className="grow">{s}</span></li>
              ))}
            </ul>
            {health.data?.unevaluated?.length ? (
              <>
                <h3>Not evaluated in this build</h3>
                <ul>{health.data.unevaluated.map((s) => <li key={s}>{s}</li>)}</ul>
              </>
            ) : null}
          </Section>
        </div>
      </div>
    </>
  );
}
