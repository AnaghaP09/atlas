import { useState } from "react";
import type { ContainmentView, Correction, Incident } from "../api/types";
import { useAction, useApi } from "../app/context";
import { ActionResult, ErrorView, Loading, Section, Time } from "../components/common";

function Corrections({ incidentId }: { incidentId: string }) {
  const data = useApi<{ corrections: Correction[] }>("/api/corrections", { incident_id: incidentId });
  if (data.error) return <ErrorView error={data.error} />;
  const items = data.data?.corrections ?? [];
  if (items.length === 0) return <p className="muted">No corrections for this incident.</p>;
  return (
    <ul data-testid="corrections">
      {items.map((c) => (
        <li key={c.correction_id}>
          {c.correction_id}: <strong>{c.state}</strong>
          {c.state === "held" ? ` — waiting for a person: ${c.reason ?? "the outcome is unknown"}. It is never sent again automatically.` : ""}
        </li>
      ))}
    </ul>
  );
}

function IncidentPanel({ incident }: { incident: Incident }) {
  const [kind, setKind] = useState("");
  const [reference, setReference] = useState("");
  const record = useAction<{ kind: string; reference: string }, unknown>(`/api/incidents/${encodeURIComponent(incident.incident_id)}/prerequisites`);
  const resume = useAction<Record<string, never>, unknown>(`/api/incidents/${encodeURIComponent(incident.incident_id)}/resume`);
  return (
    <div className="incident">
      <p>
        Incident <code>{incident.incident_id}</code> — {incident.state}. Tripped <Time iso={incident.tripped_at ?? null} /> by{" "}
        {incident.trigger_event ?? "an event"}.
      </p>
      <p>Needed before resuming: {(incident.required_prerequisites ?? []).join(", ") || "none listed"}</p>
      <p>Recorded: {(incident.recorded_prerequisites ?? []).map((p) => p.kind).join(", ") || "none yet"}</p>
      <Corrections incidentId={incident.incident_id} />
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void record.run({ kind, reference });
        }}
      >
        <label>Prerequisite kind <input value={kind} onChange={(e) => setKind(e.target.value)} /></label>
        <label>Reference (for example a run id or a correction id) <input value={reference} onChange={(e) => setReference(e.target.value)} /></label>
        <button type="submit" disabled={!kind}>Record</button>
        <ActionResult state={record.state} retry={() => void record.retrySame()} />
      </form>
      <button onClick={() => void resume.run({})}>Resume</button>
      <ActionResult state={resume.state} retry={() => void resume.retrySame()} />
    </div>
  );
}

export function Containment() {
  const data = useApi<ContainmentView>("/api/containment");
  return (
    <Section title="Kill switches" mode={data.data?.data_mode}>
      <p>
        A kill switch stops part of ATLAS when something unsafe happens, for example a write outside the sandbox branch or an
        outside connection in Offline mode. Only the auditor can resume, and only after the incident's steps are recorded.
      </p>
      {data.isLoading ? <Loading what="kill switches" /> : <ErrorView error={data.error} />}
      {(data.data?.switches ?? []).map((s) => (
        <article key={s.switch_id} className="card">
          <h3>
            {s.title ?? s.switch_id}: <span className={s.state === "tripped" ? "sev sev-critical" : ""}>{s.state}</span>
          </h3>
          {s.incident ? <IncidentPanel incident={s.incident} /> : null}
        </article>
      ))}
    </Section>
  );
}
