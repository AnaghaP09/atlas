import { ApiError } from "../api/client";
import type { Evaluation as EvaluationView, Gate } from "../api/types";
import { useApi } from "../app/context";
import { DataModeBadge, ErrorView, Loading, Section, Time } from "../components/common";
import { PROXY_NOTE } from "./Reports";

const pct = (v?: number) => (typeof v === "number" ? `${Math.round(v * 100)}%` : "—");

export function GateRow({ gate }: { gate: Gate }) {
  const isForecast = gate.gate_id === "G-13";
  return (
    <tr data-testid={`gate-${gate.gate_id}`}>
      <td>{gate.gate_id}</td>
      <td>
        {gate.status === "unevaluated" ? (
          <DataModeBadge mode="UNEVALUATED" />
        ) : (
          <span className={gate.status === "passed" ? "pass" : "fail"}>{gate.status === "passed" ? "Passed" : "Failed"}</span>
        )}
      </td>
      <td>
        {gate.summary ?? ""}
        {isForecast ? (
          <>
            <br />
            Pooled recall {pct(gate.pooled?.recall)}, precision {pct(gate.pooled?.precision)}.
            {(gate.per_close ?? []).map((c) => (
              <span key={c.close}> {c.close}: {pct(c.recall)} / {pct(c.precision)}.</span>
            ))}
            <p className="banner" data-testid="g13-proxy">{gate.proxy_note ?? PROXY_NOTE}</p>
          </>
        ) : null}
      </td>
    </tr>
  );
}

export function Evaluation() {
  const data = useApi<EvaluationView>("/api/evaluations/latest");
  const none = data.error instanceof ApiError && data.error.status === 404;
  return (
    <Section title="Quality checks" mode={data.data?.data_mode}>
      <p>
        These are the quality bars ATLAS must pass. A check that is not measured in this build shows as not evaluated, never as
        passed.
      </p>
      <p className="banner" data-testid="proxy-note-always">{data.data?.proxy_limitation ?? PROXY_NOTE}</p>
      {data.isLoading ? <Loading what="the latest evaluation" /> : null}
      {none ? <p>No evaluation has finished yet.</p> : <ErrorView error={data.error} />}
      {data.data ? (
        <>
          <p className="muted">Evaluation {data.data.evaluation_id}, finished <Time iso={data.data.completed_at ?? null} />.</p>
          <table>
            <thead><tr><th>Check</th><th>Result</th><th>Details</th></tr></thead>
            <tbody>{data.data.gates.map((g) => <GateRow key={g.gate_id} gate={g} />)}</tbody>
          </table>
        </>
      ) : null}
    </Section>
  );
}
