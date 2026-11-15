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
  reason?: string;
}

export interface RowOutcome {
  state: "passed" | "attention";
  results: StepResult[];
}

/** Plain names for the two servers. */
export function serverName(server: string): string {
  return server === "scenario" ? "safety test server" : "main server";
}

/** Names still in braces after filling: values an earlier step has not saved yet. */
export function missingValues(call: unknown): string[] {
  const found = new Set<string>();
  const walk = (v: unknown) => {
    if (typeof v === "string") for (const m of v.matchAll(/\{([a-z0-9_]+)\}/gi)) found.add(m[1]!);
    else if (Array.isArray(v)) v.forEach(walk);
    else if (v && typeof v === "object") Object.values(v).forEach(walk);
  };
  walk(call);
  return [...found];
}

function readPath(body: unknown, path: string): unknown {
  return path.split(".").reduce<unknown>((v, k) => (v && typeof v === "object" ? (v as Record<string, unknown>)[k] : undefined), body);
}

/** True when the answer has reached the state the step waits for. */
export function waitDone(body: unknown, wait: ManifestCall["wait_for"]): boolean {
  if (!wait) return true;
  const v = readPath(body, wait.path);
  if (wait.op === "in") return Array.isArray(wait.value) && wait.value.includes(v as never);
  if (wait.op === "eq") return v === wait.value;
  if (wait.op === "exists") return v !== undefined && v !== null;
  return true;
}

const WAIT_EVERY_MS = 3000;
const WAIT_TRIES = 100;

