import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { ApiError } from "../api/client";
import type { DataMode } from "../api/types";
import type { ActionState } from "../app/context";
import { Link } from "../lib/router";
import { Icon, ScoutEmblem, SentinelEmblem, type IconName } from "./icons";

const MODE_TEXT: Record<DataMode, string> = {
  REAL: "Real data",
  SYNTHETIC: "Sample data",
  STAGED: "Not in this demo",
  UNEVALUATED: "Not measured",
};

const MODE_HELP: Record<DataMode, string> = {
  REAL: "Public data, real model calls",
  SYNTHETIC: "Made-up data, never scored",
  STAGED: "Built and tested, not live",
  UNEVALUATED: "Not measured, not a pass",
};

export function DataModeBadge({ mode, reason }: { mode: DataMode | string | undefined; reason?: string | null }) {
  const m = (mode ?? "REAL") as DataMode;
  const text = MODE_TEXT[m] ?? String(mode);
  return (
    <span className={`badge badge-${m.toLowerCase()}`} title={reason ? `${MODE_HELP[m] ?? ""}: ${reason}` : (MODE_HELP[m] ?? "")} data-testid="data-mode">
      {text}
      {reason ? <span className="sr-only"> — {reason}</span> : null}
    </span>
  );
}

/** An agent's name with its emblem. Always labelled as an agent, never as a person. */
export function AgentName({ agent, compact = false }: { agent: "scout" | "sentinel" | string; compact?: boolean }) {
  if (agent === "scout")
    return (
      <span className="agent agent-scout" title="Scout is an AI agent that investigates and gathers evidence.">
        <ScoutEmblem /> Scout {compact ? null : <span className="agent-tag">agent</span>}
        <span className="sr-only"> (agent)</span>
      </span>
    );
  if (agent === "sentinel")
    return (
      <span className="agent agent-sentinel" title="Sentinel is an AI agent that checks every claim against its cited records.">
        <SentinelEmblem /> Sentinel {compact ? null : <span className="agent-tag">agent</span>}
        <span className="sr-only"> (agent)</span>
      </span>
    );
  return <span className="agent">{agent}</span>;
}

// ---------- Buttons ----------

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";

export function Button({
  variant = "secondary",
  size = "md",
  loading = false,
  icon,
  children,
  ...rest
}: {
  variant?: ButtonVariant;
  size?: "sm" | "md" | "lg";
  loading?: boolean;
  icon?: IconName;
  children: ReactNode;
} & Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "children">) {
  return (
    <button
      type="button"
      {...rest}
      className={`btn btn-${variant} btn-${size}${loading ? " is-loading" : ""}${rest.className ? ` ${rest.className}` : ""}`}
      disabled={rest.disabled || loading}
      aria-busy={loading || undefined}
    >
      {loading ? <span className="spinner" aria-hidden="true" /> : icon ? <Icon name={icon} size={16} /> : null}
      {children}
    </button>
  );
}

export function ButtonLink({ to, children, icon, variant = "secondary" }: { to: string; children: ReactNode; icon?: IconName; variant?: ButtonVariant }) {
  return (
    <Link to={to} className={`btn btn-${variant} btn-md`}>
      {icon ? <Icon name={icon} size={16} /> : null}
      {children}
    </Link>
  );
}

// ---------- Segmented control, tabs, tooltip, pagination ----------

export function Segmented<T extends string>({
  options,
  value,
  onChange,
  label,
}: {
  options: { value: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
  label: string;
}) {
  return (
    <div className="segmented" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={o.value === value}
          className={o.value === value ? "on" : undefined}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
  label,
}: {
  tabs: { value: T; label: string; count?: number }[];
  value: T;
  onChange: (v: T) => void;
  label: string;
}) {
  return (
    <div className="tabs" role="tablist" aria-label={label}>
      {tabs.map((t) => (
        <button
          key={t.value}
          type="button"
          role="tab"
          aria-selected={t.value === value}
          className={t.value === value ? "on" : undefined}
          onClick={() => onChange(t.value)}
        >
          {t.label}
          {typeof t.count === "number" ? <span className="tab-count num">{t.count}</span> : null}
        </button>
      ))}
    </div>
  );
}

/** A tooltip that shows on hover and keyboard focus. The text is also the accessible description. */
export function Tooltip({ text, children }: { text: string; children: ReactNode }) {
  const id = useId();
  return (
    <span className="tip" tabIndex={0} aria-describedby={id}>
      {children}
      <span role="tooltip" id={id} className="tip-body">{text}</span>
    </span>
  );
}

/** Long text cut to one line, with the full text in a tooltip. */
export function Truncate({ text, max = 60 }: { text: string; max?: number }) {
  if (text.length <= max) return <span>{text}</span>;
  return (
    <span className="truncate" title={text}>
      {text.slice(0, max - 1)}…
    </span>
  );
}

export function usePaged<T>(items: T[], pageSize: number) {
  const [page, setPage] = useState(0);
  const pages = Math.max(1, Math.ceil(items.length / pageSize));
  const safe = Math.min(page, pages - 1);
  return { page: safe, pages, setPage, slice: items.slice(safe * pageSize, safe * pageSize + pageSize) };
}

export function Pagination({ page, pages, setPage, total }: { page: number; pages: number; setPage: (p: number) => void; total: number }) {
  if (pages <= 1) return null;
  return (
    <nav className="pagination" aria-label="Pages">
      <Button size="sm" variant="ghost" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</Button>
      <span className="muted num">Page {page + 1} of {pages} · {total} items</span>
      <Button size="sm" variant="ghost" disabled={page >= pages - 1} onClick={() => setPage(page + 1)}>Next</Button>
    </nav>
  );
}

// ---------- Drawer (side panel) ----------

export function Drawer({ open, title, onClose, children }: { open: boolean; title: string; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) {
      if (typeof d.showModal === "function") d.showModal();
      else d.setAttribute("open", "");
    }
    if (!open && d.open) {
      if (typeof d.close === "function") d.close();
      else d.removeAttribute("open");
    }
  }, [open]);
  return (
    <dialog ref={ref} className="drawer" onClose={onClose} aria-label={title}>
      <header className="drawer-head">
        <h2>{title}</h2>
        <Button variant="ghost" size="sm" onClick={onClose}>Close</Button>
      </header>
      <div className="drawer-body">{children}</div>
    </dialog>
  );
}

