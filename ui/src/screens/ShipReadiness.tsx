// Ship readiness: what stops the release now, which services it touches, and the delivery status with
// the rule that set it. Also the two outlook cards that lead the product manager, engineering manager
// and solution architect home pages.
import { Empty, ErrorView, KpiTile, Loading, Section, Time } from "../components/common";
import { BlockerList } from "../components/OutlookStrip";
import { Link } from "../lib/router";
import { dayLabel, OWNER_LABEL, STATUS_TONE, useOutlook, useOutlookSections, type Q1, type Q2 } from "../lib/outlook";

const INPUT_LABEL: Record<string, string> = {
  failing_ci: "Failing CI jobs",
  security_gate: "Security gate",
  investigations: "Investigations waiting for a person",
  critical_issues: "Open critical issues",
};

const KIND_LABEL: Record<string, string> = {
  failing_ci: "Failing CI",
  security: "Security",
  investigation: "Investigation",
  critical_issue: "Critical issue",
};

function StatusTile({ q2 }: { q2: Q2 }) {
  return (
    <KpiTile
      icon="gauge"
      tone={q2.status === "ON_TRACK" ? "good" : q2.status === "AT_RISK" ? "warn" : q2.status === "BLOCKED" ? "critical" : "accent"}
      label="Can we still deliver?"
      value={<span className={`pill pill-${STATUS_TONE[q2.status]}`} data-testid="ship-status"><span className="pill-dot" aria-hidden="true" /> {q2.status_word}</span>}
      small
      hint={q2.reasons[0]?.text}
    />
  );
}

