import type { QueueEntry } from "../api/types";
import { useApi } from "../app/context";
import { Empty, ErrorView, Loading, Section, Time } from "../components/common";
import { EntityLink } from "../components/related";

export function Queue() {
  const data = useApi<{ entries: QueueEntry[]; data_mode?: string }>("/api/queue");
  return (
    <Section title="Waiting for a person" icon="hand" mode={data.data?.data_mode}>
      {data.isLoading ? <Loading what="the queue" /> : <ErrorView error={data.error} />}
      {data.data && data.data.entries.length === 0 ? <Empty>Nothing is waiting.</Empty> : null}
      <ul className="list">
        {(data.data?.entries ?? []).map((e, i) => (
          <li key={String(e.entry_id ?? i)}>
            <span className="pill pill-warn">{String(e.trigger ?? "review").replace(/_/g, " ")}</span>
            <span className="grow">{String(e.reason ?? "")}</span>
            {e.run_id ? <EntityLink entity="run" id={e.run_id}>Run</EntityLink> : null}
            <Time iso={e.created_at ?? null} short />
          </li>
        ))}
      </ul>
    </Section>
  );
}
