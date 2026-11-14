// Small charts drawn as plain SVG. Geometry uses SVG attributes (allowed by the page security
// policy); colours come from CSS classes and design tokens. Every chart has a text equivalent.
import { useState } from "react";

export interface Point {
  label: string;
  value: number;
}

/** A single-series sparkline with a hover readout. One series, so the card title names it (no legend). */
export function Sparkline({ points, unit, height = 64 }: { points: Point[]; unit: string; height?: number }) {
  const [hover, setHover] = useState<number | null>(null);
  const width = 320;
  const pad = 6;
  if (points.length === 0) return <p className="muted">No readings yet.</p>;
  const max = Math.max(...points.map((p) => p.value), 1);
  const min = 0;
  const step = points.length > 1 ? (width - pad * 2) / (points.length - 1) : 0;
  const y = (v: number) => height - pad - ((v - min) / (max - min || 1)) * (height - pad * 2);
  const x = (i: number) => pad + i * step;
  const line = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join(" ");
  const area = `${line} L${x(points.length - 1).toFixed(1)},${height - pad} L${x(0).toFixed(1)},${height - pad} Z`;
  const shown = hover ?? points.length - 1;
  const cur = points[shown];
  return (
    <figure className="spark">
      <svg className="chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${points.length} readings, latest ${points[points.length - 1]?.value ?? 0} ${unit}`}>
        <line className="grid-line" x1={pad} x2={width - pad} y1={height - pad} y2={height - pad} />
        <path className="spark-area" d={area} />
        <path className="spark-line" d={line} />
        {cur ? <circle className="spark-dot" cx={x(shown)} cy={y(cur.value)} r={4.5} /> : null}
        {points.map((p, i) => (
          <rect
            key={`${p.label}-${i}`}
            className="hit"
            x={x(i) - Math.max(step / 2, 6)}
            y={0}
            width={Math.max(step, 12)}
            height={height}
            onMouseEnter={() => setHover(i)}
            onMouseLeave={() => setHover(null)}
          />
        ))}
      </svg>
      <figcaption className="chart-tip num">
        {cur ? (
          <>
            <strong>{cur.value}</strong> {unit} · {cur.label}
          </>
        ) : null}
      </figcaption>
    </figure>
  );
}

/** Horizontal bars for one measure (counts per category). Values are labelled; colour is one hue. */
export function BarList({ rows, unit }: { rows: Point[]; unit: string }) {
  const [hover, setHover] = useState<number | null>(null);
  if (rows.length === 0) return <p className="muted">Nothing to show.</p>;
  const max = Math.max(...rows.map((r) => r.value), 1);
  const width = 320;
  const labelW = 0;
  const barH = 14;
  const gap = 12;
  const height = rows.length * (barH + gap + 14);
  return (
    <figure className="barlist">
      <svg className="chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={rows.map((r) => `${r.label}: ${r.value} ${unit}`).join("; ")}>
        {rows.map((r, i) => {
          const top = i * (barH + gap + 14);
          const w = Math.max(4, ((width - labelW - 40) * r.value) / max);
          return (
            <g key={r.label} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}>
              <text x={0} y={top + 10}>{r.label.length > 48 ? `${r.label.slice(0, 47)}…` : r.label}</text>
              <rect className={`bar-fill${hover === i ? " on" : ""}`} x={0} y={top + 16} width={w} height={barH} rx={4} />
              <text className="bar-value" x={w + 6} y={top + 27}>{r.value}</text>
            </g>
          );
        })}
      </svg>
      <figcaption className="chart-tip">{hover !== null && rows[hover] ? `${rows[hover]!.label}: ${rows[hover]!.value} ${unit}` : " "}</figcaption>
    </figure>
  );
}
