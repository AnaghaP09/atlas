import { useState } from "react";
import type { DemoManifest, ManifestCall, ManifestRow } from "../api/types";
import { useApi, useApp } from "../app/context";
import { Button, DataModeBadge, ErrorView, Loading } from "../components/common";
import { Icon } from "../components/icons";
import { withScope } from "../api/stream";
import { newKey } from "../lib/idempotency";
import { useRouter } from "../lib/router";

/** Fills {placeholders} from configuration values and values saved by earlier steps. */
export function fill(value: unknown, vars: Record<string, string>): unknown {
  if (typeof value === "string") return value.replace(/\{([a-z0-9_]+)\}/gi, (m, k: string) => vars[k] ?? m);
  if (Array.isArray(value)) return value.map((v) => fill(v, vars));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, fill(v, vars)]));
  return value;
}

/** A peer server address is only used if it is a plain http(s) origin. */
export function peerUrl(raw: string | undefined, rowId: string): string | null {
  if (!raw) return null;
  try {
    const u = new URL(raw);
    if (u.protocol !== "https:" && u.protocol !== "http:") return null;
    return `${u.origin}/demo?row=${encodeURIComponent(rowId)}`;
  } catch {
    return null;
  }
}

interface StepResult {
  call: string;
  status: number | "no answer";
  ok: boolean;
}

function RowCard({ row, index, thisServer, vars, setVars }: { row: ManifestRow; index: number; thisServer: string; vars: Record<string, string>; setVars: (v: Record<string, string>) => void }) {
  const { identity } = useApp();
  const [results, setResults] = useState<StepResult[]>([]);
  const [keys] = useState(() => row.calls.map(() => newKey()));
  const [running, setRunning] = useState(false);
  const onOtherServer = row.server !== thisServer;
  const failed = results.some((r) => !r.ok);
  const peer = peerUrl(vars[`${row.server}_url`] ?? vars["peer_ui_url"], row.row_id);

  async function run() {
    const { epoch, signal } = identity.snapshot;
    setRunning(true);
    const out: StepResult[] = [];
    const saved = { ...vars };
    try {
      // The page must be served by the row's server before any call runs.
      const h = await fetch("/api/health", { signal, cache: "no-store" });
      const health = (await h.json()) as { environment?: string };
      if (health.environment !== row.server) {
        out.push({ call: "check server", status: h.status, ok: false });
        return;
      }
      for (const [i, call] of row.calls.entries()) {
        const c = fill(call, saved) as ManifestCall & { save?: Record<string, string> };
        const write = c.method !== "GET";
        let headers: Record<string, string>;
        if (identity.provider.mode === "session") {
          // Signed in: the browser sends this server's session cookie and Origin; the UI adds the
          // CSRF token. A step for another role needs that user to sign in, so it stops here.
          const current = identity.snapshot.principal?.role;
          if (c.role && c.role !== current) {
            out.push({ call: `${c.method} ${c.path}: sign in as a user with the ${c.role} role to run this step`, status: "no answer", ok: false });
            break;
          }
          headers = { Accept: "application/json", ...identity.headers(write) };
        } else {
          headers = { Accept: "application/json", ...identity.provider.requestHeaders(c.role ? { role: c.role as never } : identity.snapshot.principal) };
        }
        if (write) {
          headers["Content-Type"] = "application/json";
          headers["Idempotency-Key"] = keys[i] ?? newKey();
        }
        let status: number | "no answer" = "no answer";
        try {
          const res = await fetch(withScope(c.path, identity.scope()), { method: c.method, headers, credentials: "same-origin", body: c.method === "GET" ? undefined : JSON.stringify(c.body ?? {}), signal, cache: "no-store" });
          status = res.status;
          const body = (await res.json().catch(() => ({}))) as Record<string, unknown>;
          for (const [name, field] of Object.entries(c.save ?? {})) {
            const v = body[field];
            if (typeof v === "string") saved[name] = v;
          }
        } catch {
          status = "no answer";
        }
        if (!identity.isCurrent(epoch)) return;
        const ok = typeof status === "number" && (c.expect_status ?? [200]).includes(status);
        out.push({ call: `${c.method} ${c.path}`, status, ok });
        if (!ok) break;
      }
      setVars(saved);
    } finally {
      if (identity.isCurrent(epoch)) {
        setResults(out);
        setRunning(false);
      }
    }
  }

  return (
    <article className="card demo-card" data-testid={`row-${row.row_id}`}>
      <header className="demo-card-head">
        <span className="demo-num num" aria-hidden="true">{index}</span>
        <h3>{row.title}</h3>
        <DataModeBadge mode={row.data_mode} />
      </header>
      <p className="muted">{row.shows}</p>
      <div className="demo-card-foot">
        {onOtherServer ? (
          peer ? (
            <a className="btn btn-secondary btn-sm" href={peer} rel="noopener noreferrer" referrerPolicy="no-referrer">Continue on the {row.server} server</a>
          ) : (
            <span className="blocked-note" title={`This step runs on the ${row.server} server. Open that server's address to continue.`}>
              <Icon name="lock" size={12} /> Runs on the {row.server} server
            </span>
          )
        ) : (
          <Button size="sm" icon="play" loading={running} onClick={() => void run()}>{running ? "Running…" : "Run this step"}</Button>
        )}
        {results.length ? (
          <span className={`pill pill-${failed ? "critical" : "good"}`}>{failed ? "A step did not pass" : "Passed"}</span>
        ) : null}
      </div>
      {results.length ? (
        <details className="id-details" open={failed}>
          <summary>Calls made</summary>
          <ul className="demo-calls">
            {results.map((r, i) => (
              <li key={i} className={r.ok ? "pass" : "fail"}>
                {r.call}: {String(r.status)} {r.ok ? "as expected" : "not as expected"}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </article>
  );
}

export function GuidedDemo() {
  const data = useApi<DemoManifest>("/api/demo/manifest");
  const { search } = useRouter();
  const [vars, setVars] = useState<Record<string, string> | null>(null);
  if (data.isLoading) return <Loading what="the demo steps" />;
  if (data.error) return <ErrorView error={data.error} />;
  const m = data.data;
  if (!m) return null;
  const v = vars ?? m.variables;
  const focus = search.get("row");
  const start = focus ? Math.max(0, m.manifest.rows.findIndex((r) => r.row_id === focus)) : 0;
  const rows = m.manifest.rows.slice(start);
  return (
    <div className="cards demo-cards">
      {rows.map((row, i) => (
        <RowCard key={row.row_id} row={row} index={start + i + 1} thisServer={m.this_server} vars={v} setVars={setVars} />
      ))}
    </div>
  );
}
