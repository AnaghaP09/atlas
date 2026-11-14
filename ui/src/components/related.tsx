// Entity links and the compact "Related" strip. Both hide anything the caller's menu does not include.
import type { ReactNode } from "react";
import { ApiError } from "../api/client";
import { useApi } from "../app/context";
import { LockedChip, type AccessRequestState } from "./access";
import { resolveLink, type Entity, type LinkContext } from "../links";
import { allowedPages, type Navigation } from "../lib/nav";
import { Link } from "../lib/router";
import { Icon } from "./icons";

/** The caller's allowed pages; null until the menu has loaded. */
export function useAllowed(): Set<string> | null {
  const nav = useApi<Navigation>("/api/navigation");
  return nav.data ? allowedPages(nav.data) : null;
}

/** Renders an id-bearing value as a link when the caller may open its page; otherwise plain text. */
export function EntityLink({ entity, id, ctx, children }: { entity: Entity; id: string | null | undefined; ctx?: LinkContext; children?: ReactNode }) {
  const allowed = useAllowed();
  if (!id) return <>{children ?? "—"}</>;
  const t = resolveLink(entity, id, allowed, ctx);
  if (!t) return <>{children ?? id}</>;
  return <Link to={t.to}>{children ?? id}</Link>;
}

export interface RelatedChip {
  entity: Entity;
  id: string | null | undefined;
  label: string;
  count?: number;
  ctx?: LinkContext;
}

interface ServerRelated {
  items: {
    object_type: string;
    object_id: string;
    label?: string;
    count?: number;
    path?: string;
    access: "open" | "locked";
    request?: AccessRequestState | null;
  }[];
}

/** Chips with icon + count. Open items link to their page; locked items show a lock and a "Request access" action.
 * Uses GET /api/related/{type}/{id} when the server has it; otherwise the local link map (locked items then hidden). */
export function Related({ chips, object }: { chips: RelatedChip[]; object?: { type: string; id: string } }) {
  const server = useApi<ServerRelated>(object ? `/api/related/${encodeURIComponent(object.type)}/${encodeURIComponent(object.id)}` : null);
  const fallback = !object || (server.error instanceof ApiError && server.error.status === 404) || (!!server.error && !(server.error instanceof ApiError));
  if (object && !fallback) {
    if (!server.data) return null;
    if (!server.data.items.length) return null;
    return (
      <nav className="related" aria-label="Related">
        {server.data.items.map((it) =>
          it.access === "open" && it.path ? (
            <Link key={`${it.object_type}-${it.object_id}`} to={it.path} className="chip related-chip">
              {it.label ?? it.object_type}
              {typeof it.count === "number" ? <span className="num related-count">{it.count}</span> : null}
            </Link>
          ) : (
            <LockedChip key={`${it.object_type}-${it.object_id}`} objectType={it.object_type} objectId={it.object_id} request={it.request ?? null} />
          ),
        )}
      </nav>
    );
  }
  return <LocalRelated chips={chips} />;
}

function LocalRelated({ chips }: { chips: RelatedChip[] }) {
  const allowed = useAllowed();
  const shown = chips
    .map((c) => ({ c, t: resolveLink(c.entity, c.id, allowed, c.ctx) }))
    .filter((x): x is { c: RelatedChip; t: NonNullable<ReturnType<typeof resolveLink>> } => x.t !== null && (x.c.count === undefined || x.c.count > 0));
  if (!shown.length) return null;
  return (
    <nav className="related" aria-label="Related">
      {shown.map(({ c, t }) => (
        <Link key={`${c.entity}-${c.id}-${c.label}`} to={t.to} className="chip related-chip">
          <Icon name={t.icon} size={14} />
          {c.label}
          {typeof c.count === "number" ? <span className="num related-count">{c.count}</span> : null}
        </Link>
      ))}
    </nav>
  );
}
