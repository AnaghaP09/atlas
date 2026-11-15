import { useEffect, useMemo, useState } from "react";
import type { Catalog, ManualTrigger, RunView } from "../api/types";
import { useAction, useApi } from "../app/context";
import { ActionResult, Empty, ErrorView, Loading } from "../components/common";
import { Icon, type IconName } from "../components/icons";
import { ROLE_LABELS, type Role } from "../identity/identity";
import { useRouter } from "../lib/router";
import { toast } from "../lib/toast";
import type { SnapshotItem } from "./Home";
import { DatasetPicker, datasetLine, kindOf, visibleDatasets } from "../components/dataset";

/** "release-close-v1.9.0" -> "v1.9.0" (also "<project>.release-close-3.1.3" -> "3.1.3"), so a chosen
 * snapshot also fills in its release. */
export function releaseOf(snapshotId: string): string {
  const close = /release-close-([A-Za-z0-9._-]+)$/.exec(snapshotId);
  if (close?.[1]) return close[1];
  const m = /v\d+\.\d+\.\d+/.exec(snapshotId);
  return m ? m[0] : "";
}

/** The release a data set was taken at, from its id or its stored label. */
function releaseFor(s: SnapshotItem | undefined): string {
  if (!s) return "";
  return releaseOf(s.snapshot_id) || /v?\d+\.\d+\.\d+/.exec(String(s.release_or_case ?? s.release ?? ""))?.[0] || "";
}

export interface SnapshotEvent {
  event_id: string;
  number?: number | null;
  title: string;
  state?: string;
  created_at?: string | null;
  labels?: string[];
  failure?: boolean;
}

export const SUGGESTED_QUESTIONS = [
  "What will stop the next release?",
  "Which flaky tests fail most often, and in which components?",
  "Which components carry the most open bugs?",
  "Which open issues are critical and have no owner?",
  "Are there open security findings that block the release?",
];

const TRIGGER_ICON: Record<string, IconName> = { release_close: "chart", investigate: "compass", nightly: "clock", question: "help", reverify: "ok" };

const KIND_HINT: Record<string, string> = {
  release_close: "Forecast, allocation and architecture reports for one release.",
  investigate: "Find the cause of one failure or issue, with its evidence.",
  nightly: "Investigate the failures waiting in the queue.",
  question: "Ask about the project. The answer cites its records.",
  reverify: "Check a finished run's claims again against its data.",
};

function rolesText(roles: string[]): string {
  return roles.map((r) => ROLE_LABELS[r as Role] ?? r).join(", ");
}

