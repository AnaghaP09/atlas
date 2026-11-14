import { useEffect, useMemo, useRef, useState } from "react";
import type { Envelope } from "../api/types";
import { useApi } from "../app/context";
import { Button, Empty, ErrorView, Loading, Pagination, Section, Segmented, Tabs, usePaged } from "../components/common";
import type { SnapshotItem } from "./Home";

// ---------- Data ----------

interface RawNode {
  id: string;
  type?: string;
  kind?: string;
  label?: string;
  owner_team?: string | null;
  labels?: string[];
}
interface RawEdge {
  from?: string;
  to?: string;
  source?: string;
  target?: string;
  kind: string;
}
interface RawGraph extends Envelope {
  snapshot_id: string;
  nodes: RawNode[];
  edges: RawEdge[];
  limitation?: string;
}

export interface GNode {
  id: string;
  kind: string;
  name: string;
  team: string | null;
  labels: string[];
}
export interface GEdge {
  source: string;
  target: string;
  kind: string;
}

/** The API names fields "type"/"from"/"to"; older payloads used "kind"/"source"/"target". Accept both. */
export function normalise(g: RawGraph): { nodes: GNode[]; edges: GEdge[] } {
  const nodes = g.nodes.map((n) => {
    const kind = n.type ?? n.kind ?? n.id.split(":")[0] ?? "node";
    const name = n.label ?? (n.id.includes(":") ? n.id.slice(n.id.indexOf(":") + 1) : n.id);
    return { id: n.id, kind, name, team: n.owner_team ?? null, labels: n.labels ?? [] };
  });
  const edges = g.edges
    .map((e) => ({ source: e.source ?? e.from ?? "", target: e.target ?? e.to ?? "", kind: e.kind }))
    .filter((e) => e.source && e.target);
  return { nodes, edges };
}

// ---------- Layout ----------

export interface Placed {
  id: string;
  x: number;
  y: number;
  depth: number;
}

const COL_W = 220;
const ROW_H = 22;

/** Dependency depth: 0 for components that import no other component; otherwise one more than the deepest import. Cycles are cut. */
export function depths(ids: string[], deps: Map<string, string[]>): Map<string, number> {
  const out = new Map<string, number>();
  const visiting = new Set<string>();
  const visit = (id: string): number => {
    const known = out.get(id);
    if (known !== undefined) return known;
    if (visiting.has(id)) return 0;
    visiting.add(id);
    let d = 0;
    for (const t of deps.get(id) ?? []) d = Math.max(d, visit(t) + 1);
    visiting.delete(id);
    out.set(id, d);
    return d;
  };
  for (const id of ids) visit(id);
  return out;
}

/** Layered layout: one column per depth, rows ordered by team then by how many components import it. */
export function layered(nodes: GNode[], edges: GEdge[]): { placed: Map<string, Placed>; width: number; height: number; columns: number } {
  const comps = nodes.filter((n) => n.kind === "component");
  const ids = new Set(comps.map((c) => c.id));
  const deps = new Map<string, string[]>();
  const fanIn = new Map<string, number>();
  for (const e of edges) {
    if (e.kind !== "depends_on" || !ids.has(e.source) || !ids.has(e.target)) continue;
    deps.set(e.source, [...(deps.get(e.source) ?? []), e.target]);
    fanIn.set(e.target, (fanIn.get(e.target) ?? 0) + 1);
  }
  const depth = depths([...ids], deps);
  const cols = new Map<number, GNode[]>();
  for (const c of comps) {
    const d = depth.get(c.id) ?? 0;
    cols.set(d, [...(cols.get(d) ?? []), c]);
  }
  const placed = new Map<string, Placed>();
  let maxRows = 0;
  const colKeys = [...cols.keys()].sort((a, b) => a - b);
  for (const d of colKeys) {
    const list = (cols.get(d) ?? []).sort(
      (a, b) => (a.team ?? "~").localeCompare(b.team ?? "~") || (fanIn.get(b.id) ?? 0) - (fanIn.get(a.id) ?? 0) || a.name.localeCompare(b.name),
    );
    maxRows = Math.max(maxRows, list.length);
    list.forEach((n, i) => placed.set(n.id, { id: n.id, x: 80 + d * COL_W, y: 60 + i * ROW_H, depth: d }));
  }
  return { placed, width: 80 + Math.max(colKeys.length, 1) * COL_W, height: 60 + maxRows * ROW_H + 40, columns: colKeys.length };
}

