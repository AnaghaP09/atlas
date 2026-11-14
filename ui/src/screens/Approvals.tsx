import { useEffect, useState } from "react";
import type { Approval } from "../api/types";
import { useAction, useApi } from "../app/context";
import { ActionResult, ErrorView, Loading, Section, Time } from "../components/common";

function useNow(intervalMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}

function Countdown({ iso }: { iso?: string }) {
  const now = useNow();
  if (!iso) return <span>—</span>;
  const ms = new Date(iso).getTime() - now;
  if (Number.isNaN(ms)) return <span>{iso}</span>;
  if (ms <= 0) return <span className="sev sev-high">expired</span>;
  const h = Math.floor(ms / 3_600_000);
  const m = Math.floor((ms % 3_600_000) / 60_000);
  return (
    <span>
      {h} h {m} min left (<Time iso={iso} />)
    </span>
  );
}

function ApprovalCard({ a }: { a: Approval }) {
  const decide = useAction<{ decision: string; reason?: string }, unknown>(`/api/approvals/${encodeURIComponent(a.approval_id)}/decision`);
  const [reason, setReason] = useState("");
  return (
    <article className="card" data-testid="approval">
      <h3>
        Approval {a.kind} for task {a.task_id} — {a.state}
      </h3>
      <dl className="facts">
        <dt>Publishes to</dt><dd>{a.destination_repository ?? "—"}</dd>
        <dt>Base</dt><dd>{a.base_ref ?? "—"} at {a.base_commit ?? "—"}</dd>
        <dt>Branch</dt><dd>{a.publication_ref ?? "—"}</dd>
        <dt>Verification generation</dt><dd>{a.verification_generation ?? "—"}</dd>
        <dt>Security decision</dt>
        <dd>
          {a.security_decision_id ?? "—"} valid until <Time iso={a.security_valid_until ?? null} />
        </dd>
        {a.kind === "B" ? (
          <>
            <dt>Change hash</dt><dd><code data-testid="diff-hash">{a.diff_hash ?? a.artefact_hash ?? "—"}</code></dd>
          </>
        ) : null}
        <dt>Expires</dt><dd><Countdown iso={a.expires_at} /></dd>
      </dl>
      {a.kind === "B" && typeof a.diff === "string" ? (
        <>
          <h4>The exact change that will be published</h4>
          <pre className="diff">{a.diff}</pre>
        </>
      ) : null}
      {a.state === "pending" ? (
        <div className="actions">
          <label>
            Reason (optional)
            <input value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
          <button onClick={() => void decide.run({ decision: "approve", reason })}>Approve</button>
          <button onClick={() => void decide.run({ decision: "reject", reason })}>Reject</button>
          <ActionResult state={decide.state} retry={() => void decide.retrySame()} />
        </div>
      ) : null}
    </article>
  );
}

export function Approvals() {
  const data = useApi<{ approvals: Approval[]; data_mode?: string }>("/api/approvals");
  return (
    <Section title="Approvals" mode={data.data?.data_mode}>
      <p>
        Every code change needs two approvals from the engineering manager. Approval A allows the coding agent to prepare the
        task. Approval B approves the exact change, by its hash, before anything is published. Both expire after 48 hours.
      </p>
      {data.isLoading ? <Loading what="approvals" /> : <ErrorView error={data.error} />}
      {data.data && data.data.approvals.length === 0 ? <p>No approvals.</p> : null}
      {(data.data?.approvals ?? []).map((a) => (
        <ApprovalCard key={a.approval_id} a={a} />
      ))}
    </Section>
  );
}
