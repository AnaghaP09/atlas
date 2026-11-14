import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import type { LiveEvent } from "../api/stream";
import type { Notification } from "../api/types";
import { useAction, useApi, useIdentity, useLiveStream } from "../app/context";
import { ActionResult, Button, Empty, ErrorView, Loading, Pagination, Section, Segmented, Time, usePaged } from "../components/common";
import { Icon } from "../components/icons";
import { EntityLink } from "../components/related";
import { useRouter } from "../lib/router";
import { useRunTitles } from "./Queue";

type Filter = "open" | "critical" | "high" | "warning" | "info" | "all";

const SEVERITY_WORD: Record<string, string> = { critical: "Critical", high: "High", warning: "Warning", info: "Info" };

function sentence(text: string): string {
  return text ? text[0]!.toUpperCase() + text.slice(1) : text;
}

/** "security_gate.not_run" -> "Security gate". The message itself says what happened. */
function topic(eventType: string): string {
  const head = (eventType.split(".")[0] ?? eventType).replace(/_/g, " ");
  return head.replace(/^./, (c) => c.toUpperCase());
}

function Row({ n, title, focused }: { n: Notification; title?: string; focused: boolean }) {
  const ack = useAction<Record<string, never>, unknown>(`/api/notifications/${encodeURIComponent(n.notification_id)}/ack`);
  const resolve = useAction<Record<string, never>, unknown>(`/api/notifications/${encodeURIComponent(n.notification_id)}/resolve`);
  const qc = useQueryClient();
  const refresh = () => void qc.invalidateQueries();
  return (
    <tr className={`${n.resolved ? "row-done" : ""}${focused ? " row-focus" : ""}`}>
      <td>
        <span className={`sev sev-${n.severity}`}>{SEVERITY_WORD[n.severity] ?? n.severity}</span>
      </td>
      <td>
        <span className="row-title" title={n.message}>{sentence(n.message)}</span>
        <span className="row-sub">
          {topic(n.event_type)}
          {n.kind === "reminder" ? " · Reminder" : n.kind === "decision" ? " · Needs a decision" : ""}
        </span>
      </td>
      <td>
        {n.run_id ? <EntityLink entity="run" id={n.run_id}>{title ?? "Open the run"}</EntityLink> : null}
        {n.task_id ? <EntityLink entity="task" id={n.task_id}>Task</EntityLink> : null}
        {!n.run_id && !n.task_id ? <span className="muted">—</span> : null}
      </td>
      <td className="muted"><Time iso={n.created_at} short /></td>
      <td className="row-actions always">
        {n.resolved ? (
          <span className="pill pill-good">Resolved</span>
        ) : (
          <>
            {n.acknowledged ? (
              <span className="pill pill-neutral">Seen</span>
            ) : (
              <Button size="sm" variant="ghost" loading={ack.state.status === "running"} onClick={() => void ack.run({}).then(refresh)}>Mark seen</Button>
            )}
            <Button size="sm" loading={resolve.state.status === "running"} onClick={() => void resolve.run({}).then(refresh)}>Resolve</Button>
          </>
        )}
        <ActionResult state={ack.state} retry={() => void ack.retrySame()} success="Marked as seen" />
        <ActionResult state={resolve.state} retry={() => void resolve.retrySame()} success="Resolved" />
      </td>
    </tr>
  );
}

export function Notifications() {
  const [filter, setFilter] = useState<Filter>("open");
  const data = useApi<{ notifications: Notification[]; data_mode?: string }>("/api/notifications");
  const { principal } = useIdentity();
  const dead = useApi<{ deliveries: unknown[] }>(principal?.role === "audit" ? "/api/notifications/dead-letter" : null);
  const qc = useQueryClient();
  const titles = useRunTitles();
  const { search } = useRouter();
  const focus = search.get("focus");
  const handlers = useRef({
    onEvent: (e: LiveEvent) => {
      if (e.event === "notification.created" || e.event.startsWith("containment.")) void qc.invalidateQueries();
    },
    onReset: () => void qc.invalidateQueries(),
  });
  const status = useLiveStream("/api/notifications/stream", (e) => handlers.current.onEvent(e), () => handlers.current.onReset());
  const all = (data.data?.notifications ?? []).slice().sort((a, b) => String(b.created_at ?? "").localeCompare(String(a.created_at ?? "")));
  const items = all.filter((n) => (filter === "all" ? true : filter === "open" ? !n.resolved : n.severity === filter));
  const paged = usePaged(items, 15);
  const failed = (dead.data?.deliveries ?? []).length;
  return (
    <Section
      title="Inbox"
      icon="bell"
      mode={data.data?.data_mode}
      end={
        <span className="inline-tools">
          {status !== "live" ? <span className="pill pill-warn">Live updates paused</span> : <span className="pill pill-good">Live</span>}
          {principal?.role === "audit" ? (
            <span className={`pill ${failed ? "pill-critical" : "pill-neutral"}`} title="Messages that could not be sent to an outside channel">
              <Icon name="alert" size={12} /> {failed} failed deliveries
            </span>
          ) : null}
        </span>
      }
    >
      <div className="toolbar">
        <Segmented<Filter>
          label="Show"
          value={filter}
          onChange={setFilter}
          options={[
            { value: "open", label: "Open" },
            { value: "critical", label: "Critical" },
            { value: "high", label: "High" },
            { value: "warning", label: "Warning" },
            { value: "info", label: "Info" },
            { value: "all", label: "All" },
          ]}
        />
        <span className="muted num">{items.length} of {all.length}</span>
      </div>
      {data.isLoading ? <Loading what="notifications" /> : <ErrorView error={data.error} />}
      <ErrorView error={dead.error} />
      {data.data && items.length === 0 ? <Empty>No notifications here.</Empty> : null}
      {items.length ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Level</th><th>What happened</th><th>From</th><th>When</th><th /></tr>
            </thead>
            <tbody>
              {paged.slice.map((n) => (
                <Row key={n.notification_id} n={n} title={n.run_id ? titles.get(n.run_id) : undefined} focused={focus === n.notification_id} />
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      <Pagination page={paged.page} pages={paged.pages} setPage={paged.setPage} total={items.length} />
    </Section>
  );
}
