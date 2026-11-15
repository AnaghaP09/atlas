// Product requests and hand-offs. The product manager files a request or hands an item over; the
// engineering manager and solution architect see both on their home page and answer them.
import { useState } from "react";
import { useAction, useApi, useIdentity, useInvalidateAll } from "../app/context";
import { ActionResult, Button, Empty, ErrorView, KpiTile, Loading, Section, Time } from "../components/common";
import { Link, useRouter } from "../lib/router";

export interface ProductRequest {
  request_id: string;
  kind: "technical_dependency" | "new_feature";
  title: string;
  description: string;
  link_type?: string | null;
  link_id?: string | null;
  link_label?: string | null;
  owner_role: string;
  info_role: string;
  status: string;
  reply?: string | null;
  created: string;
  for_information?: boolean;
}

export interface Handoff {
  handoff_id: string;
  subject_type: string;
  subject_title: string;
  run_id?: string | null;
  report_id?: string | null;
  to_role: string;
  note: string;
  status: string;
  reply?: string | null;
  decision?: "coding_agent" | "oncall" | null;
  created: string;
}

interface StoryHomeData {
  handoffs_waiting?: number;
  handoffs?: Handoff[];
  requests?: { new: number; owned: number; for_information: number; items: ProductRequest[] };
  my_requests?: { total: number; open: number; items: ProductRequest[] };
  handoffs_sent?: { total: number; waiting: number; items: Handoff[] };
}

export const KIND_LABEL: Record<string, string> = { technical_dependency: "Technical dependency", new_feature: "New feature" };
const ROLE_LABEL: Record<string, string> = { pm: "Product manager", em: "Engineering manager", architect: "Solution architect" };
const STATUS_LABEL: Record<string, string> = {
  new: "New", accepted: "Accepted", needs_more_info: "Needs more info", declined: "Declined",
  waiting: "Waiting", done: "Done",
};
const STATUS_TONE: Record<string, string> = {
  new: "accent", accepted: "good", needs_more_info: "warn", declined: "neutral", waiting: "warn", done: "good",
};

export function RequestStatus({ status }: { status: string }) {
  return <span className={`pill pill-${STATUS_TONE[status] ?? "neutral"}`}>{STATUS_LABEL[status] ?? status}</span>;
}

function NewRequestForm({ onDone }: { onDone: () => void }) {
  const [kind, setKind] = useState<"technical_dependency" | "new_feature">("technical_dependency");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [linkType, setLinkType] = useState("");
  const [linkId, setLinkId] = useState("");
  const send = useAction<Record<string, unknown>, { request_id: string }>("/api/requests");
  const refresh = useInvalidateAll();
  const ready = title.trim() && description.trim() && (!linkType || linkId.trim());
  return (
    <form
      className="form-grid"
      data-testid="new-request"
      onSubmit={(e) => {
        e.preventDefault();
        if (!ready) return;
        void send
          .run({ kind, title, description, ...(linkType ? { link_type: linkType, link_id: linkId, link_label: linkId } : {}) })
          .then(() => { refresh(); onDone(); });
      }}
    >
      <fieldset className="field">
        <legend>Type</legend>
        {(["technical_dependency", "new_feature"] as const).map((k) => (
          <label key={k} className="radio">
            <input type="radio" name="kind" value={k} checked={kind === k} onChange={() => setKind(k)} /> {KIND_LABEL[k]}
            <span className="muted"> · to the {k === "technical_dependency" ? "solution architect" : "engineering manager"}</span>
          </label>
        ))}
      </fieldset>
      <label className="field">
        <span>Title</span>
        <input value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} required />
      </label>
      <label className="field">
        <span>Short description</span>
        <textarea value={description} maxLength={1000} rows={3} onChange={(e) => setDescription(e.target.value)} required />
      </label>
      <label className="field">
        <span>Link (optional)</span>
        <select value={linkType} onChange={(e) => setLinkType(e.target.value)}>
          <option value="">None</option>
          <option value="report_item">Report item</option>
          <option value="component">Component</option>
        </select>
      </label>
      {linkType ? (
        <label className="field">
          <span>{linkType === "component" ? "Component name" : "Issue number or item"}</span>
          <input value={linkId} maxLength={200} onChange={(e) => setLinkId(e.target.value)} />
        </label>
      ) : null}
      <div className="form-actions">
        <Button type="submit" variant="primary" icon="arrow" disabled={!ready} loading={send.state.status === "running"}>Send request</Button>
        <ActionResult state={send.state} retry={() => void send.retrySame()} success="Request sent" />
      </div>
    </form>
  );
}

