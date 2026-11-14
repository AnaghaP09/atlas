import { useEffect, useState, type ReactNode } from "react";
import type { Health, Notification } from "./api/types";
import { useApi, useApp, useIdentity } from "./app/context";
import { AgentName, ButtonLink, PageHead } from "./components/common";
import { BrandMark, Icon, type IconName } from "./components/icons";
import { signOut } from "./identity/bootstrap";
import { ROLES, ROLE_LABELS, type Role } from "./identity/identity";
import { applyTheme, setCollapsed, setTheme, storedCollapsed, storedTheme, systemTheme, type Theme } from "./lib/theme";
import { Link, matchPath, useRouter } from "./lib/router";
import { Approvals } from "./screens/Approvals";
import { Containment } from "./screens/Containment";
import { ActivityEvidence, Dashboard } from "./screens/Dashboard";
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

interface NavItem {
  to: string;
  label: string;
  icon: IconName;
}

const NAV_GROUPS: { title: string; items: NavItem[] }[] = [
  {
    title: "Overview",
    items: [
      { to: "/", label: "Home", icon: "home" },
      { to: "/dashboard", label: "Live dashboard", icon: "pulse" },
    ],
  },
  {
    title: "Work",
    items: [
      { to: "/run-now", label: "Run now", icon: "play" },
      { to: "/schedules", label: "Automatic runs", icon: "clock" },
      { to: "/runs", label: "Runs", icon: "list" },
      { to: "/queue", label: "Needs a person", icon: "hand" },
      { to: "/approvals", label: "Approvals", icon: "check" },
      { to: "/tasks", label: "Tasks", icon: "task" },
    ],
  },
  {
    title: "Safety",
    items: [
      { to: "/notifications", label: "Notifications", icon: "bell" },
      { to: "/containment", label: "Kill switches", icon: "power" },
    ],
  },
  {
    title: "Insight",
    items: [
      { to: "/scorecard", label: "Scorecard", icon: "chart" },
      { to: "/graph", label: "Knowledge graph", icon: "graph" },
      { to: "/feedback", label: "Record a decision", icon: "pen" },
      { to: "/evaluation", label: "Quality checks", icon: "gauge" },
    ],
  },
  {
    title: "Demo",
    items: [{ to: "/demo", label: "Guided demo", icon: "compass" }],
  },
];

interface Page {
  title: string;
  sub: string;
  action?: { to: string; label: string; icon: IconName };
}

/** Every screen gets a page header: a title, one line on what it is for, and its main action. */
const PAGES: Record<string, Page> = {
  "/": { title: "Overview", sub: "What ATLAS knows about this project right now.", action: { to: "/run-now", label: "Run now", icon: "play" } },
  "/dashboard": { title: "Live dashboard", sub: "New issues, CI results and releases, polled from the project's public sources." },
  "/activity-evidence/:id": { title: "Activity record", sub: "The exact version of a record that appeared on the live dashboard." },
  "/run-now": { title: "Run now", sub: "Start a run by hand. Every run gets the same checks, limits and audit record as an automatic one." },
  "/schedules": { title: "Automatic runs", sub: "Watchers and schedules that start runs on their own.", action: { to: "/run-now", label: "Run now", icon: "play" } },
  "/runs": { title: "Runs", sub: "Every investigation and release close, with its status and reports.", action: { to: "/run-now", label: "Start a run", icon: "play" } },
  "/runs/:id": { title: "Run", sub: "Scout's steps, Sentinel's checks, the budgets used and the reports produced." },
  "/reports/:id": { title: "Report", sub: "A verified report. Every claim links to the records it cites." },
  "/evidence/:id": { title: "Evidence", sub: "A cited record, shown as plain text." },
  "/queue": { title: "Needs a person", sub: "Items ATLAS will not decide on its own." },
  "/approvals": { title: "Approvals", sub: "Approval A lets a task be prepared. Approval B publishes the exact change shown." },
  "/tasks": { title: "Tasks", sub: "Bounded changes handed to the sandboxed coding agent, from proposal to acceptance." },
  "/tasks/:id": { title: "Task", sub: "Lifecycle, checks, CI results and the human judgement for one task." },
  "/notifications": { title: "Notifications", sub: "Errors, reminders and decisions, routed by role." },
  "/containment": { title: "Kill switches", sub: "Each switch stops a part of ATLAS when something unsafe happens. Resuming needs proof." },
  "/scorecard": { title: "Scorecard", sub: "Whether the coding agent helped, compared with a human baseline." },
  "/graph": { title: "Knowledge graph", sub: "Components, owner teams and the import links between them, for one frozen snapshot." },
  "/feedback": { title: "Record a decision", sub: "Overrides and debt-acceptance decisions. They become test cases for later runs." },
  "/evaluation": { title: "Quality checks", sub: "The quality bars ATLAS must pass. A check that is not measured is never shown as passed." },
  "/demo": { title: "Guided demo", sub: "Walk through each part of ATLAS in order, on the right server for each step." },
};

