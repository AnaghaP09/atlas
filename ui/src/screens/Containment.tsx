import { useState } from "react";
import type { ContainmentView, Correction, Incident } from "../api/types";
import { useAction, useApi, useIdentity, useInvalidateAll } from "../app/context";
import { ActionResult, Button, ConfirmButton, ErrorView, Loading, Time } from "../components/common";
import { Icon } from "../components/icons";
import { Link } from "../lib/router";

export interface SwitchView {
  switch_id: string;
  title?: string;
  state: string;
  blocks?: string[];
  blocks_ids?: string[];
  incident?: (Incident & { drill?: boolean }) | null;
  last_tripped?: string | null;
  last_tripped_at?: string | null;
  can_drill?: boolean;
}

/** What a switch stops, as short chips. */
const BLOCK_LABEL: Record<string, string> = {
  preparation: "Coding tasks",
  execution: "Coding tasks",
  publication: "Publishing",
  model_calls: "Model calls",
  model: "Model calls",
  reports: "Reports",
  report_delivery: "Reports",
  runs: "Runs",
  approvals: "Approvals",
};

export function blockChips(blocks: string[] | undefined): string[] {
  return [...new Set((blocks ?? []).map((b) => BLOCK_LABEL[b] ?? b.replace(/_/g, " ")))];
}

export function switchState(s: SwitchView): "ready" | "tripped" | "drill" {
  if (s.state === "ok") return "ready";
  return s.state === "drill" || s.incident?.drill ? "drill" : "tripped";
}

const LIGHT_TEXT = { ready: "Ready", tripped: "Tripped", drill: "Tripped (drill)" };

/** The switches that are tripped and stop a given kind of work. Used to lock buttons elsewhere. */
export function useBlockedBy(kind: string): SwitchView[] {
  const data = useApi<ContainmentView>("/api/containment");
  const label = BLOCK_LABEL[kind] ?? kind;
  return ((data.data?.switches ?? []) as SwitchView[]).filter(
    (s) => s.state !== "ok" && ((s.blocks_ids ?? []).includes(kind) || blockChips(s.blocks).includes(label)),
  );
}

function Corrections({ incidentId }: { incidentId: string }) {
  const data = useApi<{ corrections: Correction[] }>("/api/corrections", { incident_id: incidentId });
  const items = data.data?.corrections ?? [];
  if (!items.length) return null;
  return (
    <ul className="plain" data-testid="corrections">
      {items.map((c) => (
        <li key={c.correction_id}>
          <span className={`pill ${c.state === "held" ? "pill-warn" : "pill-neutral"}`}>{c.state === "held" ? "Held" : c.state}</span>{" "}
          {c.state === "held" ? <span className="muted small">{c.reason ?? "outcome unknown"}</span> : null}
        </li>
      ))}
    </ul>
  );
}

function ResumePanel({ incident, done }: { incident: Incident & { drill?: boolean }; done: () => void }) {
  const [kind, setKind] = useState("");
  const [reference, setReference] = useState("");
  const base = `/api/incidents/${encodeURIComponent(incident.incident_id)}`;
  const record = useAction<{ kind: string; reference: string }, unknown>(`${base}/prerequisites`);
  const resume = useAction<Record<string, never>, unknown>(`${base}/resume`);
  const required = incident.required_prerequisites ?? [];
  const recorded = new Set((incident.recorded_prerequisites ?? []).map((p) => p.kind));
  const ready = incident.drill || required.every((r) => recorded.has(r));
  return (
    <div className="resume">
      {!incident.drill ? (
        <>
          <ul className="checklist" aria-label="Steps before resuming">
            {required.map((r) => (
              <li key={r} className={recorded.has(r) ? "done" : ""}>
                <Icon name={recorded.has(r) ? "check" : "clock"} size={14} /> {r.replace(/_/g, " ")}
              </li>
            ))}
          </ul>
          <form
            className="inline-form"
            onSubmit={(e) => {
              e.preventDefault();
              void record.run({ kind, reference }).then(done);
            }}
          >
            <select aria-label="Step" value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="">Step…</option>
              {required.filter((r) => !recorded.has(r)).map((r) => <option key={r} value={r}>{r.replace(/_/g, " ")}</option>)}
            </select>
            <input aria-label="Reference" placeholder="Reference" value={reference} onChange={(e) => setReference(e.target.value)} />
            <Button size="sm" disabled={!kind} onClick={() => void record.run({ kind, reference }).then(done)}>Record</Button>
          </form>
          <ActionResult state={record.state} retry={() => void record.retrySame()} />
        </>
      ) : null}
      <ConfirmButton
        label="Resume"
        title={incident.drill ? "End this drill?" : "Resume this switch?"}
        body={incident.drill ? "The blocked actions become available again." : "All required steps are recorded. The blocked actions become available again."}
        confirmLabel="Resume"
        variant="primary"
        icon="play"
        disabled={!ready}
        hint={ready ? undefined : "Record the steps first"}
        onConfirm={() => void resume.run({}).then(done)}
      />
      <ActionResult state={resume.state} retry={() => void resume.retrySame()} success="Switch resumed" />
      <Corrections incidentId={incident.incident_id} />
    </div>
  );
}

