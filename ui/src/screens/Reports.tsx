import { HandOff } from "./Requests";
import { useQueryClient } from "@tanstack/react-query";
import { useRunTitles } from "./Queue";
import { useRef, useState } from "react";
import type { LiveEvent } from "../api/stream";
import type { Claim, EvidenceRecord, ReportView } from "../api/types";
import { useApi, useLiveStream } from "../app/context";
import { AgentName, ErrorView, IdDetails, Loading, Section, Time, Tooltip, Truncate } from "../components/common";
import { Icon } from "../components/icons";
import { ExternalLink } from "../components/ExternalLink";
import { actionName, classificationName, ownerName, ownerTone, reportName } from "../lib/labels";
import { VerdictChip } from "./Runs";
import { Related } from "../components/related";
import { ReadOnlyBanner, type AccessGrantView } from "../components/access";
import { useIdentity } from "../app/context";
import { Markdown } from "../lib/markdown";
import { Link } from "../lib/router";
import { dayLabel, OWNER_LABEL, ownerSplit } from "../lib/outlook";

export const PROXY_NOTE =
  "Forecast accuracy is measured against a stand-in definition of a release blocker, because the public KubeVirt project does not record which issues blocked each release.";

const REPORT_EVENTS = new Set(["report.superseded", "report.corrected", "report.withdrawn", "run.generation_changed"]);

export function ReportDetail({ reportId }: { reportId: string }) {
  const runId = reportId.includes(":") ? reportId.slice(0, reportId.lastIndexOf(":")) : reportId;
  const report = useApi<ReportView>(`/api/reports/${encodeURIComponent(reportId)}`);
  const qc = useQueryClient();
  const { principal } = useIdentity();
  const [notice, setNotice] = useState<string | null>(null);
  const handlers = useRef({
    onEvent: (e: LiveEvent) => {
      if (REPORT_EVENTS.has(e.event)) {
        setNotice(
          e.event === "report.withdrawn"
            ? "Part of this report was withdrawn. The page was refreshed."
            : e.event === "report.corrected"
              ? "This report was corrected. The page was refreshed."
              : "A newer version of this report replaced the one you were reading. The page was refreshed.",
        );
        void qc.invalidateQueries();
      }
    },
    onReset: () => {
      setNotice("Live updates restarted. The report was reloaded.");
      void qc.invalidateQueries();
    },
  });
  useLiveStream(`/api/runs/${encodeURIComponent(runId)}/stream`, (e) => handlers.current.onEvent(e), () => handlers.current.onReset());

  const titles = useRunTitles();
  if (report.isLoading) return <Loading what="the report" />;
  if (report.error) return <ErrorView error={report.error} />;
  const r = report.data;
  if (!r) return null;
  const claims: Claim[] = Array.isArray(r.report.claims) ? r.report.claims : [];
  const isForecast = ["forecast", "allocation", "architecture"].includes(r.report_type);
  const proxy = typeof r.report.proxy_note === "string" ? r.report.proxy_note : null;
  return (
    <Section title={reportName(r.report_type)} icon="task" mode={r.data_mode}>
      <ReadOnlyBanner access={(r as unknown as { access?: AccessGrantView }).access} myRole={principal?.role ?? null} />
      <Related
        object={{ type: "report", id: r.report_id }}
        chips={[
          { entity: "run", id: r.run_id, label: "Run" },
          { entity: "evidence", id: claims.flatMap((c) => c.evidence_ids ?? [])[0], label: "Evidence", count: new Set(claims.flatMap((c) => c.evidence_ids ?? [])).size, ctx: { runId: r.run_id } },
        ]}
      />
      {notice ? <p className="banner" role="status">{notice}</p> : null}
      {r.superseded_by ? (
        <p className="banner banner-warn" data-testid="superseded">
          Earlier version. A newer check replaced it.
        </p>
      ) : null}
      <p className="report-meta muted">
        <Link to={`/runs/${encodeURIComponent(r.run_id)}`}>{titles.get(r.run_id) ?? "Open the run"}</Link>
        <span>Version {r.report_version}</span>
        <span>Checked by <AgentName agent="sentinel" compact /></span>
        {isForecast ? (
          <Tooltip text={proxy ?? PROXY_NOTE}>
            <span className="pill pill-neutral" data-testid="proxy-note">Scores are illustrative</span>
          </Tooltip>
        ) : null}
      </p>
      {r.withheld > 0 ? (
        <p className="withheld" role="note">
          <Icon name="lock" size={14} /> {r.withheld} item{r.withheld === 1 ? "" : "s"} hidden for your role
        </p>
      ) : null}
      {r.corrections.length ? (
        <div className="banner">
          <strong>Corrections</strong>
          <ul>{r.corrections.map((c, i) => <li key={c.correction_id ?? i}>{c.text ?? "A correction was applied."}</li>)}</ul>
        </div>
      ) : null}
      {isForecast && r.report_type !== "architecture" ? <PredictedHeader r={r} /> : null}
      <ReportBody r={r} />
    </Section>
  );
}