function AnswerForm({ r }: { r: ProductRequest }) {
  const [status, setStatus] = useState("accepted");
  const [reply, setReply] = useState("");
  const send = useAction<{ status: string; reply: string }, unknown>(`/api/requests/${encodeURIComponent(r.request_id)}/answer`);
  const refresh = useInvalidateAll();
  return (
    <form
      className="inline-form"
      data-testid="answer-request"
      onSubmit={(e) => {
        e.preventDefault();
        if (reply.trim()) void send.run({ status, reply }).then(refresh);
      }}
    >
      <select aria-label="Status" value={status} onChange={(e) => setStatus(e.target.value)}>
        <option value="accepted">Accepted</option>
        <option value="needs_more_info">Needs more info</option>
        <option value="declined">Declined</option>
      </select>
      <input aria-label="One-line reply" placeholder="One-line reply" value={reply} maxLength={200} onChange={(e) => setReply(e.target.value)} />
      <Button type="submit" size="sm" variant="primary" disabled={!reply.trim()} loading={send.state.status === "running"}>Send</Button>
      <ActionResult state={send.state} retry={() => void send.retrySame()} success="Answer sent" />
    </form>
  );
}

function RequestRow({ r, myRole, focused }: { r: ProductRequest; myRole: string | null; focused: boolean }) {
  const owner = myRole === r.owner_role;
  return (
    <li className={`request-row${focused ? " is-focus" : ""}`} data-testid="request">
      <div className="grow">
        <strong>{r.title}</strong> <span className="chip chip-quiet">{KIND_LABEL[r.kind] ?? r.kind}</span>
        {myRole === r.info_role ? <span className="chip chip-quiet">For your information</span> : null}
        <p className="muted">{r.description}</p>
        <p className="muted">
          Owner: {ROLE_LABEL[r.owner_role] ?? r.owner_role} · <Time iso={r.created} short />
          {r.link_id ? <> · {r.link_type === "component" ? "Component" : "Item"}: {r.link_label ?? r.link_id}</> : null}
        </p>
        {r.reply ? <p>Reply: {r.reply}</p> : null}
        {owner && r.status !== "accepted" && r.status !== "declined" ? <AnswerForm r={r} /> : null}
      </div>
      <RequestStatus status={r.status} />
    </li>
  );
}

export function Requests() {
  const data = useApi<{ requests: ProductRequest[] }>("/api/requests");
  const { principal } = useIdentity();
  const { search } = useRouter();
  const [open, setOpen] = useState(false);
  const role = principal?.role ?? null;
  const rows = data.data?.requests ?? [];
  return (
    <Section
      title="Requests from product"
      icon="hand"
      end={role === "pm" ? <Button variant="primary" icon="pen" onClick={() => setOpen((v) => !v)}>New request</Button> : null}
    >
      {open && role === "pm" ? <NewRequestForm onDone={() => setOpen(false)} /> : null}
      {data.isLoading ? <Loading what="requests" /> : <ErrorView error={data.error} />}
      {data.data && rows.length === 0 ? <Empty>No requests yet.</Empty> : null}
      {rows.length ? (
        <ul className="list">
          {rows.map((r) => <RequestRow key={r.request_id} r={r} myRole={role} focused={search.get("focus") === r.request_id} />)}
        </ul>
      ) : null}
    </Section>
  );
}

export function RequestsPage() {
  return (
    <>
      <Requests />
      <HandoffList />
    </>
  );
}

