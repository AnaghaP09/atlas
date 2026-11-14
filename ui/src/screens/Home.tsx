import type { Envelope, Health, Notification, RunView } from "../api/types";
import { useApi } from "../app/context";
import { DataModeBadge, ErrorView, Loading, Section, Time } from "../components/common";
import { Link } from "../lib/router";

export function Home() {
  const health = useApi<Health>("/api/health");
  const runs = useApi<{ runs: RunView[] } & Envelope>("/api/runs");
  const notes = useApi<{ notifications: Notification[] }>("/api/notifications");
  return (
    <div className="grid">
      <Section title="System">
        {health.isLoading ? <Loading what="status" /> : <ErrorView error={health.error} />}
        {health.data ? (
          <dl className="facts">
            <dt>Mode</dt>
            <dd>{health.data.mode === "offline" ? "Offline (no outside connections)" : "Connected"}</dd>
            <dt>Server</dt>
            <dd>{health.data.environment}</dd>
            <dt>Components</dt>
            <dd>
              <ul className="plain">
                {Object.entries(health.data.components).map(([k, v]) => (
                  <li key={k}>
                    {k}: {v === "not_connected" ? <DataModeBadge mode="STAGED" reason="not connected in this build yet" /> : v}
                  </li>
                ))}
              </ul>
            </dd>
          </dl>
        ) : null}
      </Section>
      <Section title="Not available in this demo" mode="STAGED">
        <ul>{(health.data?.staged ?? []).map((s) => <li key={s}>{s}</li>)}</ul>
        {health.data?.unevaluated?.length ? (
          <>
            <h3>Not evaluated in this build</h3>
            <ul>{health.data.unevaluated.map((s) => <li key={s}>{s}</li>)}</ul>
          </>
        ) : null}
      </Section>
      <Section title="Recent runs" mode={runs.data?.data_mode}>
        {runs.isLoading ? <Loading what="runs" /> : <ErrorView error={runs.error} />}
        {runs.data && runs.data.runs.length === 0 ? <p>No runs yet. Start one from Run now.</p> : null}
        <ul>
          {(runs.data?.runs ?? []).slice(0, 8).map((r) => (
            <li key={r.run_id}>
              <Link to={`/runs/${encodeURIComponent(r.run_id)}`}>{r.run_id}</Link> — {r.run_status}
            </li>
          ))}
        </ul>
      </Section>
      <Section title="Notifications">
        {notes.isLoading ? <Loading what="notifications" /> : <ErrorView error={notes.error} />}
        <p>
          {(notes.data?.notifications ?? []).filter((n) => !n.acknowledged).length} unread.{" "}
          <Link to="/notifications">Open the inbox</Link>
        </p>
        <ul>
          {(notes.data?.notifications ?? []).slice(0, 5).map((n) => (
            <li key={n.notification_id}>
              <span className={`sev sev-${n.severity}`}>{n.severity}</span> {n.message} <Time iso={n.created_at} />
            </li>
          ))}
        </ul>
      </Section>
    </div>
  );
}