// ---------- Loading, empty and error states ----------

export function Loading({ what, lines = 3 }: { what: string; lines?: number }) {
  const widths = ["w-80", "w-60", "w-40", "w-80"];
  return (
    <div className="state state-loading" role="status" aria-live="polite">
      <span className="sr-only">Loading {what}…</span>
      {Array.from({ length: lines }, (_, i) => (
        <span key={i} className={`skeleton ${widths[i % widths.length]}`} aria-hidden="true" />
      ))}
      <span className="muted loading-text" aria-hidden="true">Loading {what}…</span>
    </div>
  );
}

/** An empty state always says what to do next. */
export function Empty({ children, action }: { children: ReactNode; action?: { to: string; label: string } }) {
  return (
    <div className="state state-empty empty">
      <span className="empty-art" aria-hidden="true" />
      <p>{children}</p>
      {action ? <ButtonLink to={action.to} variant="primary" icon="arrow">{action.label}</ButtonLink> : null}
    </div>
  );
}

export function ErrorView({ error }: { error: Error | null }) {
  if (!error) return null;
  if (error instanceof ApiError) {
    if (error.refused)
      return (
        <p className="state state-refused" role="alert">
          Not allowed for your role: {error.reason}
        </p>
      );
    if (error.notConnected)
      return (
        <p className="state state-staged" role="status">
          <DataModeBadge mode="STAGED" /> {error.reason}
        </p>
      );
    return (
      <p className="state state-error" role="alert">
        The server could not do this: {error.reason || error.error}
      </p>
    );
  }
  return (
    <p className="state state-error" role="alert">
      Could not reach the server. Check that ATLAS is running.
    </p>
  );
}

export function ActionResult<R>({ state, retry, done }: { state: ActionState<R>; retry: () => void; done?: (r: R) => ReactNode }) {
  if (state.status === "running")
    return (
      <p className="state" role="status">
        <span className="spinner" aria-hidden="true" /> Working…
      </p>
    );
  if (state.status === "failed") return <ErrorView error={state.error} />;
  if (state.status === "uncertain")
    return (
      <p className="state state-error" role="alert">
        No answer came back, so the result is unknown. <button onClick={retry}>Check result</button> (sends the same request
        again; the server will not do the work twice).
      </p>
    );
  if (state.status === "done") return <div className="state state-done toast-inline" role="status">{done ? done(state.result) : "Done."}</div>;
  return null;
}

// ---------- Layout pieces ----------

export function Section({
  title,
  children,
  mode,
  reason,
  icon,
  end,
}: {
  title: string;
  children: ReactNode;
  mode?: DataMode | string;
  reason?: string | null;
  icon?: IconName;
  end?: ReactNode;
}) {
  return (
    <section className="card">
      <header className="card-head">
        {icon ? <Icon name={icon} /> : null}
        <h2>{title}</h2>
        {mode ? <DataModeBadge mode={mode} reason={reason ?? null} /> : null}
        {end ? <span className="end">{end}</span> : null}
      </header>
      {children}
    </section>
  );
}

export function PageHead({ title, actions, role, help }: { title: string; actions?: ReactNode; role?: string; help?: string | null }) {
  return (
    <div className="page-head">
      <div className="page-title">
        <h1>{title}</h1>
        {role ? <span className="pill pill-accent role-chip" data-testid="page-audience" title="Your role">{role}</span> : null}
        {help ? (
          <a className="help-link" href={help} target="_blank" rel="noopener noreferrer" aria-label={`Help: ${title}`} title="Help">
            ?
          </a>
        ) : null}
      </div>
      {actions ? <div className="actions">{actions}</div> : null}
    </div>
  );
}

