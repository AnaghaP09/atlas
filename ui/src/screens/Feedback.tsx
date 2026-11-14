import { useState } from "react";
import type { Envelope, ReportView, RunView } from "../api/types";
import { useAction, useApi } from "../app/context";
import { ActionResult, Empty, ErrorView, Loading, Section, Segmented } from "../components/common";
import { Icon } from "../components/icons";
import { runTitle } from "../lib/labels";
import { useRouter } from "../lib/router";

type Kind = "override" | "debt_acceptance";

interface Item {
  issue_id?: string | number;
  title?: string;
}

interface Body {
  run_id: string;
  item_id: string;
  kind: Kind;
  decision: string;
  rationale: string;
}

export function Feedback() {
  const { search } = useRouter();
  const runs = useApi<{ runs: RunView[] } & Envelope>("/api/runs");
  const [runId, setRunId] = useState(search.get("run") ?? "");
  const [itemId, setItemId] = useState(search.get("item") ?? "");
  const [kind, setKind] = useState<Kind>("override");
  const [why, setWhy] = useState("");
  const list = (runs.data?.runs ?? []).filter((r) => r.reports.length > 0);
  const chosen = list.find((r) => r.run_id === runId);
  const reportId = chosen ? (chosen.reports.find((r) => r.report_type === "forecast") ?? chosen.reports[0])?.report_id : undefined;
  const report = useApi<ReportView>(reportId ? `/api/reports/${encodeURIComponent(reportId)}` : null);
  const items: Item[] = Array.isArray(report.data?.report.items) ? (report.data?.report.items as Item[]) : [];
  const send = useAction<Body, unknown>("/api/feedback");
  if (runs.isLoading) return <Loading what="runs" />;
  if (runs.error) return <ErrorView error={runs.error} />;
  if (list.length === 0) return <Empty action={{ to: "/run-now", label: "Start a run" }}>There is no report to decide on yet.</Empty>;
  return (
    <Section title="Your decision" icon="pen">
      <form
        className="form-narrow"
        onSubmit={(e) => {
          e.preventDefault();
          void send
            .run({ run_id: runId, item_id: itemId, kind, decision: kind === "override" ? "overruled" : "accepted", rationale: why })
            .then(() => setWhy(""));
        }}
      >
        <label>
          Run
          <select value={runId} onChange={(e) => { setRunId(e.target.value); setItemId(""); }}>
            <option value="">Choose…</option>
            {list.map((r) => <option key={r.run_id} value={r.run_id}>{runTitle(r)}</option>)}
          </select>
        </label>
        {items.length ? (
          <label>
            Item
            <select value={itemId} onChange={(e) => setItemId(e.target.value)}>
              <option value="">The whole report</option>
              {items.map((it) => (
                <option key={String(it.issue_id)} value={String(it.issue_id)}>#{it.issue_id} {String(it.title ?? "").slice(0, 80)}</option>
              ))}
            </select>
          </label>
        ) : null}
        <Segmented<Kind>
          label="Decision"
          value={kind}
          onChange={setKind}
          options={[
            { value: "override", label: "I overrule ATLAS" },
            { value: "debt_acceptance", label: "We accept this debt" },
          ]}
        />
        <label>
          Why
          <textarea rows={4} value={why} placeholder="One or two sentences" onChange={(e) => setWhy(e.target.value)} />
        </label>
        <div className="actions-row">
          <button type="submit" className="btn btn-primary btn-md" disabled={!runId || !why.trim() || send.state.status === "running"}>
            {send.state.status === "running" ? <span className="spinner" aria-hidden="true" /> : <Icon name="ok" size={16} />}
            Record decision
          </button>
        </div>
      </form>
      <ActionResult state={send.state} retry={() => void send.retrySame()} success="Decision recorded" />
    </Section>
  );
}
