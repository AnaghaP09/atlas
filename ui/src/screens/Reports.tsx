import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import type { LiveEvent } from "../api/stream";
import type { Claim, EvidenceRecord, ReportView } from "../api/types";
import { useApi, useLiveStream } from "../app/context";
import { AgentName, DataModeBadge, ErrorView, Loading, Section } from "../components/common";
import { VerdictChip } from "./Runs";
import { Related } from "../components/related";
import { ReadOnlyBanner, type AccessGrantView } from "../components/access";
import { useIdentity } from "../app/context";
import { Markdown } from "../lib/markdown";
import { Link } from "../lib/router";

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

  if (report.isLoading) return <Loading what="the report" />;
  if (report.error) return <ErrorView error={report.error} />;
  const r = report.data;
  if (!r) return null;
  const claims: Claim[] = Array.isArray(r.report.claims) ? r.report.claims : [];
  const isForecast = r.report_type.includes("forecast") || "forecast" in r.report;
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
          This is an earlier version (generation {r.generation}). Generation {r.superseded_by} replaced it. Kept for audit only.
        </p>
      ) : null}
      <p className="muted">
        Run <Link to={`/runs/${encodeURIComponent(r.run_id)}`}>{r.run_id}</Link> · version {r.report_version} · checked by{" "}
        <AgentName agent="sentinel" />
      </p>
      {r.withheld > 0 ? (
        <p className="withheld" role="note">
          {r.withheld} item(s) withheld because your role cannot see their sources.
        </p>
      ) : null}
      {r.corrections.length ? (
        <div className="banner">
          <strong>Corrections</strong>
          <ul>{r.corrections.map((c, i) => <li key={c.correction_id ?? i}>{c.text ?? "A correction was applied."}</li>)}</ul>
        </div>
      ) : null}
      {isForecast ? (
        <p className="banner" data-testid="proxy-note">
          <DataModeBadge mode="REAL" /> Probabilities are illustrative. {proxy ?? PROXY_NOTE}
        </p>
      ) : null}
      <div className="report">
        <Markdown source={r.markdown} />
      </div>
      {claims.length ? (
        <>
          <h3>Claims and their evidence</h3>
          <ul className="claims">
            {claims.map((c) => (
              <li key={c.id}>
                <VerdictChip verdict={c.verdict} /> {c.text}{" "}
                {(c.evidence_ids ?? []).map((id) => (
                  <Link key={id} className="chip cite" to={`/evidence/${encodeURIComponent(id)}?run=${encodeURIComponent(r.run_id)}`}>
                    {id}
                  </Link>
                ))}
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </Section>
  );
}

export function EvidenceView({ recordId, runId }: { recordId: string; runId: string }) {
  const data = useApi<{ record: EvidenceRecord; data_mode?: string }>(`/api/evidence/${encodeURIComponent(recordId)}`, { run_id: runId });
  if (data.isLoading) return <Loading what="the evidence" />;
  if (data.error) return <ErrorView error={data.error} />;
  const rec = data.data?.record;
  if (!rec) return null;
  const { content, text, ...meta } = rec;
  const body = typeof text === "string" ? text : typeof content === "string" ? content : JSON.stringify(content ?? {}, null, 2);
  return (
    <Section title={`Evidence ${recordId}`} mode={data.data?.data_mode}>
      <dl className="facts">
        {Object.entries(meta)
          .filter(([, v]) => typeof v !== "object")
          .map(([k, v]) => (
            <div key={k}>
              <dt>{k.replace(/_/g, " ")}</dt>
              <dd>{String(v)}</dd>
            </div>
          ))}
      </dl>
      <pre className="evidence">{body}</pre>
      <p className="muted">Shown as plain text. Links inside evidence are not opened.</p>
    </Section>
  );
}
