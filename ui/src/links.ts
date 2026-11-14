// One map from an entity to its page. A link is shown only when the caller's menu has that page
// (or the page is a detail view any role may open; the API still decides what it returns).
import type { IconName } from "./components/icons";

export type Entity =
  | "run"
  | "report"
  | "evidence"
  | "task"
  | "approval"
  | "incident"
  | "snapshot"
  | "component"
  | "notification"
  | "queue";

export interface LinkContext {
  runId?: string | null;
  snapshotId?: string | null;
}

export interface Target {
  /** The menu page the link belongs to; null for detail pages any role may open. */
  pageId: string | null;
  to: string;
  icon: IconName;
}

const enc = encodeURIComponent;

/** Every target for an entity, in order of preference. The first one the caller may open wins. */
export function targets(entity: Entity, id: string, ctx: LinkContext = {}): Target[] {
  switch (entity) {
    case "run":
      return [{ pageId: "runs", to: `/runs/${enc(id)}`, icon: "list" }];
    case "report":
      return [{ pageId: null, to: `/reports/${enc(id)}`, icon: "task" }];
    case "evidence":
      return ctx.runId ? [{ pageId: null, to: `/evidence/${enc(id)}?run=${enc(ctx.runId)}`, icon: "database" }] : [];
    case "task":
      return [{ pageId: "tasks", to: `/tasks/${enc(id)}`, icon: "task" }];
    case "approval":
      return [{ pageId: "approvals", to: `/approvals?focus=${enc(id)}`, icon: "check" }];
    case "incident":
      return [{ pageId: "containment", to: `/containment?incident=${enc(id)}`, icon: "power" }];
    case "snapshot":
      return [
        { pageId: "graph", to: `/graph?snapshot=${enc(id)}`, icon: "database" },
        { pageId: "run_now", to: `/run-now?snapshot=${enc(id)}`, icon: "database" },
      ];
    case "component": {
      const snap = ctx.snapshotId ? `snapshot=${enc(ctx.snapshotId)}&` : "";
      const node = id.startsWith("component:") ? id : `component:${id}`;
      return [{ pageId: "graph", to: `/graph?${snap}focus=${enc(node)}`, icon: "graph" }];
    }
    case "notification":
      return [{ pageId: "notifications", to: `/notifications?focus=${enc(id)}`, icon: "bell" }];
    case "queue":
      return [{ pageId: "queue", to: `/queue?focus=${enc(id)}`, icon: "hand" }];
  }
}

/** The link the caller may follow, or null. ``allowed`` is the set of page ids on the caller's menu. */
export function resolveLink(entity: Entity, id: string | null | undefined, allowed: Set<string> | null, ctx: LinkContext = {}): Target | null {
  if (!id) return null;
  for (const t of targets(entity, id, ctx)) {
    if (t.pageId === null || (allowed !== null && allowed.has(t.pageId))) return t;
  }
  return null;
}
