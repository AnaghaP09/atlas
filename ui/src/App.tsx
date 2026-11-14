import type { ReactNode } from "react";
import type { Health } from "./api/types";
import { useApi, useApp, useIdentity } from "./app/context";
import { DataModeBadge } from "./components/common";
import { signOut } from "./identity/bootstrap";
import { ROLES, ROLE_LABELS, type Role } from "./identity/identity";
import { Link, matchPath, useRouter } from "./lib/router";
import { Approvals } from "./screens/Approvals";
import { Containment } from "./screens/Containment";
import { Evaluation } from "./screens/Evaluation";
import { Feedback } from "./screens/Feedback";
import { Graph } from "./screens/Graph";
import { GuidedDemo } from "./screens/GuidedDemo";
import { Home } from "./screens/Home";
import { Notifications } from "./screens/Notifications";
import { Queue } from "./screens/Queue";
import { EvidenceView, ReportDetail } from "./screens/Reports";
import { RunDetail, RunList } from "./screens/Runs";
import { RunNow } from "./screens/RunNow";
import { Schedules } from "./screens/Schedules";
import { Scorecard } from "./screens/Scorecard";
import { TaskDetail, TaskList } from "./screens/Tasks";

const NAV: [string, string][] = [
  ["/", "Home"],
  ["/run-now", "Run now"],
  ["/schedules", "Automatic runs"],
  ["/runs", "Runs"],
  ["/queue", "Needs a person"],
  ["/approvals", "Approvals"],
  ["/tasks", "Tasks"],
  ["/notifications", "Notifications"],
  ["/containment", "Kill switches"],
  ["/scorecard", "Scorecard"],
  ["/graph", "Knowledge graph"],
  ["/feedback", "Record a decision"],
  ["/evaluation", "Quality checks"],
  ["/demo", "Guided demo"],
];

export function Route({ path, search }: { path: string; search: URLSearchParams }): ReactNode {
  const routes: [string, (p: Record<string, string>) => ReactNode][] = [
    ["/", () => <Home />],
    ["/run-now", () => <RunNow />],
    ["/schedules", () => <Schedules />],
    ["/runs", () => <RunList />],
    ["/runs/:id", (p) => <RunDetail runId={p.id ?? ""} />],
    ["/reports/:id", (p) => <ReportDetail reportId={p.id ?? ""} />],
    ["/evidence/:id", (p) => <EvidenceView recordId={p.id ?? ""} runId={search.get("run") ?? ""} />],
    ["/queue", () => <Queue />],
    ["/approvals", () => <Approvals />],
    ["/tasks", () => <TaskList />],
    ["/tasks/:id", (p) => <TaskDetail taskId={p.id ?? ""} />],
    ["/notifications", () => <Notifications />],
    ["/containment", () => <Containment />],
    ["/scorecard", () => <Scorecard />],
    ["/graph", () => <Graph />],
    ["/feedback", () => <Feedback />],
    ["/evaluation", () => <Evaluation />],
    ["/demo", () => <GuidedDemo />],
  ];
  for (const [pattern, render] of routes) {
    const params = matchPath(pattern, path);
    if (params) return render(params);
  }
  return <p>Page not found. <Link to="/">Go home</Link></p>;
}

function RoleSwitcher() {
  const { identity } = useApp();
  const { principal, switchRole } = useIdentity();
  if (!identity.provider.rolePicker) return null;
  return (
    <label className="role-switcher">
      <span className="muted">{identity.provider.label}:</span>{" "}
      <select
        aria-label="Role"
        value={principal?.role ?? ""}
        onChange={(e) => switchRole({ role: e.target.value as Role })}
      >
        {ROLES.map((r) => (
          <option key={r} value={r}>{ROLE_LABELS[r]}</option>
        ))}
      </select>
    </label>
  );
}

/** Signed-in mode: who is signed in, the project switcher and sign-out. */
export function SessionBar() {
  const { identity } = useApp();
  const { principal, projectId, switchProject } = useIdentity();
  const session = identity.session;
  if (identity.provider.mode !== "session" || !session) return null;
  const projects = Object.keys(session.projects).sort();
  return (
    <span className="session-bar">
      <span>{session.display_name}</span>
      {principal ? <span className="muted"> · {ROLE_LABELS[principal.role]}</span> : null}{" "}
      {projects.length > 1 ? (
        <label>
          <span className="muted">Project:</span>{" "}
          <select aria-label="Project" value={projectId ?? ""} onChange={(e) => switchProject(e.target.value)}>
            {projects.map((p) => (
              <option key={p} value={p}>{p}</option>
            ))}
          </select>
        </label>
      ) : (
        <span className="muted">Project: {projectId ?? "none"}</span>
      )}{" "}
      <button type="button" onClick={() => void signOut(identity)}>Sign out</button>
    </span>
  );
}

function StatusLine() {
  const health = useApi<Health>("/api/health");
  if (!health.data) return null;
  const h = health.data;
  return (
    <span className="status-line">
      {h.mode === "offline" ? <strong>Offline</strong> : "Connected"} · {h.environment} server ·{" "}
      <DataModeBadge mode={h.data_mode ?? "REAL"} />
    </span>
  );
}

export function App() {
  const { path, search } = useRouter();
  const { epoch } = useIdentity();
  return (
    <div className="app">
      <header className="top">
        <strong className="brand">ATLAS</strong>
        <span className="muted">Agents: Scout investigates, Sentinel checks every claim.</span>
        <StatusLine />
        <RoleSwitcher />
        <SessionBar />
      </header>
      <nav className="side" aria-label="Main">
        <ul>
          {NAV.map(([to, label]) => (
            <li key={to}>
              <Link to={to} className={path === to ? "active" : undefined}>{label}</Link>
            </li>
          ))}
        </ul>
      </nav>
      {/* Keyed by the identity epoch: a role or project switch remounts every screen and drops its state. */}
      <main key={epoch} className="main">
        <Route path={path} search={search} />
      </main>
    </div>
  );
}
