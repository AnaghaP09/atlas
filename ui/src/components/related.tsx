// Entity links and the compact "Related" strip. Both hide anything the caller's menu does not include.
import type { ReactNode } from "react";
import { useApi } from "../app/context";
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

/** Chips with icon + count. A chip whose page the caller cannot open is left out. */
export function Related({ chips }: { chips: RelatedChip[] }) {
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