function SwitchCard({ s, audit, done }: { s: SwitchView; audit: boolean; done: () => void }) {
  const lastTripped = s.last_tripped_at ?? s.last_tripped ?? s.incident?.tripped_at ?? null;
  const drill = useAction<Record<string, never>, unknown>(`/api/containment/${encodeURIComponent(s.switch_id)}/drill`);
  const state = switchState(s);
  return (
    <article className={`switch-card ${state}`} data-testid={`switch-${s.switch_id}`}>
      <header className="switch-head">
        <span className={`light light-${state}`} aria-hidden="true" />
        <strong>{s.title ?? `Switch ${s.switch_id}`}</strong>
        <span className={`pill ${state === "ready" ? "pill-good" : state === "drill" ? "pill-warn" : "pill-critical"}`} data-testid="switch-state">
          {LIGHT_TEXT[state]}
        </span>
      </header>
      <div className="switch-blocks">
        {blockChips(s.blocks).map((b) => <span key={b} className="chip">{b}</span>)}
      </div>
      <div className="switch-foot">
        <span className="muted small">
          {lastTripped ? <>Last tripped <Time iso={lastTripped} short /></> : "Never tripped"}
        </span>
        {audit && state === "ready" && s.can_drill === true ? (
          <ConfirmButton
            label="Run drill"
            title={`Run a drill on "${s.title ?? "this switch"}"?`}
            body={<>This really blocks: {blockChips(s.blocks).join(", ") || "nothing"}. You can resume it right after.</>}
            confirmLabel="Run drill"
            icon="power"
            variant="secondary"
            danger
            onConfirm={() => void drill.run({}).then(done)}
          />
        ) : null}
      </div>
      <ActionResult state={drill.state} retry={() => void drill.retrySame()} success="Drill started" />
      {audit && s.incident && state !== "ready" ? <ResumePanel incident={s.incident} done={done} /> : null}
    </article>
  );
}

export function Containment() {
  const data = useApi<ContainmentView>("/api/containment");
  const { principal } = useIdentity();
  const refresh = useInvalidateAll();
  if (data.isLoading) return <Loading what="kill switches" />;
  if (data.error) return <ErrorView error={data.error} />;
  const switches = (data.data?.switches ?? []) as SwitchView[];
  const tripped = switches.filter((s) => s.state !== "ok");
  const audit = principal?.role === "audit";
  return (
    <>
      <div className={`switch-summary${tripped.length ? " tripped" : ""}`} role="status">
        <span className={`light ${tripped.length ? "light-tripped" : "light-ready"}`} aria-hidden="true" />
        <strong>{tripped.length ? `${tripped.length} of ${switches.length} tripped` : `All ${switches.length} ready`}</strong>
        {audit ? null : <span className="pill pill-neutral">View only</span>}
      </div>
      <div className="switch-grid">
        {switches.map((s) => <SwitchCard key={s.switch_id} s={s} audit={audit} done={refresh} />)}
      </div>
    </>
  );
}

/** A red bar across the top while any switch is tripped. Hidden for roles that cannot read the switches. */
export function ContainmentBanner() {
  const data = useApi<ContainmentView>("/api/containment");
  const tripped = ((data.data?.switches ?? []) as SwitchView[]).filter((s) => s.state !== "ok");
  if (!tripped.length) return null;
  const drillOnly = tripped.every((s) => s.state === "drill" || s.incident?.drill);
  return (
    <Link to="/containment" className={`containment-banner${drillOnly ? " drill" : ""}`}>
      <Icon name="power" size={16} />
      {tripped.length === 1 ? `${tripped[0]!.title ?? "A kill switch"} is tripped` : `${tripped.length} kill switches are tripped`}
      {drillOnly ? " (drill)" : ""}
    </Link>
  );
}

/** A button stopped by a tripped switch: disabled, with a lock and the switch's name. */
export function BlockedNote({ by }: { by: SwitchView[] }) {
  if (!by.length) return null;
  return (
    <span className="blocked-note" title={`Stopped by: ${by.map((s) => s.title).join(", ")}`}>
      <Icon name="lock" size={12} /> Stopped by a kill switch
    </span>
  );
}
