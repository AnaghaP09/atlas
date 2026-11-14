import type { ContainmentView, Health } from "../api/types";
import { useAction, useApi, useInvalidateAll } from "../app/context";
import { ActionResult, Button, DataModeBadge, ErrorView, KpiTile, Loading, Section, StatusPill } from "../components/common";
import { Link } from "../lib/router";

/** For the audit role: health, mode, incidents and failed deliveries on one page. */
export function SystemScreen() {
  const health = useApi<Health>("/api/health");
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
  return (
    <>
      <div className="kpis">
        <KpiTile icon="pulse" tone={current === "offline" ? "warn" : "good"} label="Mode" value={current === "offline" ? "Offline" : "Connected"} small hint={current === "offline" ? "no outside connections" : "outside calls allowed"} />
        <KpiTile icon="power" tone={tripped.length ? "critical" : "good"} label="Open incidents" value={containment.data ? tripped.length : "—"} hint="tripped switches" />
        <KpiTile icon="bell" tone={(h.notifier?.dead_letters ?? 0) > 0 ? "warn" : "good"} label="Failed deliveries" value={h.notifier?.dead_letters ?? 0} hint={<Link to="/notifications">Notifications</Link>} />
        <KpiTile icon="clock" label="Held deliveries" value={h.notifier?.paused ?? 0} hint="waiting to send" />
      </div>
      <div className="grid">
        <Section title="Mode" icon="pulse">
          <Button variant={next === "offline" ? "danger" : "primary"} loading={setMode.state.status === "running"} onClick={() => void setMode.run({ mode: next }).then(refresh)}>
            Switch to {next === "offline" ? "Offline" : "Connected"}
          </Button>
          <ActionResult state={setMode.state} retry={() => void setMode.retrySame()} done={(r) => `Mode is now ${r.mode}.`} />
        </Section>
        <Section title="Components" icon="gauge">
          <ul className="list">
            {Object.entries(h.components).map(([k, v]) => (
              <li key={k}>
                <span className="grow">{k.replace(/_/g, " ")}</span>
                {v === "not_connected" ? <DataModeBadge mode="STAGED" reason="not connected in this build yet" /> : <StatusPill status={v === "connected" ? "complete" : v} />}
              </li>
            ))}
          </ul>
        </Section>
        <Section title="Open incidents" icon="power" end={<Link to="/containment">Kill switches</Link>}>
          {tripped.length === 0 ? <p className="muted">None.</p> : null}
          <ul className="list">
            {tripped.map((s) => (
              <li key={s.switch_id}>
                <span className="grow">{s.title ?? `Switch ${s.switch_id}`}</span>
                <StatusPill status="held" />
              </li>
            ))}
          </ul>
        </Section>
      </div>
    </>
  );
}
