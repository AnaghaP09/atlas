import type { ContainmentView, Health } from "../api/types";
import { useAction, useApi, useInvalidateAll } from "../app/context";
import { ActionResult, ConfirmButton, ErrorView, KpiTile, Loading, Section } from "../components/common";
import { Link } from "../lib/router";

const COMPONENT: Record<string, string> = {
  snapshots: "Data sets",
  tasks: "Coding tasks",
  containment: "Kill switches",
  orchestrator: "Run engine",
};

const CONNECTION: Record<string, string> = {
  ANTHROPIC_API_KEY: "Model access",
  GITHUB_TOKEN: "GitHub read access",
  ATLAS_PUBLISH_TOKEN: "Publishing to GitHub",
  ATLAS_WEBHOOK_URL: "Chat delivery",
  ATLAS_MODEL_API_KEY: "Self-hosted model",
};

function words(v: string): string {
  return v.replace(/[_-]/g, " ").replace(/^./, (c) => c.toUpperCase());
}

/** For the audit role: health, mode, incidents and failed deliveries on one page. */
export function SystemScreen() {
  const health = useApi<Health & { configuration?: Record<string, boolean> }>("/api/health");
  const mode = useApi<{ mode: string }>("/api/mode");
  const containment = useApi<ContainmentView>("/api/containment");
  const setMode = useAction<{ mode: string }, { mode: string }>("/api/mode");
  const refresh = useInvalidateAll();
  if (health.isLoading) return <Loading what="system status" />;
  if (health.error) return <ErrorView error={health.error} />;
  const h = health.data!;
  const current = mode.data?.mode ?? h.mode;
  const tripped = (containment.data?.switches ?? []).filter((s) => s.state !== "ok");
  const next = current === "offline" ? "connected" : "offline";
  const connections = Object.entries(h.configuration ?? {});
  return (
    <>
      <div className="kpis">
        <KpiTile icon="pulse" tone={current === "offline" ? "warn" : "good"} label="Mode" value={current === "offline" ? "Offline" : "Connected"} small hint={current === "offline" ? "no outside connections" : "outside calls allowed"} />
        <KpiTile icon="power" tone={tripped.length ? "critical" : "good"} label="Open incidents" value={containment.data ? tripped.length : "—"} hint="tripped switches" />
        <KpiTile icon="bell" tone={(h.notifier?.dead_letters ?? 0) > 0 ? "warn" : "good"} label="Failed deliveries" value={h.notifier?.dead_letters ?? 0} hint={<Link to="/notifications">Notifications</Link>} />
        <KpiTile icon="clock" label="Held deliveries" value={h.notifier?.paused ?? 0} hint="waiting to send" />
      </div>
      <div className="grid">
        <Section title="Mode" icon="pulse" end={<span className={`pill pill-${current === "offline" ? "warn" : "good"}`}>{current === "offline" ? "Offline" : "Connected"}</span>}>
          <div className="actions-row">
            <ConfirmButton
              label={next === "offline" ? "Go offline" : "Go connected"}
              title={next === "offline" ? "Switch ATLAS to offline?" : "Switch ATLAS to connected?"}
              body={next === "offline" ? "New runs stop calling outside services until you switch back." : "Runs may call outside services again."}
              confirmLabel={next === "offline" ? "Go offline" : "Go connected"}
              size="md"
              icon="power"
              danger={next === "offline"}
              variant={next === "offline" ? "secondary" : "primary"}
              onConfirm={() => void setMode.run({ mode: next }).then(refresh)}
            />
          </div>
          <ActionResult state={setMode.state} retry={() => void setMode.retrySame()} done={(r) => `Mode is now ${r.mode}.`} />
        </Section>
        <Section title="Parts" icon="gauge">
          <ul className="list">
            {Object.entries(h.components).map(([k, v]) => (
              <li key={k}>
                <span className="grow">{COMPONENT[k] ?? words(k)}</span>
                {v === "connected" ? <span className="pill pill-good">Working</span> : <span className="pill pill-neutral">{v === "not_connected" ? "Not in this demo" : words(v)}</span>}
              </li>
            ))}
          </ul>
        </Section>
        {connections.length ? (
          <Section title="Connections" icon="key">
            <ul className="list">
              {connections.map(([k, set]) => (
                <li key={k}>
                  <span className="grow">{CONNECTION[k] ?? words(k)}</span>
                  <span className={`pill pill-${set ? "good" : "neutral"}`}>{set ? "Set up" : "Not set up"}</span>
                </li>
              ))}
            </ul>
          </Section>
        ) : null}
        <Section title="Open incidents" icon="power" end={<Link to="/containment">Kill switches</Link>}>
          {tripped.length === 0 ? <p className="muted">None.</p> : null}
          <ul className="list">
            {tripped.map((s) => (
              <li key={s.switch_id}>
                <span className="grow">{s.title ?? `Switch ${s.switch_id}`}</span>
                <span className="pill pill-critical">Stopped</span>
              </li>
            ))}
          </ul>
        </Section>
      </div>
    </>
  );
}
