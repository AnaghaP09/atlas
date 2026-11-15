import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useAction, useApi, useApp, useIdentity, useInvalidateAll, useScopedKey } from "../app/context";
import { ActionResult, ErrorView, Loading, Section } from "../components/common";
import { Icon } from "../components/icons";
import { Link } from "../lib/router";

export interface DetectField {
  key: string;
  label: string;
  value: string | string[];
  status: "detected" | "needs you";
  evidence: string;
}

export interface Proposal {
  repository: string;
  fields: DetectField[];
  project: Record<string, unknown>;
  block: string;
  api_requests: number;
  facts: { releases: { tag: string; date: string | null }[]; open_issues_and_pull_requests: number; size_kb: number };
}

interface Added {
  id: string;
  name: string;
  repository: string;
  block: string;
  live: { polling: boolean; reason?: string };
}

interface CopyJob {
  job_id: string;
  tag: string;
  state: "queued" | "running" | "done" | "failed" | "refused";
  stage: string;
  done: number;
  total: number;
  requests: number;
  budget: number;
  message: string;
  title: string | null;
  snapshot_id: string | null;
  started_at: string;
}

const EDITABLE = new Set(["project_id", "name", "ci", "release_tag_pattern", "issue_query", "security_inventory",
  "labels.bug", "labels.flaky", "labels.critical", "labels.regression"]);

function show(v: string | string[]): string {
  return Array.isArray(v) ? v.join(", ") || "none" : v;
}

/** The confirmed project as the API takes it, from the proposal and the person's changes. */
export function projectBody(p: Proposal, edits: Record<string, string>): Record<string, unknown> {
  const value = (key: string) => {
    if (key in edits) return edits[key] ?? "";
    const f = p.fields.find((x) => x.key === key);
    return f ? show(f.value) : "";
  };
  const goModule = value("go_module");
  return {
    id: value("project_id"),
    name: value("name"),
    repository: p.repository,
    ci: value("ci"),
    release_tag_pattern: value("release_tag_pattern"),
    issue_query: value("issue_query"),
    security_inventory: value("security_inventory"),
    labels: { bug: value("labels.bug"), flaky: value("labels.flaky"), critical: value("labels.critical"), regression: value("labels.regression") },
    sources: goModule && goModule !== "none" ? { go_module: goModule } : {},
  };
}

function Chip({ status }: { status: DetectField["status"] }) {
  return status === "detected" ? <span className="pill pill-neutral">detected</span> : <span className="pill pill-warn">needs you</span>;
}

function FieldRow({ f, value, onChange }: { f: DetectField; value: string; onChange: (v: string) => void }) {
  const editable = EDITABLE.has(f.key);
  return (
    <tr>
      <th scope="row">{f.label}</th>
      <td>
        {!editable ? (
          <span>{show(f.value) || "—"}</span>
        ) : f.key === "ci" ? (
          <select aria-label={f.label} value={value} onChange={(e) => onChange(e.target.value)}>
            <option value="github_actions">GitHub Actions</option>
            <option value="prow">Prow (set up by hand)</option>
            <option value="">None</option>
          </select>
        ) : (
          <input aria-label={f.label} type="text" value={value} onChange={(e) => onChange(e.target.value)} />
        )}
      </td>
      <td><Chip status={f.status} /></td>
      <td className="muted small">{f.evidence}</td>
    </tr>
  );
}

function Review({ proposal, onAdded }: { proposal: Proposal; onAdded: (a: Added) => void }) {
  const [edits, setEdits] = useState<Record<string, string>>({});
  const add = useAction<{ project: Record<string, unknown> }, Added>("/api/projects");
  const added = add.state.status === "done" ? add.state.result : null;
  const told = useRef(false);
  useEffect(() => {
    if (added && !told.current) {
      told.current = true;
      onAdded(added);
    }
  }, [added, onAdded]);
  const open = proposal.fields.filter((f) => f.status === "needs you" && EDITABLE.has(f.key) && !(f.key in edits)).length;
  return (
    <Section title={`Review ${proposal.repository}`} icon="list" end={<span className="muted">{proposal.api_requests} GitHub reads</span>}>
      <div className="table-wrap">
      <table data-testid="detected">
        <thead>
          <tr><th>Setting</th><th>Value</th><th>Source</th><th>Evidence</th></tr>
        </thead>
        <tbody>
          {proposal.fields.map((f) => (
            <FieldRow key={f.key} f={f} value={edits[f.key] ?? show(f.value)} onChange={(v) => setEdits({ ...edits, [f.key]: v })} />
          ))}
        </tbody>
      </table>
      </div>
      <p className="muted">{open ? `${open} values need you. Empty is allowed.` : "Ready to add."} Publish targets are never set here.</p>
      <div className="actions-row">
        <button type="button" className="btn btn-primary btn-md" disabled={add.state.status === "running" || Boolean(added)}
          onClick={() => void add.run({ project: projectBody(proposal, edits) })}>
          <Icon name="check" size={16} /> Add project
        </button>
      </div>
      <ActionResult state={add.state} retry={() => void add.retrySame()} done={(r) => `Added ${r.name}.`} />
    </Section>
  );
}

