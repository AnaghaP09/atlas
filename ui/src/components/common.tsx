import { useState, type ReactNode } from "react";
import { ApiError } from "../api/client";
import type { DataMode } from "../api/types";
import type { ActionState } from "../app/context";

const MODE_TEXT: Record<DataMode, string> = {
  REAL: "Real data",
  SYNTHETIC: "Synthetic data",
  STAGED: "Not available in this demo",
  UNEVALUATED: "Not evaluated",
};

const MODE_HELP: Record<DataMode, string> = {
  REAL: "Built from real public data and real model calls.",
  SYNTHETIC: "Built from data the team made up for the demo. It is never scored as real.",
  STAGED: "The code path exists and is tested, but the real-world part is outside this build.",
  UNEVALUATED: "This quality bar is not measured in this build. It is not a pass.",
};

export function DataModeBadge({ mode, reason }: { mode: DataMode | string | undefined; reason?: string | null }) {
  const m = (mode ?? "REAL") as DataMode;
  const text = MODE_TEXT[m] ?? String(mode);
  return (
    <span className={`badge badge-${m.toLowerCase()}`} title={reason ?? MODE_HELP[m] ?? ""} data-testid="data-mode">
      {text}
      {reason ? <span className="badge-reason"> — {reason}</span> : null}
    </span>
  );
}

export function AgentName({ agent }: { agent: "scout" | "sentinel" | string }) {
  if (agent === "scout") return <span className="agent" title="Scout is an AI agent that investigates and gathers evidence.">Scout (agent)</span>;
  if (agent === "sentinel")
    return <span className="agent" title="Sentinel is an AI agent that checks every claim against its cited records.">Sentinel (agent)</span>;
  return <span className="agent">{agent}</span>;
}

export function Loading({ what }: { what: string }) {
  return <p className="state state-loading" role="status">Loading {what}…</p>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="state state-empty">{children}</p>;
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
  if (state.status === "running") return <p className="state" role="status">Working…</p>;
  if (state.status === "failed") return <ErrorView error={state.error} />;
  if (state.status === "uncertain")
    return (
      <p className="state state-error" role="alert">
        No answer came back, so the result is unknown. <button onClick={retry}>Check result</button> (sends the same request
        again; the server will not do the work twice).
      </p>
    );
  if (state.status === "done") return <div className="state state-done">{done ? done(state.result) : "Done."}</div>;
  return null;
}

export function Section({ title, children, mode, reason }: { title: string; children: ReactNode; mode?: DataMode | string; reason?: string | null }) {
  return (
    <section className="card">
      <header className="card-head">
        <h2>{title}</h2>
        {mode ? <DataModeBadge mode={mode} reason={reason ?? null} /> : null}
      </header>
      {children}
    </section>
  );
}

export function Time({ iso }: { iso?: string | null }) {
  if (!iso) return <span>—</span>;
  const d = new Date(iso);
  return <time dateTime={iso}>{Number.isNaN(d.getTime()) ? iso : d.toLocaleString()}</time>;
}

export function Confirm({ text, onConfirm, label }: { text: string; onConfirm: () => void; label: string }) {
  const [open, setOpen] = useState(false);
  if (!open) return <button onClick={() => setOpen(true)}>{label}</button>;
  return (
    <span className="confirm">
      {text} <button onClick={() => { setOpen(false); onConfirm(); }}>Yes</button>{" "}
      <button onClick={() => setOpen(false)}>No</button>
    </span>
  );
}
