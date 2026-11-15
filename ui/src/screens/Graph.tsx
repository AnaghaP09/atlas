import { useEffect, useMemo, useRef, useState } from "react";
import type { Envelope } from "../api/types";
import { useApi } from "../app/context";
import { Button, Empty, ErrorView, Loading, Pagination, Section, Segmented, Tabs, usePaged } from "../components/common";
import { Icon } from "../components/icons";
import { Link } from "../lib/router";
import { useLatest } from "./RoleHome";
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
//
// Components are grouped by the top folder of their path (for example cmd, pkg, tests). Each group is
// one disc. Inside a disc the most-used components sit in the middle and are drawn larger, so the
// picture reads from the centre out. The layout is a plain calculation: the same data always gives
// the same picture, and there is nothing to wait for.

export interface Placed {
  id: string;
  x: number;
  y: number;
  r: number;
  group: string;
}

export interface GroupDisc {
  key: string;
  label: string;
  count: number;
  cx: number;
  cy: number;
  r: number;
}

export interface Layout {
  placed: Map<string, Placed>;
  groups: GroupDisc[];
  width: number;
  height: number;
}

const GROUP_NAME: Record<string, string> = {
  cmd: "Commands",
  pkg: "Packages",
  tests: "Tests",
  test: "Tests",
  staging: "Staging",
  tools: "Tools",
  api: "API",
  internal: "Internal",
  docs: "Docs",
};

export function groupOf(name: string): string {
  const first = name.split("/")[0] ?? name;
  return first || "other";
}

function groupLabel(key: string): string {
  return GROUP_NAME[key] ?? key.replace(/^./, (c) => c.toUpperCase());
}

export function fanInOf(edges: GEdge[]): Map<string, number> {
  const m = new Map<string, number>();
  for (const e of edges) if (e.kind === "depends_on") m.set(e.target, (m.get(e.target) ?? 0) + 1);
  return m;
}

const GOLDEN = Math.PI * (3 - Math.sqrt(5));
const SPACING = 21;
const MAX_GROUPS = 6;

export function clusterLayout(nodes: GNode[], edges: GEdge[]): Layout {
  const comps = nodes.filter((n) => n.kind === "component");
  const fanIn = fanInOf(edges);
  const byGroup = new Map<string, GNode[]>();
  for (const c of comps) {
    const g = groupOf(c.name);
    byGroup.set(g, [...(byGroup.get(g) ?? []), c]);
  }
  // Keep the largest groups; fold the small rest into one disc so the picture stays readable.
  let entries = [...byGroup.entries()].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]));
  if (entries.length > MAX_GROUPS) {
    const rest = entries.slice(MAX_GROUPS - 1).flatMap(([, list]) => list);
    entries = [...entries.slice(0, MAX_GROUPS - 1), ["other", rest]];
  }
  // The group other groups depend on most goes in the middle.
  const weight = (list: GNode[]) => list.reduce((n, c) => n + (fanIn.get(c.id) ?? 0), 0);
  const ranked = entries.slice().sort((a, b) => weight(b[1]) - weight(a[1]) || a[0].localeCompare(b[0]));
  const order: [string, GNode[]][] = [];
  ranked.forEach((g, i) => (i % 2 === 0 ? order.push(g) : order.unshift(g)));

  const radius = (n: number) => SPACING * Math.sqrt(Math.max(n, 1)) + 26;
  const gap = 56;
  const top = 64;
  const maxR = Math.max(...order.map(([, l]) => radius(l.length)), 60);
  const placed = new Map<string, Placed>();
  const groups: GroupDisc[] = [];
  let x = gap;
  for (const [key, list] of order) {
    const R = radius(list.length);
    const cx = x + R;
    const cy = top + maxR;
    const sorted = list.slice().sort((a, b) => (fanIn.get(b.id) ?? 0) - (fanIn.get(a.id) ?? 0) || a.name.localeCompare(b.name));
    sorted.forEach((c, i) => {
      const rr = SPACING * Math.sqrt(i + 0.35);
      const angle = i * GOLDEN;
      const size = 4 + Math.min(7, Math.sqrt(fanIn.get(c.id) ?? 0) * 1.25);
      placed.set(c.id, { id: c.id, x: cx + rr * Math.cos(angle), y: cy + rr * Math.sin(angle), r: size, group: key });
    });
    groups.push({ key, label: groupLabel(key), count: list.length, cx, cy, r: R });
    x += R * 2 + gap;
  }
  return { placed, groups, width: Math.max(x, 400), height: top + maxR * 2 + 40 };
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

