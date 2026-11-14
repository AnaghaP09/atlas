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

export interface IdentityProvider {
  /** Short name shown to the user, for example "demo identity". */
  readonly label: string;
  /** Headers added to every API request. */
  requestHeaders(principal: Principal | null): Record<string, string>;
  /** Whether the user picks a role in the UI (true) or the server decides after sign-in (false). */
  readonly rolePicker: boolean;
}

export const headerIdentity: IdentityProvider = {
  label: "Demo identity (not sign-in)",
  rolePicker: true,
  requestHeaders: (principal): Record<string, string> => {
    const h: Record<string, string> = {};
    if (principal) h["X-Atlas-Role"] = principal.role;
    return h;
  },
};

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

  constructor(provider: IdentityProvider, principal: Principal | null = null, projectId: string | null = null) {
    this.provider = provider;
    this.principal = principal;
    this.projectId = projectId;
    this.snap = this.makeSnapshot();
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

  headers(): Record<string, string> {
    return this.provider.requestHeaders(this.principal);
  }
}