/** "Hand to engineering manager" and "Hand to solution architect", for the product manager only. */
export function HandOff({ subjectType, subjectId, title, runId, reportId }: {
  subjectType: "forecast_item" | "investigation" | "report_item";
  subjectId: string;
  title: string;
  runId?: string;
  reportId?: string;
}) {
  const { principal } = useIdentity();
  const [to, setTo] = useState<"em" | "architect" | null>(null);
  const [note, setNote] = useState("");
  const send = useAction<Record<string, unknown>, unknown>("/api/handoffs");
  const refresh = useInvalidateAll();
  if (principal?.role !== "pm") return null;
  if (send.state.status === "done") return <span className="pill pill-good">Handed over</span>;
  if (!to)
    return (
      <span className="inline-tools" data-testid="handoff">
        <Button size="sm" onClick={() => setTo("em")}>Hand to engineering manager</Button>
        <Button size="sm" onClick={() => setTo("architect")}>Hand to solution architect</Button>
      </span>
    );
  return (
    <form
      className="inline-form"
      onSubmit={(e) => {
        e.preventDefault();
        if (!note.trim()) return;
        void send
          .run({ subject_type: subjectType, subject_id: subjectId, subject_title: title, to_role: to, note,
                 ...(runId ? { run_id: runId } : {}), ...(reportId ? { report_id: reportId } : {}) })
          .then(refresh);
      }}
    >
      <input aria-label="Short note" placeholder={`Note for the ${to === "em" ? "engineering manager" : "solution architect"}`} value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} />
      <Button type="submit" size="sm" variant="primary" disabled={!note.trim()} loading={send.state.status === "running"}>Send</Button>
      <Button size="sm" variant="ghost" onClick={() => setTo(null)}>Cancel</Button>
      <ActionResult state={send.state} retry={() => void send.retrySame()} success="Handed over" />
    </form>
  );
}

export const DECISION_LABEL = { coding_agent: "Delegated to coding agent", oncall: "Delegated to on-call engineer" } as const;

/** The engineering manager's decision on a hand-off, shown the same way to every role. */
export function DecisionLabel({ h, myRole }: { h: Handoff; myRole: string | null | undefined }) {
  if (h.decision) return <span className={`pill pill-${h.decision === "coding_agent" ? "accent" : "teal"}`} data-testid="handoff-decision">{DECISION_LABEL[h.decision]}</span>;
  if (h.to_role !== "em") return <RequestStatus status={h.status} />;
  return <span className="pill pill-warn" data-testid="handoff-decision">{myRole === "em" ? "Awaiting your decision" : "Awaiting decision"}</span>;
}

/** Exactly two choices for the engineering manager; the architect accepts the hand-offs sent to them. */
function HandoffAnswer({ h, myRole }: { h: Handoff; myRole: string | null | undefined }) {
  const send = useAction<{ status: string; decision?: string }, { approval_link?: string | null }>(`/api/handoffs/${encodeURIComponent(h.handoff_id)}/answer`);
  const refresh = useInvalidateAll();
  const { navigate } = useRouter();
  if (h.status !== "waiting" || myRole !== h.to_role) return <DecisionLabel h={h} myRole={myRole} />;
  const busy = send.state.status === "running";
  if (myRole === "em")
    return (
      <span className="inline-tools" data-testid="handoff-decide">
        <DecisionLabel h={h} myRole={myRole} />
        <Button size="sm" variant="primary" loading={busy} onClick={() => void send.run({ status: "accepted", decision: "coding_agent" }).then((r) => { refresh(); const to = (r as { approval_link?: string | null } | undefined)?.approval_link; if (to) navigate(to); })}>{DECISION_LABEL.coding_agent}</Button>
        <Button size="sm" loading={busy} onClick={() => void send.run({ status: "accepted", decision: "oncall" }).then(refresh)}>{DECISION_LABEL.oncall}</Button>
      </span>
    );
  return <Button size="sm" loading={busy} onClick={() => void send.run({ status: "accepted" }).then(refresh)}>Accept</Button>;
}