/** Forecast and allocation items are predictions: say so, say when the data is from, and give the split. */
function PredictedHeader({ r }: { r: ReportView }) {
  const run = useApi<{ snapshot_id?: string }>(`/api/runs/${encodeURIComponent(r.run_id)}`);
  const sid = run.data?.snapshot_id;
  const snap = useApi<{ snapshot?: { cutoff?: string; frozen_at?: string } }>(sid ? `/api/snapshots/${encodeURIComponent(sid)}` : null);
  const asOf = dayLabel(snap.data?.snapshot?.cutoff ?? snap.data?.snapshot?.frozen_at);
  const items: ReportItem[] = Array.isArray(r.report.items) ? (r.report.items as ReportItem[]) : [];
  const split = ownerSplit(items);
  return (
    <p className="report-meta" data-testid="predicted-header">
      <span className="pill pill-neutral" title="Predicted from the release close, not measured">Predicted</span>
      <span className="muted">Data as of {asOf ?? "—"}</span>
      {r.report_type === "allocation" ? (
        <span data-testid="allocation-split">
          {OWNER_LABEL.developers} <strong className="num">{split.developers}</strong> · {OWNER_LABEL.coding_agents}{" "}
          <strong className="num">{split.coding_agents}</strong> · {OWNER_LABEL.decide_first} <strong className="num">{split.decide_first}</strong>
          {split.not_allocated ? <> · {OWNER_LABEL.not_allocated} <strong className="num">{split.not_allocated}</strong></> : null}
        </span>
      ) : null}
    </p>
  );
}

