import { useState } from "react";
import { ApiError } from "../api/client";
import { useAction, useApi, useIdentity, useInvalidateAll } from "../app/context";
import { typeLabel } from "../components/access";
import { ActionResult, Button, ConfirmButton, DataModeBadge, Empty, ErrorView, Loading, Section, Tabs, Time } from "../components/common";
import { ROLE_LABELS, type Role } from "../identity/identity";
import { Link } from "../lib/router";

export interface AccessRequest {
  request_id: string;
  object_type: string;
  object_id: string;
  label?: string;
  requester?: { user?: string | null; role?: string };
  status: "pending" | "granted" | "denied" | "withdrawn" | "revoked" | "expired" | string;
  needs_decision?: boolean;
  reason?: string | null;
  created_at?: string;
  grant?: { grant_id?: string; expires_at?: string | null; automatic?: boolean } | null;
}

const STATE_WORD: Record<string, string> = {
  pending: "Waiting for a decision",
  granted: "Granted",
  denied: "Denied",
  withdrawn: "Withdrawn",
  revoked: "Taken back",
  expired: "Ended",
};

const STATE_PILL: Record<string, string> = {
  pending: "pill-warn",
  granted: "pill-good",
  denied: "pill-critical",
  withdrawn: "pill-neutral",
  revoked: "pill-neutral",
};

// Any role can own items others ask for (automatic grants can be revoked by the owning role).
const MAIN_ROLES = new Set(["pm", "em", "architect"]);
const APPROVERS = new Set(["pm", "em", "architect", "security", "oncall", "audit"]);

function RequestRow({ r, deciding, done }: { r: AccessRequest; deciding: boolean; done: () => void }) {
  const decide = useAction<{ decision: string; hours?: number }, unknown>(`/api/access-requests/${encodeURIComponent(r.request_id)}/decision`);
  const withdraw = useAction<Record<string, never>, unknown>(`/api/access-requests/${encodeURIComponent(r.request_id)}/withdraw`);
  const revoke = useAction<Record<string, never>, unknown>(`/api/access-grants/${encodeURIComponent(r.grant?.grant_id ?? "")}/revoke`);
  const role = r.requester?.role;
  return (
    <tr>
      <td>
        <span className="row-title">{r.label ?? typeLabel(r.object_type)}</span>
        {deciding ? <span className="row-sub">Asked by the {ROLE_LABELS[role as Role]?.toLowerCase() ?? role ?? "unknown role"}</span> : null}
      </td>
      <td>
        <span className={`pill ${STATE_PILL[r.status] ?? "pill-neutral"}`}>{STATE_WORD[r.status] ?? r.status}</span>
        {r.status === "granted" && r.grant?.expires_at ? <span className="row-sub">{r.grant?.automatic ? "Granted at once Â· " : ""}ends <Time iso={r.grant.expires_at} short /></span> : null}
      </td>
      <td className="muted">{r.reason || "â€”"}</td>
      <td className="muted"><Time iso={r.created_at ?? null} short /></td>
      <td className="row-actions always">
        {!deciding && r.status === "granted" ? (
          <Link className="btn btn-sm btn-secondary" to={`/object/${encodeURIComponent(r.object_type)}/${encodeURIComponent(r.object_id)}`}>Open</Link>
        ) : null}
        {deciding && r.status === "pending" ? (
          <>
            <Button size="sm" variant="primary" icon="ok" loading={decide.state.status === "running"} onClick={() => void decide.run({ decision: "grant", hours: 24 }).then(done)}>Grant for 24 h</Button>
            <Button size="sm" variant="ghost" onClick={() => void decide.run({ decision: "deny" }).then(done)}>Deny</Button>
          </>
        ) : null}
        {deciding && r.status === "granted" && r.grant?.grant_id ? (
          <ConfirmButton label="Revoke" title="Take this access back?" confirmLabel="Revoke" danger onConfirm={() => void revoke.run({}).then(done)} />
        ) : null}
        {!deciding && r.status === "pending" ? (
          <Button size="sm" variant="ghost" onClick={() => void withdraw.run({}).then(done)}>Withdraw</Button>
        ) : null}
        <ActionResult state={decide.state} retry={() => void decide.retrySame()} success="Decision recorded" />
        <ActionResult state={revoke.state} retry={() => void revoke.retrySame()} success="Access taken back" />
        <ActionResult state={withdraw.state} retry={() => void withdraw.retrySame()} success="Request withdrawn" />
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
      {list.data && list.data.requests.length === 0 ? (
        <Empty action={{ to: "/runs", label: "Open Runs" }}>
          {box === "mine" && MAIN_ROLES.has(principal?.role ?? "") ? "Your role reads every item in this project. There is nothing to ask for." : box === "mine" ? "You have not asked for anything." : "Nothing is waiting for your decision."}
        </Empty>
      ) : null}
      {list.data && list.data.requests.length ? (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Item</th><th>Status</th><th>Reason</th><th>Asked</th><th /></tr></thead>
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
