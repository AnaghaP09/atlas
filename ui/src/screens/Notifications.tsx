import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import type { LiveEvent } from "../api/stream";
import type { Notification } from "../api/types";
import { useAction, useApi, useIdentity, useLiveStream } from "../app/context";
import { ActionResult, ErrorView, Loading, Section, Time } from "../components/common";
import { EntityLink } from "../components/related";

const SEVERITIES = ["all", "critical", "high", "warning", "info"] as const;

function Row({ n }: { n: Notification }) {
  const ack = useAction<Record<string, never>, unknown>(`/api/notifications/${encodeURIComponent(n.notification_id)}/ack`);
  const resolve = useAction<Record<string, never>, unknown>(`/api/notifications/${encodeURIComponent(n.notification_id)}/resolve`);
  return (
    <li className={`note sev-border-${n.severity}`}>
      <span className={`sev sev-${n.severity}`}>{n.severity}</span> {n.kind === "reminder" ? <em>reminder</em> : null} {n.message}{" "}
      <Time iso={n.created_at} short /> {n.run_id ? <EntityLink entity="run" id={n.run_id}>Run</EntityLink> : null}{" "}
      {n.task_id ? <EntityLink entity="task" id={n.task_id}>Task</EntityLink> : null}{" "}
      {!n.acknowledged ? <button onClick={() => void ack.run({})}>Acknowledge</button> : <span className="muted">acknowledged</span>}{" "}
      {!n.resolved ? <button onClick={() => void resolve.run({})}>Resolve</button> : <span className="muted">resolved</span>}
      <ActionResult state={ack.state} retry={() => void ack.retrySame()} />
      <ActionResult state={resolve.state} retry={() => void resolve.retrySame()} />
    </li>
  );
}

export function Notifications() {
  const [severity, setSeverity] = useState<(typeof SEVERITIES)[number]>("all");
  const data = useApi<{ notifications: Notification[]; data_mode?: string }>("/api/notifications");
  const { principal } = useIdentity();
  const dead = useApi<{ deliveries: unknown[] }>(principal?.role === "audit" ? "/api/notifications/dead-letter" : null);
  const qc = useQueryClient();
  const handlers = useRef({
    onEvent: (e: LiveEvent) => {
      if (e.event === "notification.created" || e.event.startsWith("containment.")) void qc.invalidateQueries();
    },
    onReset: () => void qc.invalidateQueries(),
  });
  const status = useLiveStream("/api/notifications/stream", (e) => handlers.current.onEvent(e), () => handlers.current.onReset());
  const items = (data.data?.notifications ?? []).filter((n) => severity === "all" || n.severity === severity);
  return (
    <div className="grid">
      <Section title="Notifications" mode={data.data?.data_mode}>
        {status !== "live" ? <p className="banner">Live updates paused. Reconnecting…</p> : null}
        <p>Every error and every decision ATLAS needs ends up here, sent to the roles that must act on it.</p>
        <label>
          Show
          <select value={severity} onChange={(e) => setSeverity(e.target.value as (typeof SEVERITIES)[number])}>
            {SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        {data.isLoading ? <Loading what="notifications" /> : <ErrorView error={data.error} />}
        {data.data && items.length === 0 ? <p>No notifications.</p> : null}
        <ul className="plain">{items.map((n) => <Row key={n.notification_id} n={n} />)}</ul>
      </Section>
      {principal?.role === "audit" ? (
        <Section title="Failed deliveries">
          <ErrorView error={dead.error} />
          <p>{(dead.data?.deliveries ?? []).length} delivery(ies) could not be sent to an outside channel.</p>
        </Section>
      ) : null}
    </div>
  );
}