function RowCard({
  row, index, thisServer, vars, setVars, outcome, setOutcome, savedBy,
}: {
  row: ManifestRow;
  index: number;
  thisServer: string;
  vars: Record<string, string>;
  setVars: (v: Record<string, string>) => void;
  outcome: RowOutcome | undefined;
  setOutcome: (o: RowOutcome) => void;
  savedBy: Record<string, number>;
}) {
  const { identity } = useApp();
  const [running, setRunning] = useState(false);
  const onOtherServer = row.server !== thisServer;
  const peer = peerUrl(vars[`${row.server}_url`] ?? vars["peer_ui_url"], row.row_id);
  const results = outcome?.results ?? [];
  const failed = outcome?.state === "attention";

  async function run() {
    const { epoch, signal } = identity.snapshot;
    setRunning(true);
    const out: StepResult[] = [];
    const saved = { ...vars };
    const keys: Record<string, string> = {};
    try {
      // The page must be served by the row's server before any call runs.
      const h = await fetch("/api/health", { signal, cache: "no-store" });
      const health = (await h.json()) as { environment?: string };
      if (health.environment !== row.server) {
        out.push({ call: "Check the server", status: h.status, ok: false, reason: `This step runs on the ${serverName(row.server)}.` });
        return;
      }
      for (const call of row.calls) {
        const c = fill(call, saved) as ManifestCall;
        const missing = missingValues({ path: c.path, body: c.body, query: c.query });
        if (missing.length) {
          const first = missing.map((m) => savedBy[m]).find((n) => n !== undefined);
          out.push({
            call: `${c.method} ${c.path}`, status: "no answer", ok: false,
            reason: first !== undefined ? `Run step ${first} first.` : `Set ${missing.join(", ")} in the settings file.`,
          });
          break;
        }
        const write = c.method !== "GET";
        let headers: Record<string, string>;
        if (identity.provider.mode === "session") {
          // Signed in: the browser sends this server's session cookie and Origin; the UI adds the
          // CSRF token. A step for another role needs that user to sign in, so it stops here.
          const current = identity.snapshot.principal?.role;
          if (c.role && c.role !== current) {
            out.push({ call: `${c.method} ${c.path}`, status: "no answer", ok: false, reason: `Sign in as a user with the ${c.role} role.` });
            break;
          }
          headers = { Accept: "application/json", ...identity.headers(write) };
        } else {
          headers = { Accept: "application/json", ...identity.provider.requestHeaders(c.role ? { role: c.role as never } : identity.snapshot.principal) };
        }
        if (write) {
          headers["Content-Type"] = "application/json";
          // Calls that share a key are the same request sent twice on purpose.
          const name = c.key ?? `${out.length}`;
          headers["Idempotency-Key"] = keys[name] ?? (keys[name] = newKey());
        }
        const query = c.query ? `?${new URLSearchParams(c.query).toString()}` : "";
        let status: number | "no answer" = "no answer";
        let reason: string | undefined;
        try {
          for (let attempt = 0; attempt < WAIT_TRIES; attempt += 1) {
            const res = await fetch(withScope(`${c.path}${query}`, identity.scope()), {
              method: c.method, headers, credentials: "same-origin",
              body: c.method === "GET" ? undefined : JSON.stringify(c.body ?? {}), signal, cache: "no-store",
            });
            status = res.status;
            const body = (await res.json().catch(() => ({}))) as Record<string, unknown>;
            if (typeof body.reason === "string") reason = body.reason;
            for (const [name, field] of Object.entries(c.save ?? {})) {
              const v = body[field];
              if (typeof v === "string") saved[name] = v;
            }
            if (!c.wait_for || c.method !== "GET" || !res.ok || waitDone(body, c.wait_for)) break;
            await new Promise((r) => setTimeout(r, WAIT_EVERY_MS));
            if (!identity.isCurrent(epoch)) return;
          }
        } catch {
          status = "no answer";
        }
        if (!identity.isCurrent(epoch)) return;
        const ok = typeof status === "number" && (c.expect_status ?? [200]).includes(status);
        out.push({ call: `${c.method} ${c.path}`, status, ok, reason: ok ? undefined : reason });
        if (!ok) break;
      }
      setVars(saved);
    } finally {
      if (identity.isCurrent(epoch)) {
        setOutcome({ state: out.length > 0 && out.every((r) => r.ok) ? "passed" : "attention", results: out });
        setRunning(false);
      }
    }
  }

  const why = results.find((r) => !r.ok)?.reason;
  return (
    <article className={`card demo-card${outcome ? ` demo-${outcome.state}` : ""}`} data-testid={`row-${row.row_id}`}>
      <header className="demo-card-head">
        <span className="demo-num num" aria-hidden="true">{outcome?.state === "passed" ? <Icon name="check" size={13} /> : index}</span>
        <h3>{row.title}</h3>
        <DataModeBadge mode={row.data_mode} />
      </header>
      <p className="muted">{row.shows}</p>
      <div className="demo-card-foot">
        {onOtherServer ? (
          peer ? (
            <a className="btn btn-secondary btn-sm" href={peer} rel="noopener noreferrer" referrerPolicy="no-referrer">Open on the {serverName(row.server)}</a>
          ) : (
            <span className="blocked-note" title={`Start the ${serverName(row.server)} to run this step.`}>
              <Icon name="lock" size={12} /> Needs the {serverName(row.server)}
            </span>
          )
        ) : (
          <Button size="sm" icon="play" variant={outcome ? "secondary" : "primary"} loading={running} onClick={() => void run()}>
            {running ? "Running…" : outcome ? "Run again" : "Run this step"}
          </Button>
        )}
        {!onOtherServer && typeof row.estimated_cost_usd === "number" && row.estimated_cost_usd > 0 ? (
          <span className="pill pill-neutral num" title="Expected model spend for this step">About ${row.estimated_cost_usd.toFixed(2)}</span>
        ) : null}
        {outcome ? (
          <span className={`pill pill-${failed ? "warn" : "good"}`}>{failed ? "Needs attention" : "Passed"}</span>
        ) : null}
      </div>
      {failed && why ? <p className="demo-why">{why}</p> : null}
      {results.length ? (
        <details className="id-details">
          <summary>Details</summary>
          <ul className="demo-calls">
            {results.map((r, i) => (
              <li key={i} className={r.ok ? "pass" : "fail"}>
                {r.call}: {String(r.status)}
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
  const [outcomes, setOutcomes] = useState<Record<string, RowOutcome>>({});
  if (data.isLoading) return <Loading what="the demo steps" />;
  if (data.error) return <ErrorView error={data.error} />;
  const m = data.data;
  if (!m) return null;
  const v = vars ?? m.variables;
  const all = m.manifest.rows;
  const focus = search.get("row");
  const start = focus ? Math.max(0, all.findIndex((r) => r.row_id === focus)) : 0;
  const rows = all.slice(start);
  // Which step saves each value, so a later step can say what to run first.
  const savedBy: Record<string, number> = {};
  all.forEach((row, i) => row.calls.forEach((c) => Object.keys(c.save ?? {}).forEach((name) => (savedBy[name] ??= i + 1))));
  const here = all.filter((r) => r.server === m.this_server);
  const passed = here.filter((r) => outcomes[r.row_id]?.state === "passed").length;
  const attention = here.filter((r) => outcomes[r.row_id]?.state === "attention").length;
  const other = m.this_server === "scenario" ? "normal" : "scenario";
  return (
    <>
      <div className="demo-progress" role="status">
        <progress max={here.length} value={passed} aria-label="Steps passed" />
        <span className="num">{passed} of {here.length} passed</span>
        {attention ? <span className="pill pill-warn">{attention} need attention</span> : null}
        {all.length > here.length ? <span className="muted">{all.length - here.length} run on the {serverName(other)}</span> : null}
      </div>
      <div className="cards demo-cards">
        {rows.map((row, i) => (
          <RowCard
            key={row.row_id} row={row} index={start + i + 1} thisServer={m.this_server} vars={v} setVars={setVars}
            outcome={outcomes[row.row_id]} setOutcome={(o) => setOutcomes((prev) => ({ ...prev, [row.row_id]: o }))} savedBy={savedBy}
          />
        ))}
      </div>
    </>
  );
}