export function Route({ path, search }: { path: string; search: URLSearchParams }): ReactNode {
  const routes: [string, (p: Record<string, string>) => ReactNode][] = [
    ["/", () => <Home />],
    ["/dashboard", () => <Dashboard />],
    [
      "/activity-evidence/:id",
      (p) => <ActivityEvidence recordId={p.id ?? ""} project={search.get("project") ?? ""} version={search.get("version") ?? ""} />,
    ],
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
    if (params) {
      const page = PAGES[pattern];
      return (
        <>
          {page ? (
            <PageHead
              title={page.title}
              sub={page.sub}
              actions={page.action ? <ButtonLink to={page.action.to} variant="primary" icon={page.action.icon}>{page.action.label}</ButtonLink> : null}
            />
          ) : null}
          {render(params)}
        </>
      );
    }
  }
  return (
    <>
      <PageHead title="Page not found" sub="This address does not match any screen." />
      <p><Link to="/">Go to the overview</Link></p>
    </>
  );
}

/** Demo identity: switch project with the same reset as a role switch. */
function HeaderProjectSwitcher() {
  const { identity } = useApp();
  const { projectId } = useIdentity();
  const list = useApi<{ projects: { id: string; name: string }[]; current: string | null }>(
    identity.provider.mode === "header" ? "/api/projects" : null,
  );
  if (identity.provider.mode !== "header" || !list.data || list.data.projects.length < 2) return null;
  const current = projectId ?? list.data.current ?? "";
  return (
    <label className="top-field role-switcher">
      Project
      <select
        aria-label="Project"
        value={current}
        onChange={(e) => identity.transition({ projectId: e.target.value, principal: identity.snapshot.principal })}
      >
        {list.data.projects.map((p) => (
          <option key={p.id} value={p.id}>{p.name || p.id}</option>
        ))}
      </select>
    </label>
  );
}

