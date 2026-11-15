import { useEffect, useState } from "react";
import type { Approval, TaskView } from "../api/types";
import { useAction, useIdentity, useApi, useInvalidateAll } from "../app/context";
import { ActionResult, Button, Empty, ErrorView, IdDetails, Loading, StatusPill, Time } from "../components/common";
import { LockedChip } from "../components/access";
import { ExternalLink } from "../components/ExternalLink";
import { Icon } from "../components/icons";
import { EntityLink } from "../components/related";
import { useRouter } from "../lib/router";
import { BlockedNote, useBlockedBy } from "./Containment";

function useNow(intervalMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}

export function Countdown({ iso }: { iso?: string | null }) {
  const now = useNow();
  if (!iso) return <span className="muted">—</span>;
  const ms = new Date(iso).getTime() - now;
  if (Number.isNaN(ms)) return <span>{iso}</span>;
  if (ms <= 0) return <span className="pill pill-critical">Expired</span>;
  const h = Math.floor(ms / 3_600_000);
  const m = Math.floor((ms % 3_600_000) / 60_000);
  return (
    <span className={`pill ${h < 4 ? "pill-warn" : "pill-neutral"}`} title={new Date(iso).toLocaleString()}>
      <Icon name="clock" size={12} /> {h > 0 ? `${h} h ${m} min left` : `${m} min left`}
    </span>
  );
}

const ROLE_WORD: Record<string, string> = { pm: "product manager", em: "engineering manager", architect: "solution architect" };

function shortHash(v: unknown): string | null {
  return typeof v === "string" && v ? (v.length > 14 ? `${v.slice(0, 12)}…` : v) : null;
}

/** One approval per task: the engineering manager's single click covers preparing and publishing. */
function ApprovalCard({ a, focused, pr, task }: { a: Approval; focused: boolean; pr?: string | null; task?: TaskView }) {
  const { navigate } = useRouter();
  const t = (task ?? {}) as { target_change?: string | null; permitted_scope?: string[]; target_label?: string | null };
  const decide = useAction<{ decision: string; review_minutes?: number }, unknown>(`/api/approvals/${encodeURIComponent(a.approval_id)}/decision`);
  const oneClick = useAction<Record<string, never>, unknown>(`/api/tasks/${encodeURIComponent(a.task_id)}/approve-and-publish`);
  const { principal } = useIdentity();
  const refresh = useInvalidateAll();
  const blocked = useBlockedBy("preparation");
  const pending = a.state === "pending" || a.state === "requested";
  const isEm = principal?.role === "em";
  // An approval that was used to publish is closed by the system afterwards; it still counts as approved.
  const used = ["published", "awaiting_validation", "accepted"].includes(String((task as { state?: string } | undefined)?.state ?? ""));
  const approved = a.state === "granted" || (a.state === "voided" && used);
  const status = pending ? "Waiting for approval" : approved ? "Approved" : a.state === "voided" ? "Cancelled" : a.state === "rejected" ? "Rejected" : a.state;
  const tone = pending ? "held" : approved ? "granted" : a.state;
  return (
    <article className={`card approval${focused ? " is-focus" : ""}`} data-testid="approval">
      <header className="approval-head">
        <span className="approval-kind">Approval</span>
        <h2>Let the coding agent fix this and open a draft pull request</h2>
        {a.created_at ? (
          <span className="muted small" data-testid="approval-time">
            {pending ? "Requested " : `${status} `}<Time iso={a.created_at} />
            {!pending && a.approver_role ? ` by the ${ROLE_WORD[a.approver_role] ?? a.approver_role}` : ""}
          </span>
        ) : null}
        <span className="end inline-tools">
          <StatusPill status={tone} />
        </span>
      </header>
      <div className="approval-grid">
        <div>
          <span className="fact-label">Publishes to</span>
          <span className="fact-value">{a.destination_repository ?? "—"}</span>
        </div>
        <div>
          <span className="fact-label">Based on</span>
          <span className="fact-value mono">{a.base_ref ?? "—"}{shortHash(a.base_commit) ? ` · ${shortHash(a.base_commit)}` : ""}</span>
        </div>
        <div>
          <span className="fact-label">Draft pull request</span>
          <span className="fact-value">{pr ? <ExternalLink href={pr} label="Open the draft pull request" /> : <span className="muted">Not opened yet</span>}</span>
        </div>
        <div>
          <span className="fact-label">Task</span>
          <span className="fact-value"><EntityLink entity="task" id={a.task_id}>Open the task</EntityLink></span>
        </div>
      </div>
      {t.target_change || (t.permitted_scope && t.permitted_scope.length) ? (
        <div className="approval-change" data-testid="approval-change">
          <span className="fact-label">What the coding agent will change</span>
          {t.target_change ? <p>{t.target_change}</p> : null}
          {t.permitted_scope && t.permitted_scope.length ? <p className="muted small">Files it may touch: <span className="mono">{t.permitted_scope.join(", ")}</span></p> : null}
          {t.target_label ? <p className="muted small">{t.target_label}</p> : null}
        </div>
      ) : null}
      {!pending && typeof a.diff === "string" && a.diff ? <pre className="diff" aria-label="The change">{a.diff}</pre> : null}
      {pending ? (
        <div className="approval-actions">
          {isEm ? (
            <>
              <Button variant="primary" icon={blocked.length ? "lock" : "ok"} disabled={blocked.length > 0}
                loading={oneClick.state.status === "running"} onClick={() => void oneClick.run({}).then(() => { refresh(); if (a.task_id) navigate(`/tasks/${encodeURIComponent(a.task_id)}`); })}>Approve</Button>
              <Button variant="secondary" disabled={oneClick.state.status === "running"} onClick={() => void decide.run({ decision: "reject" }).then(refresh)}>Reject</Button>
              {oneClick.state.status === "running" ? <span className="muted small">The coding agent is preparing the change in the sandbox. This takes a minute or two.</span> : null}
            </>
          ) : (
            <span className="muted small">Waiting for the engineering manager to approve.</span>
          )}
          <BlockedNote by={blocked} />
          <ActionResult state={decide.state} retry={() => void decide.retrySame()} success="Rejected" />
          <ActionResult state={oneClick.state} retry={() => void oneClick.retrySame()} success="Approved. The coding agent prepared the change, every check passed, and the draft pull request is open." />
        </div>
      ) : null}
      <IdDetails items={[["Approval id", a.approval_id], ["Task id", a.task_id], ["Security decision", a.security_decision_id ?? null]]} />
    </article>
  );
}

