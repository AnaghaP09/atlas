// A small, safe Markdown renderer for report text. It builds React elements only: no raw HTML,
// no images, no embedded content. Supported: headings, paragraphs, bullet lists, **bold**,
// *italic*, `code` and [text](url) links, which go through the external-link policy.

import { Fragment, type ReactNode } from "react";
import { ExternalLink } from "../components/ExternalLink";

function inline(text: string, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  const pattern = /(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`|\[[^\]]+\]\([^)\s]+\))/g;
  let last = 0;
  let i = 0;
  for (const m of text.matchAll(pattern)) {
    const idx = m.index ?? 0;
    if (idx > last) out.push(text.slice(last, idx));
    const token = m[0];
    const key = `${keyBase}-${i++}`;
    if (token.startsWith("**")) out.push(<strong key={key}>{token.slice(2, -2)}</strong>);
    else if (token.startsWith("`")) out.push(<code key={key}>{token.slice(1, -1)}</code>);
    else if (token.startsWith("[")) {
      const close = token.indexOf("](");
      out.push(<ExternalLink key={key} href={token.slice(close + 2, -1)} label={token.slice(1, close)} />);
    } else out.push(<em key={key}>{token.slice(1, -1)}</em>);
    last = idx + token.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function Markdown({ source }: { source: string }) {
  const blocks: ReactNode[] = [];
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  let list: string[] = [];
  let para: string[] = [];
  const flushList = () => {
    if (list.length) {
      const items = list;
      blocks.push(
        <ul key={`ul-${blocks.length}`}>
          {items.map((t, j) => (
            <li key={j}>{inline(t, `li-${blocks.length}-${j}`)}</li>
          ))}
        </ul>,
      );
      list = [];
    }
  };
  const flushPara = () => {
    if (para.length) {
      blocks.push(<p key={`p-${blocks.length}`}>{inline(para.join(" "), `p-${blocks.length}`)}</p>);
      para = [];
    }
  };
  for (const line of lines) {
    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    if (h) {
      flushList();
      flushPara();
      const level = Math.min((h[1] ?? "#").length + 1, 5);
      const content = inline(h[2] ?? "", `h-${blocks.length}`);
      const key = `h-${blocks.length}`;
      blocks.push(
        level === 2 ? <h2 key={key}>{content}</h2> : level === 3 ? <h3 key={key}>{content}</h3> : <h4 key={key}>{content}</h4>,
      );
      continue;
    }
    const li = /^\s*[-*]\s+(.*)$/.exec(line);
    if (li) {
      flushPara();
      list.push(li[1] ?? "");
      continue;
    }
    if (line.trim() === "") {
      flushList();
      flushPara();
      continue;
    }
    flushList();
    para.push(line.trim());
  }
  flushList();
  flushPara();
  return <div className="markdown">{blocks.map((b, i) => <Fragment key={i}>{b}</Fragment>)}</div>;
}
