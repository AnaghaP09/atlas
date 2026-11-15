import type { Envelope, Schedule } from "../api/types";
import { useAction, useApi, useIdentity, useInvalidateAll } from "../app/context";
import { ActionResult, Button, Empty, ErrorView, Loading, Section, Time } from "../components/common";

const KIND: Record<string, string> = {
  release: "New release",
  release_close: "New release",
  failure: "New failure",
  nightly: "Nightly sweep",
};

const STATE: Record<string, [string, string]> = {
  enabled: ["On", "good"],
  paused: ["Paused", "warn"],
  disabled: ["Off", "neutral"],
};

/** Roles the server lets turn automatic runs on, pause them or run them once. */
const CONTROL_ROLES = new Set(["em", "audit"]);

function words(v: string): string {
  return v.replace(/[_-]/g, " ").replace(/^./, (c) => c.toUpperCase());
}

/** A plain title: the server's title unless it only repeats the id. */
export function scheduleTitle(s: Schedule): string {
  return s.title && s.title !== s.schedule_id ? s.title : (KIND[s.kind] ?? words(s.kind));
}

/** "Every 5 minutes", "Every 2 hours", or how an event-driven watcher starts. */
export function intervalText(seconds: number | null | undefined): string {
  if (!seconds) return "When something new arrives";
  if (seconds % 86400 === 0) return seconds === 86400 ? "Every day" : `Every ${seconds / 86400} days`;
  if (seconds % 3600 === 0) return seconds === 3600 ? "Every hour" : `Every ${seconds / 3600} hours`;
  if (seconds % 60 === 0) return seconds === 60 ? "Every minute" : `Every ${seconds / 60} minutes`;
  return `Every ${seconds} seconds`;
}

function ScheduleRow({ s, canControl }: { s: Schedule; canControl: boolean }) {
  const base = `/api/schedules/${encodeURIComponent(s.schedule_id)}`;
  const enable = useAction<Record<string, never>, unknown>(`${base}/enable`);
  const pause = useAction<Record<string, never>, unknown>(`${base}/pause`);
  const once = useAction<Record<string, never>, unknown>(`${base}/run-once`);
  const reload = useInvalidateAll();
  const [word, tone] = STATE[s.state] ?? [words(s.state), "neutral"];
  return (
    <tr data-testid={`schedule-${s.schedule_id}`}>
      <td>
        <span className="row-title">{scheduleTitle(s)}</span>
        <span className="row-sub">
          {intervalText(s.interval_seconds)}
          {s.spends_model_budget ? " · spends model budget" : " · no model spend"}
          {s.external ? " · reads outside data" : ""}
        </span>
      </td>
      <td>
        <span className={`pill pill-${tone}`}>{word}</span>
        {s.state !== "enabled" && s.reason ? <span className="row-sub" data-testid="schedule-reason">{s.reason}</span> : null}
      </td>
      <td className="muted">
        {s.last_run ? <Time iso={s.last_run} short /> : "Never"}
        {s.last_event_id ? <span className="row-sub">last event {s.last_event_id}</span> : null}
      </td>
      <td className="muted">{s.state !== "enabled" ? "Not while paused" : s.next_run ? <Time iso={s.next_run} short /> : "When something new arrives"}</td>
      {canControl ? (
        <td className="row-actions always">
          {s.state === "enabled" ? (
            <Button size="sm" variant="ghost" loading={pause.state.status === "running"} onClick={() => void pause.run({}).then(reload)}>Pause</Button>
          ) : (
            <Button size="sm" variant="ghost" loading={enable.state.status === "running"} onClick={() => void enable.run({}).then(reload)}>Turn on</Button>
          )}
          <Button size="sm" icon="play" loading={once.state.status === "running"} onClick={() => void once.run({}).then(reload)}>Run once</Button>
          <ActionResult state={enable.state} retry={() => void enable.retrySame()} success="Turned on" />
          <ActionResult state={pause.state} retry={() => void pause.retrySame()} success="Paused" />
          <ActionResult state={once.state} retry={() => void once.retrySame()} success="Run started" />
        </td>
      ) : null}
    </tr>
  );
}

export function Schedules() {
  const data = useApi<{ schedules: Schedule[] } & Envelope>("/api/schedules");
  const { principal } = useIdentity();
  const canControl = CONTROL_ROLES.has(String(principal?.role ?? ""));
  const list = data.data?.schedules ?? [];
  return (
    <Section title="Automatic runs" icon="clock" mode={data.data?.data_mode} end={list.length ? <span className="muted num">{list.length}</span> : null}>
      {data.isLoading ? <Loading what="automatic runs" /> : <ErrorView error={data.error} />}
      {data.data && list.length === 0 ? (
        <Empty>No automatic runs are set up on this server.</Empty>
      ) : null}
      {list.length > 0 && !canControl ? (
        <p className="muted" data-testid="schedules-read-only">You can see automatic runs. The engineering manager turns them on, pauses them or runs them once.</p>
      ) : null}
      {list.length > 0 ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Automatic run</th><th>State</th><th>Last run</th><th>Next run</th>{canControl ? <th /> : null}</tr>
            </thead>
            <tbody>{list.map((s) => <ScheduleRow key={s.schedule_id} s={s} canControl={canControl} />)}</tbody>
          </table>
        </div>
      ) : null}
    </Section>
  );
}
