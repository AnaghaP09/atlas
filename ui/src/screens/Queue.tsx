import type { QueueEntry } from "../api/types";
import { useApi } from "../app/context";
import { ErrorView, Loading, Section, Time } from "../components/common";
import { Link } from "../lib/router";

export function Queue() {
  const data = useApi<{ entries: QueueEntry[]; data_mode?: string }>("/api/queue");
  return (
    <Section title="Needs a person" mode={data.data?.data_mode}>
      <p>ATLAS stops and asks a person here: low confidence, a blocking security finding, an injection attempt, any code change, and similar cases.</p>
      {data.isLoading ? <Loading what="the queue" /> : <ErrorView error={data.error} />}
      {data.data && data.data.entries.length === 0 ? <p>Nothing is waiting.</p> : null}
      <ul>
        {(data.data?.entries ?? []).map((e, i) => (
          <li key={String(e.entry_id ?? i)}>
            <strong>{String(e.trigger ?? "review")}</strong> — {String(e.reason ?? "")} <Time iso={e.created_at ?? null} />{" "}
            {e.run_id ? <Link to={`/runs/${encodeURIComponent(e.run_id)}`}>open run</Link> : null}
          </li>
        ))}
      </ul>
    </Section>
  );
}