function RoleSwitcher() {
  const { identity } = useApp();
  const { principal, switchRole } = useIdentity();
  if (!identity.provider.rolePicker) return null;
  return (
    <label className="top-field role-switcher">
      <span className="demo-id">{identity.provider.label}</span>
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
  const initials = session.display_name
    .split(/\s+/)
    .map((w) => w[0] ?? "")
    .join("")
    .slice(0, 2)
    .toUpperCase();
  return (
    <span className="session-bar">
      {projects.length > 1 ? (
        <label className="top-field">
          Project
          <select aria-label="Project" value={projectId ?? ""} onChange={(e) => switchProject(e.target.value)}>
            {projects.map((p) => (
              <option key={p} value={p}>{p}</option>
            ))}
          </select>
        </label>
      ) : (
        <span className="muted">Project: {projectId ?? "none"}</span>
      )}
      <span className="who">
        <span className="avatar" aria-hidden="true">{initials || "?"}</span>
        <span>
          {session.display_name}
          {principal ? <span className="muted"> · {ROLE_LABELS[principal.role]}</span> : null}
        </span>
      </span>
      <button type="button" className="btn btn-ghost btn-sm" onClick={() => void signOut(identity)}>Sign out</button>
    </span>
  );
}

type HealthWithSpend = Health & { spend?: { used_usd?: number; ceiling_usd?: number } | null };

function ModeBadge() {
  const health = useApi<HealthWithSpend>("/api/health");
  if (!health.data) return null;
  const offline = health.data.mode === "offline";
  return (
    <span className={`mode-pill${offline ? " offline" : ""}`} title={`${health.data.environment} server`}>
      <span className="pulse" aria-hidden="true" />
      {offline ? "Offline" : "Connected"}
    </span>
  );
}

function SpendMeter() {
  const health = useApi<HealthWithSpend>("/api/health");
  const spend = health.data?.spend;
  if (!spend || typeof spend.used_usd !== "number" || typeof spend.ceiling_usd !== "number") return null;
  return (
    <span className="spend" title="Model spend against the configured ceiling">
      <span>
        Spend <span className="num">${spend.used_usd.toFixed(2)}</span> of ${spend.ceiling_usd.toFixed(0)}
      </span>
      <progress className={`bar thin${spend.used_usd / Math.max(spend.ceiling_usd, 1) >= 0.8 ? " hot" : ""}`} max={Math.max(spend.ceiling_usd, 1)} value={Math.min(spend.used_usd, spend.ceiling_usd)} />
    </span>
  );
}

function Bell() {
  const notes = useApi<{ notifications: Notification[] }>("/api/notifications");
  const unread = (notes.data?.notifications ?? []).filter((n) => !n.acknowledged).length;
  return (
    <Link to="/notifications" className="bell" aria-label={`Notifications, ${unread} unread`}>
      <Icon name="bell" size={18} />
      {unread > 0 ? <span className="bell-count num">{unread > 99 ? "99+" : unread}</span> : null}
    </Link>
  );
}

function ThemeToggle() {
  const [theme, setThemeState] = useState<Theme>(() => storedTheme() ?? systemTheme());
  useEffect(() => {
    applyTheme(storedTheme());
  }, []);
  const next: Theme = theme === "dark" ? "light" : "dark";
  return (
    <button
      type="button"
      className="btn btn-secondary theme-toggle"
      aria-label={`Switch to ${next} theme`}
      title={`Switch to ${next} theme`}
      onClick={() => {
        setTheme(next);
        setThemeState(next);
      }}
    >
      <Icon name={theme === "dark" ? "sun" : "moon"} size={18} />
    </button>
  );
}

function isActive(to: string, path: string) {
  return to === "/" ? path === "/" : path === to || path.startsWith(`${to}/`);
}

export function App() {
  const { path, search } = useRouter();
  const { epoch } = useIdentity();
  const [collapsed, setCollapsedState] = useState(storedCollapsed);
  return (
    <div className={`app${collapsed ? " collapsed" : ""}`}>
      <aside className="side">
        <Link to="/" className="brand" aria-label="ATLAS home">
          <BrandMark />
          <span className="brand-text">
            <span className="brand-name">ATLAS</span>
            <span className="brand-sub">Engineering control plane</span>
          </span>
        </Link>
        <nav aria-label="Main">
          {NAV_GROUPS.map((g) => (
            <div className="nav-group" key={g.title}>
              <p className="nav-group-title">{g.title}</p>
              <ul>
                {g.items.map((item) => (
                  <li key={item.to}>
                    <Link to={item.to} className={`nav-link${isActive(item.to, path) ? " active" : ""}`}>
                      <Icon name={item.icon} size={18} />
                      <span className="nav-label">{item.label}</span>
                    </Link>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </nav>
        <div className="side-foot">
          <div className="agents" aria-label="Agents">
            <span className="agent-line"><AgentName agent="scout" /></span>
            <span className="agent-line"><AgentName agent="sentinel" /></span>
            <span>Agents: Scout investigates, Sentinel checks every claim.</span>
          </div>
          <button
            type="button"
            className="btn collapse-btn"
            aria-label={collapsed ? "Expand the sidebar" : "Collapse the sidebar"}
            onClick={() => {
              setCollapsedState(!collapsed);
              setCollapsed(!collapsed);
            }}
          >
            <Icon name="menu" size={16} />
            <span className="nav-label">{collapsed ? "Expand" : "Collapse"}</span>
          </button>
        </div>
      </aside>
      <header className="top">
        <HeaderProjectSwitcher />
        <ModeBadge />
        <span className="spacer" />
        <SpendMeter />
        <RoleSwitcher />
        <SessionBar />
        <Bell />
        <ThemeToggle />
      </header>
      {/* Keyed by the identity epoch: a role or project switch remounts every screen and drops its state. */}
      <main key={epoch} className="main">
        <Route path={path} search={search} />
      </main>
    </div>
  );
}