interface ForecastRow {
  title?: string | null;
  issue_id?: string | null;
  component?: string | null;
  score?: number | null;
}

/** "sig/storage" and a forecast item's "storage" name the same area. */
function areaName(team: string | null): string | null {
  return team ? team.replace(/^[^/:]+[/:]/, "") : null;
}

function shortName(name: string): string {
  const last = name.split("/").slice(1).join("/") || name;
  return last.length > 26 ? `${last.slice(0, 25)}…` : last;
}

export interface LabelSpot {
  id: string;
  x: number;
  y: number;
}

/** Places labels so none overlap: most-used components first, above the dot, else below, else left out.
 *  `scale` is how many drawing units one screen pixel covers, so label size follows the zoom. */
export function placeLabels(
  wanted: { id: string; x: number; y: number; r: number; text: string; weight: number; must?: boolean }[],
  scale = 1,
): LabelSpot[] {
  const charW = 5.8 * scale;
  const h = 12 * scale;
  const taken: { x0: number; x1: number; y0: number; y1: number }[] = [];
  const out: LabelSpot[] = [];
  const order = [...wanted].sort((a, b) => Number(Boolean(b.must)) - Number(Boolean(a.must)) || b.weight - a.weight);
  for (const w of order) {
    const half = (w.text.length * charW) / 2 + 2 * scale;
    for (const y of [w.y - w.r - 4 * scale, w.y + w.r + 11 * scale]) {
      const box = { x0: w.x - half, x1: w.x + half, y0: y - h, y1: y + 2 * scale };
      const clash = taken.some((t) => box.x0 < t.x1 && box.x1 > t.x0 && box.y0 < t.y1 && box.y1 > t.y0);
      if (!clash || (w.must && y > w.y)) {
        taken.push(box);
        out.push({ id: w.id, x: w.x, y });
        break;
      }
    }
  }
  return out;
}

