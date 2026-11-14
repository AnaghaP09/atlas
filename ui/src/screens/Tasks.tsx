import { useState } from "react";
import type { TaskView } from "../api/types";
import { useAction, useApi } from "../app/context";
import type { Approval } from "../api/types";
import { ActionResult, Button, Empty, ErrorView, IdDetails, Loading, Section, StatusPill } from "../components/common";
import { EntityLink, Related } from "../components/related";
import { ExternalLink } from "../components/ExternalLink";
import { Link } from "../lib/router";

export function TaskList() {
  const data = useApi<{ tasks: TaskView[]; data_mode?: string }>("/api/tasks");
  return (
    <Section title="Coding-agent tasks" icon="task" mode={data.data?.data_mode}>
      {data.isLoading ? <Loading what="tasks" /> : <ErrorView error={data.error} />}
      {data.data && data.data.tasks.length === 0 ? <Empty>No tasks yet.</Empty> : null}
      {data.data && data.data.tasks.length ? (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Task</th><th>Action</th><th>Status</th><th>Run</th></tr></thead>
            <tbody>
              {data.data.tasks.map((t) => (
                <tr key={t.task_id}>
                  <td className="mono"><Link to={`/tasks/${encodeURIComponent(t.task_id)}`}>{t.task_id}</Link></td>
                  <td>{(t.action ?? "—").replace(/_/g, " ").toLowerCase()}</td>
                  <td><StatusPill status={t.state} /></td>
                  <td className="mono"><EntityLink entity="run" id={t.run_id} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </Section>
  );
}

function show(value: unknown): string {
  if (value === null || value === undefined) return "not yet (no publication)";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

export function TaskDetail({ taskId }: { taskId: string }) {
  const base = `/api/tasks/${encodeURIComponent(taskId)}`;
  const task = useApi<TaskView>(base);
  const prepare = useAction<Record<string, never>, unknown>(`${base}/prepare`);
  const publish = useAction<Record<string, never>, unknown>(`${base}/publish`);
  const refresh = useAction<Record<string, never>, unknown>(`${base}/security-refresh`);
  const adjudicate = useAction<{ verdict: string; note: string }, unknown>(`${base}/adjudicate`);
  const [note, setNote] = useState("");
  const approvals = useApi<{ approvals: Approval[] }>("/api/approvals");
  if (task.isLoading) return <Loading what="the task" />;
  if (task.error) return <ErrorView error={task.error} />;
  const t = task.data;
  if (!t) return null;
  return (
    <Section title={(t.action ?? "Task").replace(/_/g, " ").toLowerCase()} icon="task" mode={t.data_mode} end={<StatusPill status={t.state} />}>
      <Related
        chips={[
          { entity: "run", id: t.run_id, label: "Run" },
          { entity: "approval", id: (approvals.data?.approvals ?? []).find((a) => a.task_id === t.task_id)?.approval_id, label: "Approvals", count: (approvals.data?.approvals ?? []).filter((a) => a.task_id === t.task_id).length },
        ]}
      />
      <dl className="facts">
        <dt>Status</dt><dd data-testid="task-state">{t.state}</dd>
        {t.support_status ? (<><dt>Support</dt><dd>{t.support_status}</dd></>) : null}
        <dt>Security check</dt><dd>{t.security ? `${t.security.state ?? "unknown"}${t.security.valid_until ? `, valid until ${t.security.valid_until}` : ""}` : "not run"}</dd>
        <dt>Draft pull request</dt>
        <dd>{t.publication?.pr_url ? <ExternalLink href={t.publication.pr_url} label="Open the draft pull request" /> : "not published yet"}</dd>
        <dt>CI</dt><dd>{show(t.ci)}</dd>
        <dt>Behaviour test</dt><dd>{show(t.behavioural_check)}</dd>
        <dt>Person's judgement</dt><dd>{show(t.adjudication)}</dd>
      </dl>
      {t.checks ? (
        <>
          <h3>Checks for the next step</h3>
          <ul>{Object.entries(t.checks).map(([k, v]) => <li key={k}>{k}: {v}</li>)}</ul>
        </>
      ) : null}
      <div className="actions">
        <Button variant="primary" icon="play" loading={prepare.state.status === "running"} onClick={() => void prepare.run({})}>Prepare</Button>
        <Button icon="arrow" loading={publish.state.status === "running"} onClick={() => void publish.run({})}>Publish draft PR</Button>
        <Button variant="ghost" icon="shield" loading={refresh.state.status === "running"} onClick={() => void refresh.run({})}>Refresh security</Button>
        <ActionResult state={prepare.state} retry={() => void prepare.retrySame()} />
        <ActionResult state={publish.state} retry={() => void publish.retrySame()} />
        <ActionResult state={refresh.state} retry={() => void refresh.retrySame()} />
      </div>
      <IdDetails items={[["Task id", t.task_id], ["Action id", t.action_id ?? null], ["Run id", t.run_id ?? null], ["Head commit", t.publication?.head_commit ?? null]]} />
      <h3>Does the fix work?</h3>
      <label>
        Note
        <input value={note} onChange={(e) => setNote(e.target.value)} />
      </label>
      <button onClick={() => void adjudicate.run({ verdict: "works", note })}>It works</button>
      <button onClick={() => void adjudicate.run({ verdict: "does_not_work", note })}>It does not work</button>
      <ActionResult state={adjudicate.state} retry={() => void adjudicate.retrySame()} />
    </Section>
  );
}
