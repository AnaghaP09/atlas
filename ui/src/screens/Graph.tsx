import { useEffect, useMemo, useRef, useState } from "react";
import type { Envelope } from "../api/types";
import { useApi } from "../app/context";
import { Button, Empty, ErrorView, Loading, Pagination, Section, Segmented, Tabs, usePaged } from "../components/common";
import type { SnapshotItem } from "./Home";
import { DatasetPicker, kindOf, visibleDatasets } from "../components/dataset";
import { useRouter } from "../lib/router";

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

export interface Layout {
  placed: Map<string, Placed>;
  width: number;
  height: number;
  /** Where the row of components with no links starts, if there are any. */
  looseTop: number | null;
}

const W = 1200;
const H = 760;

/** Starting positions: each team gets its own sector, so teams start (and mostly stay) together. Deterministic. */
function seedPositions(comps: GNode[]): Map<string, { x: number; y: number }> {
  const teams = [...new Set(comps.map((c) => c.team ?? "~none"))].sort();
  const out = new Map<string, { x: number; y: number }>();
  comps
    .slice()
    .sort((a, b) => (a.team ?? "~none").localeCompare(b.team ?? "~none") || a.name.localeCompare(b.name))
    .forEach((c, i) => {
      const t = teams.indexOf(c.team ?? "~none");
      const angle = (2 * Math.PI * (t + 0.5)) / teams.length + (i % 7) * 0.05;
      const radius = 120 + ((i * 37) % 220);
      out.set(c.id, { x: W / 2 + radius * Math.cos(angle), y: H / 2 + radius * Math.sin(angle) });
    });
  return out;
}

export interface ForceState {
  ids: string[];
  pos: Float64Array;
  links: [number, number][];
  team: Int32Array;
  temp: number;
  iter: number;
}

export function forceInit(nodes: GNode[], edges: GEdge[]): ForceState {
  const comps = nodes.filter((n) => n.kind === "component");
  const ids = comps.map((c) => c.id);
  const index = new Map(ids.map((id, i) => [id, i]));
  const seed = seedPositions(comps);
  const pos = new Float64Array(ids.length * 2);
  ids.forEach((id, i) => {
    const p = seed.get(id)!;
    pos[i * 2] = p.x;
    pos[i * 2 + 1] = p.y;
  });
  const seen = new Set<string>();
  const links: [number, number][] = [];
  for (const e of edges) {
    if (e.kind !== "depends_on") continue;
    const a = index.get(e.source);
    const b = index.get(e.target);
    if (a === undefined || b === undefined || a === b) continue;
    const key = a < b ? `${a}-${b}` : `${b}-${a}`;
    if (seen.has(key)) continue;
    seen.add(key);
    links.push([a, b]);
  }
  const teams = [...new Set(comps.map((c) => c.team ?? "~none"))].sort();
  const team = new Int32Array(ids.length);
  comps.forEach((c, i) => (team[i] = teams.indexOf(c.team ?? "~none")));
  return { ids, pos, links, team, temp: 60, iter: 0 };
}

/** Runs a batch of force iterations (repulsion, link springs, gravity, weak team cohesion). */
export function forceStep(st: ForceState, iterations: number) {
  const n = st.ids.length;
  if (n === 0) return;
  const k = Math.sqrt((W * H) / n) * 0.55;
  const disp = new Float64Array(n * 2);
  for (let it = 0; it < iterations; it++) {
    disp.fill(0);
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        let dx = st.pos[i * 2]! - st.pos[j * 2]!;
        let dy = st.pos[i * 2 + 1]! - st.pos[j * 2 + 1]!;
        let d2 = dx * dx + dy * dy;
        if (d2 < 0.01) {
          dx = 0.1 * ((i % 3) - 1) || 0.1;
          dy = 0.1;
          d2 = 0.02;
        }
        const f = (k * k) / d2;
        const same = st.team[i] === st.team[j] && st.team[i] !== -1 ? 0.7 : 1;
        disp[i * 2]! += dx * f * same;
        disp[i * 2 + 1]! += dy * f * same;
        disp[j * 2]! -= dx * f * same;
        disp[j * 2 + 1]! -= dy * f * same;
      }
    }
    for (const [a, b] of st.links) {
      const dx = st.pos[a * 2]! - st.pos[b * 2]!;
      const dy = st.pos[a * 2 + 1]! - st.pos[b * 2 + 1]!;
      const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const f = (d * d) / k / d / 4;
      disp[a * 2]! -= dx * f;
      disp[a * 2 + 1]! -= dy * f;
      disp[b * 2]! += dx * f;
      disp[b * 2 + 1]! += dy * f;
    }
    for (let i = 0; i < n; i++) {
      disp[i * 2]! += (W / 2 - st.pos[i * 2]!) * 0.02;
      disp[i * 2 + 1]! += (H / 2 - st.pos[i * 2 + 1]!) * 0.02;
      const dx = disp[i * 2]!;
      const dy = disp[i * 2 + 1]!;
      const len = Math.sqrt(dx * dx + dy * dy) || 1;
      const step = Math.min(len, st.temp);
      st.pos[i * 2]! += (dx / len) * step;
      st.pos[i * 2 + 1]! += (dy / len) * step;
    }
    st.temp = Math.max(1, st.temp * 0.97);
    st.iter += 1;
  }
}

