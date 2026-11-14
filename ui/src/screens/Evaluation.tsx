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

export function GateRow({ gate }: { gate: Gate }) {
  const isForecast = gate.gate_id === "G-13";
  return (
    <article className={`gate ${gate.status}${isForecast ? " gate-wide" : ""}`} data-testid={`gate-${gate.gate_id}`}>
      <header className="gate-head">
        <strong>{GATE_TITLES[gate.gate_id] ?? "Quality check"}</strong>
        <span className="end"><GateStatus gate={gate} /></span>
      </header>
      {gate.summary ? <p className="muted">{gate.summary}</p> : null}
      {isForecast ? (
        <>
          <p className="num">
            Pooled recall <strong>{pct(gate.pooled?.recall)}</strong>, precision <strong>{pct(gate.pooled?.precision)}</strong>.
            {(gate.per_close ?? []).map((c) => (
              <span key={c.close}> {c.close}: {pct(c.recall)} / {pct(c.precision)}.</span>
            ))}
          </p>
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
