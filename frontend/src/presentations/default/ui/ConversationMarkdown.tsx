import { Fragment, memo, useMemo, type ReactNode } from 'react';
import { fromMarkdown } from 'mdast-util-from-markdown';
import { gfm } from 'micromark-extension-gfm';
import { gfmFromMarkdown } from 'mdast-util-gfm';
import type { List, ListItem, Nodes, Parents, Root, Table } from 'mdast';
import type { Match } from './chatSearch';

/**
 * P063: one safe Markdown/GFM renderer for every Conversation body (PC, TC, published and provisional).
 *
 * Message text is UNTRUSTED. It is parsed to an mdast tree (micromark + GFM, no HTML interpretation) and rendered
 * by this module's own allowlisted node → React mapping: raw HTML is shown as text, images become a text
 * placeholder (never `<img src>`), links keep an href only for http:, https:, mailto: and in-page `#`. There is no
 * `dangerouslySetInnerHTML`, no rehype/HTML pass and no network fetch.
 *
 * Search/highlight use the SAME tree: `project()` concatenates the text a reader actually sees (text, inline code,
 * code blocks, table cells, image placeholders...) into `text`, remembering where each leaf starts. Matches are
 * offsets into that visible text, so hidden delimiters (`**`, backticks) and link destinations are never searched.
 * A block boundary contributes one `\n` separator (never rendered) so a term cannot join two blocks. A match that
 * crosses formatted segments (`ва**жно**`) is highlighted piecewise: every piece is a `.rosc-chat-mark` carrying
 * `data-rosc-match`, only the piece holding the match start carries the unique `id` used for scrolling.
 */

/** Above this size (or on any parser failure / excessive nesting) the body is shown as escaped plain text. */
export const MAX_MARKDOWN_CHARS = 100_000;
const MAX_DEPTH = 48;
/** Parents whose `html` child is a block (shown as its own literal paragraph). */
const FLOW = new Set(['root', 'blockquote', 'listItem', 'footnoteDefinition']);
const CACHE_LIMIT = 256;

export type Projection = { root: Root | null; text: string; starts: Map<object, number>; urls: Map<string, string> };

const BLOCKS = new Set(['paragraph', 'heading', 'code', 'blockquote', 'list', 'listItem', 'table', 'tableRow',
  'tableCell', 'thematicBreak', 'html', 'footnoteDefinition']);

function imagePlaceholder(alt: string | null | undefined): string {
  return alt ? `[изображение: ${alt}]` : '[изображение]';
}

/** The visible text a node shows itself (before any children), or null. */
function ownText(node: Nodes): string | null {
  switch (node.type) {
    case 'image': case 'imageReference': return imagePlaceholder(node.alt);
    case 'footnoteReference': return `[${node.label ?? node.identifier}]`;
    case 'footnoteDefinition': return `[${node.label ?? node.identifier}] `;
    case 'break': return '\n';
    case 'definition': return null;
    default: return 'value' in node && typeof node.value === 'string' ? node.value : null;
  }
}

function plain(body: string): Projection {
  return { root: null, text: body, starts: new Map(), urls: new Map() };
}

export function projectUncached(body: string): Projection {
  if (body.length > MAX_MARKDOWN_CHARS) return plain(body);
  try {
    const root = fromMarkdown(body, { extensions: [gfm()], mdastExtensions: [gfmFromMarkdown()] });
    const starts = new Map<object, number>();
    const urls = new Map<string, string>();
    let text = '';
    const walk = (node: Nodes, depth: number) => {
      if (depth > MAX_DEPTH) throw new RangeError('markdown nesting limit');
      if (node.type === 'definition') { if (!urls.has(node.identifier)) urls.set(node.identifier, node.url); return; }
      if (BLOCKS.has(node.type) && text.length > 0 && !text.endsWith('\n')) text += '\n';
      const own = ownText(node);
      if (own !== null) { starts.set(node, text.length); text += own; }
      if ('children' in node) for (const child of node.children) walk(child as Nodes, depth + 1);
    };
    walk(root, 0);
    return { root, text, starts, urls };
  } catch {
    return plain(body);
  }
}

const cache = new Map<string, Projection>();

/** Bounded (LRU) cache for published bodies: search and render share one parse per body. */
export function project(body: string): Projection {
  const hit = cache.get(body);
  if (hit) { cache.delete(body); cache.set(body, hit); return hit; }
  const value = projectUncached(body);
  cache.set(body, value);
  if (cache.size > CACHE_LIMIT) cache.delete(cache.keys().next().value as string);
  return value;
}

export function visibleText(body: string): string {
  return project(body).text;
}

const SAFE_SCHEME = /^(https?:\/\/|mailto:)/;

