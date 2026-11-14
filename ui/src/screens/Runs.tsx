import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import type { LiveEvent } from "../api/stream";
import type { Claim, Envelope, ReportView, RunView } from "../api/types";
import { useAction, useApi, useLiveStream } from "../app/context";
import {
  ActionResult,
  AgentName,
  Button,
  Empty,
  ErrorView,
  Loading,
  Pagination,
  Section,
  StatusPill,
  UsageBar,
  usePaged,
} from "../components/common";
import { Icon, ScoutEmblem, SentinelEmblem } from "../components/icons";
import { Link } from "../lib/router";

export function RunList() {
  const data = useApi<{ runs: RunView[] } & Envelope>("/api/runs");
  const runs = data.data?.runs ?? [];
  const paged = usePaged(runs, 15);
  return (
    <Section title="All runs" icon="list" mode={data.data?.data_mode}>
      {data.isLoading ? <Loading what="runs" /> : <ErrorView error={data.error} />}
      {data.data && runs.length === 0 ? (
        <Empty action={{ to: "/run-now", label: "Start a release close" }}>No runs yet. Start a release close from Run now.</Empty>
      ) : null}
      {runs.length ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Run</th><th>Status</th><th>Started by</th><th>Snapshot</th><th className="num">Reports</th></tr>
            </thead>
            <tbody>
              {paged.slice.map((r) => (
                <tr key={r.run_id}>
                  <td className="mono"><Link to={`/runs/${encodeURIComponent(r.run_id)}`}>{r.run_id}</Link></td>
                  <td><StatusPill status={r.run_status} /></td>
                  <td>{describeStarter(r)}</td>
                  <td className="mono">{r.snapshot_id}</td>
                  <td className="num">{r.reports.length}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      <Pagination page={paged.page} pages={paged.pages} setPage={paged.setPage} total={runs.length} />
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

/** The canvas limits for one investigation. */
export const LIMITS = { steps: 30, tool_calls: 60, seconds: 15 * 60 };

interface Cost {
  steps?: number;
  tool_calls?: number;
  seconds?: number;
  usd?: number;
  input_tokens?: number;
  output_tokens?: number;
}

function Budgets({ cost }: { cost: Cost | null }) {
  if (!cost) return <p className="muted">No usage recorded yet.</p>;
  return (
    <div className="meters">
      <UsageBar label="Steps" used={cost.steps ?? 0} limit={LIMITS.steps} />
      <UsageBar label="Tool calls" used={cost.tool_calls ?? 0} limit={LIMITS.tool_calls} />
      <UsageBar label="Minutes" used={Math.round(((cost.seconds ?? 0) / 60) * 10) / 10} limit={LIMITS.seconds / 60} />
      <p className="muted num">
        Model spend ${(cost.usd ?? 0).toFixed(3)} · {(cost.input_tokens ?? 0).toLocaleString()} input tokens ·{" "}
        {(cost.output_tokens ?? 0).toLocaleString()} output tokens
      </p>
    </div>
  );
}

export function VerdictChip({ verdict }: { verdict?: string }) {
  const v = (verdict ?? "missing").toLowerCase();
  const text = v === "supported" ? "Supported" : v === "rejected" ? "Rejected" : v === "contradicted" ? "Contradicted" : "No verdict";
  return <span className={`chip verdict-${v}`}>{text}</span>;
}

function ReportVerdicts({ reportId }: { reportId: string }) {
  const rep = useApi<ReportView>(`/api/reports/${encodeURIComponent(reportId)}`);
  if (!rep.data) return null;
  const claims: Claim[] = Array.isArray(rep.data.report.claims) ? rep.data.report.claims : [];
  const counts = new Map<string, number>();
  for (const c of claims) counts.set(c.verdict ?? "missing", (counts.get(c.verdict ?? "missing") ?? 0) + 1);
  if (!claims.length) return <span className="muted">no claims</span>;
  return (
    <span>
      {[...counts.entries()].map(([v, n]) => (
        <span key={v}>
          <VerdictChip verdict={v} /> <span className="num">{n}</span>{" "}
        </span>
      ))}
    </span>
  );
}

function eventText(e: LiveEvent): string {
  const d = e.data;
  if (typeof d.text === "string") return d.text;
  if (typeof d.tool === "string") return `Called ${d.tool}${typeof d.step === "number" ? ` (step ${d.step})` : ""}`;
  if (typeof d.stage === "string") return `${d.stage}${typeof d.claims === "number" ? `: ${d.claims} claims` : ""}`;
  if (typeof d.status === "string") return `Status: ${d.status}`;
  return e.event.replace(/[._]/g, " ");
}

function Timeline({ events }: { events: LiveEvent[] }) {
  if (events.length === 0) return <Empty>No live events yet. They appear here while the run is working.</Empty>;
  return (
    <ol className="timeline" aria-live="polite">
      {events.map((e) => {
        const agent = typeof e.data.agent === "string" ? e.data.agent : null;
        return (
          <li key={e.event_id || `${e.event}-${e.emitted_at}`}>
            <span className={`tl-marker ${agent ?? ""}`} aria-hidden="true">
              {agent === "scout" ? <ScoutEmblem size={18} /> : agent === "sentinel" ? <SentinelEmblem size={18} /> : <Icon name="pulse" size={14} />}
            </span>
            <div className="tl-body">
              <span className="tl-event">{e.event}</span>
              {agent ? <> · <AgentName agent={agent} compact /></> : null}
              <p className="tl-text">{eventText(e)}</p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

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
  const cost = r.cost && typeof r.cost === "object" ? (r.cost as Cost) : null;
  return (
    <div className="grid-2">
      <div>
        <Section title={r.run_id} icon="list" mode={r.data_mode} end={<StatusPill status={r.run_status} />}>
          {status !== "live" ? <p className="banner">Live updates paused. Reconnecting…</p> : null}
          {notice ? <p className="banner banner-note">{notice}</p> : null}
          <dl className="facts">
            <dt>Status</dt><dd>{r.run_status}{r.hold_reason ? ` (held: ${r.hold_reason})` : ""}</dd>
            <dt>Started by</dt><dd>{describeStarter(r)}</dd>
            <dt>Snapshot</dt><dd className="mono">{r.snapshot_id}</dd>
            <dt>Verification</dt>
            <dd>
              {r.authoritative_generation ? `generation ${r.authoritative_generation}` : "not verified yet"}
              {r.superseded_generations.length ? ` (earlier: ${r.superseded_generations.join(", ")}, kept for audit only)` : ""}
            </dd>
            {r.error ? (<><dt>Error</dt><dd className="state-error">{r.error}</dd></>) : null}
          </dl>
          <p>
            Investigated by <AgentName agent="scout" />; every claim checked by <AgentName agent="sentinel" />.
          </p>
        </Section>
        <Section title="Progress" icon="pulse" end={<span className="muted">{status === "live" ? "Live" : "Paused"}</span>}>
          <Timeline events={events} />
        </Section>
      </div>
      <div>
        <Section title="Budgets used" icon="gauge">
          <Budgets cost={cost} />
        </Section>
        <Section title="Reports" icon="task">
          {r.reports.length === 0 ? <Empty>No reports yet. They appear when Sentinel has checked the claims.</Empty> : null}
          <ul className="list">
            {r.reports.map((rep) => (
              <li key={rep.report_id}>
                <span className="grow">
                  <Link to={`/reports/${encodeURIComponent(rep.report_id)}`}>{rep.report_type}</Link>{" "}
                  <span className="muted num">v{rep.report_version}</span>
                  <br />
                  <ReportVerdicts reportId={rep.report_id} />
                </span>
              </li>
            ))}
          </ul>
          <Button icon="shield" onClick={() => void reverify.run({})} loading={reverify.state.status === "running"}>
            Check the claims again
          </Button>
          <ActionResult state={reverify.state} retry={() => void reverify.retrySame()} />
        </Section>
      </div>
    </div>
  );
}