/** Every hand-off in the project, with its decision, for the product manager, engineering manager and architect. */
export function HandoffList() {
  const { principal } = useIdentity();
  const data = useApi<{ handoffs: Handoff[] }>("/api/handoffs");
  const rows = data.data?.handoffs ?? [];
  if (!rows.length) return null;
  return (
    <Section title="Hand-offs" icon="hand">
      <ul className="list" data-testid="handoffs">
        {rows.map((h) => {
          const link = subjectLink(h);
          return (
            <li key={h.handoff_id}>
              <span className="grow">{link ? <Link to={link}>{h.subject_title}</Link> : <strong>{h.subject_title}</strong>}<span className="muted"> · to the {ROLE_LABEL[h.to_role]?.toLowerCase()} · {h.note}</span></span>
              <HandoffAnswer h={h} myRole={principal?.role} />
            </li>
          );
        })}
      </ul>
    </Section>
  );
}

function subjectLink(h: Handoff): string | null {
  if (h.report_id) return `/reports/${encodeURIComponent(h.report_id)}`;
  if (h.run_id) return `/runs/${encodeURIComponent(h.run_id)}`;
  return null;
}

/** The hand-off and request figures on the home page of the product manager, engineering manager and architect. */
export function StoryHome() {
  const { principal } = useIdentity();
  const role = principal?.role;
  const data = useApi<StoryHomeData>(role === "pm" || role === "em" || role === "architect" ? "/api/story/home" : null);
  const d = data.data;
  if (!d) return null;
  if (role === "pm") {
    const mine = d.my_requests;
    return (
      <Section title="My requests" icon="hand" end={<Link to="/requests">Open requests</Link>}>
        <div className="kpis">
          <KpiTile icon="hand" label="Requests open" value={mine?.open ?? 0} hint={`${mine?.total ?? 0} in all`} />
          <KpiTile icon="task" tone="teal" label="Hand-offs waiting" value={d.handoffs_sent?.waiting ?? 0} hint={`${d.handoffs_sent?.total ?? 0} sent`} />
        </div>
        {mine?.items.length ? (
          <ul className="list" data-testid="my-requests">
            {mine.items.map((r) => (
              <li key={r.request_id}><span className="grow"><Link to={`/requests?focus=${encodeURIComponent(r.request_id)}`}>{r.title}</Link> <span className="muted">{KIND_LABEL[r.kind]}</span>{r.reply ? <span className="muted"> · {r.reply}</span> : null}</span><RequestStatus status={r.status} /></li>
            ))}
          </ul>
        ) : <Empty action={{ to: "/requests", label: "New request" }}>No requests yet.</Empty>}
      </Section>
    );
  }
  const req = d.requests;
  return (
    <>
      <div className="kpis">
        <KpiTile icon="hand" tone={(d.handoffs_waiting ?? 0) ? "warn" : "good"} label="Hand-offs waiting" value={d.handoffs_waiting ?? 0} hint="from the product manager" testId="handoffs-waiting" />
        <KpiTile icon="list" tone={(req?.new ?? 0) ? "warn" : "good"} label="Requests from product" value={req?.new ?? 0} hint={`new · ${req?.owned ?? 0} owned by you · ${req?.for_information ?? 0} for information`} testId="requests-from-product" />
      </div>
      {d.handoffs?.length ? (
        <Section title="Hand-offs waiting" icon="hand">
          <ul className="list" data-testid="handoff-list">
            {d.handoffs.map((h) => {
              const link = subjectLink(h);
              return (
                <li key={h.handoff_id}>
                  <span className="grow">{link ? <Link to={link}>{h.subject_title}</Link> : <strong>{h.subject_title}</strong>}<span className="muted"> · {h.note}</span></span>
                  <HandoffAnswer h={h} myRole={role} />
                </li>
              );
            })}
          </ul>
        </Section>
      ) : null}
      {req?.items.length ? (
        <Section title="Requests from product" icon="list" end={<Link to="/requests">Open requests</Link>}>
          <ul className="list" data-testid="request-list">
            {req.items.map((r) => (
              <li key={r.request_id}>
                <span className="grow"><Link to={`/requests?focus=${encodeURIComponent(r.request_id)}`}>{r.title}</Link> <span className="muted">{KIND_LABEL[r.kind]}{r.for_information ? " · for your information" : ""}</span></span>
                <RequestStatus status={r.status} />
              </li>
            ))}
          </ul>
        </Section>
      ) : null}
    </>
  );
}
