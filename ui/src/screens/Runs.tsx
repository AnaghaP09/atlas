import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import type { LiveEvent } from "../api/stream";
import type { Claim, Envelope, ReportView, RunView } from "../api/types";
import { useAction, useApi, useIdentity, useLiveStream } from "../app/context";
import { CreateTask } from "./Tasks";
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
import { Related } from "../components/related";
import { DatasetName } from "../components/dataset";
import type { Notification, QueueEntry, TaskView } from "../api/types";
import { IdDetails } from "../components/common";
import { Link } from "../lib/router";
import { reportName, runTitle, stageName, starterName, toolName } from "../lib/labels";
import { Time } from "../components/common";

export function RunList() {
  const data = useApi<{ runs: RunView[] } & Envelope>("/api/runs");
  const runs = (data.data?.runs ?? []).slice().sort((a, b) => String(b.started_at ?? "").localeCompare(String(a.started_at ?? "")));
  const paged = usePaged(runs, 15);
  return (
    <Section title="All runs" icon="list" mode={data.data?.data_mode}>
      {data.isLoading ? <Loading what="runs" /> : <ErrorView error={data.error} />}
      {data.data && runs.length === 0 ? (
        <Empty action={{ to: "/run-now", label: "Start a release close" }}>No runs yet.</Empty>
      ) : null}
      {runs.length ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Run</th><th>Status</th><th>Started by</th><th>Started</th><th className="num">Reports</th></tr>
            </thead>
            <tbody>
              {paged.slice.map((r) => (
                <tr key={r.run_id}>
                  <td>
                    <Link to={`/runs/${encodeURIComponent(r.run_id)}`} className="row-title">{r.kind_label ?? runTitle(r)}</Link>
                    {r.subject ? <span className="row-sub" title={r.subject}>{r.subject}</span> : null}
                  </td>
                  <td><StatusPill status={r.run_status} /></td>
                  <td>{starterName(r.started_by)}</td>
                  <td className="muted"><Time iso={r.started_at} short /></td>
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
  return starterName(s);
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

/** The limits for one investigation. */
export const LIMITS = { steps: 30, tool_calls: 60, seconds: 15 * 60 };

interface Cost {
  steps?: number;
  tool_calls?: number;
  seconds?: number;
  usd?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  spend_recorded?: boolean;
}

function Budgets({ cost }: { cost: Cost | null }) {
  if (!cost) return <p className="muted">No usage recorded yet.</p>;
  return (
    <div className="meters">
      <UsageBar label="Steps" used={cost.steps ?? 0} limit={LIMITS.steps} />
      <UsageBar label="Tool calls" used={cost.tool_calls ?? 0} limit={LIMITS.tool_calls} />
      <UsageBar label="Minutes" used={Math.round(((cost.seconds ?? 0) / 60) * 10) / 10} limit={LIMITS.seconds / 60} />
      {cost.spend_recorded === false ? (
        <p className="muted">Model spend not recorded</p>
      ) : (
        <p className="muted num">
          Model spend ${(cost.usd ?? 0).toFixed(3)} · {(cost.input_tokens ?? 0).toLocaleString()} input tokens ·{" "}
          {(cost.output_tokens ?? 0).toLocaleString()} output tokens
        </p>
      )}
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
  if (typeof d.tool === "string") return toolName(d.tool);
  if (typeof d.stage === "string") return `${stageName(d.stage)}${typeof d.claims === "number" ? ` · ${d.claims} claims` : ""}`;
  if (typeof d.status === "string") return stageName(d.status);
  return e.event.replace(/[._]/g, " ");
}

function Timeline({ events, finished }: { events: LiveEvent[]; finished: boolean }) {
  if (events.length === 0) return <p className="muted">{finished ? "Finished. Steps are shown while a run is in progress." : "Waiting for the first step…"}</p>;
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
              <p className="tl-text">{eventText(e)}</p>
              <span className="tl-meta">
                {agent ? <AgentName agent={agent} compact /> : null}
                {typeof e.data.step === "number" ? <span className="muted num">Step {e.data.step}</span> : null}
                <Time iso={e.emitted_at} short />
              </span>
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
  const tasks = useApi<{ tasks: TaskView[] }>("/api/tasks");
  const notes = useApi<{ notifications: Notification[] }>("/api/notifications");
  const queue = useApi<{ entries: QueueEntry[] }>("/api/queue");
  const { principal } = useIdentity();

  if (run.isLoading) return <Loading what="the run" />;
  if (run.error) return <ErrorView error={run.error} />;
  const r = run.data;
  if (!r) return null;
  const cost = r.cost && typeof r.cost === "object" ? (r.cost as Cost) : null;
  return (
    <div className="grid-2">
      <div>
        <Section title={runTitle(r)} icon="list" mode={r.data_mode} end={<StatusPill status={r.run_status} />}>
          <Related
            chips={[
              { entity: "snapshot", id: r.snapshot_id, label: "Data set" },
              { entity: "task", id: (tasks.data?.tasks ?? []).find((t) => t.run_id === r.run_id)?.task_id, label: "Tasks", count: (tasks.data?.tasks ?? []).filter((t) => t.run_id === r.run_id).length },
              { entity: "notification", id: (notes.data?.notifications ?? []).find((n) => n.run_id === r.run_id)?.notification_id, label: "Notifications", count: (notes.data?.notifications ?? []).filter((n) => n.run_id === r.run_id).length },
              { entity: "queue", id: String((queue.data?.entries ?? []).find((q) => q.run_id === r.run_id)?.entry_id ?? ""), label: "Needs a person", count: (queue.data?.entries ?? []).filter((q) => q.run_id === r.run_id).length },
            ]}
          />
          {status !== "live" ? <p className="banner">Live updates paused. Reconnecting…</p> : null}
          {notice ? <p className="banner banner-note">{notice}</p> : null}
          <dl className="facts">
            {r.hold_reason ? (<><dt>On hold</dt><dd>{r.hold_reason}</dd></>) : null}
            <dt>Started by</dt><dd>{starterName(r.started_by)}</dd>
            <dt>Started</dt><dd><Time iso={r.started_at} /></dd>
            <dt>Data as of</dt><dd><DatasetName id={r.snapshot_id} /></dd>
            <dt>Sentinel check</dt>
            <dd>
              {r.authoritative_generation ? `Check ${r.authoritative_generation}` : "Not checked yet"}
              {r.superseded_generations.length ? ` · ${r.superseded_generations.length} earlier` : ""}
            </dd>
            {r.error ? (<><dt>Error</dt><dd className="state-error">{r.error}</dd></>) : null}
          </dl>
          <p className="agents-line">
            <AgentName agent="scout" /> <AgentName agent="sentinel" />
          </p>
          <IdDetails items={[["Run id", r.run_id], ["Data set", r.snapshot_id], ["Project", r.project_id ?? null]]} />
        </Section>
        <Section title="Progress" icon="pulse" end={["complete", "partial", "unverified", "failed", "refused"].includes(r.run_status) ? null : <span className={`pill ${status === "live" ? "pill-good" : "pill-warn"}`}>{status === "live" ? "Live" : "Paused"}</span>}>
          <Timeline events={events} finished={["complete", "partial", "unverified", "failed", "refused"].includes(r.run_status)} />
        </Section>
      </div>
      <div>
        <Section title="Budgets used" icon="gauge">
          <Budgets cost={cost} />
        </Section>
        <Section title="Reports" icon="task">
          {r.reports.length === 0 ? <p className="muted">No reports yet.</p> : null}
          <ul className="list">
            {r.reports.map((rep) => (
              <li key={rep.report_id}>
                <span className="grow">
                  <Link to={`/reports/${encodeURIComponent(rep.report_id)}`}>{reportName(rep.report_type)}</Link>{" "}
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
          <ActionResult state={reverify.state} retry={() => void reverify.retrySame()} success="Check started" />
          {principal?.role === "em" && r.kind === "failure" && r.reports.length > 0 && !(tasks.data?.tasks ?? []).some((t) => t.run_id === r.run_id) ? (
            <div className="actions-row">
              <CreateTask runId={r.run_id} />
            </div>
          ) : null}
        </Section>
      </div>
    </div>
  );
}