export function KpiTile({
  label,
  value,
  hint,
  icon,
  tone = "accent",
  small = false,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  icon: IconName;
  tone?: "accent" | "teal" | "warn" | "critical" | "good";
  small?: boolean;
}) {
  return (
    <div className={`kpi tone-${tone}`}>
      <div className="kpi-label"><Icon name={icon} size={16} /> {label}</div>
      <p className={`kpi-value${small ? " small" : ""}`}>{value}</p>
      {hint ? <p className="kpi-hint">{hint}</p> : null}
    </div>
  );
}

const STATUS_TONE: Record<string, string> = {
  complete: "good",
  completed: "good",
  passed: "good",
  accepted: "good",
  running: "info",
  connected: "good",
  ok: "good",
  admitted: "info",
  queued: "info",
  partial: "warn",
  held: "warn",
  unverified: "warn",
  awaiting_reapproval: "warn",
  refused: "serious",
  failed: "critical",
  voided: "critical",
};

const STATUS_WORD: Record<string, string> = {
  complete: "Done",
  completed: "Done",
  passed: "Passed",
  accepted: "Accepted",
  running: "Running",
  admitted: "Queued",
  queued: "Queued",
  partial: "Partial",
  held: "Needs a person",
  unverified: "Not verified",
  awaiting_reapproval: "Needs approval",
  refused: "Refused",
  failed: "Failed",
  voided: "Voided",
  connected: "Connected",
  ok: "OK",
};

/** Run, task or gate status as a pill. The words always carry the meaning, never the colour alone. */
export function StatusPill({ status }: { status: string | null | undefined }) {
  const s = (status ?? "unknown").toLowerCase();
  const tone = STATUS_TONE[s] ?? "neutral";
  return (
    <span className={`pill pill-${tone}`}>
      <span className="pill-dot" aria-hidden="true" />
      {STATUS_WORD[s] ?? s.replace(/_/g, " ")}
    </span>
  );
}

const DATE_FMT = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });

/** "5 min ago", "3 h ago", "2 days ago". */
export function relative(d: Date, now = new Date()): string {
  const s = Math.round((now.getTime() - d.getTime()) / 1000);
  const past = s >= 0;
  const a = Math.abs(s);
  const v = a < 60 ? "just now" : a < 3600 ? `${Math.round(a / 60)} min` : a < 86400 ? `${Math.round(a / 3600)} h` : `${Math.round(a / 86400)} days`;
  if (v === "just now") return v;
  return past ? `${v} ago` : `in ${v}`;
}

/** "14 Nov 2026, 14:37 · 5 min ago". */
export function Time({ iso, short = false }: { iso?: string | null; short?: boolean }) {
  if (!iso) return <span>—</span>;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return <time dateTime={iso}>{iso}</time>;
  return (
    <time dateTime={iso} title={DATE_FMT.format(d)}>
      {short ? relative(d) : `${DATE_FMT.format(d)} · ${relative(d)}`}
    </time>
  );
}

/** Technical ids stay out of the way: a small "Details" disclosure with a copy button. */
export function IdDetails({ items }: { items: [string, string | null | undefined][] }) {
  const shown = items.filter(([, v]) => v);
  if (!shown.length) return null;
  return (
    <details className="id-details">
      <summary>Details</summary>
      <dl>
        {shown.map(([k, v]) => (
          <div key={k}>
            <dt>{k}</dt>
            <dd>
              <code>{v}</code>
              <CopyButton text={v ?? ""} />
            </dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

export function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="btn btn-ghost btn-sm copy"
      aria-label="Copy"
      title="Copy"
      onClick={() => {
        try {
          void navigator.clipboard?.writeText(text).then(() => setDone(true));
        } catch {
          setDone(false);
        }
      }}
    >
      {done ? "Copied" : "Copy"}
    </button>
  );
}

export function Confirm({ text, onConfirm, label }: { text: string; onConfirm: () => void; label: string }) {
  const [open, setOpen] = useState(false);
  if (!open) return <button onClick={() => setOpen(true)}>{label}</button>;
  return (
    <span className="confirm">
      {text} <button className="btn-danger" onClick={() => { setOpen(false); onConfirm(); }}>Yes</button>{" "}
      <button onClick={() => setOpen(false)}>No</button>
    </span>
  );
}

/** A labelled usage bar: used against a limit. */
export function UsageBar({ label, used, limit, unit = "" }: { label: string; used: number; limit: number; unit?: string }) {
  const hot = limit > 0 && used / limit >= 0.8;
  return (
    <div className="meter-row">
      <span>{label}</span>
      <progress className={`bar${hot ? " hot" : ""}`} max={Math.max(limit, 1)} value={Math.min(used, limit)} aria-label={`${label}: ${used} of ${limit}`} />
      <span className="num">{used}{unit} / {limit}{unit}</span>
    </div>
  );
}
