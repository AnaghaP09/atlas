import { useEffect, useState } from "react";
import type { Approval } from "../api/types";
import { useAction, useApi, useInvalidateAll } from "../app/context";
import { ActionResult, Button, ConfirmButton, Empty, ErrorView, IdDetails, Loading, StatusPill, Time } from "../components/common";
import { LockedChip } from "../components/access";
import { Icon } from "../components/icons";
import { EntityLink } from "../components/related";
import { useRouter } from "../lib/router";
import { BlockedNote, useBlockedBy } from "./Containment";

function useNow(intervalMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}

export function Countdown({ iso }: { iso?: string | null }) {
  const now = useNow();
  if (!iso) return <span className="muted">—</span>;
  const ms = new Date(iso).getTime() - now;
  if (Number.isNaN(ms)) return <span>{iso}</span>;
  if (ms <= 0) return <span className="pill pill-critical">Expired</span>;
  const h = Math.floor(ms / 3_600_000);
  const m = Math.floor((ms % 3_600_000) / 60_000);
  return (
    <span className={`pill ${h < 4 ? "pill-warn" : "pill-neutral"}`} title={new Date(iso).toLocaleString()}>
      <Icon name="clock" size={12} /> {h > 0 ? `${h} h ${m} min left` : `${m} min left`}
    </span>
  );
}

const KIND_TITLE: Record<string, string> = { A: "Let the coding agent prepare the change", B: "Publish this exact change" };

function shortHash(v: unknown): string | null {
  return typeof v === "string" && v ? (v.length > 14 ? `${v.slice(0, 12)}…` : v) : null;
}

function ApprovalCard({ a, focused }: { a: Approval; focused: boolean }) {
  const decide = useAction<{ decision: string; review_minutes?: number }, unknown>(`/api/approvals/${encodeURIComponent(a.approval_id)}/decision`);
  const refresh = useInvalidateAll();
  const blocked = useBlockedBy(a.kind === "B" ? "publication" : "preparation");
  const pending = a.state === "pending";
  const hash = a.diff_hash ?? a.artefact_hash;
  return (
    <article className={`card approval${focused ? " is-focus" : ""}`} data-testid="approval">
      <header className="approval-head">
        <span className="approval-kind">Approval {a.kind}</span>
        <h2>{KIND_TITLE[a.kind] ?? "Approval"}</h2>
        <span className="end inline-tools">
          {pending ? <Countdown iso={a.expires_at} /> : null}
          <StatusPill status={pending ? "held" : a.state} />
        </span>
      </header>
      <div className="approval-grid">
        <div>
          <span className="fact-label">Publishes to</span>
          <span className="fact-value">{a.destination_repository ?? "—"}</span>
        </div>
        <div>
          <span className="fact-label">Branch</span>
          <span className="fact-value mono">{a.publication_ref ?? "—"}</span>
        </div>
        <div>
          <span className="fact-label">Based on</span>
          <span className="fact-value mono">{a.base_ref ?? "—"}{shortHash(a.base_commit) ? ` · ${shortHash(a.base_commit)}` : ""}</span>
        </div>
        <div>
          <span className="fact-label">Security check valid until</span>
          <span className="fact-value">{a.security_valid_until ? <Time iso={a.security_valid_until} short /> : "—"}</span>
        </div>
        {a.kind === "B" ? (
          <div>
            <span className="fact-label">Change fingerprint</span>
            <span className="fact-value mono" data-testid="diff-hash" title={String(hash ?? "")}>{shortHash(hash) ?? "—"}</span>
          </div>
        ) : null}
        <div>
          <span className="fact-label">Task</span>
          <span className="fact-value"><EntityLink entity="task" id={a.task_id}>Open the task</EntityLink></span>
        </div>
      </div>
      {a.kind === "B" && typeof a.diff === "string" ? <pre className="diff" aria-label="The change that will be published">{a.diff}</pre> : null}
      {pending ? (
        <div className="approval-actions">
          <ConfirmButton
            label="Approve"
            title={a.kind === "B" ? "Publish this change as a draft pull request?" : "Let the coding agent prepare this change?"}
            body={a.kind === "B" ? "Only the change shown here can be published." : "Nothing is published at this step."}
            confirmLabel="Approve"
            variant="primary"
            size="md"
            icon={blocked.length ? "lock" : "ok"}
            disabled={blocked.length > 0}
            hint={blocked.length ? `Stopped by: ${blocked[0]!.title}` : undefined}
            onConfirm={() => void decide.run({ decision: "approve" }).then(refresh)}
          />
          <Button variant="secondary" onClick={() => void decide.run({ decision: "reject" }).then(refresh)}>Reject</Button>
          <BlockedNote by={blocked} />
          <ActionResult state={decide.state} retry={() => void decide.retrySame()} success="Decision recorded" />
        </div>
      ) : null}
      <IdDetails items={[["Approval id", a.approval_id], ["Task id", a.task_id], ["Check number", a.verification_generation != null ? String(a.verification_generation) : null], ["Security decision", a.security_decision_id ?? null], ["Change fingerprint", typeof hash === "string" ? hash : null]]} />
    </article>
  );
}

interface LockedItem {
  object_type: string;
  object_id: string;
}

export function Approvals() {
  const data = useApi<{ approvals: Approval[]; locked_items?: LockedItem[]; data_mode?: string }>("/api/approvals");
  const { search } = useRouter();
  const focus = search.get("focus");
  if (data.isLoading) return <Loading what="approvals" />;
  if (data.error) return <ErrorView error={data.error} />;
  const all = (data.data?.approvals ?? []).slice().sort((a, b) => Number(b.state === "pending") - Number(a.state === "pending"));
  const locked = data.data?.locked_items ?? [];
  if (all.length === 0 && locked.length === 0)
    return <Empty action={{ to: "/tasks", label: "Open tasks" }}>No approvals are waiting.</Empty>;
  return (
    <div className="approval-list">
      {all.map((a) => <ApprovalCard key={a.approval_id} a={a} focused={focus === a.approval_id} />)}
      {locked.length ? (
        <div className="chip-row">
          {locked.map((l) => <LockedChip key={l.object_id} objectType={l.object_type} objectId={l.object_id} />)}
        </div>
      ) : null}
    </div>
  );
}
