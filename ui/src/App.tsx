import { useEffect, useState, type ReactNode } from "react";
import type { Health } from "./api/types";
import { useApi, useApp, useIdentity } from "./app/context";
import { AgentName, ButtonLink, PageHead } from "./components/common";
import { BrandMark, Icon, type IconName } from "./components/icons";
import { LatestReport } from "./screens/LatestReport";
import { LockedChip } from "./components/access";
import { AccessRequests } from "./screens/AccessRequests";
import { ContainmentBanner } from "./screens/Containment";
import { MetricsSync, useMetrics } from "./lib/metrics";
import { SystemScreen } from "./screens/System";
import { signOut } from "./identity/bootstrap";
import { ROLES, ROLE_LABELS, type Role } from "./identity/identity";
import { allowedPages, findPage, joinRoles, PAGE_ICON, pageIdForPath, type Navigation } from "./lib/nav";
import { applyTheme, setCollapsed, setTheme, storedCollapsed, storedTheme, systemTheme, type Theme } from "./lib/theme";
import { Link, matchPath, useRouter } from "./lib/router";
import { helpFor, loadHelpIndex, onHelpIndex } from "./lib/helpIndex";
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
  "/access-requests": { title: "Access requests", sub: "" },
  "/system": { title: "System and mode", sub: "Health, Connected or Offline mode, open incidents and failed notification deliveries." },
  "/latest-report/:type": { title: "Latest report", sub: "The most recent verified report of this kind for the project." },
};

export function Route({ path, search, nav }: { path: string; search: URLSearchParams; nav?: Navigation }): ReactNode {
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
    ["/system", () => <SystemScreen />],
    ["/access-requests", () => <AccessRequests />],
    ["/latest-report/:type", (p) => <LatestReport kind={p.type ?? ""} />],
  ];
  const pageId = pageIdForPath(path);
  const entry = pageId && nav ? findPage(nav, pageId) : undefined;
  if (pageId && nav && !allowedPages(nav).has(pageId)) {
    const hidden = nav.other_pages.find((p) => p.page_id === pageId);
    return (
      <>
        <PageHead title={hidden?.title ?? "Not on your menu"} role={capitalise(nav.role_title)} />
        <div className="role-notice" role="note" data-testid="role-notice">
          <strong>This page is for the {hidden ? joinRoles(hidden.role_titles) : "other roles"}.</strong>
          <LockedChip objectType="page" objectId={pageId} />
          <ButtonLink to="/" variant="ghost" icon="home">Your home</ButtonLink>
        </div>
      </>
    );
  }
  for (const [pattern, render] of routes) {
    const params = matchPath(pattern, path);
    if (params) {
      const page = PAGES[pattern];
      return (
        <>
          {page ? (
            <PageHead
              title={entry?.title && pageId?.startsWith("report_") ? entry.title : page.title}
              role={nav?.role_title ? capitalise(nav.role_title) : undefined}
              help={pageId ? helpFor(pageId) : null}
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
      <PageHead title="Page not found" />
      <p><Link to="/">Go to the overview</Link></p>
    </>
  );
}

function capitalise(s: string): string {
  return s ? s[0]!.toUpperCase() + s.slice(1) : s;
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
  const m = useMetrics();
  const used = m.data?.spend_used_usd;
  const ceiling = m.data?.spend_ceiling_usd;
  if (typeof used !== "number" || typeof ceiling !== "number") return null;
  return (
    <span className="spend" title="Model spend">
      <span>
        Spend <span className="num">${used.toFixed(2)}</span> of ${ceiling.toFixed(0)}
      </span>
      <progress className={`bar thin${used / Math.max(ceiling, 1) >= 0.8 ? " hot" : ""}`} max={Math.max(ceiling, 1)} value={Math.min(used, ceiling)} />
    </span>
  );
}

function Bell() {
  const m = useMetrics();
  const unread = m.data?.unread_notifications ?? 0;
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

function NavBadge({ pageId }: { pageId: string }) {
  const m = useMetrics();
  const n =
    pageId === "queue" ? m.data?.needs_a_person : pageId === "approvals" ? m.data?.approvals_pending : pageId === "notifications" ? m.data?.unread_notifications : pageId === "containment" ? m.data?.kill_switches_tripped : null;
  if (!n) return null;
  return <span className={`nav-count num${pageId === "containment" ? " danger" : ""}`}>{n > 99 ? "99+" : n}</span>;
}

function isActive(to: string, path: string) {
  return to === "/" ? path === "/" : path === to || path.startsWith(`${to}/`);
}

export function App() {
  const { path, search } = useRouter();
  const { epoch } = useIdentity();
  const [collapsed, setCollapsedState] = useState(storedCollapsed);
  // Keyed by role and project like every other read, so a switch fetches the new menu.
  const nav = useApi<Navigation>("/api/navigation");
  const [, setHelpReady] = useState(0);
  useEffect(() => {
    loadHelpIndex();
    return onHelpIndex(() => setHelpReady((n) => n + 1));
  }, []);
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
          {nav.data ? (
            nav.data.groups.map((g) => (
              <div className="nav-group" key={g.title}>
                <p className="nav-group-title">{g.title}</p>
                <ul>
                  {g.pages.map((item) => (
                    <li key={item.page_id}>
                      <Link to={item.path} className={`nav-link${isActive(item.path, path) ? " active" : ""}`} title={item.for}>
                        <Icon name={PAGE_ICON[item.page_id] ?? "list"} size={18} />
                        <span className="nav-label">{item.title}</span>
                        <NavBadge pageId={item.page_id} />
                      </Link>
                    </li>
                  ))}
                </ul>
              </div>
            ))
          ) : (
            <div className="nav-loading" aria-hidden="true">
              <span className="skeleton w-80" /><span className="skeleton w-60" /><span className="skeleton w-80" />
            </div>
          )}
          {nav.data ? <p className="nav-role">Menu for the {nav.data.role_title}</p> : null}
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
      <MetricsSync />
      <main key={epoch} className="main">
        <ContainmentBanner />
        <Route path={path} search={search} nav={nav.data} />
      </main>
    </div>
  );
}