function GraphDrawing({ nodes, edges, limitation, initialFocus, snapshotId }: { nodes: GNode[]; edges: GEdge[]; limitation?: string; initialFocus?: string | null; snapshotId: string }) {
  const layout = useMemo(() => clusterLayout(nodes, edges), [nodes, edges]);
  const [box, setBox] = useState<Box>({ x: 0, y: 0, w: layout.width, h: layout.height });
  const byId = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);
  const { slot, top } = useMemo(() => teamSlots(nodes), [nodes]);
  const compEdges = useMemo(() => edges.filter((e) => e.kind !== "owned_by_team" && layout.placed.has(e.source) && layout.placed.has(e.target)), [edges, layout]);
  const fanIn = useMemo(() => fanInOf(compEdges), [compEdges]);
  const hubs = useMemo(
    () => new Set([...fanIn.entries()].sort((a, b) => b[1] - a[1]).slice(0, 7).map(([id]) => id)),
    [fanIn],
  );
  const issues = useMemo(() => nodes.filter((n) => n.kind === "issue"), [nodes]);
  const forecast = useLatest("forecast");
  const forecastItems: ForecastRow[] = (forecast.report?.report?.forecast as { items?: ForecastRow[] } | undefined)?.items ?? [];

  const [selected, setSelected] = useState<string | null>(initialFocus && layout.placed.has(initialFocus) ? initialFocus : null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [links, setLinks] = useState<LinkMode>("key");
  const [showTest, setShowTest] = useState(false);
  const [query, setQuery] = useState("");
  const svgRef = useRef<SVGSVGElement>(null);
  const drag = useRef<{ x: number; y: number; box: Box } | null>(null);

  const fit = () => setBox({ x: 0, y: 0, w: layout.width, h: layout.height });

  // Wheel zoom needs a non-passive listener so the page does not scroll at the same time.
  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const onWheel = (ev: WheelEvent) => {
      ev.preventDefault();
      setBox((b) => {
        const rect = el.getBoundingClientRect();
        const px = b.x + ((ev.clientX - rect.left) / rect.width) * b.w;
        const py = b.y + ((ev.clientY - rect.top) / rect.height) * b.h;
        const k = ev.deltaY > 0 ? 1.15 : 1 / 1.15;
        const w = Math.min(Math.max(b.w * k, 160), layout.width * 3);
        const h = (b.h / b.w) * w;
        return { x: px - ((px - b.x) / b.w) * w, y: py - ((py - b.y) / b.h) * h, w, h };
      });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [layout]);

  const zoom = (k: number) => setBox((b) => ({ x: b.x + (b.w - b.w * k) / 2, y: b.y + (b.h - b.h * k) / 2, w: b.w * k, h: b.h * k }));

  const focusOn = (id: string) => {
    const p = layout.placed.get(id);
    setSelected(id);
    setQuery("");
    if (p) setBox((b) => ({ x: p.x - 260, y: p.y - (260 * b.h) / b.w, w: 520, h: (520 * b.h) / b.w }));
  };

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
  const zoomedIn = box.w < layout.width * 0.55;
  // Labels: the selected dot always, then the most-used components; overlapping ones are left out.
  const wantedLabels = [...layout.placed.values()].flatMap((p) => {
    const n = byId.get(p.id);
    if (!n) return [];
    const dim = Boolean(focus) && p.id !== focus && !neighbours.has(p.id);
    const show = !dim && (p.id === focus || hubs.has(p.id) || (zoomedIn && p.r > 5) || (Boolean(focus) && neighbours.has(p.id) && neighbours.size <= 14));
    return show ? [{ id: p.id, x: p.x, y: p.y, r: p.r, text: shortName(n.name), weight: fanIn.get(p.id) ?? 0, must: p.id === focus }] : [];
  });
  const labelSpots = new Map(placeLabels(wantedLabels).map((l) => [l.id, l]));
  const matches = query.trim()
    ? nodes.filter((n) => n.kind === "component" && n.name.toLowerCase().includes(query.trim().toLowerCase())).slice(0, 8)
    : [];
  const detail = selected ? byId.get(selected) : focus ? byId.get(focus) : null;
  const imports = detail ? compEdges.filter((e) => e.source === detail.id && e.kind === "depends_on").map((e) => e.target) : [];
  const users = detail ? compEdges.filter((e) => e.target === detail.id && e.kind === "depends_on").map((e) => e.source) : [];
  const area = detail ? areaName(detail.team) : null;
  const teamIssues = detail?.team ? issues.filter((i) => i.labels.includes(detail.team as string)) : [];
  const teamForecast = area ? forecastItems.filter((f) => f.component === area) : [];

  const nameList = (ids: string[]) => (
    <ul className="node-list">
      {ids.slice(0, 12).map((id) => (
        <li key={id}>
          <button type="button" className="link-button" onClick={() => focusOn(id)}>{byId.get(id)?.name ?? id}</button>
        </li>
      ))}
      {ids.length > 12 ? <li className="muted">and {ids.length - 12} more</li> : null}
    </ul>
  );

  return (
    <>
      <div className="graph-bar">
        <span className="search-field">
          <Icon name="search" size={16} />
          <label className="sr-only" htmlFor="graph-search">Find a component</label>
          <input
            id="graph-search"
            placeholder="Find a component"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && matches[0]) focusOn(matches[0].id);
            }}
          />
        </span>
        <Segmented<LinkMode>
          label="Links shown"
          value={links}
          onChange={setLinks}
          options={[
            { value: "key", label: "Main links" },
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
          <Button size="sm" onClick={fit}>Fit</Button>
        </span>
        <ul className="legend" aria-label="Owner teams">
          {top.map((t, i) => (
            <li key={t.team}><span className={`swatch s${i + 1}`} aria-hidden="true" /> {areaName(t.team)}</li>
          ))}
          <li><span className="swatch other" aria-hidden="true" /> Other teams</li>
          <li><span className="swatch none" aria-hidden="true" /> No owner</li>
        </ul>
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
          preserveAspectRatio="xMidYMid meet"
          role="img"
          aria-label={`Dependency graph of ${layout.placed.size} components in ${layout.groups.length} groups`}
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
          {layout.groups.map((g) => (
            <g key={g.key}>
              <circle className="group-disc" cx={g.cx} cy={g.cy} r={g.r} />
              <text className="group-label" x={g.cx} y={g.cy - g.r - 14} textAnchor="middle">
                {g.label} · {g.count}
              </text>
            </g>
          ))}
          {shownEdges.map((e, i) => {
            const a = layout.placed.get(e.source);
            const b = layout.placed.get(e.target);
            if (!a || !b) return null;
            const side = focus ? (e.source === focus ? " out" : " in") : "";
            return <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y} className={`edge ${e.kind}${side}`} />;
          })}
          {[...layout.placed.values()].map((p) => {
            const n = byId.get(p.id);
            if (!n) return null;
            const dim = Boolean(focus) && p.id !== focus && !neighbours.has(p.id);
            const spot = labelSpots.get(p.id);
            return (
              <g key={p.id}>
                <circle
                  cx={p.x}
                  cy={p.y}
                  r={p.r}
                  className={`node ${nodeClass(n, slot)}${p.id === selected ? " sel" : ""}${dim ? " dim" : ""}`}
                  tabIndex={0}
                  onMouseEnter={() => setHovered(p.id)}
                  onMouseLeave={() => setHovered(null)}
                  onFocus={() => setHovered(p.id)}
                  onBlur={() => setHovered(null)}
                  onClick={() => setSelected(p.id === selected ? null : p.id)}
                  onPointerDown={(e) => e.stopPropagation()}
                >
                  <title>{`${n.name} · ${n.team ?? "no owner team"} · used by ${fanIn.get(p.id) ?? 0}`}</title>
                </circle>
                {spot ? (
                  <text className="node-label" x={spot.x} y={spot.y} textAnchor="middle">
                    {shortName(n.name)}
                  </text>
                ) : null}
              </g>
            );
          })}
        </svg>
        <aside className="node-detail" aria-live="polite">
          {detail ? (
            <>
              <p className="detail-kicker">{groupOf(detail.name)}</p>
              <h3>{detail.name.split("/").slice(1).join("/") || detail.name}</h3>
              <div className="detail-stats">
                <div><span className="num">{users.length}</span><span>used by</span></div>
                <div><span className="num">{imports.length}</span><span>imports</span></div>
                <div><span className="num">{teamIssues.length}</span><span>open issues</span></div>
              </div>
              <p className="detail-owner">
                <Icon name="user" size={14} /> {detail.team ?? "No owner team recorded"}
              </p>
              {teamForecast.length ? (
                <>
                  <h4>Likely to carry over <span className="pill pill-warn num">{teamForecast.length}</span></h4>
                  <ul className="node-list">
                    {teamForecast.slice(0, 5).map((f) => (
                      <li key={f.issue_id ?? f.title ?? ""} title={f.title ?? undefined}>
                        <span className="num muted">#{f.issue_id}</span> {f.title && f.title.length > 44 ? `${f.title.slice(0, 43)}…` : f.title}
                      </li>
                    ))}
                  </ul>
                  {forecast.link ? <Link className="btn btn-ghost btn-sm" to={`/reports/${encodeURIComponent(forecast.link.report_id)}`}>Open the forecast</Link> : null}
                </>
              ) : null}
              {teamIssues.length ? (
                <>
                  <h4>Open issues for {area}</h4>
                  <p className="chip-row">
                    {teamIssues.slice(0, 14).map((i) => <span key={i.id} className="chip chip-quiet num">#{i.name}</span>)}
                    {teamIssues.length > 14 ? <span className="muted small">+{teamIssues.length - 14}</span> : null}
                  </p>
                </>
              ) : null}
              {users.length ? (<><h4>Used by</h4>{nameList(users)}</>) : null}
              {imports.length ? (<><h4>Imports</h4>{nameList(imports)}</>) : null}
              {selected ? <Button size="sm" variant="ghost" onClick={() => setSelected(null)}>Clear</Button> : null}
            </>
          ) : (
            <div className="detail-empty">
              <Icon name="graph" size={22} />
              <p>Select a component</p>
              <p className="muted small">Larger dots are used by more components.</p>
            </div>
          )}
          {limitation ? (
            <p className="detail-foot">
              <span className="pill pill-neutral" title={limitation}>Import links only</span>
            </p>
          ) : null}
        </aside>
      </div>
      <span className="sr-only" data-testid="graph-snapshot">{snapshotId}</span>
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
                <td><span className="row-title">{r.name}</span></td>
                <td>{r.team ?? <span className="muted">—</span>}</td>
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
  if (!issues.length) return <Empty>No open issues at this point in time.</Empty>;
  return (
    <>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Issue</th><th>Labels</th></tr></thead>
          <tbody>
            {paged.slice.map((n) => (
              <tr key={n.id}>
                <td className="num">#{n.name}</td>
                <td><span className="chip-row tight">{n.labels.map((l) => <span key={l} className="chip chip-quiet">{l}</span>)}</span></td>
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
      {view === "graph" ? <GraphDrawing nodes={data.nodes} edges={data.edges} limitation={g.data?.limitation} initialFocus={focus} snapshotId={snapshotId} /> : null}
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
