// Small line icons drawn in-repo (24 x 24 grid, 2 px strokes, currentColor). No icon package.
import type { ReactNode } from "react";

export type IconName =
  | "home" | "pulse" | "play" | "clock" | "list" | "hand" | "check" | "task" | "bell" | "power"
  | "chart" | "graph" | "pen" | "gauge" | "compass" | "sun" | "moon" | "menu" | "database" | "arrow" | "shield" | "lock" | "key"
  | "help" | "close" | "search" | "alert" | "ok" | "user" | "code" | "scale";

const PATHS: Record<IconName, ReactNode> = {
  home: <><path d="M3 11l9-7 9 7" /><path d="M5 10v10h14V10" /><path d="M10 20v-6h4v6" /></>,
  pulse: <path d="M3 12h4l3-7 4 14 3-7h4" />,
  play: <><circle cx="12" cy="12" r="9" /><path d="M10 8l6 4-6 4z" /></>,
  clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
  list: <><path d="M8 6h13M8 12h13M8 18h13" /><circle cx="4" cy="6" r="1" /><circle cx="4" cy="12" r="1" /><circle cx="4" cy="18" r="1" /></>,
  hand: <><path d="M7 11V6a2 2 0 014 0v5" /><path d="M11 10V4a2 2 0 014 0v6" /><path d="M15 10V6a2 2 0 014 0v8a7 7 0 01-7 7h-1a7 7 0 01-6-3l-2-4a2 2 0 013-2l2 2" /></>,
  check: <><rect x="3" y="3" width="18" height="18" rx="4" /><path d="M8 12l3 3 5-6" /></>,
  task: <><rect x="4" y="3" width="16" height="18" rx="3" /><path d="M8 8h8M8 12h8M8 16h5" /></>,
  bell: <><path d="M6 16V11a6 6 0 0112 0v5l2 2H4z" /><path d="M10 20a2 2 0 004 0" /></>,
  power: <><path d="M12 3v8" /><path d="M6.3 7.3a8 8 0 1011.4 0" /></>,
  chart: <><path d="M4 20V10M10 20V4M16 20v-7M22 20H2" /></>,
  graph: <><circle cx="6" cy="6" r="2.5" /><circle cx="18" cy="7" r="2.5" /><circle cx="12" cy="18" r="2.5" /><path d="M8 7l8 0M7.5 8l3.5 8M16.8 9l-3.6 7" /></>,
  pen: <><path d="M4 20l4-1 11-11-3-3L5 16z" /><path d="M14 6l3 3" /></>,
  gauge: <><path d="M4 18a8 8 0 1116 0" /><path d="M12 18l4-6" /></>,
  compass: <><circle cx="12" cy="12" r="9" /><path d="M15.5 8.5l-2 5-5 2 2-5z" /></>,
  sun: <><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M19 5l-1.5 1.5M6.5 17.5L5 19" /></>,
  moon: <path d="M20 14.5A8 8 0 019.5 4a8 8 0 1010.5 10.5z" />,
  menu: <path d="M4 7h16M4 12h16M4 17h16" />,
  database: <><ellipse cx="12" cy="6" rx="8" ry="3" /><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6" /><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3" /></>,
  arrow: <path d="M5 12h14M13 6l6 6-6 6" />,
  lock: <><rect x="5" y="11" width="14" height="10" rx="2" /><path d="M8 11V7a4 4 0 018 0v4" /></>,
  key: <><circle cx="8" cy="15" r="4" /><path d="M11 12l9-9M17 6l3 3" /></>,
  help: <><circle cx="12" cy="12" r="9" /><path d="M9.5 9.5a2.5 2.5 0 114 2c-.9.7-1.5 1.2-1.5 2.5" /><path d="M12 17.5v.01" /></>,
  close: <path d="M6 6l12 12M18 6L6 18" />,
  search: <><circle cx="11" cy="11" r="7" /><path d="M16.5 16.5L21 21" /></>,
  alert: <><path d="M12 3l9.5 17h-19z" /><path d="M12 10v4M12 17v.01" /></>,
  ok: <><circle cx="12" cy="12" r="9" /><path d="M8 12.5l2.8 2.8L16 9.5" /></>,
  user: <><circle cx="12" cy="8" r="4" /><path d="M4 21a8 8 0 0116 0" /></>,
  code: <><path d="M8 7l-5 5 5 5M16 7l5 5-5 5" /></>,
  scale: <><path d="M12 4v16M6 20h12" /><path d="M5 8h14" /><path d="M5 8l-3 6a3 3 0 006 0zM19 8l-3 6a3 3 0 006 0z" /></>,
  shield: <><path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z" /><path d="M9 12l2 2 4-4" /></>,
};

export function Icon({ name, size = 20, label }: { name: IconName; size?: number; label?: string }) {
  return (
    <svg
      className="icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    >
      {PATHS[name]}
    </svg>
  );
}

/** Scout's emblem: a compass, because Scout finds its way through the evidence. */
export function ScoutEmblem({ size = 22 }: { size?: number }) {
  return (
    <svg className="agent-emblem" width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="12" r="10" fill="currentColor" opacity="0.16" />
      <circle cx="12" cy="12" r="8.5" fill="none" stroke="currentColor" strokeWidth="1.8" />
      <path d="M15.8 8.2l-2.3 5.3-5.3 2.3 2.3-5.3z" fill="currentColor" />
      <circle cx="12" cy="12" r="1.2" fill="var(--surface)" />
    </svg>
  );
}

/** Sentinel's emblem: a shield, because Sentinel guards what gets published. */
export function SentinelEmblem({ size = 22 }: { size?: number }) {
  return (
    <svg className="agent-emblem" width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
      <path d="M12 2.5l8 3v6.2c0 5.1-3.5 8.4-8 9.8-4.5-1.4-8-4.7-8-9.8V5.5z" fill="currentColor" opacity="0.16" />
      <path d="M12 2.5l8 3v6.2c0 5.1-3.5 8.4-8 9.8-4.5-1.4-8-4.7-8-9.8V5.5z" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
      <path d="M8.5 12l2.4 2.4 4.6-4.8" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** The ATLAS mark: a globe with a meridian and an orbit. */
export function BrandMark() {
  return (
    <svg className="brand-mark" viewBox="0 0 40 40" aria-hidden="true">
      <defs>
        <linearGradient id="atlas-g" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#8f84f2" />
          <stop offset="1" stopColor="#3cc2b8" />
        </linearGradient>
      </defs>
      <rect x="1" y="1" width="38" height="38" rx="11" fill="url(#atlas-g)" />
      <circle cx="20" cy="20" r="10" fill="none" stroke="#fff" strokeWidth="2" />
      <ellipse cx="20" cy="20" rx="4.5" ry="10" fill="none" stroke="#fff" strokeWidth="1.6" opacity="0.9" />
      <path d="M8 24c6 3 18 3 24-2" fill="none" stroke="#fff" strokeWidth="1.6" strokeLinecap="round" opacity="0.8" />
    </svg>
  );
}
