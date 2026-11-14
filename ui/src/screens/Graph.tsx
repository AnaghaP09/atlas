import { useState } from "react";
import type { GraphView } from "../api/types";
import { useApi } from "../app/context";
import { ErrorView, Loading, Section } from "../components/common";

interface SnapshotItem {
  snapshot_id: string;
  release?: string;
  mode?: string;
}

/** Places nodes on a circle so the layout is the same every time. */
export function circleLayout(ids: string[], size = 520): Record<string, { x: number; y: number }> {
  const r = size / 2 - 40;
  const c = size / 2;
  const out: Record<string, { x: number; y: number }> = {};
  ids.forEach((id, i) => {
    const a = (2 * Math.PI * i) / Math.max(ids.length, 1);
    out[id] = { x: c + r * Math.cos(a), y: c + r * Math.sin(a) };
  });
  return out;
}

function GraphPicture({ snapshotId }: { snapshotId: string }) {
  const g = useApi<GraphView>(`/api/graph/${encodeURIComponent(snapshotId)}`);
  if (g.isLoading) return <Loading what="the graph" />;
  if (g.error) return <ErrorView error={g.error} />;
  const nodes = (g.data?.nodes ?? []).slice(0, 80);
  const pos = circleLayout(nodes.map((n) => n.id));
  const edges = (g.data?.edges ?? []).filter((e) => pos[e.source] && pos[e.target]);
  return (
    <>
      <p className="muted">
        {g.data?.nodes.length ?? 0} nodes and {g.data?.edges.length ?? 0} links in snapshot {snapshotId}
        {(g.data?.nodes.length ?? 0) > 80 ? " (showing the first 80)" : ""}.
      </p>
      <svg viewBox="0 0 520 520" role="img" aria-label="Knowledge graph" className="graph">
        {edges.map((e, i) => {
          const a = pos[e.source]!;
          const b = pos[e.target]!;
          return <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y} className={`edge edge-${e.kind}`} />;
        })}
        {nodes.map((n) => {
          const p = pos[n.id]!;
          return (
            <g key={n.id}>
              <circle cx={p.x} cy={p.y} r={6} className={`node node-${n.kind}`} />
              <title>{`${n.kind}: ${n.label ?? n.id}`}</title>
            </g>
          );
        })}
      </svg>
      <ul className="legend">{Array.from(new Set(nodes.map((n) => n.kind))).map((k) => <li key={k} className={`node-${k}`}>{k}</li>)}</ul>
    </>
  );
}

export function Graph() {
  const snaps = useApi<{ snapshots: SnapshotItem[]; data_mode?: string }>("/api/snapshots");
  const [selected, setSelected] = useState<string>("");
  return (
    <Section title="Knowledge graph" mode={snaps.data?.data_mode}>
      <p>Components, issues, commits and the links between them, for one frozen snapshot.</p>
      {snaps.isLoading ? <Loading what="snapshots" /> : <ErrorView error={snaps.error} />}
      <label>
        Snapshot
        <select value={selected} onChange={(e) => setSelected(e.target.value)}>
          <option value="">Choose a snapshot</option>
          {(snaps.data?.snapshots ?? []).map((s) => (
            <option key={s.snapshot_id} value={s.snapshot_id}>
              {s.snapshot_id}
              {s.release ? ` (${s.release})` : ""}
            </option>
          ))}
        </select>
      </label>
      {selected ? <GraphPicture snapshotId={selected} /> : null}
    </Section>
  );
}
