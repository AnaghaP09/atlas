import { useState } from "react";
import type { TaskView } from "../api/types";
import { useAction, useApi } from "../app/context";
import { ActionResult, ErrorView, Loading, Section } from "../components/common";
import { ExternalLink } from "../components/ExternalLink";
import { Link } from "../lib/router";

export function TaskList() {
  const data = useApi<{ tasks: TaskView[]; data_mode?: string }>("/api/tasks");
  return (
    <Section title="Coding-agent tasks" mode={data.data?.data_mode}>
      {data.isLoading ? <Loading what="tasks" /> : <ErrorView error={data.error} />}
      {data.data && data.data.tasks.length === 0 ? <p>No tasks.</p> : null}
      <ul>
        {(data.data?.tasks ?? []).map((t) => (
          <li key={t.task_id}>
            <Link to={`/tasks/${encodeURIComponent(t.task_id)}`}>{t.task_id}</Link> — {t.state}
          </li>
        ))}
      </ul>
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
  if (task.isLoading) return <Loading what="the task" />;
  if (task.error) return <ErrorView error={task.error} />;
  const t = task.data;
  if (!t) return null;
  return (
    <Section title={`Task ${t.task_id}`} mode={t.data_mode}>
      <dl className="facts">
        <dt>State</dt><dd data-testid="task-state">{t.state}</dd>
        <dt>Action</dt><dd>{t.action ?? "—"}</dd>
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
        <button onClick={() => void prepare.run({})}>Prepare in the sandbox</button>
        <ActionResult state={prepare.state} retry={() => void prepare.retrySame()} />
        <button onClick={() => void publish.run({})}>Publish the draft pull request</button>
        <ActionResult state={publish.state} retry={() => void publish.retrySame()} />
        <button onClick={() => void refresh.run({})}>Refresh the security check</button>
        <ActionResult state={refresh.state} retry={() => void refresh.retrySame()} />
      </div>
      {t.run_id ? <p><Link to={`/runs/${encodeURIComponent(t.run_id)}`}>Open the run behind this task</Link></p> : null}
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
