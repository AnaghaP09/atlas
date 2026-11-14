import type { Envelope, QueueEntry, RunView } from "../api/types";
import { useApi } from "../app/context";
import { Empty, ErrorView, Loading, Pagination, Section, Time, usePaged } from "../components/common";
import { EntityLink } from "../components/related";
import { runTitle } from "../lib/labels";
import { useRouter } from "../lib/router";

const REASON: Record<string, string> = {
  security_not_run: "Security check not run",
  blocking_security: "Blocking security finding",
  low_confidence: "Low confidence",
  injection_detected: "Hidden instruction found",
  insufficient_evidence_high_rank: "Evidence missing",
  verifier_rejections: "Claims rejected",
  code_change: "Code change to approve",
  withheld_payload: "Data withheld",
  budget_exceeded: "Limit reached",
  nightly_backlog: "Backlog too long",
  forecast_slip: "Forecast slipped",
  requirements_at_risk: "Requirement at risk",
  scorecard_negative: "Coding agent result negative",
  access_request: "Access request",
};

export function reasonName(code: string | null | undefined): string {
  const c = String(code ?? "review");
  return REASON[c] ?? c.replace(/_/g, " ").replace(/^./, (x) => x.toUpperCase());
}

function sentence(text: string): string {
  return text ? text[0]!.toUpperCase() + text.slice(1) : text;
}

/** Run titles by id, so lists can say what a run was instead of showing its id. */
export function useRunTitles(): Map<string, string> {
  const runs = useApi<{ runs: RunView[] } & Envelope>("/api/runs");
  return new Map((runs.data?.runs ?? []).map((r) => [r.run_id, runTitle(r)]));
}

export function Queue() {
  const data = useApi<{ entries: QueueEntry[]; data_mode?: string }>("/api/queue");
  const titles = useRunTitles();
  const { search } = useRouter();
  const focus = search.get("focus");
  const entries = (data.data?.entries ?? []).slice().sort((a, b) => String(b.created ?? b.created_at ?? "").localeCompare(String(a.created ?? a.created_at ?? "")));
  const paged = usePaged(entries, 15);
  return (
    <Section title="Waiting for a person" icon="hand" mode={data.data?.data_mode} end={entries.length ? <span className="muted num">{entries.length} open</span> : null}>
      {data.isLoading ? <Loading what="the queue" /> : <ErrorView error={data.error} />}
      {data.data && entries.length === 0 ? <Empty>Nothing is waiting.</Empty> : null}
      {entries.length ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Why</th><th>Detail</th><th>From</th><th>Raised</th></tr>
            </thead>
            <tbody>
              {paged.slice.map((e, i) => {
                const id = String(e.entry_id ?? e.id ?? i);
                const runId = typeof e.run_id === "string" ? e.run_id : null;
                return (
                  <tr key={id} className={focus && focus === id ? "row-focus" : undefined}>
                    <td><span className="pill pill-warn">{reasonName(e.trigger)}</span></td>
                    <td><span className="row-title" title={String(e.reason ?? "")}>{sentence(String(e.reason ?? ""))}</span></td>
                    <td>{runId ? <EntityLink entity="run" id={runId}>{titles.get(runId) ?? "Open the run"}</EntityLink> : <span className="muted">—</span>}</td>
                    <td className="muted"><Time iso={(e.created as string | undefined) ?? e.created_at ?? null} short /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}
      <Pagination page={paged.page} pages={paged.pages} setPage={paged.setPage} total={entries.length} />
    </Section>
  );
}