/** An href only for http(s)/mailto/in-page `#`; anything else (javascript:, data:, vbscript:, relative...) → null. */
export function safeHref(url: string | null | undefined): string | null {
  if (!url) return null;
  // eslint-disable-next-line no-control-regex
  if (/[\u0000- \u007f-\u009f]/.test(url)) return null; // disguised schemes (`java\tscript:`) are refused, not cleaned
  if (url.startsWith('#')) return url;
  if (!SAFE_SCHEME.test(url.toLowerCase())) return null;
  try {
    const parsed = new URL(url);
    return ['http:', 'https:', 'mailto:'].includes(parsed.protocol) ? url : null;
  } catch { return null; }
}

function safeLanguage(lang: string | null | undefined): string | null {
  const value = (lang ?? '').trim();
  return /^[A-Za-z0-9_+#.-]{1,32}$/.test(value) ? value : null;
}

type Ctx = { p: Projection; matches: Match[]; currentId: string | null; tail: ReactNode; tailHost: Nodes | null };

function highlight(value: string, node: Nodes, ctx: Ctx): ReactNode {
  const start = ctx.p.starts.get(node);
  if (start === undefined || ctx.matches.length === 0) return value;
  return segments(value, start, ctx);
}

function segments(value: string, start: number, ctx: Ctx): ReactNode {
  const end = start + value.length;
  const hits = ctx.matches.filter(match => match.start < end && match.end > start);
  if (hits.length === 0) return value;
  const nodes: ReactNode[] = [];
  let cursor = 0;
  for (const match of hits) {
    const from = Math.max(match.start, start) - start;
    const to = Math.min(match.end, end) - start;
    if (from > cursor) nodes.push(value.slice(cursor, from));
    nodes.push(<mark key={`${match.id}:${start}`} id={match.start >= start ? match.id : undefined}
      data-rosc-match={match.id}
      className={match.id === ctx.currentId ? 'rosc-chat-mark rosc-chat-mark--current' : 'rosc-chat-mark'}>
      {value.slice(from, to)}
    </mark>);
    cursor = to;
  }
  if (cursor < value.length) nodes.push(value.slice(cursor));
  return nodes;
}

function children(node: Parents, ctx: Ctx, tight = false): ReactNode[] {
  return node.children.map((child, index) => renderNode(child as Nodes, index, node, ctx, tight));
}

function withTail(node: Nodes, content: ReactNode[], ctx: Ctx): ReactNode[] {
  return node === ctx.tailHost ? [...content, <span key="tail">{ctx.tail}</span>] : content;
}

function renderList(node: List, key: number, ctx: Ctx): ReactNode {
  const loose = node.spread || node.children.some(item => item.spread);
  const task = node.children.some(item => typeof item.checked === 'boolean');
  const className = task ? 'rosc-md-list rosc-md-list--task' : 'rosc-md-list';
  const items = node.children.map((item, index) => renderItem(item, index, ctx, !loose));
  return node.ordered
    ? <ol key={key} className={className} start={node.start != null && node.start !== 1 ? node.start : undefined}>{items}</ol>
    : <ul key={key} className={className}>{items}</ul>;
}

function renderItem(node: ListItem, key: number, ctx: Ctx, tight: boolean): ReactNode {
  const box = typeof node.checked === 'boolean'
    ? <input type="checkbox" className="rosc-md-task" checked={node.checked} disabled readOnly tabIndex={-1}
      aria-label={node.checked ? 'выполнено' : 'не выполнено'} />
    : null;
  return <li key={key}>{box}{children(node, ctx, tight)}</li>;
}

function renderTable(node: Table, key: number, ctx: Ctx): ReactNode {
  const align = (index: number) => {
    const value = node.align?.[index];
    return value ? `rosc-md-align-${value}` : undefined;
  };
  const [head, ...body] = node.children;
  return <div key={key} className="rosc-md-table-wrap">
    <table className="rosc-md-table">
      {head && <thead><tr>{head.children.map((cell, index) =>
        <th key={index} className={align(index)}>{children(cell, ctx)}</th>)}</tr></thead>}
      {body.length > 0 && <tbody>{body.map((row, rowIndex) => <tr key={rowIndex}>{row.children.map((cell, index) =>
        <td key={index} className={align(index)}>{children(cell, ctx)}</td>)}</tr>)}</tbody>}
    </table>
  </div>;
}

function renderLink(url: string | null | undefined, key: number, node: Parents, ctx: Ctx): ReactNode {
  const href = safeHref(url);
  if (!href) return <span key={key} className="rosc-md-link rosc-md-link--blocked">{children(node, ctx)}</span>;
  const external = !href.startsWith('#') && !href.toLowerCase().startsWith('mailto:');
  return <a key={key} className="rosc-md-link" href={href}
    {...(external ? { target: '_blank', rel: 'noopener noreferrer' } : {})}>{children(node, ctx)}</a>;
}

function renderNode(node: Nodes, key: number, parent: Parents | null, ctx: Ctx, tight = false): ReactNode {
  switch (node.type) {
    // Text stays bare text nodes of its block (no wrapper), so a plain one-line message reads exactly as before.
    case 'text': return <Fragment key={key}>{highlight(node.value, node, ctx)}</Fragment>;
    case 'paragraph': {
      const content = withTail(node, children(node, ctx), ctx);
      return tight ? <Fragment key={key}>{content}</Fragment> : <p key={key}>{content}</p>;
    }
    case 'heading': {
      const Tag = `h${node.depth}` as 'h1';
      return <Tag key={key} className="rosc-md-h">{withTail(node, children(node, ctx), ctx)}</Tag>;
    }
    case 'emphasis': return <em key={key}>{children(node, ctx)}</em>;
    case 'strong': return <strong key={key}>{children(node, ctx)}</strong>;
    case 'delete': return <del key={key}>{children(node, ctx)}</del>;
    case 'inlineCode': return <code key={key} className="rosc-md-code">{highlight(node.value, node, ctx)}</code>;
    case 'code': {
      const lang = safeLanguage(node.lang);
      return <pre key={key} className="rosc-md-pre" data-language={lang ?? undefined}>
        <code className={lang ? `language-${lang}` : undefined}>{highlight(node.value, node, ctx)}</code></pre>;
    }
    case 'blockquote': return <blockquote key={key} className="rosc-md-quote">{children(node, ctx)}</blockquote>;
    case 'list': return renderList(node, key, ctx);
    case 'listItem': return renderItem(node, key, ctx, tight);
    case 'thematicBreak': return <hr key={key} className="rosc-md-hr" />;
    case 'table': return renderTable(node, key, ctx);
    case 'break': return <br key={key} />;
    case 'link': return renderLink(node.url, key, node, ctx);
    case 'linkReference': return renderLink(ctx.p.urls.get(node.identifier), key, node, ctx);
    case 'image': case 'imageReference':
      return <span key={key} className="rosc-md-image">{highlight(imagePlaceholder(node.alt), node, ctx)}</span>;
    case 'html': {
      // Raw HTML is untrusted text: shown literally (React escapes it), never interpreted.
      const text = highlight(node.value, node, ctx);
      return parent && FLOW.has(parent.type)
        ? <p key={key} className="rosc-md-html">{text}</p> : <Fragment key={key}>{text}</Fragment>;
    }
    case 'definition': return null;
    case 'footnoteReference':
      return <sup key={key} className="rosc-md-fnref">{highlight(`[${node.label ?? node.identifier}]`, node, ctx)}</sup>;
    case 'footnoteDefinition':
      return <div key={key} className="rosc-md-footnote">
        {highlight(`[${node.label ?? node.identifier}] `, node, ctx)}{children(node, ctx)}</div>;
    default: {
      const own = ownText(node);
      if (own !== null) return <span key={key}>{highlight(own, node, ctx)}</span>;
      return 'children' in node ? <span key={key}>{children(node as Parents, ctx)}</span> : null;
    }
  }
}

type Props = { body: string; matches?: Match[]; currentId?: string | null; tail?: ReactNode; transient?: boolean };

function Markdown({ body, matches = [], currentId = null, tail = null, transient = false }: Props) {
  // A provisional draft grows every batch: parse it without filling the shared cache with stale prefixes.
  const p = useMemo(() => (transient ? projectUncached(body) : project(body)), [body, transient]);
  if (!p.root) {
    const ctx: Ctx = { p, matches, currentId, tail, tailHost: null };
    return <div className="rosc-chat-body rosc-md">
      <p className="rosc-md-plain">{matches.length ? segments(body, 0, ctx) : body}{tail}</p>
    </div>;
  }
  const last = p.root.children[p.root.children.length - 1] as Nodes | undefined;
  const tailHost = tail && last && (last.type === 'paragraph' || last.type === 'heading') ? last : null;
  const ctx: Ctx = { p, matches, currentId, tail, tailHost };
  return <div className="rosc-chat-body rosc-md">
    {children(p.root, ctx)}
    {tail && !tailHost ? tail : null}
  </div>;
}

function sameMatches(a: Match[], b: Match[]): boolean {
  return a.length === b.length && a.every((match, index) =>
    match.id === b[index].id && match.start === b[index].start && match.end === b[index].end);
}

const relevant = (props: Props) => (props.currentId && props.matches?.some(match => match.id === props.currentId)
  ? props.currentId : null);

/** Memoised: a draft batch re-renders the panel, but unchanged published bodies are not re-rendered. */
export const ConversationMarkdown = memo(Markdown, (prev, next) => prev.body === next.body
  && prev.transient === next.transient && prev.tail === next.tail
  && sameMatches(prev.matches ?? [], next.matches ?? []) && relevant(prev) === relevant(next));
