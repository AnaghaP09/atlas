import type { Envelope, Schedule } from "../api/types";
import { useAction, useApi, useInvalidateAll } from "../app/context";
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

function words(v: string): string {
  return v.replace(/[_-]/g, " ").replace(/^./, (c) => c.toUpperCase());
}

function ScheduleRow({ s }: { s: Schedule }) {
  const base = `/api/schedules/${encodeURIComponent(s.schedule_id)}`;
  const enable = useAction<Record<string, never>, unknown>(`${base}/enable`);
  const pause = useAction<Record<string, never>, unknown>(`${base}/pause`);
  const once = useAction<Record<string, never>, unknown>(`${base}/run-once`);
  const reload = useInvalidateAll();
  const [word, tone] = STATE[s.state] ?? [words(s.state), "neutral"];
  return (
    <tr>
      <td>
        <span className="row-title">{KIND[s.kind] ?? words(s.kind)}</span>
        <span className="row-sub">{words(s.schedule_id)}</span>
      </td>
      <td>
        <span className={`pill pill-${tone}`} title={s.reason ?? undefined}>{word}</span>
      </td>
      <td className="muted">{s.last_run ? <Time iso={s.last_run} short /> : "Never"}</td>
      <td className="muted">{s.next_run ? <Time iso={s.next_run} short /> : "When something new arrives"}</td>
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
    </tr>
  );
}

export function Schedules() {
  const data = useApi<{ schedules: Schedule[] } & Envelope>("/api/schedules");
  const list = data.data?.schedules ?? [];
  return (
    <Section title="Automatic runs" icon="clock" mode={data.data?.data_mode} end={list.length ? <span className="muted num">{list.length}</span> : null}>
      {data.isLoading ? <Loading what="automatic runs" /> : <ErrorView error={data.error} />}
      {data.data && list.length === 0 ? (
        <Empty>No automatic runs are set up on this server.</Empty>
      ) : null}
      {list.length > 0 ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Starts on</th><th>State</th><th>Last run</th><th>Next run</th><th /></tr>
            </thead>
            <tbody>{list.map((s) => <ScheduleRow key={s.schedule_id} s={s} />)}</tbody>
          </table>
        </div>
      ) : null}
    </Section>
  );
}