/** Collapse a task's approval rows into one: the waiting one if any, else the newest decision. */
function onePerTask(rows: Approval[]): Approval[] {
  const byTask = new Map<string, Approval>();
  for (const r of rows.slice().sort((x, y) => String(y.created_at ?? "").localeCompare(String(x.created_at ?? "")))) {
    const cur = byTask.get(r.task_id);
    const waiting = r.state === "requested" || r.state === "pending";
    if (!cur || (waiting && !(cur.state === "requested" || cur.state === "pending"))) byTask.set(r.task_id, r);
  }
  return [...byTask.values()].sort((x, y) => String(y.created_at ?? "").localeCompare(String(x.created_at ?? "")));
}

interface LockedItem {
  object_type: string;
  object_id: string;
}

export function Approvals() {
  const data = useApi<{ approvals: Approval[]; locked_items?: LockedItem[]; data_mode?: string }>("/api/approvals");
  const tasks = useApi<{ tasks: TaskView[] }>("/api/tasks");
  const { search } = useRouter();
  const focus = search.get("focus");
  if (data.isLoading) return <Loading what="approvals" />;
  if (data.error) return <ErrorView error={data.error} />;
  const all = onePerTask(data.data?.approvals ?? []);
  const prOf = (taskId: string): string | null => {
    const t = (tasks.data?.tasks ?? []).find((x) => x.task_id === taskId);
    const pr = t?.pull_request as { url?: string } | null | undefined;
    return pr?.url ?? null;
  };
  const locked = data.data?.locked_items ?? [];
  if (all.length === 0 && locked.length === 0)
    return <Empty action={{ to: "/tasks", label: "Open tasks" }}>No approvals are waiting.</Empty>;
  return (
    <div className="approval-list">
      {all.map((a) => <ApprovalCard key={a.approval_id} a={a} focused={focus === a.approval_id} pr={prOf(a.task_id)} task={(tasks.data?.tasks ?? []).find((x) => x.task_id === a.task_id)} />)}
      {locked.length ? (
        <div className="chip-row">
          {locked.map((l) => <LockedChip key={l.object_id} objectType={l.object_type} objectId={l.object_id} />)}
        </div>
      ) : null}
    </div>
  );
}
