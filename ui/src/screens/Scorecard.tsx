import type { ScorecardRow } from "../api/types";
import { useApi } from "../app/context";
import { DataModeBadge, ErrorView, Loading, Section } from "../components/common";

export function Scorecard() {
  const data = useApi<{ rows: ScorecardRow[]; data_mode?: string }>("/api/scorecard");
  return (
    <Section title="Did the coding agents help?" mode={data.data?.data_mode}>
      <p>
        Each task class is compared with matched human work. Results carry a range and can be negative. Without a valid human
        baseline the net contribution shows as unavailable.
      </p>
      {data.isLoading ? <Loading what="the scorecard" /> : <ErrorView error={data.error} />}
      {data.data && data.data.rows.length === 0 ? <p>No rows yet.</p> : null}
      <table>
        <thead><tr><th>Task class</th><th>Net contribution</th><th>Range</th><th>Data</th></tr></thead>
        <tbody>
          {(data.data?.rows ?? []).map((r) => (
            <tr key={r.task_class}>
              <td>{r.task_class}</td>
              <td>{r.net_contribution === "unavailable" || r.net_contribution == null ? "unavailable (no valid baseline)" : r.net_contribution}</td>
              <td>{r.range ? `${r.range[0]} to ${r.range[1]}` : "—"}</td>
              <td><DataModeBadge mode={r.data_mode} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  );
}