export function ShipReadiness() {
  const sections = useOutlookSections();
  const outlook = useOutlook();
  if (!sections.includes("q2")) return <Empty>Ship readiness is not part of this role's pages yet.</Empty>;
  if (outlook.isLoading) return <Loading what="ship readiness" />;
  if (outlook.error) return <ErrorView error={outlook.error} />;
  const q2 = outlook.data?.q2;
  if (!q2) return null;
  const inputs = q2.inputs;
  return (
    <>
      <div className="kpis">
        <StatusTile q2={q2} />
        <KpiTile icon="power" tone={q2.blocking_count ? "critical" : q2.blocker_count ? "warn" : "accent"} label="Blockers" value={q2.blocker_count} hint={`${q2.blocking_count} block, ${q2.blocker_count - q2.blocking_count} risk`} />
        <KpiTile icon="graph" tone="teal" label="Services affected" value={q2.service_count} hint={<a href="#services">See the list</a>} />
        <KpiTile icon="clock" label="Inputs checked" value={<Time iso={outlook.data?.computed_at} short />} small hint={q2.last_poll ? <>sources polled <Time iso={q2.last_poll} short /></> : "no live poll yet"} />
      </div>

      <Section title="Delivery status" icon="gauge" end={<Link to="/help/release-outlook">The rules</Link>}>
        <ul className="list" data-testid="ship-reasons">
          {q2.reasons.map((r) => (
            <li key={r.rule}>
              <span className={`pill pill-${STATUS_TONE[r.status]}`}>{r.status === "BLOCKED" ? "Blocked" : r.status === "AT_RISK" ? "At risk" : r.status === "ON_TRACK" ? "On track" : "Not measured"}</span>
              <span className="grow">{r.text}</span>
              <span className="muted mono">{r.rule}</span>
            </li>
          ))}
        </ul>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Input</th><th className="num">Value</th><th>Source</th></tr></thead>
            <tbody>
              <tr>
                <td>{INPUT_LABEL.failing_ci}</td>
                <td className="num">{inputs.failing_ci ? inputs.failing_ci.count : <span className="pill pill-neutral">Not measured</span>}</td>
                <td>{inputs.failing_ci ? <Link to="/dashboard">Live dashboard</Link> : <span className="muted">{q2.missing_inputs.find((m) => m.input === "failing_ci")?.reason}</span>}</td>
              </tr>
              <tr>
                <td>{INPUT_LABEL.security_gate}</td>
                <td className="num">{inputs.security_gate ? `${inputs.security_gate.state === "blocked" ? "Blocked" : "Passed"} · ${inputs.security_gate.blocking_findings} blocking` : <span className="pill pill-neutral">Not measured</span>}</td>
                <td>{inputs.security_gate ? <Link to="/dashboard">Live dashboard (dependency scan)</Link> : <span className="muted">{q2.missing_inputs.find((m) => m.input === "security_gate")?.reason}</span>}</td>
              </tr>
              <tr>
                <td>{INPUT_LABEL.investigations}</td>
                <td className="num">{inputs.investigations ? `${inputs.investigations.escalated} decide · ${inputs.investigations.held} held` : <span className="pill pill-neutral">Not measured</span>}</td>
                <td>{inputs.investigations ? <Link to="/runs">Runs</Link> : <span className="muted">{q2.missing_inputs.find((m) => m.input === "investigations")?.reason}</span>}</td>
              </tr>
              <tr>
                <td>{INPUT_LABEL.critical_issues}</td>
                <td className="num">{inputs.critical_issues ? inputs.critical_issues.count : <span className="pill pill-neutral">Not measured</span>}</td>
                <td>{inputs.critical_issues ? <><Link to="/dashboard">Live dashboard</Link> <span className="muted mono">{inputs.critical_issues.label}</span></> : <span className="muted">{q2.missing_inputs.find((m) => m.input === "critical_issues")?.reason}</span>}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </Section>

      <Section title="Blockers" icon="power">
        {q2.blockers.length === 0 ? <p className="muted">None found in the measured inputs.</p> : (
          <div className="table-wrap">
            <table data-testid="blockers-table">
              <thead><tr><th>What</th><th>Kind</th><th>Service</th><th>Effect</th></tr></thead>
              <tbody>
                {q2.blockers.map((b, n) => (
                  <tr key={`${b.kind}-${n}`}>
                    <td>{b.link ? <Link to={b.link}>{b.title}</Link> : b.title}{b.reason ? <span className="row-sub">{b.reason}</span> : null}</td>
                    <td>{KIND_LABEL[b.kind] ?? b.kind}</td>
                    <td>{b.component ?? <span className="muted">not mapped</span>}</td>
                    <td><span className={`pill pill-${b.effect === "blocks" ? "critical" : "warn"}`}>{b.effect === "blocks" ? "Blocks" : "Risk"}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <span id="services" />
      <Section title="Affected services" icon="graph" end={q2.graph_link ? <Link to={q2.graph_link}>Knowledge graph</Link> : null}>
        {q2.affected_services.length === 0 ? <p className="muted">None.</p> : (
          <div className="table-wrap">
            <table data-testid="services-table">
              <thead><tr><th>Service</th><th className="num">Blockers</th><th className="num">Blocking</th><th>From</th></tr></thead>
              <tbody>
                {q2.affected_services.map((s) => (
                  <tr key={s.component}>
                    <td>{s.link ? <Link to={s.link}>{s.component}</Link> : <span className="muted">{s.component}</span>}</td>
                    <td className="num">{s.blockers}</td>
                    <td className="num">{s.blocks}</td>
                    <td>{Object.entries(s.kinds).map(([k, v]) => `${KIND_LABEL[k] ?? k} ${v}`).join(" · ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="muted">CI jobs map to a service by the sig name in the job name; issues by their sig label; dependency findings to {"project-dependencies"}.</p>
      </Section>
    </>
  );
}

function Q1Card({ q1 }: { q1: Q1 }) {
  const asOf = dayLabel(q1.dataset?.as_of);
  if (q1.state === "none") {
    return (
      <Section title="What will the next release inherit?" icon="chart">
        <Empty action={q1.run_action.allowed ? { to: q1.run_action.link, label: "Run a release close" } : undefined}>
          No release close yet.{q1.run_action.allowed ? "" : " A product manager or engineering manager can run one."}
        </Empty>
      </Section>
    );
  }
  return (
    <Section
      title="What will the next release inherit?"
      icon="chart"
      end={<span className="pill pill-neutral" title="Predicted from the release close">Predicted{asOf ? ` · data as of ${asOf}` : ""}</span>}
    >
      <p className="outlook-lead" data-testid="home-q1">
        <strong className="num">{q1.total}</strong> item{q1.total === 1 ? "" : "s"} ·{" "}
        {(["developers", "coding_agents", "decide_first"] as const).map((b, i) => (
          <span key={b}>{i ? " · " : ""}{OWNER_LABEL[b]} <strong className="num">{q1.split[b]}</strong></span>
        ))}
        {q1.split.not_allocated ? <span> · {OWNER_LABEL.not_allocated} <strong className="num">{q1.split.not_allocated}</strong></span> : null}
      </p>
      <ul className="list">
        {q1.items.slice(0, 5).map((i, n) => (
          <li key={i.id ?? n}>
            <span className="grow">
              <span className="row-title">{i.title}</span>
              <span className="row-sub">{[i.issue_id ? `#${i.issue_id}` : null, i.component, i.reason].filter(Boolean).join(" · ")}</span>
            </span>
            <span className="pill pill-neutral">{OWNER_LABEL[i.owner]}</span>
            {i.evidence_link ? <Link className="chip cite" to={i.evidence_link}>Evidence</Link> : null}
          </li>
        ))}
      </ul>
      <p className="muted">
        {q1.dataset?.title ?? "Release close"} · scores illustrative ·{" "}
        <Link to={q1.report_access === "open" && q1.report_link ? q1.report_link : (q1.run_link ?? "/runs")}>All items</Link>
      </p>
    </Section>
  );
}

function Q2Card({ q2 }: { q2: Q2 }) {
  return (
    <Section title="What will stop us shipping?" icon="power" end={<Link to="/ship-readiness">Ship readiness</Link>}>
      <p className="outlook-lead" data-testid="home-q2">
        <span className={`pill pill-${STATUS_TONE[q2.status]}`}><span className="pill-dot" aria-hidden="true" /> {q2.status_word}</span>{" "}
        <strong className="num">{q2.blocker_count}</strong> blocker{q2.blocker_count === 1 ? "" : "s"} ·{" "}
        <strong className="num">{q2.service_count}</strong> service{q2.service_count === 1 ? "" : "s"}
      </p>
      <BlockerList q2={{ ...q2, blockers: q2.blockers.slice(0, 5) }} showLink={false} />
      {q2.blockers.length > 5 ? <p className="muted">and {q2.blockers.length - 5} more on <Link to="/ship-readiness">Ship readiness</Link></p> : null}
    </Section>
  );
}

/** The top of the home page for the roles the outlook is built for: Q1 and Q2 first. */
export function OutlookHomeLead() {
  const sections = useOutlookSections();
  const outlook = useOutlook();
  if (!sections.length) return null;
  if (outlook.isLoading) return <Loading what="the release outlook" lines={2} />;
  if (outlook.error) return <ErrorView error={outlook.error} />;
  const o = outlook.data;
  if (!o) return null;
  return (
    <div className="grid-2 outlook-home" data-testid="outlook-home">
      <div>{o.q1 ? <Q1Card q1={o.q1} /> : null}</div>
      <div>{o.q2 ? <Q2Card q2={o.q2} /> : null}</div>
    </div>
  );
}
