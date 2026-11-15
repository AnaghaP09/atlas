import { ApiError } from "../api/client";
import type { Evaluation as EvaluationView, Gate } from "../api/types";
import { useApi } from "../app/context";
import { DataModeBadge, Empty, ErrorView, Loading, Section, Time } from "../components/common";
import { PROXY_NOTE } from "./Reports";
import type { SnapshotItem } from "./Home";
import { datasetLine } from "../components/dataset";

const pct = (v?: number) => (typeof v === "number" ? `${Math.round(v * 100)}%` : "—");

/** What each check measures, in plain words. */
const GATE_TITLES: Record<string, string> = {
  "G-1": "Golden examples",
  "G-2": "Verifier catches planted errors",
  "G-3": "Every claim supported",
  "G-4": "Injection handling",
  "G-5": "Loop limits and speed",
  "G-6": "Outbound connections",
  "G-7": "Cost measurement",
  "G-8": "Carry-over forecast",
  "G-9": "Coding task accepted",
  "G-10": "Stage proofs",
  "G-11": "Access control",
  "G-12": "Kill switches",
  "G-13": "Forecast accuracy (proxy blockers)",
};

function GateStatus({ gate }: { gate: Gate }) {
  if (gate.status === "unevaluated") return <DataModeBadge mode="UNEVALUATED" />;
  const ok = gate.status === "passed";
  return <span className={`pill ${ok ? "pill-good" : "pill-critical"}`}>{ok ? "Passed" : "Failed"}</span>;
}

/** Shown beside every forecast-accuracy figure. */
export const FORECAST_NOTE =
  "Counts only blockers already open when the forecast was made; uses a substitute for release blockers because KubeVirt does not record them.";

/** "3 of 5 (60%)", or "not measurable" when the denominator is empty. */
export function share(part: number | undefined, whole: number | undefined, ratio: number | null | undefined): string {
  if (ratio === null || whole === 0) return "not measurable";
  if (typeof part === "number" && typeof whole === "number") return `${part} of ${whole} (${pct(ratio ?? part / whole)})`;
  return typeof ratio === "number" ? pct(ratio) : "not measurable";
}

/** True when the gate names a person as its checker, not a tool or a provisional label. */
export function checkedByPerson(gate: Gate): boolean {
  return /person/i.test(gate.checked_by ?? "") && !/provisional/i.test(gate.checked_by ?? "");
}

function summaryText(gate: Gate): string | null {
  if (!gate.summary) return null;
  if (!checkedByPerson(gate)) return gate.summary;
  const rest = gate.summary.split("; ").filter((p) => !/^Checked by /.test(p)).join("; ");
  return rest || null;
}

function CloseLine({ label, c, testId }: { label: string; c: Gate["pooled"]; testId: string }) {
  return (
    <li data-testid={testId}>
      <span className="num">
        <strong>{label}</strong>: recall {share(c?.matched, c?.blockers, c?.recall)}, precision {share(c?.named_blockers, c?.predictions, c?.precision)}.
      </span>{" "}
      <span className="muted">{c?.note ?? FORECAST_NOTE}</span>
    </li>
  );
}

export function GateRow({ gate }: { gate: Gate }) {
  const isForecast = gate.gate_id === "G-13";
  const summary = summaryText(gate);
  return (
    <article className={`gate ${gate.status}${isForecast ? " gate-wide" : ""}`} data-testid={`gate-${gate.gate_id}`}>
      <header className="gate-head">
        <strong>{GATE_TITLES[gate.gate_id] ?? "Quality check"}</strong>
        <span className="end">
          {checkedByPerson(gate) ? <span className="pill pill-neutral" data-testid={`checked-${gate.gate_id}`}>Checked by one person</span> : null}{" "}
          <GateStatus gate={gate} />
        </span>
      </header>
      {summary ? <p className="muted">{summary}</p> : null}
      {isForecast ? (
        <>
          <ul className="list">
            {gate.pooled ? <CloseLine label="All releases" c={gate.pooled} testId="g13-pooled" /> : null}
            {(gate.per_close ?? []).map((c) => (
              <CloseLine key={c.close} label={`Release ${c.close}`} c={c} testId={`g13-close-${c.close}`} />
            ))}
          </ul>
          <p className="banner banner-note" data-testid="g13-proxy">{gate.proxy_note ?? PROXY_NOTE}</p>
        </>
      ) : null}
    </article>
  );
}

function TestCopies() {
  const snaps = useApi<{ snapshots: SnapshotItem[] }>("/api/snapshots");
  const copies = (snaps.data?.snapshots ?? []).filter((s) => s.is_test_copy || (s.kind ?? "").endsWith("-aliased"));
  if (!copies.length) return null;
  return (
    <Section title="Test copies" icon="database" end={<span className="pill pill-neutral" title="Names disguised for testing">Names disguised</span>}>
      <ul className="list">
        {copies.map((s) => (
          <li key={s.snapshot_id}>
            <span className="grow">{datasetLine(s)}</span>
          </li>
        ))}
      </ul>
    </Section>
  );
}

export function Evaluation() {
  const data = useApi<EvaluationView>("/api/evaluations/latest");
  const none = data.error instanceof ApiError && data.error.status === 404;
  const gates = data.data?.gates ?? [];
  const passed = gates.filter((g) => g.status === "passed").length;
  const failed = gates.filter((g) => g.status === "failed").length;
  const unevaluated = gates.filter((g) => g.status === "unevaluated").length;
  return (
    <>
      <p className="banner banner-note" data-testid="proxy-note-always">{data.data?.proxy_limitation ?? PROXY_NOTE}</p>
      <Section title="Latest evaluation" icon="gauge" mode={data.data?.data_mode}>
        {data.isLoading ? <Loading what="the latest evaluation" /> : null}
        {none ? (
          <Empty>No evaluation has finished yet.</Empty>
        ) : (
          <ErrorView error={data.error} />
        )}
        {data.data ? (
          <>
            <p className="muted">
              Evaluation <span className="mono">{data.data.evaluation_id}</span>, finished <Time iso={data.data.completed_at ?? null} />.{" "}
              <span className="num">
                {passed} passed · {failed} failed · {unevaluated} not evaluated
              </span>
            </p>
            <div className="gates">{gates.map((g) => <GateRow key={g.gate_id} gate={g} />)}</div>
          </>
        ) : null}
      </Section>
      <TestCopies />
    </>
  );
}