function day(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

/** Newest data set first; release closes only list releases. */
function datasetsFor(kind: string, list: SnapshotItem[]): SnapshotItem[] {
  const shown = visibleDatasets(list).filter((s) => (kind === "release_close" ? kindOf(s) === "release" : true));
  return shown.slice().sort((a, b) => String(b.as_of ?? b.cutoff ?? "").localeCompare(String(a.as_of ?? a.cutoff ?? "")));
}

/** "Which failure or issue?": searchable, failures first, newest first, titles as labels. */
function EventPicker({ snapshotId, value, onChange, preset }: { snapshotId: string; value: string; onChange: (id: string, title: string) => void; preset: string }) {
  const [q, setQ] = useState("");
  const data = useApi<{ events: SnapshotEvent[]; total: number }>(snapshotId ? `/api/snapshots/${encodeURIComponent(snapshotId)}/events` : null);
  const all = useMemo(() => data.data?.events ?? [], [data.data]);
  useEffect(() => {
    if (!preset || value) return;
    const hit = all.find((e) => e.event_id === preset);
    if (hit) onChange(hit.event_id, hit.title);
  }, [all, preset, value, onChange]);
  if (!snapshotId) return <p className="muted">Choose a point in time first.</p>;
  if (data.isLoading) return <Loading what="the failures in this data set" lines={1} />;
  if (data.error) return <ErrorView error={data.error} />;
  if (all.length === 0) return <p className="muted">This data set has no issues to investigate. Choose another point in time.</p>;
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  const match = (e: SnapshotEvent) => words.every((w) => `${e.title} #${e.event_id}`.toLowerCase().includes(w));
  const shown = all.filter(match);
  const failures = shown.filter((e) => e.failure);
  const others = shown.filter((e) => !e.failure);
  const option = (e: SnapshotEvent) => (
    <option key={e.event_id} value={e.event_id}>
      {e.title} Ã‚Â· #{e.event_id}{e.created_at ? ` Ã‚Â· ${day(e.created_at)}` : ""}{e.state === "closed" ? " Ã‚Â· closed" : ""}
    </option>
  );
  const presetMissing = preset && !all.some((e) => e.event_id === preset);
  return (
    <div className="event-picker">
      <label>
        Find a failure or issue
        <input type="search" placeholder="Search by title" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search failures and issues" />
      </label>
      <label>
        Which failure or issue?
        <select
          value={value}
          aria-label="Which failure or issue?"
          onChange={(e) => {
            const hit = all.find((x) => x.event_id === e.target.value);
            onChange(e.target.value, hit?.title ?? "");
          }}
        >
          <option value="">{shown.length ? `ChooseÃ¢â‚¬Â¦ (${shown.length} of ${all.length})` : "Nothing matches the search"}</option>
          {failures.length ? <optgroup label="Failures and flaky tests">{failures.map(option)}</optgroup> : null}
          {others.length ? <optgroup label="Other issues">{others.map(option)}</optgroup> : null}
        </select>
      </label>
      {presetMissing ? <p className="muted">Issue #{preset} is not in this data set. Choose a later point in time or another issue.</p> : null}
    </div>
  );
}

/** "Which question?": a suggestion, or your own words. */
function QuestionPicker({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const suggested = SUGGESTED_QUESTIONS.includes(value) ? value : "";
  return (
    <>
      <label>
        Suggested questions
        <select value={suggested} aria-label="Suggested questions" onChange={(e) => onChange(e.target.value)}>
          <option value="">Choose a suggestionÃ¢â‚¬Â¦</option>
          {SUGGESTED_QUESTIONS.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </label>
      <label>
        Your question
        <textarea name="question" rows={3} placeholder="Or write your own question" value={value} onChange={(e) => onChange(e.target.value)} />
      </label>
    </>
  );
}

/** "Which run?": finished runs by their readable names, newest first. */
function RunPicker({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const runs = useApi<{ runs: RunView[] }>("/api/runs");
  if (runs.isLoading) return <Loading what="runs" lines={1} />;
  if (runs.error) return <ErrorView error={runs.error} />;
  const done = (runs.data?.runs ?? [])
    .filter((r) => r.reports.length > 0)
    .sort((a, b) => String(b.started_at ?? "").localeCompare(String(a.started_at ?? "")));
  if (!done.length) return <p className="muted">No finished run to check yet. Start a release close or an investigation first.</p>;
  return (
    <label>
      Which run?
      <select value={value} aria-label="Which run?" onChange={(e) => onChange(e.target.value)}>
        <option value="">ChooseÃ¢â‚¬Â¦</option>
        {done.map((r) => (
          <option key={r.run_id} value={r.run_id}>{r.title ?? r.kind_label ?? "Run"}{r.started_at ? ` Ã‚Â· ${day(r.started_at)}` : ""}</option>
        ))}
      </select>
    </label>
  );
}

function TriggerForm({ trigger, snapshots, preset, presetIssue }: { trigger: ManualTrigger; snapshots: SnapshotItem[]; preset: string; presetIssue: string }) {
  const { navigate } = useRouter();
  const kind = trigger.trigger_id;
  const usesData = trigger.parameters.some((p) => p.name === "snapshot_id");
  const choices = useMemo(() => datasetsFor(kind, snapshots), [kind, snapshots]);
  const [picked, setSnapshotId] = useState("");
  // Until a person chooses, the linked page's data set or the newest one is used.
  const snapshotId = picked || (preset && choices.some((s) => s.snapshot_id === preset) ? preset : usesData ? choices[0]?.snapshot_id ?? "" : "");
  const [eventId, setEventId] = useState("");
  const [summary, setSummary] = useState("");
  const [question, setQuestion] = useState("");
  const [runId, setRunId] = useState("");
  const path = kind === "reverify" ? trigger.path.replace("{run_id}", encodeURIComponent(runId)) : trigger.path;
  const action = useAction<Record<string, string>, { run_id?: string }>(path);
  const available = trigger.availability === "available";
  const chosen = choices.find((s) => s.snapshot_id === snapshotId);
  const release = releaseFor(chosen);
  const ready =
    kind === "reverify" ? Boolean(runId)
    : !usesData || !snapshotId ? false
    : kind === "release_close" ? Boolean(release)
    : kind === "investigate" ? Boolean(eventId)
    : kind === "question" ? question.trim().length > 0
    : true;
  const started = action.state.status === "done" ? action.state.result.run_id : undefined;
  useEffect(() => {
    if (started) {
      toast(kind === "reverify" ? "Check started" : "Run started");
      navigate(`/runs/${encodeURIComponent(started)}`);
    }
  }, [started, navigate, kind]);
  const onEvent = useMemo(() => (id: string, title: string) => { setEventId(id); setSummary(title); }, []);
  const body = (): Record<string, string> => {
    if (kind === "reverify") return {};
    const b: Record<string, string> = { snapshot_id: snapshotId };
    if (kind === "release_close") b.release_id = release;
    if (kind === "investigate") Object.assign(b, { event_id: eventId, summary });
    if (kind === "question") b.question = question.trim();
    return b;
  };
  return (
    <form
      className={`trigger${available ? "" : " trigger-off"}`}
      data-testid={`trigger-${kind}`}
      onSubmit={(e) => {
        e.preventDefault();
        void action.run(body());
      }}
    >
      <div className="trigger-head">
        <span className="trigger-icon"><Icon name={TRIGGER_ICON[kind] ?? "play"} size={18} /></span>
        <h3>{trigger.title}</h3>
      </div>
      <p className="muted">{KIND_HINT[kind] ?? ""}</p>
      {usesData ? (
        choices.length ? (
          <DatasetPicker
            value={snapshotId}
            onChange={(v) => { setSnapshotId(v); setEventId(""); setSummary(""); }}
            list={choices}
          />
        ) : (
          <Empty action={{ to: "/add-project", label: "Add project data" }}>No data set for this project yet.</Empty>
        )
      ) : null}
      {kind === "release_close" && chosen ? (
        <p className="fact-line">Release <strong>{release || "not known for this data set"}</strong> Ã‚Â· {datasetLine(chosen)}</p>
      ) : null}
      {kind === "investigate" ? <EventPicker snapshotId={snapshotId} value={eventId} onChange={onEvent} preset={presetIssue} /> : null}
      {kind === "question" ? <QuestionPicker value={question} onChange={setQuestion} /> : null}
      {kind === "reverify" ? <RunPicker value={runId} onChange={setRunId} /> : null}
      <div className="trigger-foot">
        <button type="submit" className="btn btn-primary btn-md" title={!available ? trigger.reason ?? undefined : undefined} disabled={!available || !ready || action.state.status === "running"}>
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
  const presetKind = search.get("kind") ?? "";
  const [picked, setPicked] = useState("");
  if (catalog.isLoading) return <Loading what="the trigger list" />;
  if (catalog.error) return <ErrorView error={catalog.error} />;
  // Only the kinds this role may start; a paused kind stays listed with its reason.
  const order = (catalog.data?.manual ?? []).filter((t) => t.reason !== "role");
  if (order.length === 0) return <Empty action={{ to: "/runs", label: "Open Runs" }}>Your role does not start runs by hand.</Empty>;
  const fallback =
    (presetIssue && order.find((t) => t.trigger_id === "investigate" && t.reason !== "role")) ||
    (presetKind && order.find((t) => t.trigger_id === presetKind)) ||
    (preset && order.find((t) => t.trigger_id === "release_close" && t.reason !== "role")) ||
    order.find((t) => t.availability === "available") ||
    order[0];
  const current = order.find((t) => t.trigger_id === picked) ?? fallback!;
  return (
    <div className="run-now">
      <label className="run-kind">
        What kind of run?
        <select value={current.trigger_id} aria-label="What kind of run?" onChange={(e) => setPicked(e.target.value)}>
          {order.map((t) => (
            <option key={t.trigger_id} value={t.trigger_id}>
              {t.title}
            </option>
          ))}
        </select>
      </label>
      <div className="cards trigger-cards">
        <TriggerForm key={current.trigger_id} trigger={current} snapshots={snaps.data?.snapshots ?? []} preset={preset} presetIssue={presetIssue} />
      </div>
    </div>
  );
}