// ---------- Team colours: three named teams, then "other", then "no owner" (secondary encoding: outline) ----------

export function teamSlots(nodes: GNode[]): { slot: Map<string, string>; top: { team: string; count: number }[] } {
  const counts = new Map<string, number>();
  for (const n of nodes) if (n.kind === "component" && n.team) counts.set(n.team, (counts.get(n.team) ?? 0) + 1);
  const top = [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, 3)
    .map(([team, count]) => ({ team, count }));
  const slot = new Map<string, string>();
  top.forEach((t, i) => slot.set(t.team, `t${i}`));
  return { slot, top };
}

function nodeClass(n: GNode, slot: Map<string, string>): string {
  if (!n.team) return "node-none";
  return `node-${slot.get(n.team) ?? "other"}`;
}

// ---------- View ----------

interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

type LinkMode = "key" | "all" | "none";

function GraphCanvas({ nodes, edges, limitation }: { nodes: GNode[]; edges: GEdge[]; limitation?: string }) {
  const [layout, setLayout] = useState<ReturnType<typeof layered> | null>(null);
  const [box, setBox] = useState<Box | null>(null);
  // The layout runs after the first paint so the page never freezes on a large graph.
  useEffect(() => {
    let cancelled = false;
    const t = window.setTimeout(() => {
      if (cancelled) return;
      const l = layered(nodes, edges);
      setLayout(l);
      setBox({ x: 0, y: 0, w: l.width, h: Math.max(l.height, 400) });
    }, 0);
    return () => {
      cancelled = true;
      window.clearTimeout(t);
    };
  }, [nodes, edges]);

  const byId = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);
  const { slot, top } = useMemo(() => teamSlots(nodes), [nodes]);
  const compEdges = useMemo(() => edges.filter((e) => e.kind !== "owned_by_team" && byId.has(e.source) && byId.has(e.target)), [edges, byId]);
  const fanIn = useMemo(() => {
    const m = new Map<string, number>();
    for (const e of compEdges) if (e.kind === "depends_on") m.set(e.target, (m.get(e.target) ?? 0) + 1);
    return m;
  }, [compEdges]);
  const hubs = useMemo(
    () => new Set([...fanIn.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12).map(([id]) => id)),
    [fanIn],
  );

  const [selected, setSelected] = useState<string | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [links, setLinks] = useState<LinkMode>("key");
  const [showTest, setShowTest] = useState(false);
  const [query, setQuery] = useState("");
  const svgRef = useRef<SVGSVGElement>(null);
  const drag = useRef<{ x: number; y: number; box: Box } | null>(null);

  const fit = (l = layout) => (l ? setBox({ x: 0, y: 0, w: l.width, h: Math.max(l.height, 400) }) : undefined);

  // Wheel zoom needs a non-passive listener so the page does not scroll at the same time.
  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const onWheel = (ev: WheelEvent) => {
      ev.preventDefault();
      setBox((b) => {
        if (!b) return b;
        const rect = el.getBoundingClientRect();
        const px = b.x + ((ev.clientX - rect.left) / rect.width) * b.w;
        const py = b.y + ((ev.clientY - rect.top) / rect.height) * b.h;
        const k = ev.deltaY > 0 ? 1.15 : 1 / 1.15;
        const w = Math.min(Math.max(b.w * k, 120), 20000);
        const h = (b.h / b.w) * w;
        return { x: px - ((px - b.x) / b.w) * w, y: py - ((py - b.y) / b.h) * h, w, h };
      });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [layout]);

  const zoom = (k: number) =>
    setBox((b) => (b ? { x: b.x + (b.w - b.w * k) / 2, y: b.y + (b.h - b.h * k) / 2, w: b.w * k, h: b.h * k } : b));

  const focusOn = (id: string) => {
    const p = layout?.placed.get(id);
    setSelected(id);
    if (p) setBox((b) => ({ x: p.x - 360, y: p.y - 220, w: 720, h: (720 * (b?.h ?? 1)) / (b?.w ?? 1) }));
  };

  if (!layout || !box) return <Loading what="the graph layout" lines={4} />;

  const focus = hovered ?? selected;
  const neighbours = new Set<string>();
  if (focus) for (const e of compEdges) if (e.source === focus) neighbours.add(e.target); else if (e.target === focus) neighbours.add(e.source);
  const shownEdges = compEdges.filter((e) => {
    if (e.kind === "test_depends_on" && !showTest) return false;
    if (focus) return e.source === focus || e.target === focus;
    if (links === "none") return false;
    if (links === "key") return hubs.has(e.target);
    return true;
  });
  const zoomedIn = box.w < 1100;
  const matches = query.trim()
    ? nodes.filter((n) => n.kind === "component" && n.name.toLowerCase().includes(query.trim().toLowerCase())).slice(0, 8)
    : [];
  const detail = focus ? byId.get(focus) : null;
  const detailDeps = detail ? compEdges.filter((e) => e.source === detail.id && e.kind === "depends_on").map((e) => byId.get(e.target)?.name ?? e.target) : [];
  const detailUsers = detail ? compEdges.filter((e) => e.target === detail.id && e.kind === "depends_on").map((e) => byId.get(e.source)?.name ?? e.source) : [];

  return (
    <>
      <div className="graph-bar">
        <label className="sr-only" htmlFor="graph-search">Find a component</label>
        <input
          id="graph-search"
          placeholder="Find a component, for example pkg/virt-handler"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && matches[0]) focusOn(matches[0].id);
          }}
        />
        <Segmented<LinkMode>
          label="Links shown"
          value={links}
          onChange={setLinks}
          options={[
            { value: "key", label: "Key links" },
            { value: "all", label: "All links" },
            { value: "none", label: "No links" },
          ]}
        />
        <label className="check">
          <input type="checkbox" checked={showTest} onChange={(e) => setShowTest(e.target.checked)} /> Test imports
        </label>
        <span className="zoom-controls" role="group" aria-label="Zoom">
          <Button size="sm" onClick={() => zoom(1 / 1.3)} aria-label="Zoom in">+</Button>
          <Button size="sm" onClick={() => zoom(1.3)} aria-label="Zoom out">−</Button>
          <Button size="sm" onClick={() => fit()}>Fit</Button>
        </span>
      </div>
      {matches.length ? (
        <p className="graph-matches">
          {matches.map((m) => (
            <button key={m.id} type="button" className="chip" onClick={() => focusOn(m.id)}>{m.name}</button>
          ))}
        </p>
      ) : null}
      <div className="graph-wrap">
        <svg
          ref={svgRef}
          className="graph"
          viewBox={`${box.x} ${box.y} ${box.w} ${box.h}`}
          preserveAspectRatio="xMidYMin meet"
          role="img"
          aria-label={`Dependency graph of ${layout.placed.size} components in ${layout.columns} layers`}
          onPointerDown={(e) => {
            (e.target as Element).setPointerCapture?.(e.pointerId);
            drag.current = { x: e.clientX, y: e.clientY, box };
          }}
          onPointerMove={(e) => {
            const d = drag.current;
            const el = svgRef.current;
            if (!d || !el) return;
            const rect = el.getBoundingClientRect();
            const sx = d.box.w / rect.width;
            const sy = d.box.h / rect.height;
            setBox({ ...d.box, x: d.box.x - (e.clientX - d.x) * sx, y: d.box.y - (e.clientY - d.y) * sy });
          }}
          onPointerUp={() => (drag.current = null)}
          onPointerLeave={() => (drag.current = null)}
        >
          {Array.from({ length: layout.columns }, (_, d) => (
            <text key={d} className="col-label" x={80 + d * COL_W - 10} y={32}>
              {d === 0 ? "Foundation" : `Layer ${d}`}
            </text>
          ))}
          {shownEdges.map((e, i) => {
            const a = layout.placed.get(e.source);
            const b = layout.placed.get(e.target);
            if (!a || !b) return null;
            const mx = (a.x + b.x) / 2;
            return (
              <path
                key={i}
                d={`M${a.x},${a.y} C${mx},${a.y} ${mx},${b.y} ${b.x},${b.y}`}
                className={`edge ${e.kind}${focus ? " hl" : ""}`}
                fill="none"
              />
            );
          })}
          {[...layout.placed.values()].map((p) => {
            const n = byId.get(p.id)!;
            const dim = focus && p.id !== focus && !neighbours.has(p.id);
            const showLabel = zoomedIn || p.id === focus || neighbours.has(p.id) || hubs.has(p.id);
            return (
              <g key={p.id}>
                <circle
                  cx={p.x}
                  cy={p.y}
                  r={hubs.has(p.id) ? 7 : 5}
                  className={`node ${nodeClass(n, slot)}${p.id === selected ? " sel" : ""}${dim ? " dim" : ""}`}
                  tabIndex={0}
                  onMouseEnter={() => setHovered(p.id)}
                  onMouseLeave={() => setHovered(null)}
                  onFocus={() => setHovered(p.id)}
                  onBlur={() => setHovered(null)}
                  onClick={() => setSelected(p.id === selected ? null : p.id)}
                  onPointerDown={(e) => e.stopPropagation()}
                >
                  <title>{`${n.name}${n.team ? ` · ${n.team}` : " · no owner team"} · used by ${fanIn.get(p.id) ?? 0}`}</title>
                </circle>
                {showLabel && !dim ? (
                  <text className="node-label" x={p.x + 10} y={p.y + 3}>
                    {n.name.length > 30 ? `…${n.name.slice(-29)}` : n.name}
                  </text>
                ) : null}
              </g>
            );
          })}
        </svg>
        <aside className="node-detail" aria-live="polite">
          {detail ? (
            <>
              <h3>{detail.name}</h3>
              <dl className="facts">
                <dt>Owner team</dt><dd>{detail.team ?? "none recorded"}</dd>
                <dt>Used by</dt><dd className="num">{detailUsers.length} components</dd>
                <dt>Imports</dt><dd className="num">{detailDeps.length} components</dd>
              </dl>
              {detailDeps.length ? (<><p className="muted">Imports</p><ul>{detailDeps.slice(0, 30).map((d) => <li key={d}>{d}</li>)}</ul></>) : null}
              {detailUsers.length ? (<><p className="muted">Used by</p><ul>{detailUsers.slice(0, 30).map((d) => <li key={d}>{d}</li>)}</ul></>) : null}
              {selected ? <Button size="sm" variant="ghost" onClick={() => setSelected(null)}>Clear selection</Button> : null}
            </>
          ) : (
            <>
              <h3>How to read this</h3>
              <p className="muted">
                Columns are dependency layers: foundation packages on the left, the components that import them to the right. Hover
                or click a dot to see what it imports and what uses it. Drag to pan, scroll or use + and − to zoom.
              </p>
            </>
          )}
          <ul className="legend" aria-label="Owner teams">
            {top.map((t, i) => (
              <li key={t.team}><span className={`swatch s${i + 1}`} aria-hidden="true" /> {t.team} <span className="muted num">{t.count}</span></li>
            ))}
            <li><span className="swatch other" aria-hidden="true" /> Other teams</li>
            <li><span className="swatch none" aria-hidden="true" /> No owner team</li>
          </ul>
          {limitation ? <p className="graph-note">{limitation}</p> : null}
        </aside>
      </div>
    </>
  );
}

