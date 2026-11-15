import { useEffect, useState } from "react";
import type { Catalog, ManualTrigger } from "../api/types";
import { useAction, useApi } from "../app/context";
import { ActionResult, Empty, ErrorView, Loading } from "../components/common";
import { Icon, type IconName } from "../components/icons";
import { ROLE_LABELS, type Role } from "../identity/identity";
import { useRouter } from "../lib/router";
import { toast } from "../lib/toast";
import type { SnapshotItem } from "./Home";
import { DatasetPicker } from "../components/dataset";

/** "release-close-v1.9.0" -> "v1.9.0" (also "<project>.release-close-3.1.3" -> "3.1.3"), so a chosen
 * snapshot also fills in its release. */
export function releaseOf(snapshotId: string): string {
  const close = /release-close-([A-Za-z0-9._-]+)$/.exec(snapshotId);
  if (close?.[1]) return close[1];
  const m = /v\d+\.\d+\.\d+/.exec(snapshotId);
  return m ? m[0] : "";
}

const FIELD: Record<string, { label: string; placeholder?: string; long?: boolean }> = {
  event_id: { label: "Issue number", placeholder: "For example 18234" },
  summary: { label: "What failed", placeholder: "Optional" },
  question: { label: "Your question", placeholder: "What will stop the next release?", long: true },
  queued: { label: "Queued failures", placeholder: "Optional" },
  release_id: { label: "Release", placeholder: "For example v1.9.0" },
};

const TRIGGER_ICON: Record<string, IconName> = { release_close: "chart", investigate: "compass", nightly: "clock", question: "help" };

function fieldLabel(name: string): string {
  return FIELD[name]?.label ?? name.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

function rolesText(roles: string[]): string {
  return roles.map((r) => ROLE_LABELS[r as Role] ?? r).join(", ");
}

function TriggerForm({ trigger, snapshots, preset, presetIssue }: { trigger: ManualTrigger; snapshots: SnapshotItem[]; preset: string; presetIssue: string }) {
  const { navigate } = useRouter();
  const hasSnapshot = trigger.parameters.some((p) => p.name === "snapshot_id");
  const [values, setValues] = useState<Record<string, string>>(() => {
    const v: Record<string, string> = {};
    if (hasSnapshot && preset) {
      v.snapshot_id = preset;
      if (trigger.parameters.some((p) => p.name === "release_id")) v.release_id = releaseOf(preset);
    }
    if (presetIssue && trigger.parameters.some((p) => p.name === "event_id")) v.event_id = presetIssue;
    return v;
  });
  const action = useAction<Record<string, string>, { run_id?: string }>(trigger.path);
  const available = trigger.availability === "available";
  const missing = trigger.parameters.filter((p) => p.required && !values[p.name]);
  const started = action.state.status === "done" ? action.state.result.run_id : undefined;
  useEffect(() => {
    if (started) {
      toast("Run started");
      navigate(`/runs/${encodeURIComponent(started)}`);
    }
  }, [started, navigate]);
  const set = (name: string, value: string) => {
    const next = { ...values, [name]: value };
    if (name === "snapshot_id" && trigger.parameters.some((p) => p.name === "release_id")) next.release_id = releaseOf(value) || values.release_id || "";
    setValues(next);
  };
  // Optional timestamps are advanced options the picker already covers.
  const fields = trigger.parameters.filter((p) => !(p.type === "date-time" && !p.required));
  return (
    <form
      className={`trigger${available ? "" : " trigger-off"}${presetIssue && values.event_id ? " trigger-focus" : ""}`}
      onSubmit={(e) => {
        e.preventDefault();
        const body = Object.fromEntries(Object.entries(values).filter(([, v]) => v !== ""));
        void action.run(body);
      }}
    >
      <div className="trigger-head">
        <span className="trigger-icon"><Icon name={TRIGGER_ICON[trigger.trigger_id] ?? "play"} size={18} /></span>
        <h3>{trigger.title}</h3>
      </div>
      {fields.map((p) =>
        p.name === "snapshot_id" && snapshots.length ? (
          <DatasetPicker key={p.name} value={values[p.name] ?? ""} onChange={(v) => set(p.name, v)} list={snapshots} />
        ) : p.name === "release_id" && hasSnapshot && releaseOf(values.snapshot_id ?? "") ? null : (
          <label key={p.name}>
            {fieldLabel(p.name)}
            {FIELD[p.name]?.long ? (
              <textarea name={p.name} rows={3} placeholder={FIELD[p.name]?.placeholder} value={values[p.name] ?? ""} onChange={(e) => set(p.name, e.target.value)} />
            ) : (
              <input name={p.name} type="text" placeholder={FIELD[p.name]?.placeholder} value={values[p.name] ?? ""} onChange={(e) => set(p.name, e.target.value)} />
            )}
          </label>
        ),
      )}
      <div className="trigger-foot">
        <button type="submit" className="btn btn-primary btn-md" title={!available ? trigger.reason ?? undefined : undefined} disabled={!available || missing.length > 0 || action.state.status === "running"}>
          {action.state.status === "running" ? <span className="spinner" aria-hidden="true" /> : <Icon name={available ? "play" : "lock"} size={16} />}
          Run now
        </button>
        {!available ? (
          <span className="blocked-note" data-testid="unavailable-reason" title={trigger.reason ?? undefined}>
            <Icon name="lock" size={12} /> {trigger.reason === "offline" ? "Paused while offline" : trigger.reason ? trigger.reason.replace(/^kill switch /i, "Stopped by kill switch: ").replace(/^./, (c) => c.toUpperCase()) : `For: ${rolesText(trigger.allowed_roles)}`}
          </span>
        ) : null}
      </div>
      <ActionResult state={action.state} retry={() => void action.retrySame()} done={() => "Started"} />
    </form>
  );
}

export function RunNow() {
  const { search } = useRouter();
  const catalog = useApi<Catalog>("/api/triggers/catalog");
  const snaps = useApi<{ snapshots: SnapshotItem[] }>("/api/snapshots");
  const preset = search.get("snapshot") ?? "";
  const presetIssue = search.get("investigate") ?? "";
  if (catalog.isLoading) return <Loading what="the trigger list" />;
  if (catalog.error) return <ErrorView error={catalog.error} />;
  // Checking a run's claims again starts from the run itself, so it is not offered here.
  const manual = (catalog.data?.manual ?? []).filter((t) => !t.path.includes("{"));
  if (manual.length === 0) return <Empty>No manual triggers are configured on this server.</Empty>;
  const ordered = presetIssue ? manual.slice().sort((a, b) => Number(b.trigger_id === "investigate") - Number(a.trigger_id === "investigate")) : manual;
  return (
    <div className="cards trigger-cards">
      {ordered.map((t) => (
        <TriggerForm key={t.trigger_id} trigger={t} snapshots={snaps.data?.snapshots ?? []} preset={preset} presetIssue={presetIssue} />
      ))}
    </div>
  );
}
