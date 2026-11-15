import { useState } from "react";
import type { Approval, TaskView } from "../api/types";
import { useAction, useApi, useIdentity, useInvalidateAll } from "../app/context";
import { ActionResult, Button, ConfirmButton, Empty, ErrorView, IdDetails, Loading, Section, Time } from "../components/common";
import { isReadOnly, LockedChip, ReadOnlyBanner, type AccessGrantView } from "../components/access";
import { ExternalLink } from "../components/ExternalLink";
import { Icon } from "../components/icons";
import { Related } from "../components/related";
import { actionName } from "../lib/labels";
import { Link, useRouter } from "../lib/router";
import { BlockedNote, useBlockedBy } from "./Containment";
import { useRunTitles } from "./Queue";

const STEPS: { state: string; label: string }[] = [
  { state: "proposed", label: "Proposed" },
  { state: "approved_a", label: "Approved to prepare" },
  { state: "prepared", label: "Prepared" },
  { state: "approved_b", label: "Approved to publish" },
  { state: "published", label: "Published" },
  { state: "awaiting_validation", label: "Being checked" },
  { state: "accepted", label: "Accepted" },
];

const STATE_WORD: Record<string, string> = {
  proposed: "Proposed",
  approved_a: "Approved to prepare",
  prepared: "Prepared",
  approved_b: "Approved to publish",
  published: "Published",
  awaiting_validation: "Being checked",
  accepted: "Accepted",
  failed: "Failed",
  voided: "Cancelled",
  awaiting_reapproval: "Needs approval again",
};

/** What happens next when there is no button to press at this step. */
const STEP_NOTE: Record<string, string> = {
  proposed: "Next: approval A on the Approvals page starts the coding agent.",
  prepared: "The change is ready. Next: approval B on the Approvals page.",
  published: "The draft pull request is open. CI is running on it.",
  awaiting_validation: "The draft pull request is open and being checked. Next: record whether the fix works.",
  accepted: "Done: the fix was checked and accepted.",
  failed: "This task stopped. Start a new one from the investigation.",
  voided: "This task was cancelled.",
  awaiting_reapproval: "Something changed. Approve again on the Approvals page.",
};

