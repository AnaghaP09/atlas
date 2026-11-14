// Locked items and access requests. Ordinary items unlock at once, read-only for a limited time;
// sensitive items wait for the security or audit role. The server decides which; the UI shows it.
import { useState } from "react";
import { useAction } from "../app/context";
import { Button, Time } from "./common";
import { Icon } from "./icons";

export interface AccessRequestState {
  request_id?: string;
  state: "pending" | "granted" | "denied" | "withdrawn" | string;
  expires_at?: string | null;
}

export interface AccessGrantView {
  mode: "read_only" | string;
  expires_at?: string | null;
  automatic?: boolean;
  owning_role?: string | null;
  owning_role_title?: string | null;
}

const TYPE_LABEL: Record<string, string> = {
  report: "Report",
  task: "Task",
  approval: "Approval",
  run: "Run",
  graph: "Graph",
  scorecard: "Scorecard row",
  incident: "Incident",
  finding: "Security finding",
  page: "Page",
  notification: "Notification",
  evidence: "Evidence",
};

export function typeLabel(t: string): string {
  return TYPE_LABEL[t] ?? t.replace(/_/g, " ");
}

/** A locked item: lock icon, its type only (never its title), and a "Request access" action. */
export function LockedChip({ objectType, objectId, request }: { objectType: string; objectId: string; request?: AccessRequestState | null }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const send = useAction<{ object_type: string; object_id: string; reason?: string }, { status: string; expires_at?: string | null; request_id?: string }>(
    "/api/access-requests",
  );
  const result = send.state.status === "done" ? send.state.result : null;
  const state = result ? (result.status === "granted" ? "granted" : "pending") : request?.state;
  const expires = result?.expires_at ?? request?.expires_at ?? null;
  return (
    <span className="locked">
      <span className="chip locked-chip" title="No access">
        <Icon name="lock" size={14} /> {typeLabel(objectType)}
      </span>
      {state === "pending" ? <span className="pill pill-warn" data-testid="access-state">Requested</span> : null}
      {state === "denied" ? <span className="pill pill-critical" data-testid="access-state">Denied</span> : null}
      {state === "granted" ? (
        <span className="pill pill-info" data-testid="access-state">
          Granted until <Time iso={expires} short />
        </span>
      ) : null}
      {!state || state === "withdrawn" ? (
        open ? (
          <span className="request-pop" role="dialog" aria-label="Request access">
            <input aria-label="Reason (optional)" placeholder="Reason (optional)" value={reason} maxLength={140} onChange={(e) => setReason(e.target.value)} />
            <Button
              size="sm"
              variant="primary"
              loading={send.state.status === "running"}
              onClick={() => void send.run({ object_type: objectType, object_id: objectId, ...(reason ? { reason } : {}) })}
            >
              Send
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
          </span>
        ) : (
          <Button size="sm" variant="ghost" icon="lock" onClick={() => setOpen(true)}>Request access</Button>
        )
      ) : null}
      {send.state.status === "failed" ? <span className="state-error">Not sent</span> : null}
    </span>
  );
}

/** Shown on a view opened through a grant: read-only pill, and one muted line for automatic grants. */
export function ReadOnlyBanner({ access, myRole }: { access?: AccessGrantView | null; myRole?: string | null }) {
  if (!access || access.mode !== "read_only") return null;
  const showNote = access.automatic && access.owning_role && access.owning_role !== myRole;
  return (
    <p className="read-only" data-testid="read-only">
      <span className="pill pill-info">
        <Icon name="lock" size={12} /> Read-only until <Time iso={access.expires_at ?? null} short />
      </span>
      {showNote ? (
        <span className="muted small" data-testid="exception-note">
          Meant for the {access.owning_role_title ?? access.owning_role} only. Opened as an exception for this demo.
        </span>
      ) : null}
    </p>
  );
}

export function isReadOnly(access?: AccessGrantView | null): boolean {
  return !!access && access.mode === "read_only";
}
