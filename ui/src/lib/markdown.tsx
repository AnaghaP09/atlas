// A small, safe Markdown renderer for report text and guide pages. It builds React elements only:
// no raw HTML, no images, no embedded content. Supported: headings, paragraphs, bullet and numbered
// lists, simple tables, fenced code, **bold**, *italic*, `code` and [text](url) links. Links go
// through the external-link policy unless the caller maps them to a page inside ATLAS.

import { Fragment, type ReactNode } from "react";
import { ExternalLink } from "../components/ExternalLink";
import { Link } from "./router";

type InternalLink = (href: string) => string | null;

function inline(text: string, keyBase: string, internal?: InternalLink): ReactNode[] {
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
      const href = token.slice(close + 2, -1);
      const label = token.slice(1, close);
      const inside = internal ? internal(href) : null;
      out.push(inside ? <Link key={key} to={inside}>{label}</Link> : <ExternalLink key={key} href={href} label={label} />);
    } else out.push(<em key={key}>{token.slice(1, -1)}</em>);
    last = idx + token.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function cells(line: string): string[] {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}

const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

export function Markdown({ source, internal }: { source: string; internal?: InternalLink }) {
  const blocks: ReactNode[] = [];
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  let list: string[] = [];
  let ordered = false;
  let para: string[] = [];
  const flushList = () => {
    if (list.length) {
      const items = list;
      const key = `l-${blocks.length}`;
      const body = items.map((t, j) => <li key={j}>{inline(t, `${key}-${j}`, internal)}</li>);
      blocks.push(ordered ? <ol key={key}>{body}</ol> : <ul key={key}>{body}</ul>);
      list = [];
    }
  };
  const flushPara = () => {
    if (para.length) {
      blocks.push(<p key={`p-${blocks.length}`}>{inline(para.join(" "), `p-${blocks.length}`, internal)}</p>);
      para = [];
    }
  };
  for (let n = 0; n < lines.length; n++) {
    const line = lines[n] ?? "";
    if (/^\s*```/.test(line)) {
      flushList();
      flushPara();
      const code: string[] = [];
      n++;
      while (n < lines.length && !/^\s*```/.test(lines[n] ?? "")) code.push(lines[n++] ?? "");
      blocks.push(<pre key={`c-${blocks.length}`} className="code-block"><code>{code.join("\n")}</code></pre>);
      continue;
    }
    if (line.includes("|") && TABLE_RULE.test(lines[n + 1] ?? "")) {
      flushList();
      flushPara();
      const head = cells(line);
      const rows: string[][] = [];
      n += 2;
      while (n < lines.length && (lines[n] ?? "").includes("|") && (lines[n] ?? "").trim() !== "") rows.push(cells(lines[n++] ?? ""));
      n--;
      const key = `t-${blocks.length}`;
      blocks.push(
        <div className="table-wrap" key={key}>
          <table>
            <thead>
              <tr>{head.map((h, j) => <th key={j}>{inline(h, `${key}-h${j}`, internal)}</th>)}</tr>
            </thead>
            <tbody>
              {rows.map((r, ri) => (
                <tr key={ri}>{head.map((_, j) => <td key={j}>{inline(r[j] ?? "", `${key}-${ri}-${j}`, internal)}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }
    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    if (h) {
      flushList();
      flushPara();
      const level = Math.min((h[1] ?? "#").length + 1, 5);
      const content = inline(h[2] ?? "", `h-${blocks.length}`, internal);
      const key = `h-${blocks.length}`;
      blocks.push(
        level === 2 ? <h2 key={key}>{content}</h2> : level === 3 ? <h3 key={key}>{content}</h3> : <h4 key={key}>{content}</h4>,
      );
      continue;
    }
    const li = /^\s*[-*]\s+(.*)$/.exec(line);
    const oli = li ? null : /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (li || oli) {
      flushPara();
      const wantOrdered = Boolean(oli);
      if (list.length && ordered !== wantOrdered) flushList();
      ordered = wantOrdered;
      list.push((li ?? oli)?.[1] ?? "");
      continue;
    }
    if (line.trim() === "") {
      flushList();
      flushPara();
      continue;
    }
    if (list.length && /^\s{2,}\S/.test(line)) {
      list[list.length - 1] = `${list[list.length - 1]} ${line.trim()}`;
      continue;
    }
    flushList();
    para.push(line.trim());
  }
  flushList();
  flushPara();
  return <div className="markdown">{blocks.map((b, i) => <Fragment key={i}>{b}</Fragment>)}</div>;
}