/** Turns the force state into placed nodes. Linked components fill the canvas (scaled on the 3rd to 97th
 * percentile so a few stragglers cannot shrink the picture); components with no links sit in a row at the bottom. */
export function forceResult(st: ForceState): Layout {
  const n = st.ids.length;
  const degree = new Int32Array(n);
  for (const [a, b] of st.links) {
    degree[a]! += 1;
    degree[b]! += 1;
  }
  const linked: number[] = [];
  const loose: number[] = [];
  for (let i = 0; i < n; i++) (degree[i]! > 0 ? linked : loose).push(i);
  const q = (vals: number[], f: number) => {
    const sorted = vals.slice().sort((x, y) => x - y);
    return sorted[Math.min(sorted.length - 1, Math.max(0, Math.round(f * (sorted.length - 1))))] ?? 0;
  };
  const xs = linked.map((i) => st.pos[i * 2]!);
  const ys = linked.map((i) => st.pos[i * 2 + 1]!);
  const minX = q(xs, 0.03), maxX = q(xs, 0.97), minY = q(ys, 0.03), maxY = q(ys, 0.97);
  const margin = 70;
  const strip = loose.length ? 70 : 0;
  const innerW = W - margin * 2;
  const innerH = H - margin * 2 - strip;
  const placed = new Map<string, Placed>();
  const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
  for (const i of linked) {
    const x = margin + ((st.pos[i * 2]! - minX) / Math.max(maxX - minX, 1)) * innerW;
    const y = margin + ((st.pos[i * 2 + 1]! - minY) / Math.max(maxY - minY, 1)) * innerH;
    placed.set(st.ids[i]!, { id: st.ids[i]!, x: clamp(x, 24, W - 24), y: clamp(y, 24, H - strip - 24), depth: 0 });
  }
  loose.forEach((i, k) => {
    const per = Math.max(1, Math.floor((W - margin * 2) / 26));
    placed.set(st.ids[i]!, {
      id: st.ids[i]!,
      x: margin + (k % per) * 26,
      y: H - strip + 24 + Math.floor(k / per) * 22,
      depth: -1,
    });
  });
  return { placed, width: W, height: H + (loose.length > 0 ? Math.floor(loose.length / Math.max(1, Math.floor((W - 140) / 26))) * 22 : 0), looseTop: loose.length ? H - strip : null };
}

