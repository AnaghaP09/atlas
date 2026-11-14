// Who the user is, and the "identity epoch".
//
// Sign-in is a pluggable provider. This build uses the header provider: the user picks a demo role
// and every request carries it in a header. A sign-in provider (session cookie) can replace it
// without touching the screens.
//
// Every change of role or project starts a new epoch. Starting an epoch aborts all requests and
// live streams of the old epoch, and lets the app clear cached data and screen state. Anything that
// finishes later and still belongs to an old epoch is ignored.

export const ROLES = ["pm", "em", "architect", "security", "oncall", "audit"] as const;
export type Role = (typeof ROLES)[number];

export const ROLE_LABELS: Record<Role, string> = {
  pm: "Product manager",
  em: "Engineering manager",
  architect: "Solution architect",
  security: "Security engineer",
  oncall: "On-call engineer",
  audit: "Auditor",
};

export interface Principal {
  role: Role;
}

/** What the server says about a signed-in user (GET /api/session). */
export interface SessionInfo {
  user: string;
  display_name: string;
  /** Role per project, for example { kubevirt: "pm", "argo-cd": "pm" }. */
  projects: Record<string, Role>;
  default_project: string | null;
  csrf_token: string;
  expires_at: number;
  idle_seconds: number;
}

/** Facts a provider needs to build the headers for one request. */
export interface RequestContext {
  write: boolean;
  csrfToken: string | null;
}

export interface IdentityProvider {
  /** "header": the demo role header. "session": built-in sign-in with a session cookie. */
  readonly mode: "header" | "session";
  /** Short name shown to the user, for example "demo identity". */
  readonly label: string;
  /** Whether the user picks a role in the UI (true) or the server decides after sign-in (false). */
  readonly rolePicker: boolean;
  /** Headers added to every API request. */
  requestHeaders(principal: Principal | null, ctx?: RequestContext): Record<string, string>;
  /** Query parameters added to every API path (the selected project, in sign-in mode). */
  scopeQuery(projectId: string | null): Record<string, string>;
  /** Called when the server says the user is not signed in. */
  onUnauthorised(): void;
}

export const headerIdentity: IdentityProvider = {
  mode: "header",
  label: "Demo identity (not sign-in)",
  rolePicker: true,
  requestHeaders: (principal): Record<string, string> => {
    const h: Record<string, string> = {};
    if (principal) h["X-Atlas-Role"] = principal.role;
    return h;
  },
  scopeQuery: () => ({}),
  onUnauthorised: () => undefined,
};

/**
 * Built-in sign-in. The browser sends the session cookie by itself; the UI adds the CSRF token
 * to every write and the selected project to every request. It never sends a role header.
 */
export function sessionIdentity(navigate: (url: string) => void = (url) => window.location.assign(url)): IdentityProvider {
  return {
    mode: "session",
    label: "Signed in",
    rolePicker: false,
    requestHeaders: (_principal, ctx): Record<string, string> =>
      ctx?.write && ctx.csrfToken ? { "X-CSRF-Token": ctx.csrfToken } : {},
    scopeQuery: (projectId): Record<string, string> => (projectId ? { project_id: projectId } : {}),
    onUnauthorised: () => navigate("/login"),
  };
}

export interface IdentitySnapshot {
  principal: Principal | null;
  projectId: string | null;
  epoch: number;
  signal: AbortSignal;
}

type Listener = (snap: IdentitySnapshot) => void;

export class IdentityStore {
  private principal: Principal | null;
  private projectId: string | null;
  private epoch = 0;
  private controller = new AbortController();
  private listeners = new Set<Listener>();
  private snap: IdentitySnapshot;
  readonly provider: IdentityProvider;
  readonly session: SessionInfo | null;

  constructor(provider: IdentityProvider, principal: Principal | null = null, projectId: string | null = null,
    session: SessionInfo | null = null) {
    this.provider = provider;
    this.principal = principal;
    this.projectId = projectId;
    this.session = session;
    this.snap = this.makeSnapshot();
  }

  /** Signed-in mode: start from the user's default project and their role in it. */
  static fromSession(provider: IdentityProvider, session: SessionInfo): IdentityStore {
    const projects = Object.keys(session.projects);
    const project = session.default_project && session.projects[session.default_project]
      ? session.default_project
      : (projects[0] ?? null);
    const role = project ? session.projects[project] : undefined;
    return new IdentityStore(provider, role ? { role } : null, project, session);
  }

  private makeSnapshot(): IdentitySnapshot {
    return { principal: this.principal, projectId: this.projectId, epoch: this.epoch, signal: this.controller.signal };
  }

  get snapshot(): IdentitySnapshot {
    return this.snap;
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** One atomic transition for a role change, a project change, or both. */
  transition(change: { principal?: Principal | null; projectId?: string | null }): void {
    const nextPrincipal = change.principal !== undefined ? change.principal : this.principal;
    const nextProject = change.projectId !== undefined ? change.projectId : this.projectId;
    if ((nextPrincipal?.role ?? null) === (this.principal?.role ?? null) && nextProject === this.projectId) {
      return;
    }
    this.controller.abort(new DOMException("identity changed", "AbortError"));
    this.controller = new AbortController();
    this.principal = nextPrincipal;
    this.projectId = nextProject;
    this.epoch += 1;
    this.snap = this.makeSnapshot();
    for (const l of this.listeners) l(this.snap);
  }

  isCurrent(epoch: number): boolean {
    return epoch === this.epoch;
  }

  /** Signed-in mode: switch project and take the user's role in it, as one transition. */
  selectProject(projectId: string | null): void {
    const role = projectId && this.session ? this.session.projects[projectId] : undefined;
    this.transition(role ? { projectId, principal: { role } } : { projectId });
  }

  headers(write = false): Record<string, string> {
    return this.provider.requestHeaders(this.principal, { write, csrfToken: this.session?.csrf_token ?? null });
  }

  /** Query parameters every API request carries for the current identity. */
  scope(): Record<string, string> {
    return this.provider.scopeQuery(this.projectId);
  }
}