function ComponentTable({ nodes, edges }: { nodes: GNode[]; edges: GEdge[] }) {
  const rows = useMemo(() => {
    const fanIn = new Map<string, number>();
    const fanOut = new Map<string, number>();
    for (const e of edges)
      if (e.kind === "depends_on") {
        fanIn.set(e.target, (fanIn.get(e.target) ?? 0) + 1);
        fanOut.set(e.source, (fanOut.get(e.source) ?? 0) + 1);
      }
    return nodes
      .filter((n) => n.kind === "component")
      .map((n) => ({ ...n, fanIn: fanIn.get(n.id) ?? 0, fanOut: fanOut.get(n.id) ?? 0 }))
      .sort((a, b) => b.fanIn - a.fanIn || a.name.localeCompare(b.name));
  }, [nodes, edges]);
  const paged = usePaged(rows, 25);
  return (
    <>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Component</th><th>Owner team</th><th className="num">Used by</th><th className="num">Imports</th></tr></thead>
          <tbody>
            {paged.slice.map((r) => (
              <tr key={r.id}>
                <td className="mono">{r.name}</td>
                <td>{r.team ?? <span className="muted">none</span>}</td>
                <td className="num">{r.fanIn}</td>
                <td className="num">{r.fanOut}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Pagination page={paged.page} pages={paged.pages} setPage={paged.setPage} total={rows.length} />
    </>
  );
}

function IssueTable({ nodes }: { nodes: GNode[] }) {
  const issues = nodes.filter((n) => n.kind === "issue");
  const paged = usePaged(issues, 25);
  if (!issues.length) return <Empty>This snapshot has no open issues.</Empty>;
  return (
    <>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Issue</th><th>Labels</th></tr></thead>
          <tbody>
            {paged.slice.map((n) => (
              <tr key={n.id}>
                <td className="mono">#{n.name}</td>
                <td>{n.labels.map((l) => <span key={l} className="chip">{l}</span>)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Pagination page={paged.page} pages={paged.pages} setPage={paged.setPage} total={issues.length} />
    </>
  );
}

type View = "graph" | "components" | "issues";

function GraphPicture({ snapshotId }: { snapshotId: string }) {
  const g = useApi<RawGraph>(`/api/graph/${encodeURIComponent(snapshotId)}`);
  const [view, setView] = useState<View>("graph");
  const data = useMemo(() => (g.data ? normalise(g.data) : null), [g.data]);
  if (g.isLoading) return <Loading what="the graph" lines={4} />;
  if (g.error) return <ErrorView error={g.error} />;
  if (!data) return null;
  const comps = data.nodes.filter((n) => n.kind === "component").length;
  const issues = data.nodes.filter((n) => n.kind === "issue").length;
  if (comps === 0)
    return <Empty>This snapshot has no component graph. Pick a release close snapshot, for example release-close-v1.9.0.</Empty>;
  return (
    <>
      <Tabs<View>
        label="Graph views"
        value={view}
        onChange={setView}
        tabs={[
          { value: "graph", label: "Graph" },
          { value: "components", label: "Components", count: comps },
          { value: "issues", label: "Open issues", count: issues },
        ]}
      />
      {view === "graph" ? <GraphCanvas nodes={data.nodes} edges={data.edges} limitation={g.data?.limitation} /> : null}
      {view === "components" ? <ComponentTable nodes={data.nodes} edges={data.edges} /> : null}
      {view === "issues" ? <IssueTable nodes={data.nodes} /> : null}
    </>
  );
}

export function Graph() {
  const snaps = useApi<{ snapshots: SnapshotItem[]; data_mode?: string }>("/api/snapshots");
  const list = snaps.data?.snapshots ?? [];
  const firstRelease = list.filter((s) => s.kind === "releases").map((s) => s.snapshot_id).sort().pop() ?? "";
  const [chosen, setChosen] = useState<string | null>(null);
  const selected = chosen ?? firstRelease;
  return (
    <Section
      title="Dependency graph"
      icon="graph"
      mode={snaps.data?.data_mode}
      end={
        <label className="top-field">
          Snapshot
          <select value={selected} onChange={(e) => setChosen(e.target.value)} aria-label="Snapshot">
            <option value="">Choose a snapshot</option>
            {list.map((s) => (
              <option key={s.snapshot_id} value={s.snapshot_id}>{s.snapshot_id}</option>
            ))}
          </select>
        </label>
      }
    >
      {snaps.isLoading ? <Loading what="snapshots" /> : <ErrorView error={snaps.error} />}
      {snaps.data && list.length === 0 ? <Empty>No snapshots yet. Freeze one from live data on Run now.</Empty> : null}
      {selected ? <GraphPicture key={selected} snapshotId={selected} /> : null}
    </Section>
  );
}