function Jobs({ projectId }: { projectId: string }) {
  const { client } = useApp();
  const path = `/api/projects/${encodeURIComponent(projectId)}/data-copies`;
  const key = useScopedKey(path, "jobs");
  const jobs = useQuery<{ jobs: CopyJob[] }, Error>({
    queryKey: key,
    queryFn: () => client.get<{ jobs: CopyJob[] }>(path),
    refetchInterval: (q) => ((q.state.data?.jobs ?? []).some((j) => j.state === "running" || j.state === "queued") ? 2000 : false),
  });
  if (!jobs.data?.jobs.length) return null;
  return (
    <ul className="list" data-testid="copy-jobs">
      {jobs.data.jobs.map((j) => (
        <li key={j.job_id} className="copy-job">
          <span className="grow">
            <strong>{j.title ?? `Release ${j.tag}`}</strong>
            <br />
            <span className="muted small">
              {j.state === "running" ? `${j.stage}${j.total ? ` (${j.done} of ${j.total})` : ""}` : j.message || j.stage} · {j.requests} of {j.budget} GitHub reads
            </span>
            {j.state === "running" && j.total ? <progress className="bar thin" max={j.total} value={j.done} /> : null}
          </span>
          <span className={`pill pill-${j.state === "done" ? "good" : j.state === "running" || j.state === "queued" ? "neutral" : "warn"}`}>
            {j.state === "done" ? "Ready" : j.state === "running" ? "Building" : j.state === "queued" ? "Waiting" : j.state === "refused" ? "Refused" : "Failed"}
          </span>
          {j.state === "done" && j.snapshot_id ? <Link to={`/run-now?snapshot=${encodeURIComponent(j.snapshot_id)}`}>Run release close</Link> : null}
        </li>
      ))}
    </ul>
  );
}

function DataCopies() {
  const { projectId } = useIdentity();
  const pid = projectId ?? "";
  const tags = useApi<{ tags: { tag: string; date: string | null; copy_exists: boolean }[] }>(
    pid ? `/api/projects/${encodeURIComponent(pid)}/release-tags` : null,
  );
  const [tag, setTag] = useState("");
  const build = useAction<{ tag: string }, CopyJob>(`/api/projects/${encodeURIComponent(pid)}/data-copies`);
  const refresh = useInvalidateAll();
  const chosen = tag || tags.data?.tags[0]?.tag || "";
  if (!pid) return null;
  return (
    <Section title={`Data as of a release · ${pid}`} icon="database">
      {tags.isLoading ? <Loading what="release tags" lines={1} /> : null}
      {tags.error ? <ErrorView error={tags.error} /> : null}
      {tags.data ? (
        tags.data.tags.length ? (
          <div className="actions-row">
            <label>
              Release tag
              <select aria-label="Release tag" value={chosen} onChange={(e) => setTag(e.target.value)}>
                {tags.data.tags.map((t) => (
                  <option key={t.tag} value={t.tag}>
                    {t.tag}{t.date ? ` · ${t.date.slice(0, 10)}` : ""}{t.copy_exists ? " · built" : ""}
                  </option>
                ))}
              </select>
            </label>
            <button type="button" className="btn btn-primary btn-md" disabled={!chosen || build.state.status === "running"}
              onClick={() => void build.run({ tag: chosen }).then(refresh)}>
              <Icon name="database" size={16} /> Build copy
            </button>
          </div>
        ) : (
          <p className="muted">No release tags match this project's tag pattern.</p>
        )
      ) : null}
      <ActionResult state={build.state} retry={() => void build.retrySame()} done={() => "Building."} />
      <Jobs projectId={pid} />
    </Section>
  );
}

/** For the audit role: add a public repository as a project, then build its Data as of copies. */
export function AddProject() {
  const { identity } = useApp();
  const defaults = useApi<{ repository: string; connected: boolean; github_token: boolean }>("/api/projects/add/defaults");
  const [link, setLink] = useState<string | null>(null);
  const detect = useAction<{ repository: string }, Proposal>("/api/projects/detect");
  const [added, setAdded] = useState<Added | null>(null);
  const refresh = useInvalidateAll();
  const repo = link ?? defaults.data?.repository ?? "";
  const proposal = detect.state.status === "done" ? detect.state.result : null;
  return (
    <>
      <Section title="Repository" icon="search">
        <form className="actions-row" onSubmit={(e) => { e.preventDefault(); setAdded(null); void detect.run({ repository: repo.trim() }); }}>
          <label className="grow">
            Public GitHub link
            <input aria-label="Public GitHub link" type="text" placeholder="https://github.com/owner/name" value={repo}
              onChange={(e) => setLink(e.target.value)} />
          </label>
          <button type="submit" className="btn btn-primary btn-md" disabled={!repo.trim() || detect.state.status === "running"}>
            <Icon name="search" size={16} /> Detect
          </button>
        </form>
        {defaults.data && !defaults.data.github_token ? <p className="muted small">No GitHub token: large repositories cannot get Data as of copies.</p> : null}
        {detect.state.status === "failed" || detect.state.status === "uncertain" || detect.state.status === "running" ? (
          <ActionResult state={detect.state} retry={() => void detect.retrySame()} />
        ) : null}
      </Section>
      {proposal && !added ? <Review key={proposal.repository} proposal={proposal} onAdded={(a) => { setAdded(a); refresh(); }} /> : null}
      {added ? (
        <Section title={`${added.name} added`} icon="ok">
          <p>{added.live.polling ? "Live polling started." : added.live.reason}</p>
          <div className="actions-row">
            <button type="button" className="btn btn-primary btn-md"
              onClick={() => identity.transition({ projectId: added.id, principal: identity.snapshot.principal })}>
              <Icon name="arrow" size={16} /> Switch to {added.name}
            </button>
          </div>
        </Section>
      ) : null}
      <DataCopies />
    </>
  );
}