/** The body of a report: the conclusion, the ranked items or the text, and the claims that were checked. */
export function ReportBody({ r }: { r: ReportView }) {
  const claims: Claim[] = Array.isArray(r.report.claims) ? r.report.claims : [];
  const items: ReportItem[] = Array.isArray(r.report.items) ? (r.report.items as ReportItem[]) : [];
  return (
    <>
      <Verdict report={r.report} />
      {r.report_type === "investigation" ? (
        <HandOff subjectType="investigation" subjectId={r.run_id} title={String((r.report.root_cause as { text?: string } | undefined)?.text ?? "Investigation").slice(0, 280)} runId={r.run_id} reportId={r.report_id} />
      ) : null}
      {items.length ? (
        <ItemsTable items={items} runId={r.run_id} kind={r.report_type} claims={claims} />
      ) : (
        <div className="report">
          {r.report_type === "security_gate" ? <SecurityState markdown={r.markdown} /> : <Markdown source={stripHeader(r.markdown)} />}
        </div>
      )}
      {claims.length ? (
        <details className="claims-block" open={!items.length}>
          <summary>
            Claims checked <span className="pill pill-neutral num">{claims.length}</span>
          </summary>
          <ul className="claims">
            {claims.map((c) => (
              <li key={c.id}>
                <VerdictChip verdict={c.verdict} />
                <span className="claim-text">{c.text}</span>
                <span className="cites">
                  {(c.evidence_ids ?? []).map((id, n) => (
                    <Link key={id} className="chip cite" title="Open the cited record" to={`/evidence/${encodeURIComponent(id)}?run=${encodeURIComponent(r.run_id)}`}>
                      Record {n + 1}
                    </Link>
                  ))}
                </span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </>
  );
}

function value(x: unknown): string | null {
  if (x && typeof x === "object" && "value" in x) return typeof (x as { value?: unknown }).value === "string" ? ((x as { value: string }).value) : null;
  return typeof x === "string" ? x : null;
}

/** What an investigation concluded: the kind of problem, what to do next and how sure ATLAS is. */
function Verdict({ report }: { report: Record<string, unknown> }) {
  const kind = classificationName(value(report.classification));
  const action = actionName(value(report.action));
  const confidence = typeof report.confidence === "number" ? report.confidence : null;
  const flagged = report.injection_detected === true;
  const held = report.safety_disposition === "HOLD_FOR_HUMAN" || report.safety_disposition === "REJECTED";
  const missing = Array.isArray(report.insufficient_evidence) ? report.insufficient_evidence.length : 0;
  if (value(report.classification) === "CARRY_OVER_FORECAST" || (!kind && !action && !flagged && !missing)) return null;
  return (
    <div className="verdict" data-testid="verdict">
      <div>
        <span className="verdict-label">Finding</span>
        <span className="verdict-value">{kind ?? "No finding"}</span>
      </div>
      <div>
        <span className="verdict-label">Next step</span>
        <span className="verdict-value">{action ?? "None proposed"}</span>
      </div>
      <div>
        <span className="verdict-label">Confidence</span>
        <span className="verdict-value num">{confidence === null ? "Not stated" : `${Math.round(confidence * 100)}%`}</span>
      </div>
      {flagged ? <span className="pill pill-critical"><Icon name="alert" size={12} /> Hidden instruction found</span> : null}
      {held && !flagged ? <span className="pill pill-warn">Held for a person</span> : null}
      {missing ? <span className="pill pill-neutral">{missing} gap{missing === 1 ? "" : "s"} in the evidence</span> : null}
    </div>
  );
}

interface ReportItem {
  id?: string;
  issue_id?: string | null;
  title?: string | null;
  component?: string | null;
  score?: number | null;
  allocation?: string | null;
  allocation_reason?: string | null;
  owner_team?: string | null;
  evidence?: string[];
  claim_ids?: string[];
}

const SECURITY_STATE: Record<string, [string, string]> = {
  passed: ["Passed", "good"],
  accepted_with_exception: ["Accepted with an exception", "warn"],
  blocked: ["Blocked", "critical"],
  not_run: ["Not run", "warn"],
};

/** The security check of a run as two plain facts. */
function SecurityState({ markdown }: { markdown: string }) {
  const state = /^State:\s*(\S+)/m.exec(markdown)?.[1] ?? "not_run";
  const until = /^Valid until:\s*(\S+)/m.exec(markdown)?.[1];
  const [word, tone] = SECURITY_STATE[state] ?? [state.replace(/_/g, " "), "neutral"];
  return (
    <div className="approval-grid">
      <div>
        <span className="fact-label">Security check</span>
        <span className="fact-value"><span className={`pill pill-${tone}`}>{word}</span></span>
      </div>
      <div>
        <span className="fact-label">Valid until</span>
        <span className="fact-value">{until && until !== "None" ? <Time iso={until} short /> : <span className="muted">Not set</span>}</span>
      </div>
    </div>
  );
}

/** The backend text starts with a header that repeats what the page already shows; keep the body only. */
export function stripHeader(md: string): string {
  const lines = md.replace(/\r\n/g, "\n").split("\n");
  const first = lines.findIndex((l, i) => i > 0 && /^##\s/.test(l));
  const header = (l: string) => /^(# |Run `|Data: |Investigated by )/.test(l.trim());
  const body = first > 0 ? lines.slice(first) : lines.filter((l) => !header(l));
  return body.filter((l) => !/^\d+ item\(s\) withheld/.test(l.trim())).join("\n");
}

function ItemsTable({ items, runId, kind, claims }: { items: ReportItem[]; runId: string; kind: string; claims: Claim[] }) {
  const verdictOf = (it: ReportItem) => claims.find((c) => (it.claim_ids ?? []).includes(c.id))?.verdict;
  const byComponent = new Map<string, number>();
  for (const it of items) byComponent.set(it.component ?? "No component", (byComponent.get(it.component ?? "No component") ?? 0) + 1);
  return (
    <>
      {kind === "architecture" ? (
        <div className="chip-row" aria-label="Items per component">
          {[...byComponent.entries()].sort((x, y) => y[1] - x[1]).map(([c, n]) => (
            <span className="chip" key={c}>{c} <span className="num">{n}</span></span>
          ))}
        </div>
      ) : null}
      <div className="table-wrap">
        <table className="table-items">
          <thead>
            <tr>
              <th>Issue</th>
              <th>Component</th>
              {kind === "allocation" ? <th>Proposed owner</th> : null}
              <th className="num"><Tooltip text="Higher means more likely to carry over">Risk score</Tooltip></th>
              <th>Check</th>
              <th>Evidence</th>
            </tr>
          </thead>
          <tbody>
            {items.map((it, i) => (
              <tr key={it.id ?? i}>
                <td>
                  <span className="row-title"><Truncate text={it.title ?? "Untitled item"} max={84} /></span>
                  {it.issue_id ? <span className="row-sub num">#{it.issue_id}</span> : null}
                </td>
                <td>
                  {it.component ?? <span className="muted">—</span>}
                  {kind === "allocation" && it.owner_team ? <span className="row-sub">Owner team {it.owner_team}</span> : null}
                </td>
                {kind === "allocation" ? (
                  <td>
                    {ownerName(it.allocation) ? (
                      <span className={`pill pill-${ownerTone(it.allocation)}`} title={it.allocation_reason ?? undefined}>{ownerName(it.allocation)}</span>
                    ) : (
                      <span className="pill pill-neutral">No proposal</span>
                    )}
                    {it.allocation_reason ? <span className="row-sub" data-testid="allocation-reason">{it.allocation_reason}</span> : null}
                  </td>
                ) : null}
                <td className="num">{typeof it.score === "number" ? it.score.toFixed(2) : "—"}</td>
                <td><VerdictChip verdict={verdictOf(it)} /></td>
                <td>
                  {kind === "forecast" ? (
                    <HandOff subjectType="forecast_item" subjectId={String(it.issue_id ?? it.id ?? i)} title={it.title ?? "Forecast item"} runId={runId} reportId={`${runId}:forecast`} />
                  ) : null}
                  {(it.evidence ?? []).slice(0, 3).map((id, n) => (
                    <Link key={id} className="chip cite" title="Open the cited record" to={`/evidence/${encodeURIComponent(id)}?run=${encodeURIComponent(runId)}`}>
                      Record {n + 1}
                    </Link>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

export function EvidenceView({ recordId, runId }: { recordId: string; runId: string }) {
  const data = useApi<{ record: EvidenceRecord; data_mode?: string }>(`/api/evidence/${encodeURIComponent(recordId)}`, { run_id: runId });
  if (data.isLoading) return <Loading what="the evidence" />;
  if (data.error) return <ErrorView error={data.error} />;
  const rec = data.data?.record;
  if (!rec) return null;
  return <EvidenceCard rec={rec} mode={data.data?.data_mode} runId={runId} />;
}

const SOURCE_NAME: Record<string, string> = {
  issues: "Issue tracker",
  git: "Git",
  ci: "CI",
  security: "Security scan",
  architecture: "Architecture",
  kubernetes: "Cluster",
  telemetry: "Telemetry",
  capacity: "Capacity",
};

/** One cited record: what it is, where it came from and its text. Technical fields stay under Details. */
export function EvidenceCard({ rec, mode, runId }: { rec: EvidenceRecord; mode?: string; runId?: string }) {
  const { content, text, ...meta } = rec;
  const m = meta as Record<string, unknown>;
  const inner = content && typeof content === "object" ? (content as Record<string, unknown>) : {};
  const title = [inner.title, m.title].find((v) => typeof v === "string") as string | undefined;
  const bodyText = [text, inner.body, inner.text, typeof content === "string" ? content : null].find((v) => typeof v === "string") as string | undefined;
  const body = bodyText ?? JSON.stringify(content ?? {}, null, 2);
  const source = String(m.source ?? "");
  const url = [m.url, inner.url, inner.html_url].find((v) => typeof v === "string") as string | undefined;
  const labels = Array.isArray(inner.labels) ? (inner.labels as unknown[]).map(String).slice(0, 8) : [];
  return (
    <Section title={title ?? "Cited record"} icon="database" mode={mode}>
      <div className="chip-row">
        <span className="chip">{SOURCE_NAME[source] ?? (source || "Record")}</span>
        {typeof inner.state === "string" ? <span className="chip">{String(inner.state).toLowerCase()}</span> : null}
        {labels.map((l) => <span className="chip chip-quiet" key={l}>{l}</span>)}
        {url ? <ExternalLink href={url} label="Open source" /> : null}
        {runId ? <Link className="chip" to={`/runs/${encodeURIComponent(runId)}`}>Run</Link> : null}
      </div>
      <dl className="facts">
        {typeof m.valid_from === "string" ? (<><dt>True from</dt><dd><Time iso={m.valid_from} /></dd></>) : null}
        {typeof m.observed_at === "string" ? (<><dt>Collected</dt><dd><Time iso={m.observed_at} /></dd></>) : null}
        {typeof m.provenance === "string" ? (<><dt>Origin</dt><dd>{m.provenance === "observed" ? "Collected from the source" : m.provenance === "synthetic" ? "Sample data" : "Calculated"}</dd></>) : null}
      </dl>
      <pre className="evidence">{body}</pre>
      <IdDetails
        items={Object.entries(m)
          .filter(([, v]) => typeof v === "string" || typeof v === "number")
          .map(([k, v]) => [k.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()), String(v)] as [string, string])}
      />
    </Section>
  );
}
