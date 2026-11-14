// The user guide, shown inside ATLAS. Pages are Markdown served by the backend and rendered with the
// safe renderer, so guide text can never run code or load anything from outside.
import { useEffect, useState } from "react";
import { Empty, Loading, PageHead } from "../components/common";
import { Icon } from "../components/icons";
import { Markdown } from "../lib/markdown";
import { Link } from "../lib/router";

interface GuideIndex {
  pages: string[];
  titles?: Record<string, string>;
}

const GROUPS: { title: string; slugs: string[] }[] = [
  { title: "Start here", slugs: ["index", "install", "sign-in-and-projects", "glossary", "data-as-of"] },
  { title: "By role", slugs: ["product-manager", "engineering-manager", "solution-architect", "security-engineer", "on-call-engineer", "audit"] },
];

/** A link written in a guide page ("runs.md", "./glossary.md#term") becomes a page inside ATLAS. */
export function guideLink(href: string): string | null {
  const m = /^(?:\.\/)?([a-z0-9-]+)\.md(#[\w-]*)?$/.exec(href);
  return m ? `/help/${m[1]}` : null;
}

function useText(url: string): { text: string | null; failed: boolean } {
  const [state, setState] = useState<{ url: string; text: string | null; failed: boolean }>({ url, text: null, failed: false });
  useEffect(() => {
    let live = true;
    fetch(url, { headers: { Accept: "text/markdown, text/plain" } })
      .then(async (r) => {
        const body = r.ok ? await r.text() : null;
        if (live) setState({ url, text: body, failed: body === null });
      })
      .catch(() => {
        if (live) setState({ url, text: null, failed: true });
      });
    return () => {
      live = false;
    };
  }, [url]);
  return state.url === url ? state : { text: null, failed: false };
}

function useIndex(): GuideIndex | null {
  const [index, setIndex] = useState<GuideIndex | null>(null);
  useEffect(() => {
    let live = true;
    fetch("/docs/guide/pages.json", { headers: { Accept: "application/json" } })
      .then(async (r) => (r.ok ? ((await r.json()) as GuideIndex) : { pages: [] }))
      .then((d) => {
        if (live) setIndex(d);
      })
      .catch(() => {
        if (live) setIndex({ pages: [] });
      });
    return () => {
      live = false;
    };
  }, []);
  return index;
}

export function Help({ slug }: { slug: string }) {
  const index = useIndex();
  const page = useText(`/docs/guide/${encodeURIComponent(slug)}.md`);
  const titles = index?.titles ?? {};
  const grouped = new Set(GROUPS.flatMap((g) => g.slugs));
  const screens = (index?.pages ?? []).filter((s) => !grouped.has(s));
  const name = (s: string) => titles[s] ?? s.replace(/-/g, " ").replace(/^./, (c) => c.toUpperCase());
  const entry = (s: string) =>
    (index?.pages ?? []).includes(s) ? (
      <li key={s}>
        <Link to={`/help/${s}`} className={s === slug ? "on" : undefined}>{name(s)}</Link>
      </li>
    ) : null;
  return (
    <>
      <PageHead title="Guide" />
      <div className="help-layout">
        <nav className="help-nav card" aria-label="Guide pages">
          {GROUPS.map((g) => (
            <div key={g.title}>
              <h3>{g.title}</h3>
              <ul>{g.slugs.map(entry)}</ul>
            </div>
          ))}
          <div>
            <h3>Screens</h3>
            <ul>{screens.map(entry)}</ul>
          </div>
        </nav>
        <article className="card help-page">
          {page.failed ? (
            <Empty action={{ to: "/help/index", label: "Guide home" }}>This guide page does not exist.</Empty>
          ) : page.text === null ? (
            <Loading what="the guide" />
          ) : (
            <Markdown source={page.text} internal={guideLink} />
          )}
          <p className="help-foot muted">
            <Icon name="shield" size={14} /> Shown as plain text. Nothing is loaded from outside ATLAS.
          </p>
        </article>
      </div>
    </>
  );
}
