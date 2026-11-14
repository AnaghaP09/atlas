import { useState } from "react";
import { ApiError } from "../api/client";
import { useAction, useApi, useIdentity, useInvalidateAll } from "../app/context";
import { typeLabel } from "../components/access";
import { Button, DataModeBadge, Empty, ErrorView, Loading, Section, Tabs, Time } from "../components/common";

export interface AccessRequest {
  request_id: string;
  object_type: string;
  object_id: string;
  requester_role?: string;
  state: "pending" | "granted" | "denied" | "withdrawn" | "revoked" | string;
  reason?: string | null;
  created_at?: string;
  expires_at?: string | null;
  grant_id?: string | null;
  automatic?: boolean;
}

const STATE_PILL: Record<string, string> = {
  pending: "pill-warn",
  granted: "pill-info",
  denied: "pill-critical",
  withdrawn: "pill-neutral",
  revoked: "pill-neutral",
};

const APPROVERS = new Set(["security", "audit"]);

function RequestRow({ r, deciding, done }: { r: AccessRequest; deciding: boolean; done: () => void }) {
  const decide = useAction<{ decision: string; hours?: number }, unknown>(`/api/access-requests/${encodeURIComponent(r.request_id)}/decision`);
  const withdraw = useAction<Record<string, never>, unknown>(`/api/access-requests/${encodeURIComponent(r.request_id)}/withdraw`);
  const revoke = useAction<Record<string, never>, unknown>(`/api/access-grants/${encodeURIComponent(r.grant_id ?? "")}/revoke`);
  return (
    <tr>
      <td>{typeLabel(r.object_type)}</td>
      <td>{deciding ? r.requester_role ?? "—" : null}</td>
      <td>
        <span className={`pill ${STATE_PILL[r.state] ?? "pill-neutral"}`}>{r.state}</span>
        {r.state === "granted" && r.expires_at ? <> <span className="muted">until <Time iso={r.expires_at} short /></span></> : null}
      </td>
      <td className="muted">{r.reason ?? ""}</td>
      <td><Time iso={r.created_at ?? null} short /></td>
      <td className="row-actions">
        {deciding && r.state === "pending" ? (
          <>
            <Button size="sm" variant="primary" onClick={() => void decide.run({ decision: "grant", hours: 24 }).then(done)}>Grant 24 h</Button>
            <Button size="sm" variant="danger" onClick={() => void decide.run({ decision: "deny" }).then(done)}>Deny</Button>
          </>
        ) : null}
        {deciding && r.state === "granted" && r.grant_id ? (
          <Button size="sm" variant="danger" onClick={() => void revoke.run({}).then(done)}>Revoke</Button>
        ) : null}
        {!deciding && r.state === "pending" ? (
          <Button size="sm" variant="ghost" onClick={() => void withdraw.run({}).then(done)}>Withdraw</Button>
        ) : null}
      </td>
    </tr>
  );
}

type Box = "mine" | "to_decide";

export function AccessRequests() {
  const { principal } = useIdentity();
  const approver = APPROVERS.has(principal?.role ?? "");
  const [box, setBox] = useState<Box>("mine");
  const list = useApi<{ requests: AccessRequest[] }>("/api/access-requests", { box });
  const refresh = useInvalidateAll();
  const missing = list.error instanceof ApiError && list.error.status === 404;
  return (
    <Section title={box === "mine" ? "My requests" : "To decide"} icon="key">
      {approver ? (
        <Tabs<Box> label="Requests" value={box} onChange={setBox} tabs={[{ value: "mine", label: "Mine" }, { value: "to_decide", label: "To decide" }]} />
      ) : null}
      {list.isLoading ? <Loading what="requests" /> : null}
      {missing ? <DataModeBadge mode="STAGED" reason="access requests are not connected yet" /> : <ErrorView error={list.error} />}
      {list.data && list.data.requests.length === 0 ? <Empty>No requests.</Empty> : null}
      {list.data && list.data.requests.length ? (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Item</th><th>{box === "to_decide" ? "Requested by" : ""}</th><th>Status</th><th>Reason</th><th>When</th><th /></tr></thead>
            <tbody>
              {list.data.requests.map((r) => (
                <RequestRow key={r.request_id} r={r} deciding={box === "to_decide"} done={refresh} />
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </Section>
  );
}