/** Runs the whole layout synchronously (used by tests). */
export function forceLayout(nodes: GNode[], edges: GEdge[], iterations = 220): Layout {
  const st = forceInit(nodes, edges);
  forceStep(st, iterations);
  return forceResult(st);
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

function GraphCanvas({ nodes, edges, limitation, initialFocus }: { nodes: GNode[]; edges: GEdge[]; limitation?: string; initialFocus?: string | null }) {
  const [layout, setLayout] = useState<Layout | null>(null);
  const [box, setBox] = useState<Box | null>(null);
  const [progress, setProgress] = useState(0);
  // The layout is computed in small chunks between frames, so the page stays responsive on large graphs.
  useEffect(() => {
    let cancelled = false;
    const total = 220;
    const st = forceInit(nodes, edges);
    let t = 0;
    const tick = () => {
      if (cancelled) return;
      forceStep(st, 10);
      if (st.iter < total) {
        setProgress(Math.round((st.iter / total) * 100));
        t = window.setTimeout(tick, 0);
        return;
      }
      const l = forceResult(st);
      setLayout(l);
      setBox({ x: 0, y: 0, w: l.width, h: l.height });
    };
    t = window.setTimeout(tick, 0);
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

  const [selected, setSelected] = useState<string | null>(initialFocus ?? null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [links, setLinks] = useState<LinkMode>("key");
  const [showTest, setShowTest] = useState(false);
  const [query, setQuery] = useState("");
  const svgRef = useRef<SVGSVGElement>(null);
  const drag = useRef<{ x: number; y: number; box: Box } | null>(null);

  const fit = (l = layout) => (l ? setBox({ x: 0, y: 0, w: l.width, h: l.height }) : undefined);

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

  if (!layout || !box)
    return (
      <div role="status" className="graph-progress">
        <span className="muted">Arranging {nodes.filter((n) => n.kind === "component").length} components… {progress}%</span>
        <progress className="bar" max={100} value={progress} />
      </div>
    );

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
  const zoomedIn = box.w < 700;
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
          aria-label={`Dependency graph of ${layout.placed.size} components`}
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
          {layout.looseTop !== null ? (
            <>
              <line className="strip-line" x1={40} x2={layout.width - 40} y1={layout.looseTop} y2={layout.looseTop} />
              <text className="col-label" x={70} y={layout.looseTop + 14}>No import links in this snapshot</text>
            </>
          ) : null}
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
              <h3 className="mono">{detail.name}</h3>
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
            <p className="muted">Hover or click a dot.</p>
          )}
          <ul className="legend" aria-label="Owner teams">
            {top.map((t, i) => (
              <li key={t.team}><span className={`swatch s${i + 1}`} aria-hidden="true" /> {t.team} <span className="muted num">{t.count}</span></li>
            ))}
            <li><span className="swatch other" aria-hidden="true" /> Other teams</li>
            <li><span className="swatch none" aria-hidden="true" /> No owner team</li>
          </ul>
          {limitation ? (
            <span className="pill pill-neutral" title={limitation}>
              Import links only
            </span>
          ) : null}
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

function GraphPicture({ snapshotId, focus }: { snapshotId: string; focus: string | null }) {
  const g = useApi<RawGraph>(`/api/graph/${encodeURIComponent(snapshotId)}`);
  const [view, setView] = useState<View>("graph");
  const data = useMemo(() => (g.data ? normalise(g.data) : null), [g.data]);
  if (g.isLoading) return <Loading what="the graph" lines={4} />;
  if (g.error) return <ErrorView error={g.error} />;
  if (!data) return null;
  const comps = data.nodes.filter((n) => n.kind === "component").length;
  const issues = data.nodes.filter((n) => n.kind === "issue").length;
  if (comps === 0)
    return <Empty>No component graph for this point in time. Pick a release.</Empty>;
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
      {view === "graph" ? <GraphCanvas nodes={data.nodes} edges={data.edges} limitation={g.data?.limitation} initialFocus={focus} /> : null}
      {view === "components" ? <ComponentTable nodes={data.nodes} edges={data.edges} /> : null}
      {view === "issues" ? <IssueTable nodes={data.nodes} /> : null}
    </>
  );
}

export function Graph() {
  const snaps = useApi<{ snapshots: SnapshotItem[]; data_mode?: string }>("/api/snapshots");
  const list = snaps.data?.snapshots ?? [];
  const { search } = useRouter();
  const firstRelease = visibleDatasets(list).filter((s) => kindOf(s) === "release").map((s) => s.snapshot_id).sort().pop() ?? "";
  const [chosen, setChosen] = useState<string | null>(search.get("snapshot"));
  const selected = chosen ?? firstRelease;
  return (
    <Section
      title="Dependency graph"
      icon="graph"
      mode={snaps.data?.data_mode}
      end={
        <span className="top-field"><DatasetPicker value={selected} onChange={setChosen} list={list} /></span>
      }
    >
      {snaps.isLoading ? <Loading what="snapshots" /> : <ErrorView error={snaps.error} />}
      {snaps.data && list.length === 0 ? <Empty>No data sets yet.</Empty> : null}
      {selected ? <GraphPicture key={selected} snapshotId={selected} focus={search.get("focus")} /> : null}
    </Section>
  );
}
