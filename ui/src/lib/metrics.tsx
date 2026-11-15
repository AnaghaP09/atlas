// One live metrics store per project. Every number in the shell and on the home pages reads from it.
// It refetches when the project stream says a number changed, after a stream reset, and every 30 s
// as a safety net (paused while the tab is hidden). A role or project switch starts a new store.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useApp, useLiveStream, useScopedKey } from "../app/context";
import { useCurrentProject } from "../screens/Dashboard";
import { onOutlookEvent } from "./outlook";

export interface Metrics {
  project_id: string;
  metrics_version: number;
  computed_at: string;
  open_forecast_items: number | null;
  runs_total: number | null;
  runs_by_status: Record<string, number> | null;
  needs_a_person: number | null;
  approvals_pending: number | null;
  kill_switches_tripped: number | null;
  kill_switches_total: number | null;
  unread_notifications: number | null;
  spend_used_usd: number | null;
  spend_ceiling_usd: number | null;
  last_poll: string | null;
  dashboard_version: number | null;
  dashboard_counts: Record<string, number>;
}

export const SAFETY_REFETCH_MS = 30_000;

function metricsPath(project: string | null): string | null {
  return project ? `/api/projects/${encodeURIComponent(project)}/metrics` : null;
}

export function useMetrics() {
  const project = useCurrentProject();
  const { client } = useApp();
  const path = metricsPath(project);
  const key = useScopedKey(path, "metrics");
  return useQuery<Metrics, Error>({
    queryKey: key,
    queryFn: () => client.get<Metrics>(path as string),
    enabled: path !== null,
    refetchInterval: SAFETY_REFETCH_MS,
    refetchIntervalInBackground: false,
  });
}

/** Mounted once in the shell: listens on the project stream and refetches the metrics when told. */
export function MetricsSync() {
  const project = useCurrentProject();
  const qc = useQueryClient();
  const path = metricsPath(project);
  const key = useScopedKey(path, "metrics");
  const keyRef = useRef(key);
  useEffect(() => {
    keyRef.current = key;
  });
  useLiveStream(
    project ? `/api/projects/${encodeURIComponent(project)}/stream` : null,
    (e) => {
      if (e.event === "project.metrics_changed") void qc.invalidateQueries({ queryKey: keyRef.current });
      onOutlookEvent(qc, e);
    },
    () => {
      void qc.invalidateQueries({ queryKey: keyRef.current });
      onOutlookEvent(qc, null);
    },
  );
  return null;
}

/** A number that briefly highlights when it changes and shows a small up or down step. */
export function LiveNumber({ value, unit }: { value: number | null | undefined; unit?: string }) {
  const [prev, setPrev] = useState<number | null | undefined>(value);
  const [delta, setDelta] = useState<number | null>(null);
  if (value !== prev) {
    setPrev(value);
    if (typeof value === "number" && typeof prev === "number" && value !== prev) setDelta(value - prev);
  }
  useEffect(() => {
    if (delta === null) return;
    const t = window.setTimeout(() => setDelta(null), 2500);
    return () => window.clearTimeout(t);
  }, [delta]);
  if (value === null || value === undefined) return <span className="muted">—</span>;
  return (
    <span className={`live-num${delta !== null ? " flash" : ""}`}>
      {value.toLocaleString()}
      {unit ? <span className="unit"> {unit}</span> : null}
      {delta !== null ? <span className={`delta ${delta > 0 ? "up" : "down"}`}>{delta > 0 ? `▲${delta}` : `▼${Math.abs(delta)}`}</span> : null}
    </span>
  );
}
