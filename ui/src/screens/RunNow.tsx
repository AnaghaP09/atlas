import { useState } from "react";
import type { Catalog, ManualTrigger } from "../api/types";
import { useAction, useApi } from "../app/context";
import { ActionResult, Empty, ErrorView, Loading } from "../components/common";
import { Icon } from "../components/icons";
import { Link, useRouter } from "../lib/router";
import type { SnapshotItem } from "./Home";
import { DatasetPicker } from "../components/dataset";

/** "release-close-v1.9.0" -> "v1.9.0", so a chosen snapshot also fills in its release. */
export function releaseOf(snapshotId: string): string {
  const m = /v\d+\.\d+\.\d+/.exec(snapshotId);
  return m ? m[0] : "";
}

function TriggerForm({ trigger, snapshots, preset }: { trigger: ManualTrigger; snapshots: SnapshotItem[]; preset: string }) {
  const hasSnapshot = trigger.parameters.some((p) => p.name === "snapshot_id");
  const [values, setValues] = useState<Record<string, string>>(() => {
    if (!hasSnapshot || !preset) return {};
    const v: Record<string, string> = { snapshot_id: preset };
    if (trigger.parameters.some((p) => p.name === "release_id")) v.release_id = releaseOf(preset);
    return v;
  });
  const action = useAction<Record<string, string>, { run_id?: string }>(trigger.path);
  const available = trigger.availability === "available";
  const missing = trigger.parameters.filter((p) => p.required && !values[p.name]);
  const set = (name: string, value: string) => {
    const next = { ...values, [name]: value };
    if (name === "snapshot_id" && trigger.parameters.some((p) => p.name === "release_id") && !values.release_id) next.release_id = releaseOf(value);
    setValues(next);
  };
  return (
    <form
      className={`trigger${available ? "" : " trigger-off"}`}
      onSubmit={(e) => {
        e.preventDefault();
        const body = Object.fromEntries(Object.entries(values).filter(([, v]) => v !== ""));
        void action.run(body);
      }}
    >
      <div className="trigger-head">
        <span className="trigger-icon"><Icon name="play" size={18} /></span>
        <div>
          <h3>{trigger.title}</h3>
          <p className="muted">Allowed roles: {trigger.allowed_roles.join(", ")}</p>
        </div>
      </div>
      {trigger.parameters.map((p) =>
        p.name === "snapshot_id" && snapshots.length ? (
          <DatasetPicker key={p.name} value={values[p.name] ?? ""} onChange={(v) => set(p.name, v)} list={snapshots} />
        ) : p.name === "release_id" && hasSnapshot ? null : (
          <label key={p.name}>
            {p.name.replace(/_/g, " ")}
            {p.required ? " (required)" : ""}
            <input
              name={p.name}
              type={p.type === "date-time" ? "datetime-local" : "text"}
              value={values[p.name] ?? ""}
              onChange={(e) => set(p.name, e.target.value)}
            />
          </label>
        ),
      )}
      {!available ? (
        <p className="state state-staged" data-testid="unavailable-reason">
          Not available now: {trigger.reason ?? "the server did not say why"}
        </p>
      ) : null}
      <div className="trigger-foot">
        <button type="submit" className="btn btn-primary btn-md" disabled={!available || missing.length > 0 || action.state.status === "running"}>
          Run now
        </button>
        {available && missing.length ? <span className="muted">Fill in: {missing.map((m) => m.name.replace(/_/g, " ")).join(", ")}</span> : null}
      </div>
      <ActionResult
        state={action.state}
        retry={() => void action.retrySame()}
        done={(r) => (r.run_id ? <span>Started. <Link to={`/runs/${encodeURIComponent(r.run_id)}`}>Open run {r.run_id}</Link></span> : "Started.")}
      />
    </form>
  );
}

export function RunNow() {
  const { search } = useRouter();
  const catalog = useApi<Catalog>("/api/triggers/catalog");
  const snaps = useApi<{ snapshots: SnapshotItem[] }>("/api/snapshots");
  const preset = search.get("snapshot") ?? "";
  if (catalog.isLoading) return <Loading what="the trigger list" />;
  if (catalog.error) return <ErrorView error={catalog.error} />;
  const manual = catalog.data?.manual ?? [];
  if (manual.length === 0) return <Empty>No manual triggers are configured on this server.</Empty>;
  return (
    <div className="cards">
      {manual.map((t) => (
        <TriggerForm key={t.trigger_id} trigger={t} snapshots={snaps.data?.snapshots ?? []} preset={preset} />
      ))}
    </div>
  );
}