export function taskStateWord(state: string | null | undefined): string {
  const s = String(state ?? "");
  return STATE_WORD[s] ?? s.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

function TaskState({ state }: { state: string }) {
  const tone = state === "accepted" ? "good" : state === "failed" || state === "voided" ? "critical" : state === "awaiting_reapproval" ? "warn" : "info";
  return (
    <span className={`pill pill-${tone}`} data-testid="task-state">
      <span className="pill-dot" aria-hidden="true" />
      {taskStateWord(state)}
    </span>
  );
}

interface LockedItem {
  object_type: string;
  object_id: string;
}

export function TaskList() {
  const data = useApi<{ tasks: TaskView[]; locked_items?: LockedItem[]; data_mode?: string }>("/api/tasks");
  const titles = useRunTitles();
  const tasks = data.data?.tasks ?? [];
  const locked = data.data?.locked_items ?? [];
  return (
    <Section title="Coding tasks" icon="task" mode={data.data?.data_mode} end={tasks.length ? <span className="muted num">{tasks.length}</span> : null}>
      {data.isLoading ? <Loading what="tasks" /> : <ErrorView error={data.error} />}
      {data.data && tasks.length === 0 && locked.length === 0 ? (
        <Empty action={{ to: "/runs", label: "Open runs" }}>No coding tasks yet. Create one from an investigation.</Empty>
      ) : null}
      {tasks.length ? (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Task</th><th>Status</th><th>Security check</th><th>Draft pull request</th></tr></thead>
            <tbody>
              {tasks.map((t) => (
                <tr key={t.task_id}>
                  <td>
                    <Link to={`/tasks/${encodeURIComponent(t.task_id)}`} className="row-title">{actionName(t.action) ?? "Coding task"}</Link>
                    <span className="row-sub">{(t.run_id && titles.get(t.run_id)) || "From an investigation"}</span>
                  </td>
                  <td><TaskState state={t.state} /></td>
                  <td><SecurityPill security={t.security} /></td>
                  <td>{prUrl(t) ? <ExternalLink href={prUrl(t) as string} label="Open" /> : <span className="muted">Not published</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {locked.length ? (
        <div className="chip-row locked-row">
          {locked.map((l) => <LockedChip key={l.object_id} objectType={l.object_type} objectId={l.object_id} />)}
        </div>
      ) : null}
    </Section>
  );
}

function prUrl(t: TaskView): string | null {
  const pr = (t.pull_request ?? t.publication) as { pr_url?: string; url?: string; html_url?: string } | null | undefined;
  return pr?.pr_url ?? pr?.html_url ?? pr?.url ?? null;
}

function SecurityPill({ security }: { security: TaskView["security"] }) {
  const state = security?.state ?? "not_run";
  const word: Record<string, [string, string]> = {
    passed: ["Passed", "good"],
    accepted_with_exception: ["Accepted with an exception", "warn"],
    blocked: ["Blocked", "critical"],
    not_run: ["Not run", "neutral"],
  };
  const [text, tone] = word[state] ?? [state.replace(/_/g, " "), "neutral"];
  return (
    <span className={`pill pill-${tone}`} title={security?.valid_until ? `Valid until ${new Date(security.valid_until).toLocaleString()}` : undefined}>
      {text}
    </span>
  );
}

function resultWord(v: unknown): { text: string; tone: string } {
  if (v === null || v === undefined) return { text: "Not yet", tone: "neutral" };
  const s = typeof v === "string" ? v : typeof v === "object" ? String((v as { verdict?: unknown; state?: unknown; status?: unknown }).verdict ?? (v as { state?: unknown }).state ?? (v as { status?: unknown }).status ?? "recorded") : String(v);
  const good = ["passed", "success", "correct", "accepted"].includes(s);
  const bad = ["failed", "failure", "incorrect"].includes(s);
  return { text: s.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()), tone: good ? "good" : bad ? "critical" : "info" };
}

function CheckList({ checks }: { checks: Record<string, string> }) {
  const entries = Object.entries(checks);
  if (!entries.length) return null;
  return (
    <ul className="check-grid">
      {entries.map(([name, state]) => {
        const ok = state === "passed";
        const off = state === "unavailable" || state === "not_run";
        return (
          <li key={name} className={ok ? "ok" : off ? "off" : "bad"}>
            <Icon name={ok ? "ok" : off ? "clock" : "alert"} size={14} />
            <span>{name.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase())}</span>
            <span className="muted small">{state.replace(/_/g, " ")}</span>
          </li>
        );
      })}
    </ul>
  );
}

export function TaskDetail({ taskId }: { taskId: string }) {
  const base = `/api/tasks/${encodeURIComponent(taskId)}`;
  const task = useApi<{ task?: TaskView; read_only?: boolean } & TaskView>(base);
  const prepare = useAction<Record<string, never>, unknown>(`${base}/prepare`);
  const publish = useAction<Record<string, never>, unknown>(`${base}/publish`);
  const refresh = useAction<Record<string, never>, unknown>(`${base}/security-refresh`);
  const adjudicate = useAction<{ verdict: string; note: string }, unknown>(`${base}/adjudicate`);
  const [note, setNote] = useState("");
  const approvals = useApi<{ approvals: Approval[] }>("/api/approvals");
  const { principal } = useIdentity();
  const titles = useRunTitles();
  const reload = useInvalidateAll();
  const prepBlock = useBlockedBy("preparation");
  const pubBlock = useBlockedBy("publication");
  if (task.isLoading) return <Loading what="the task" />;
  if (task.error) return <ErrorView error={task.error} />;
  if (!task.data) return null;
  // The API wraps the task; older responses sent it bare.
  const t: TaskView = task.data.task ?? task.data;
  const access = (task.data.access ?? t.access) as AccessGrantView | undefined;
  const readOnly = isReadOnly(access) || task.data.read_only === true;
  const mine = (approvals.data?.approvals ?? []).filter((a) => a.task_id === t.task_id);
  const pending = mine.find((a) => a.state === "pending");
  const stepIndex = STEPS.findIndex((s) => s.state === t.state);
  const offTrack = stepIndex < 0;
  const ci = resultWord(t.ci_observations ?? t.ci);
  const behaviour = resultWord(t.behavioural_check);
  const judged = resultWord(t.adjudication);
  const pr = prUrl(t);
  const canJudge = ["published", "awaiting_validation"].includes(t.state);
  return (
    <>
      <Section title={actionName(t.action) ?? "Coding task"} icon="task" mode={t.data_mode ?? task.data.data_mode} end={<TaskState state={t.state} />}>
        <ReadOnlyBanner access={access} myRole={principal?.role ?? null} />
        <Related
          object={{ type: "task", id: t.task_id }}
          chips={[
            { entity: "run", id: t.run_id, label: (t.run_id && titles.get(t.run_id)) || "Run" },
            { entity: "approval", id: mine[0]?.approval_id, label: "Approvals", count: mine.length },
          ]}
        />
        <ol className={`stepper${offTrack ? " off" : ""}`} aria-label="Progress">
          {STEPS.map((s, i) => (
            <li key={s.state} className={!offTrack && i < stepIndex ? "done" : !offTrack && i === stepIndex ? "now" : ""} aria-current={i === stepIndex ? "step" : undefined}>
              <span className="step-dot" aria-hidden="true">{!offTrack && i < stepIndex ? <Icon name="ok" size={14} /> : i + 1}</span>
              <span className="step-label">{s.label}</span>
            </li>
          ))}
        </ol>
        {pending ? (
          <p className="banner banner-note">
            <Icon name="hand" size={16} /> Waiting for approval {pending.kind}. <Link to={`/approvals?focus=${encodeURIComponent(pending.approval_id)}`}>Open approvals</Link>
          </p>
        ) : null}
        <div className="approval-grid">
          <div>
            <span className="fact-label">Security check</span>
            <span className="fact-value"><SecurityPill security={t.security} />{t.security?.valid_until ? <span className="muted small"> until <Time iso={t.security.valid_until} short /></span> : null}</span>
          </div>
          <div>
            <span className="fact-label">Draft pull request</span>
            <span className="fact-value">{pr ? <ExternalLink href={pr} label="Open the draft pull request" /> : <span className="muted">Not published yet</span>}</span>
          </div>
          <div>
            <span className="fact-label">CI on the change</span>
            <span className="fact-value"><span className={`pill pill-${ci.tone}`}>{ci.text}</span></span>
          </div>
          <div>
            <span className="fact-label">Behaviour test</span>
            <span className="fact-value"><span className={`pill pill-${behaviour.tone}`}>{behaviour.text}</span></span>
          </div>
          <div>
            <span className="fact-label">A person's judgement</span>
            <span className="fact-value"><span className={`pill pill-${judged.tone}`}>{judged.text}</span></span>
          </div>
          {t.support_status ? (
            <div>
              <span className="fact-label">Evidence behind it</span>
              <span className="fact-value"><span className={`pill pill-${t.support_status === "withdrawn" ? "critical" : "good"}`}>{t.support_status === "withdrawn" ? "Withdrawn" : "Still supported"}</span></span>
            </div>
          ) : null}
        </div>
        {t.checks ? <CheckList checks={t.checks} /> : null}
        <div className="actions-row">
          {t.state === "approved_a" ? (
            <Button variant="primary" icon={prepBlock.length ? "lock" : "play"} disabled={readOnly || prepBlock.length > 0} title={prepBlock.length ? `Stopped by: ${prepBlock[0]!.title}` : undefined} loading={prepare.state.status === "running"} onClick={() => void prepare.run({}).then(reload)}>Prepare the change</Button>
          ) : null}
          {t.state === "approved_b" ? (
            <Button variant="primary" icon={pubBlock.length ? "lock" : "arrow"} disabled={readOnly || pubBlock.length > 0} title={pubBlock.length ? `Stopped by: ${pubBlock[0]!.title}` : undefined} loading={publish.state.status === "running"} onClick={() => void publish.run({}).then(reload)}>Publish draft pull request</Button>
          ) : null}
          {t.state !== "approved_a" && t.state !== "approved_b" ? (
            <span className="muted small">{STEP_NOTE[t.state] ?? "Nothing to run at this step."}</span>
          ) : null}
          <Button variant="ghost" icon="shield" disabled={readOnly} loading={refresh.state.status === "running"} onClick={() => void refresh.run({}).then(reload)}>Refresh security check</Button>
          <BlockedNote by={[...prepBlock, ...pubBlock]} />
        </div>
        <ActionResult state={prepare.state} retry={() => void prepare.retrySame()} success="Change prepared" />
        <ActionResult state={publish.state} retry={() => void publish.retrySame()} success="Draft pull request published" />
        <ActionResult state={refresh.state} retry={() => void refresh.retrySame()} success="Security check refreshed" />
        <IdDetails items={[["Task id", t.task_id], ["Action id", t.action_id ?? null], ["Run id", t.run_id ?? null]]} />
      </Section>
      {canJudge || t.adjudication ? (
        <Section title="Does the fix work?" icon="scale">
          <label className="sr-only" htmlFor="judge-note">Note</label>
          <input id="judge-note" placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} disabled={readOnly || !canJudge} />
          <div className="actions-row">
            <ConfirmButton label="It works" title="Record that the fix works?" confirmLabel="Record" variant="primary" size="md" icon="ok" disabled={readOnly || !canJudge} onConfirm={() => void adjudicate.run({ verdict: "correct", note }).then(reload)} />
            <ConfirmButton label="It does not work" title="Record that the fix does not work?" confirmLabel="Record" size="md" danger disabled={readOnly || !canJudge} onConfirm={() => void adjudicate.run({ verdict: "incorrect", note }).then(reload)} />
          </div>
          <ActionResult state={adjudicate.state} retry={() => void adjudicate.retrySame()} success="Judgement recorded" />
        </Section>
      ) : null}
    </>
  );
}

/** On a run: turn an investigation that recommends the coding agent into a task. */
export function CreateTask({ runId }: { runId: string }) {
  const create = useAction<{ run_id: string }, { task?: TaskView }>("/api/tasks");
  const { navigate } = useRouter();
  const made = create.state.status === "done" ? create.state.result.task?.task_id : undefined;
  const report = useApi<{ report: { action?: { value?: string | null } | string | null } }>(`/api/reports/${encodeURIComponent(`${runId}:investigation`)}`);
  const action = report.data?.report.action;
  const recommended = (typeof action === "string" ? action : action?.value) === "DELEGATE_TO_CODING_AGENT";
  if (!report.data) return null;
  if (!recommended) return <p className="quiet-note"><Icon name="code" size={14} /> No code change was recommended, so there is no coding task to create.</p>;
  if (made) {
    return (
      <p className="state state-done toast-inline">
        Task created. <Link to={`/tasks/${encodeURIComponent(made)}`}>Open the task</Link>
      </p>
    );
  }
  return (
    <>
      <Button
        icon="code"
        loading={create.state.status === "running"}
        onClick={() =>
          void create.run({ run_id: runId }).then((r) => {
            const id = (r as { task?: TaskView } | undefined)?.task?.task_id;
            if (id) navigate(`/tasks/${encodeURIComponent(id)}`);
          })
        }
      >
        Create coding task
      </Button>
      <ActionResult state={create.state} retry={() => void create.retrySame()} done={() => "Task created"} />
    </>
  );
}
