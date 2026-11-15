// The "Release outlook" strip under the top bar, on every page, for the roles the server names.
// Left: what the next release inherits and who should take it. Right: ship status now.
// Terse labels only; the guide page explains the rules.
import { useState } from "react";
import { Link } from "../lib/router";
import { dayLabel, OWNER_LABEL, STATUS_TONE, useOutlook, useOutlookSections, type OwnerBucket, type Outlook, type Q1, type Q2 } from "../lib/outlook";
import { ButtonLink, Drawer } from "./common";
import "./outlook.css";

const BUCKETS: OwnerBucket[] = ["developers", "coding_agents", "decide_first"];

type Open = { kind: "q1"; bucket: OwnerBucket | null } | { kind: "q2" } | null;

export function OutlookStrip() {
  const sections = useOutlookSections();
  const outlook = useOutlook();
  const [open, setOpen] = useState<Open>(null);
  if (!sections.length) return null;
  const o = outlook.data;
  return (
    <section className="outlook-strip" aria-label="Release outlook" data-testid="outlook-strip">
      <span className="outlook-title">Release outlook</span>
      {!o ? (
        <span className="muted outlook-part">{outlook.error ? "Outlook not available" : "Loading…"}</span>
      ) : (
        <>
          {o.q1 ? <Q1Part q1={o.q1} onOpen={(bucket) => setOpen({ kind: "q1", bucket })} /> : null}
          <span className="spacer" />
          {o.q2 ? <Q2Part q2={o.q2} onOpen={() => setOpen({ kind: "q2" })} /> : null}
        </>
      )}
      {o ? <OutlookDrawer outlook={o} open={open} onClose={() => setOpen(null)} /> : null}
    </section>
  );
}

function Q1Part({ q1, onOpen }: { q1: Q1; onOpen: (bucket: OwnerBucket | null) => void }) {
  if (q1.state === "none") {
    return (
      <span className="outlook-part" data-testid="outlook-q1">
        <span>No release close yet</span>
        {q1.in_progress ? (
          <Link to={q1.in_progress.link}>Running</Link>
        ) : q1.run_action.allowed ? (
          <Link className="btn btn-sm btn-primary" to={q1.run_action.link} data-testid="outlook-run">Run</Link>
        ) : null}
      </span>
    );
  }
  const asOf = dayLabel(q1.dataset?.as_of);
  return (
    <span className="outlook-part" data-testid="outlook-q1">
      <button type="button" className="link-button" onClick={() => onOpen(null)} title="What the next release inherits">
        Next release inherits <strong className="num">{q1.total}</strong> item{q1.total === 1 ? "" : "s"}
      </button>
      {BUCKETS.map((b) => (
        <button key={b} type="button" className="link-button outlook-seg" onClick={() => onOpen(b)} data-testid={`outlook-${b}`}>
          {OWNER_LABEL[b]} <strong className="num">{q1.split[b]}</strong>
        </button>
      ))}
      {q1.split.not_allocated ? (
        <button type="button" className="link-button outlook-seg" onClick={() => onOpen("not_allocated")}>
          {OWNER_LABEL.not_allocated} <strong className="num">{q1.split.not_allocated}</strong>
        </button>
      ) : null}
      <span className="pill pill-neutral outlook-tag" title={`Predicted from the release close${asOf ? `; data as of ${asOf}` : ""}`}>
        Predicted{asOf ? ` · ${asOf}` : ""}
      </span>
    </span>
  );
}

function Q2Part({ q2, onOpen }: { q2: Q2; onOpen: () => void }) {
  const tone = STATUS_TONE[q2.status];
  return (
    <span className="outlook-part" data-testid="outlook-q2">
      <Link to="/ship-readiness" className={`pill pill-${tone} outlook-status`} title={q2.reasons[0]?.text} data-testid="outlook-status">
        <span className="pill-dot" aria-hidden="true" /> Ship status: {q2.status_word}
      </Link>
      {q2.status !== "NOT_MEASURED" || q2.blocker_count ? (
        <>
          <button type="button" className="link-button outlook-seg" onClick={onOpen} data-testid="outlook-blockers">
            <strong className="num">{q2.blocker_count}</strong> blocker{q2.blocker_count === 1 ? "" : "s"}
          </button>
          <Link className="outlook-seg" to="/ship-readiness#services" data-testid="outlook-services">
            <strong className="num">{q2.service_count}</strong> service{q2.service_count === 1 ? "" : "s"}
          </Link>
        </>
      ) : (
        <span className="muted outlook-seg">{q2.missing_inputs.length} input{q2.missing_inputs.length === 1 ? "" : "s"} missing</span>
      )}
    </span>
  );
}

