import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import type { LiveEvent } from "../api/stream";
import type { Envelope, RunView } from "../api/types";
import { useAction, useApi, useLiveStream } from "../app/context";
import { ActionResult, AgentName, ErrorView, Loading, Section } from "../components/common";
import { Link } from "../lib/router";

export function RunList() {
  const data = useApi<{ runs: RunView[] } & Envelope>("/api/runs");
  return (
    <Section title="Runs" mode={data.data?.data_mode}>
      {data.isLoading ? <Loading what="runs" /> : <ErrorView error={data.error} />}
      {data.data && data.data.runs.length === 0 ? <p>No runs yet.</p> : null}
      <table>
        <thead>
          <tr><th>Run</th><th>Status</th><th>Started by</th><th>Snapshot</th></tr>
        </thead>
        <tbody>
          {(data.data?.runs ?? []).map((r) => (
            <tr key={r.run_id}>
              <td><Link to={`/runs/${encodeURIComponent(r.run_id)}`}>{r.run_id}</Link></td>
              <td>{r.run_status}</td>
              <td>{describeStarter(r)}</td>
              <td>{r.snapshot_id}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  );
}

export function describeStarter(r: Pick<RunView, "started_by">): string {
  const s = r.started_by;
  if (!s) return "unknown";
  if (s.kind === "person") return `a person (${s.role ?? "unknown role"})`;
  if (s.kind === "watcher") return `watcher ${s.watcher_id ?? ""}`.trim();
  return "schedule";
}

/** Events that make cached run or report data out of date. */
const INVALIDATING = new Set([
  "run.status_changed",
  "run.generation_changed",
  "report.superseded",
  "report.corrected",
  "report.withdrawn",
  "task.state_changed",
  "approval.voided",
]);

export function RunDetail({ runId }: { runId: string }) {
  const path = `/api/runs/${encodeURIComponent(runId)}`;
  const run = useApi<RunView>(path);
  const qc = useQueryClient();
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const reverify = useAction<Record<string, never>, { authoritative_generation?: number }>(`${path}/reverify`);
  const handlers = useRef({
    onEvent: (e: LiveEvent) => {
      setEvents((prev) => [...prev.slice(-199), e]);
      if (INVALIDATING.has(e.event)) void qc.invalidateQueries();
      if (e.event === "run.generation_changed") setNotice("A newer verification replaced the earlier one. Reports were refreshed.");
    },
    onReset: () => {
      setEvents([]);
      setNotice("Live updates restarted. Everything on this page was reloaded.");
      void qc.invalidateQueries();
    },
  });
  const status = useLiveStream(`${path}/stream`, (e) => handlers.current.onEvent(e), () => handlers.current.onReset());

  if (run.isLoading) return <Loading what="the run" />;
  if (run.error) return <ErrorView error={run.error} />;
  const r = run.data;
  if (!r) return null;
  return (
    <div className="grid">
      <Section title={`Run ${r.run_id}`} mode={r.data_mode}>
        {status !== "live" ? <p className="banner">Live updates paused. Reconnecting…</p> : null}
        {notice ? <p className="banner">{notice}</p> : null}
        <dl className="facts">
          <dt>Status</dt><dd>{r.run_status}{r.hold_reason ? ` (held: ${r.hold_reason})` : ""}</dd>
          <dt>Started by</dt><dd>{describeStarter(r)}</dd>
          <dt>Snapshot</dt><dd>{r.snapshot_id}</dd>
          <dt>Verification</dt>
          <dd>
            {r.authoritative_generation ? `generation ${r.authoritative_generation}` : "not verified yet"}
            {r.superseded_generations.length ? ` (earlier: ${r.superseded_generations.join(", ")}, kept for audit only)` : ""}
          </dd>
          {r.error ? (<><dt>Error</dt><dd>{r.error}</dd></>) : null}
        </dl>
        <p>
          Investigated by <AgentName agent="scout" />; every claim checked by <AgentName agent="sentinel" />.
        </p>
        <h3>Reports</h3>
        {r.reports.length === 0 ? <p>No reports yet.</p> : null}
        <ul>
          {r.reports.map((rep) => (
            <li key={rep.report_id}>
              <Link to={`/reports/${encodeURIComponent(rep.report_id)}`}>{rep.report_type}</Link> (version {rep.report_version})
            </li>
          ))}
        </ul>
        <button onClick={() => void reverify.run({})}>Check the claims again</button>
        <ActionResult state={reverify.state} retry={() => void reverify.retrySame()} />
      </Section>
      <Section title="Live progress">
        {events.length === 0 ? <p>No events yet.</p> : null}
        <ol className="events">
          {events.map((e) => (
            <li key={e.event_id || `${e.event}-${e.emitted_at}`}>
              <code>{e.event}</code>
              {typeof e.data.agent === "string" ? <> — <AgentName agent={e.data.agent} /></> : null}
              {typeof e.data.text === "string" ? <> — {e.data.text}</> : null}
            </li>
          ))}
        </ol>
      </Section>
    </div>
  );
}
