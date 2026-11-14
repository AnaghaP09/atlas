import type { Envelope, Schedule } from "../api/types";
import { useAction, useApi } from "../app/context";
import { ActionResult, ErrorView, Loading, Section, Time } from "../components/common";

function ScheduleRow({ s }: { s: Schedule }) {
  const enable = useAction<Record<string, never>, unknown>(`/api/schedules/${encodeURIComponent(s.schedule_id)}/enable`);
  const pause = useAction<Record<string, never>, unknown>(`/api/schedules/${encodeURIComponent(s.schedule_id)}/pause`);
  const once = useAction<Record<string, never>, unknown>(`/api/schedules/${encodeURIComponent(s.schedule_id)}/run-once`);
  return (
    <tr>
      <td>{s.schedule_id}</td>
      <td>{s.kind}</td>
      <td>
        {s.state}
        {s.reason ? <span className="muted"> ({s.reason})</span> : null}
      </td>
      <td><Time iso={s.last_run} /></td>
      <td>{s.next_run ? <Time iso={s.next_run} /> : "—"}</td>
      <td className="actions">
        <button onClick={() => void enable.run({})}>Enable</button>
        <button onClick={() => void pause.run({})}>Pause</button>
        <button onClick={() => void once.run({})}>Run once</button>
        {[enable, pause, once].map((a, i) => (
          <ActionResult key={i} state={a.state} retry={() => void a.retrySame()} />
        ))}
      </td>
    </tr>
  );
}

export function Schedules() {
  const data = useApi<{ schedules: Schedule[] } & Envelope>("/api/schedules");
  return (
    <Section title="Automatic runs" mode={data.data?.data_mode}>
      <p>
        Watchers start runs on their own: a new release starts a release close, a new failure starts an investigation, and the
        nightly sweep reviews waiting failures. The engineering manager and the auditor can enable, pause or run each one.
      </p>
      {data.isLoading ? <Loading what="schedules" /> : <ErrorView error={data.error} />}
      {data.data && data.data.schedules.length === 0 ? <p>No watchers are configured on this server.</p> : null}
      {data.data && data.data.schedules.length > 0 ? (
        <table>
          <thead>
            <tr><th>Watcher</th><th>Kind</th><th>State</th><th>Last run</th><th>Next run</th><th>Actions</th></tr>
          </thead>
          <tbody>{data.data.schedules.map((s) => <ScheduleRow key={s.schedule_id} s={s} />)}</tbody>
        </table>
      ) : null}
    </Section>
  );
}
