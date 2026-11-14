import { useState } from "react";
import type { Catalog, ManualTrigger } from "../api/types";
import { useAction, useApi } from "../app/context";
import { ActionResult, ErrorView, Loading, Section } from "../components/common";
import { Link } from "../lib/router";

function TriggerForm({ trigger }: { trigger: ManualTrigger }) {
  const [values, setValues] = useState<Record<string, string>>({});
  const action = useAction<Record<string, string>, { run_id?: string }>(trigger.path);
  const available = trigger.availability === "available";
  const missing = trigger.parameters.filter((p) => p.required && !values[p.name]);
  return (
    <form
      className="trigger"
      onSubmit={(e) => {
        e.preventDefault();
        const body = Object.fromEntries(Object.entries(values).filter(([, v]) => v !== ""));
        void action.run(body);
      }}
    >
      <h3>{trigger.title}</h3>
      <p className="muted">Allowed roles: {trigger.allowed_roles.join(", ")}</p>
      {trigger.parameters.map((p) => (
        <label key={p.name}>
          {p.name.replace(/_/g, " ")}
          {p.required ? " (required)" : ""}
          <input
            name={p.name}
            type={p.type === "date-time" ? "datetime-local" : "text"}
            value={values[p.name] ?? ""}
            onChange={(e) => setValues({ ...values, [p.name]: e.target.value })}
          />
        </label>
      ))}
      {!available ? (
        <p className="state state-staged" data-testid="unavailable-reason">
          Not available now: {trigger.reason ?? "the server did not say why"}
        </p>
      ) : null}
      <button type="submit" disabled={!available || missing.length > 0 || action.state.status === "running"}>
        Run now
      </button>
      <ActionResult
        state={action.state}
        retry={() => void action.retrySame()}
        done={(r) => (r.run_id ? <span>Started. <Link to={`/runs/${encodeURIComponent(r.run_id)}`}>Open run {r.run_id}</Link></span> : "Started.")}
      />
    </form>
  );
}

export function RunNow() {
  const catalog = useApi<Catalog>("/api/triggers/catalog");
  return (
    <Section title="Run now" mode={catalog.data?.data_mode}>
      <p>
        Start a run by hand. Manual runs follow the same budgets, guardrails and audit record as automatic runs. The server
        decides whether each run is available; a disabled button shows its reason.
      </p>
      {catalog.isLoading ? <Loading what="the trigger list" /> : <ErrorView error={catalog.error} />}
      <div className="cards">
        {(catalog.data?.manual ?? []).map((t) => (
          <TriggerForm key={t.trigger_id} trigger={t} />
        ))}
      </div>
    </Section>
  );
}
