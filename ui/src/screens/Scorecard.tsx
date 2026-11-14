import type { ScorecardRow } from "../api/types";
import { useApi } from "../app/context";
import { DataModeBadge, Empty, ErrorView, Loading, Section } from "../components/common";
import { Link } from "../lib/router";

function words(v: string): string {
  return v.replace(/[_-]/g, " ").replace(/^./, (c) => c.toUpperCase());
}

function signed(n: number): string {
  return `${n > 0 ? "+" : ""}${n}`;
}

function Net({ value }: { value: ScorecardRow["net_contribution"] }) {
  if (value === "unavailable" || value == null) return <span className="pill pill-neutral" title="There is no valid human baseline to compare with">Not measured</span>;
  const tone = value > 0 ? "good" : value < 0 ? "critical" : "neutral";
  return <span className={`pill pill-${tone} num`}>{signed(value)}</span>;
}

export function Scorecard() {
  const data = useApi<{ rows: ScorecardRow[]; data_mode?: string }>("/api/scorecard");
  const rows = data.data?.rows ?? [];
  return (
    <Section title="Did the coding agent help?" icon="chart" mode={data.data?.data_mode} end={rows.length ? <span className="muted num">{rows.length}</span> : null}>
      {data.isLoading ? <Loading what="the scorecard" /> : <ErrorView error={data.error} />}
      {data.data && rows.length === 0 ? (
        <Empty action={{ to: "/tasks", label: "Open tasks" }}>No coding task has finished yet, so there is nothing to score.</Empty>
      ) : null}
      {rows.length ? (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Kind of task</th><th>Net contribution</th><th>Range</th><th>Data</th><th /></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.task_class}>
                  <td><span className="row-title">{words(r.task_class)}</span></td>
                  <td><Net value={r.net_contribution} /></td>
                  <td className="num">{r.range ? `${signed(r.range[0])} to ${signed(r.range[1])}` : <span className="muted">—</span>}</td>
                  <td><DataModeBadge mode={r.data_mode} /></td>
                  <td className="row-actions always"><Link to="/tasks">Tasks</Link></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </Section>
  );
}