function OutlookDrawer({ outlook, open, onClose }: { outlook: Outlook; open: Open; onClose: () => void }) {
  const q1 = outlook.q1;
  const q2 = outlook.q2;
  const title = open?.kind === "q2" ? "What stops shipping" : open?.kind === "q1" && open.bucket ? `Next release · ${OWNER_LABEL[open.bucket]}` : "Next release inherits";
  return (
    <Drawer open={open !== null} title={title} onClose={onClose}>
      {open?.kind === "q1" && q1 ? <Q1List q1={q1} bucket={open.bucket} /> : null}
      {open?.kind === "q2" && q2 ? <BlockerList q2={q2} /> : null}
    </Drawer>
  );
}

function Q1List({ q1, bucket }: { q1: Q1; bucket: OwnerBucket | null }) {
  const items = q1.items.filter((i) => bucket === null || i.owner === bucket);
  return (
    <>
      <p className="muted">
        {q1.dataset?.title ?? "Release close"}
        {dayLabel(q1.dataset?.as_of) ? ` · data as of ${dayLabel(q1.dataset?.as_of)}` : ""} · scores illustrative
      </p>
      {items.length === 0 ? <p className="muted">None.</p> : null}
      <ul className="list">
        {items.map((i, n) => (
          <li key={`${i.id ?? n}`}>
            <span className="grow">
              <span className="row-title">{i.title}</span>
              <span className="row-sub">
                {[i.issue_id ? `#${i.issue_id}` : null, i.component, i.owner_team, i.reason].filter(Boolean).join(" · ")}
              </span>
            </span>
            <span className="pill pill-neutral">{OWNER_LABEL[i.owner]}</span>
            {i.evidence_link ? <Link className="chip cite" to={i.evidence_link}>Evidence</Link> : null}
          </li>
        ))}
      </ul>
      <p className="drawer-links">
        {q1.report_link ? <Link to={q1.report_access === "open" ? q1.report_link : (q1.run_link ?? "/runs")}>{q1.report_access === "open" ? "Forecast report" : "The release close run"}</Link> : null}
        {q1.allocation_link && q1.allocation_access === "open" ? <Link to={q1.allocation_link}>Allocation report</Link> : null}
        <Link to="/help/release-outlook">How this is worked out</Link>
      </p>
    </>
  );
}

const KIND_LABEL: Record<string, string> = {
  failing_ci: "Failing CI job",
  security: "Security gate",
  investigation: "Investigation",
  critical_issue: "Critical issue",
};

export function BlockerList({ q2, showLink = true }: { q2: Q2; showLink?: boolean }) {
  return (
    <>
      <ul className="list">
        {q2.reasons.map((r) => (
          <li key={r.rule}>
            <span className={`pill pill-${STATUS_TONE[r.status]}`}>{r.status === "NOT_MEASURED" ? "Not measured" : r.status === "BLOCKED" ? "Blocks" : r.status === "AT_RISK" ? "Risk" : "Clear"}</span>
            <span className="grow">{r.text}</span>
          </li>
        ))}
      </ul>
      <ul className="list">
        {q2.blockers.slice(0, 30).map((b, n) => (
          <li key={`${b.kind}-${n}`}>
            <span className="grow">
              {b.link ? <Link to={b.link} className="row-title">{b.title}</Link> : <span className="row-title">{b.title}</span>}
              <span className="row-sub">{[KIND_LABEL[b.kind], b.component ?? "not mapped", b.reason].filter(Boolean).join(" · ")}</span>
            </span>
            <span className={`pill pill-${b.effect === "blocks" ? "critical" : "warn"}`}>{b.effect === "blocks" ? "Blocks" : "Risk"}</span>
          </li>
        ))}
      </ul>
      {q2.blockers.length > 30 ? <p className="muted">and {q2.blockers.length - 30} more</p> : null}
      {showLink ? <ButtonLink to="/ship-readiness" variant="secondary" icon="arrow">Ship readiness</ButtonLink> : null}
    </>
  );
}

export function BetaPill() {
  return <span className="pill pill-neutral beta-pill" title="This role's pages are in beta" data-testid="beta-pill">Beta</span>;
}
